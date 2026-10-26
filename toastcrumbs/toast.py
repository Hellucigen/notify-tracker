"""Parsing toast payloads.

Payloads are stored as plain XML -- no compression, no binary encoding -- which
is what makes deleted notifications recoverable at all (see
:mod:`toastcrumbs.carve`).

The schema is loose. Toasts, tiles and badges share a table but not a shape, the
binding template varies by app and Windows version, and third-party apps put
whatever they like inside. So this reads structurally rather than by template:
pull every ``<text>`` node in document order and let the caller decide what the
first one means. In practice element order matches reading order, which is the
property that matters when reading it back.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree

from .model import Notification

#: Matches a whole toast/tile/badge element, including a leading XML declaration.
PAYLOAD_PATTERN = re.compile(
    rb"(?:<\?xml[^>]*\?>\s*)?<(toast|tile|badge)\b.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)


class PayloadError(ValueError):
    """Raised when a payload is not usable XML."""


def parse_payload(notification: Notification) -> Notification:
    """Populate ``text``, ``images``, ``launch`` and ``template`` in place."""
    try:
        root = ElementTree.fromstring(notification.payload)
    except ElementTree.ParseError as exc:
        raise PayloadError(f"malformed payload: {exc}") from exc

    notification.text = [
        " ".join(node.itertext()).strip()
        for node in root.iter()
        if _localname(node.tag) == "text" and "".join(node.itertext()).strip()
    ]
    notification.images = [
        src
        for node in root.iter()
        if _localname(node.tag) == "image"
        for src in [node.get("src") or node.get("Src")]
        if src
    ]
    notification.actions = [
        (node.get("content") or "", node.get("arguments") or "")
        for node in root.iter()
        if _localname(node.tag) == "action"
    ]
    notification.has_input = any(_localname(n.tag) == "input" for n in root.iter())
    notification.launch = root.get("launch")

    binding = next(
        (n for n in root.iter() if _localname(n.tag) == "binding"),
        None,
    )
    if binding is not None:
        notification.template = binding.get("template")

    if notification.kind is None:
        notification.kind = _localname(root.tag)
    return notification


def try_parse_payload(notification: Notification) -> Notification:
    """Parse, tolerating failure.

    Carved payloads are frequently truncated at a page boundary, and a partial
    toast whose text nodes survived is still worth keeping. Losing it to a parse
    error would defeat the point of carving.
    """
    try:
        return parse_payload(notification)
    except PayloadError:
        notification.text = _salvage_text(notification.payload)
        if notification.kind is None:
            notification.kind = _salvage_kind(notification.payload)
        return notification


def _salvage_kind(payload: str) -> str | None:
    """Read the root element name straight out of the text.

    Worth doing separately: a fragment can be too damaged for an XML parser
    while still plainly announcing what it was.
    """
    match = re.search(r"<(toast|tile|badge)\b", payload, re.IGNORECASE)
    return match.group(1).lower() if match else None


def _salvage_text(payload: str) -> list[str]:
    """Recover text nodes from XML too broken for a real parser."""
    found = []
    for match in re.findall(r"<text[^>]*>(.*?)</text>", payload, re.DOTALL | re.I):
        # A real parser would unwrap CDATA; doing it by regex means doing this
        # by hand, or the section markers end up displayed as message text.
        cleaned = re.sub(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", r"\1", match, flags=re.DOTALL)
        if cleaned.strip():
            found.append(cleaned.strip())
    return found


def _localname(tag: str) -> str:
    """Strip any XML namespace: ``{urn:foo}text`` -> ``text``."""
    return tag.rsplit("}", 1)[-1].lower()
