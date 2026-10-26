"""Recovering notifications that are no longer in the live tables.

Windows expires toasts aggressively -- a database whose ``Notification`` table
holds a couple of dozen rows may have carried thousands. Deleting a row does not
scrub the page it lived on, and the write-ahead log keeps whole page images from
before the delete. The bytes usually outlive the record by a long way.

Carving them is unusually tractable here because payloads are stored as plain
XML. There is no need to reconstruct SQLite's record format at all: scan the raw
bytes for well-formed toast, tile and badge elements and read them directly.
That degrades gracefully too, since a payload truncated at a page boundary still
yields the text nodes that survived.

What is lost is the surrounding row -- arrival time, handler, tag. A carved
notification therefore proves *that* something was shown, and what it said, but
usually not when or by whom.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .database import sidecars
from .model import Notification
from .toast import PAYLOAD_PATTERN, try_parse_payload

#: Self-closing badges carry their value in an attribute and have no end tag.
SELF_CLOSING_PATTERN = re.compile(rb"<(badge|tile)\b[^>]*/>", re.IGNORECASE)

#: A UTF-16 payload appears as ASCII interleaved with NULs.
UTF16_MARKER = re.compile(rb"<\x00(?:t\x00o\x00a\x00s\x00t|t\x00i\x00l\x00e)\x00", re.I)

#: Root elements a payload can have.
ROOT_TAGS = (b"toast", b"tile", b"badge")

#: Nothing legitimate approaches this -- real payloads observed run to under
#: 2KB, and this leaves an order of magnitude of headroom. A fragment longer
#: than it means the scan has run past a truncated record into unrelated pages.
MAX_FRAGMENT = 32 * 1024

#: Where the salvageable part of a truncated fragment ends.
LAST_ELEMENT = re.compile(rb".*</(?:text|binding|visual|action|image)\s*>", re.DOTALL | re.I)

#: An XML declaration is picked up when it sits immediately before a root tag.
DECLARATION = re.compile(rb"<\?xml[^>]*\?>\s*$", re.IGNORECASE)


@dataclass
class CarveStats:
    """What a carving pass found, per source file."""

    source: str
    bytes_scanned: int
    fragments: int = 0
    unique: int = 0
    already_live: int = 0
    partial: int = 0

    def summary(self) -> str:
        partial = f", {self.partial} truncated" if self.partial else ""
        return (
            f"{self.source}: {self.bytes_scanned:,} bytes, {self.fragments} fragments, "
            f"{self.unique} not in live tables ({self.already_live} already known{partial})"
        )


def carve_file(path: str | Path, known: set[str] | None = None) -> tuple[list[Notification], CarveStats]:
    """Scan one file for notification payloads.

    ``known`` holds normalised payloads already recovered from the live tables;
    anything matching one of those is counted but not returned, so the result is
    only what SQL could not reach.
    """
    path = Path(path)
    data = path.read_bytes()
    known = known if known is not None else set()

    stats = CarveStats(source=path.name, bytes_scanned=len(data))
    seen: set[str] = set()
    recovered: list[Notification] = []

    for payload, offset, complete in _fragments(data):
        stats.fragments += 1
        fingerprint = _normalise(payload)
        if fingerprint in known or fingerprint in seen:
            if fingerprint in known:
                stats.already_live += 1
            continue

        notification = try_parse_payload(
            Notification(
                payload=payload,
                recovered=True,
                partial=not complete,
                source=f"{path.name}@{offset:#x}" + ("" if complete else " (truncated)"),
            )
        )
        # A truncated record whose text did not survive is just wreckage.
        if not complete and not notification.text:
            continue

        seen.add(fingerprint)
        stats.unique += 1
        stats.partial += 0 if complete else 1
        recovered.append(notification)

    return recovered, stats


def carve_all(
    path: str | Path, known: set[str] | None = None
) -> tuple[list[Notification], list[CarveStats]]:
    """Carve the database and its sidecars.

    The ``-wal`` is scanned as well as the database, and usually yields more:
    it holds page images from before recent deletes.
    """
    path = Path(path)
    known = set(known or ())
    everything: list[Notification] = []
    reports: list[CarveStats] = []

    for target in [path, *sidecars(path)]:
        found, stats = carve_file(target, known)
        known |= {_normalise(n.payload) for n in found}
        everything.extend(found)
        reports.append(stats)

    return everything, reports


def _fragments(data: bytes) -> list[tuple[str, int, bool]]:
    """Every payload-shaped run of bytes, as ``(xml, offset, complete)``.

    Freed pages are full of *truncated* records -- a payload whose tail was
    overwritten, leaving an opening tag with no matching close. A plain
    non-greedy ``<toast>.*?</toast>`` will happily run past that wreckage and
    stop at the next record's closing tag, splicing two notifications into one
    and attributing the second one's text to the first. On a real database that
    produced a single 794KB "notification" carrying text from dozens.

    So matching is done by walking openers and closers together: if another
    opening tag appears before the closing one, the first record is truncated
    and is emitted on its own rather than being welded to its neighbour.
    """
    found: list[tuple[str, int, bool]] = []

    self_closing = [
        (match.start(), match.end()) for match in SELF_CLOSING_PATTERN.finditer(data)
    ]
    for start, end in self_closing:
        found.append((data[start:end].decode("utf-8", errors="replace"), start, True))

    for tag in ROOT_TAGS:
        found.extend(_scan_tag(data, tag, self_closing))

    # Some payloads are stored UTF-16. Decoding the whole buffer and rescanning
    # is cheap next to how often it is the difference between recovering a
    # message and missing it.
    if UTF16_MARKER.search(data):
        text = data.decode("utf-16-le", errors="replace")
        found.extend(
            (match.group(0), match.start() * 2, True)
            for match in re.finditer(
                PAYLOAD_PATTERN.pattern.decode(), text, re.DOTALL | re.IGNORECASE
            )
        )

    return sorted(found, key=lambda item: item[1])


def _scan_tag(
    data: bytes, tag: bytes, skip: list[tuple[int, int]]
) -> list[tuple[str, int, bool]]:
    """Pair up ``<tag>`` openers with their real closers."""
    openers = [
        match.start()
        for match in re.finditer(rb"<" + tag + rb"[\s>]", data, re.IGNORECASE)
        if not any(begin <= match.start() < end for begin, end in skip)
    ]
    closers = [
        (match.start(), match.end())
        for match in re.finditer(rb"</" + tag + rb"\s*>", data, re.IGNORECASE)
    ]
    if not openers:
        return []

    found: list[tuple[str, int, bool]] = []
    index = closer_index = 0
    while index < len(openers):
        start = openers[index]
        while closer_index < len(closers) and closers[closer_index][0] < start:
            closer_index += 1

        following = openers[index + 1] if index + 1 < len(openers) else None
        # A truncated record ends wherever the surviving text stops. The cap
        # applies whichever way that boundary is found: the next opener can
        # itself be hundreds of kilobytes away, and everything in between is
        # unrelated page content, not part of this notification.
        salvage_end = min(following or len(data), start + MAX_FRAGMENT)

        if closer_index >= len(closers):
            found.append(_fragment(data, start, salvage_end, complete=False))
            break

        closer_start, closer_end = closers[closer_index]
        truncated = (following is not None and following < closer_start) or (
            closer_end - start > MAX_FRAGMENT
        )
        if truncated:
            found.append(_fragment(data, start, salvage_end, complete=False))
            index += 1
            continue

        found.append(_fragment(data, start, closer_end, complete=True))
        while index < len(openers) and openers[index] < closer_end:
            index += 1

    return found


def _fragment(data: bytes, start: int, end: int, complete: bool) -> tuple[str, int, bool]:
    """Slice ``data``, pulling in an XML declaration if one sits just before.

    Truncated fragments are trimmed back to their last recognisable element.
    Everything past it is unrelated page content that happened to sit after the
    overwritten tail, and carrying it around would bloat every export with
    kilobytes of noise while adding nothing recoverable.
    """
    declaration = DECLARATION.search(data, max(0, start - 80), start)
    if declaration:
        start = declaration.start()

    body = data[start:end]
    if not complete:
        trimmed = LAST_ELEMENT.match(body)
        if trimmed:
            body = trimmed.group(0)

    return body.decode("utf-8", errors="replace"), start, complete


def _normalise(payload: str) -> str:
    """Collapse whitespace so trivially different encodings compare equal."""
    return re.sub(r"\s+", " ", payload).strip().lower()


def live_fingerprints(notifications: list[Notification]) -> set[str]:
    return {_normalise(n.payload) for n in notifications if n.payload}
