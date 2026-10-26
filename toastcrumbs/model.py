"""Records recovered from a Windows notification database."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

#: Windows FILETIME epoch: 100-nanosecond ticks since 1601-01-01 UTC.
FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def from_filetime(ticks: int | None) -> datetime | None:
    """Convert a Windows FILETIME to an aware UTC datetime.

    Zero and None both mean "not set" -- the schema uses 0 as a null for
    ExpiryTime on notifications that do not expire.
    """
    if not ticks:
        return None
    try:
        return FILETIME_EPOCH + timedelta(microseconds=ticks // 10)
    except (OverflowError, ValueError):
        return None


def is_resource_reference(text: str) -> bool:
    return text.startswith("ms-resource:")


def friendly_name(primary_id: str) -> str:
    """Reduce an AUMID to something readable.

    Store apps identify themselves as ``PackageFamilyName!AppId``, where the
    family name is ``Name_PublisherHash`` -- so ``MSTeams_8wekyb3d8bbwe!MSTeams``
    is just MSTeams, and ``Microsoft.Todos_8wekyb3d8bbwe!App`` is Microsoft.Todos.
    Desktop handlers use free-form strings and are left alone.
    """
    if not primary_id:
        return "(unknown app)"

    family = primary_id.split("!", 1)[0]
    name, sep, publisher = family.rpartition("_")
    # A publisher hash is a fixed-length lowercase alphanumeric run; anything
    # else is part of the name and must not be trimmed away.
    if sep and name and publisher.isalnum() and publisher.islower() and len(publisher) >= 12:
        return name
    return primary_id


@dataclass
class Handler:
    """An application registered to raise notifications.

    Interesting in its own right, and often more durable than the
    notifications themselves: handlers persist long after the toasts they
    produced have been purged, so the table is a record of what was installed
    and when it first registered.
    """

    record_id: int
    primary_id: str
    handler_type: str | None = None
    created: datetime | None = None
    modified: datetime | None = None
    parent_id: str | None = None
    display_name: str | None = None

    @property
    def label(self) -> str:
        """Best available human-readable name for the app.

        ``HandlerAssets`` carries a DisplayName for only a handful of apps, so
        most of the time the readable name has to come out of the AUMID.
        """
        return self.display_name or friendly_name(self.primary_id)

    @property
    def is_desktop_app(self) -> bool:
        return (self.handler_type or "") == "app:desktop"


@dataclass
class Notification:
    """A single toast, tile or badge.

    ``recovered`` marks records carved out of the write-ahead log or from
    unallocated space rather than read from a live table. Those have no
    surrounding row, so identifiers and timestamps are usually absent -- the
    payload text is what survives.
    """

    payload: str
    order: int | None = None
    notification_id: int | None = None
    handler_id: int | None = None
    kind: str | None = None
    arrival: datetime | None = None
    expiry: datetime | None = None
    tag: str | None = None
    group: str | None = None
    boot_id: int | None = None
    handler: Handler | None = None
    recovered: bool = False
    #: True when the payload was cut short -- its tail was overwritten before it
    #: could be carved, so the text present is all there is.
    partial: bool = False
    source: str = "live"

    #: Filled in by :mod:`toastcrumbs.toast` once the payload is parsed.
    text: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    #: Buttons offered on the toast, as ``(label, arguments)``. Worth capturing:
    #: they record what the user was invited to do, and the arguments often
    #: carry conversation or message identifiers.
    actions: list[tuple[str, str]] = field(default_factory=list)
    #: True when the toast carried an inline reply box.
    has_input: bool = False
    launch: str | None = None
    template: str | None = None

    @property
    def app(self) -> str:
        return self.handler.label if self.handler else "(unknown app)"

    @property
    def summary(self) -> str:
        """The notification as a person would have read it.

        Tiles repeat the same strings across their Medium/Wide/Large bindings,
        so the raw text list says "Plan your tasks for today" three times.
        Duplicates are collapsed, first occurrence winning, which is what a
        person actually saw.
        """
        seen: set[str] = set()
        unique = [t for t in self.text if t and not (t in seen or seen.add(t))]
        if not unique:
            return "(no text)"
        if all(is_resource_reference(t) for t in unique):
            return f"(text held as unresolved resource reference: {unique[0]})"
        return " — ".join(unique)

    @property
    def unresolved_text(self) -> bool:
        """True when the payload points at a string table instead of carrying text.

        Some first-party apps store ``ms-resource://`` references that Windows
        resolves at display time against the app's own resources. The message
        the user read is genuinely not in the database, and presenting the URI
        as though it were the message would be misleading.
        """
        return bool(self.text) and all(is_resource_reference(t) for t in self.text)

    def as_dict(self) -> dict:
        return {
            "arrival": self.arrival.isoformat() if self.arrival else None,
            "app": self.app,
            "primary_id": self.handler.primary_id if self.handler else None,
            "type": self.kind,
            "text": self.text,
            "summary": self.summary,
            "unresolved_text": self.unresolved_text,
            "images": self.images,
            "actions": [{"content": c, "arguments": a} for c, a in self.actions],
            "has_input": self.has_input,
            "launch": self.launch,
            "template": self.template,
            "tag": self.tag,
            "group": self.group,
            "notification_id": self.notification_id,
            "expiry": self.expiry.isoformat() if self.expiry else None,
            "recovered": self.recovered,
            "partial": self.partial,
            "source": self.source,
        }
