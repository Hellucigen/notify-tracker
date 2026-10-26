"""Building synthetic notification databases.

A real ``wpndatabase.db`` is a transcript of someone's messages, so it cannot be
committed, shared, or used in CI. This builds one to the genuine schema instead:
same tables, same FILETIME columns, same XML payloads.

It also reproduces the conditions the recovery code exists for. ``deleted``
removes rows while leaving their bytes on freed pages, which is what Windows
expiring a toast looks like on disk, and :func:`wal_database` leaves an
un-checkpointed write-ahead log so the sidecar handling can be tested honestly.
"""

from __future__ import annotations

import random
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .model import FILETIME_EPOCH

SCHEMA = """
CREATE TABLE [NotificationHandler] (
    [RecordId] INTEGER PRIMARY KEY, [PrimaryId] TEXT NOT NULL COLLATE NOCASE,
    [WNSId] TEXT COLLATE NOCASE, [HandlerType] TEXT, [WNFEventName] INT64,
    [SystemDataPropertySet] BLOB, [CreatedTime] DATETIME, [ModifiedTime] DATETIME,
    [ParentId] TEXT COLLATE NOCASE, [ContainerSid] TEXT COLLATE NOCASE);

CREATE TABLE [Notification] (
    [Order] INTEGER NOT NULL PRIMARY KEY, [Id] INTEGER NOT NULL,
    [HandlerId] INTEGER REFERENCES [NotificationHandler]([RecordId]),
    [ActivityId] GUID, [Type] TEXT NOT NULL, [Payload] BLOB, [Tag] TEXT,
    [Group] TEXT, [ExpiryTime] INT64, [ArrivalTime] INT64,
    [DataVersion] INT64 DEFAULT '0', [PayloadType] TEXT NOT NULL,
    [BootId] INT64 DEFAULT '0', [ExpiresOnReboot] BOOLEAN DEFAULT 'FALSE',
    UNIQUE([Id]) ON CONFLICT REPLACE);

CREATE TABLE [HandlerAssets] (
    [HandlerId] INTEGER REFERENCES [NotificationHandler]([RecordId]),
    [AssetKey] TEXT NOT NULL, [AssetValue] TEXT,
    PRIMARY KEY([AssetKey], [HandlerId]) ON CONFLICT REPLACE);

CREATE TABLE [HandlerSettings] (
    [HandlerId] INTEGER REFERENCES [NotificationHandler]([RecordId]),
    [SettingKey] TEXT NOT NULL, [Value] INT,
    PRIMARY KEY([SettingKey], [HandlerId]) ON CONFLICT REPLACE);

CREATE TABLE [NotificationData] (
    [NotificationId] INTEGER REFERENCES [Notification]([Id]),
    [Key] TEXT NOT NULL, [Value] TEXT,
    PRIMARY KEY([Key], [NotificationId]) ON CONFLICT REPLACE);
"""

APPS = [
    ("Microsoft.SkypeApp_kzf8qxf38zg5c!App", "Skype", "app:immersive"),
    ("WhatsApp.WhatsApp_4j42bk0ppgd5m!App", "WhatsApp", "app:immersive"),
    ("Microsoft.Outlook.Desktop.App", "Outlook", "app:desktop"),
    ("Microsoft.Windows.Defender", "Windows Security", "app:system"),
    ("com.squirrel.Slack.Slack", "Slack", "app:desktop"),
]

MESSAGES = [
    ("Dana Reyes", "the vpn creds expired again, can you reset mine?"),
    ("IT Helpdesk", "Ticket #4417 assigned to you: RDP access for contractor"),
    ("Sam Okafor", "sent the quarterly numbers to the shared drive"),
    ("Build Server", "nightly build #4417 finished, 2 warnings"),
    ("Priya Nair", "standup moved to 10:15"),
    ("Backup Service", "Nightly backup completed with 2 warnings"),
    ("Dana Reyes", "did you get the file? it says opened but no reply"),
    ("Calendar", "Design review with the platform team in 15 minutes"),
]


def _leave_deleted_rows_behind(connection: sqlite3.Connection) -> None:
    """Make deleting a row leave its bytes on the page, as Windows does.

    Whether SQLite zeroes freed content or leaves it in place is a build-time
    default, and it differs by platform: Debian and Ubuntu ship SQLite with
    secure_delete enabled, while the SQLite that writes a real wpndatabase.db on
    Windows does not.

    A fixture that models a Windows database therefore has to pin the setting
    rather than inherit it, or the deleted rows it is supposed to hide are
    scrubbed and there is nothing left to carve. Left unpinned, the recovery
    tests pass on Windows and fail everywhere else.
    """
    connection.execute("PRAGMA secure_delete = OFF")


def to_filetime(moment: datetime) -> int:
    return int((moment - FILETIME_EPOCH).total_seconds() * 10_000_000)


def toast_xml(title: str, body: str, image: str | None = None, launch: str | None = None) -> str:
    """A payload shaped like the ones Windows actually stores."""
    launch_attr = f' launch="{launch}"' if launch else ""
    image_node = f'<image placement="appLogoOverride" src="{image}"/>' if image else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<toast{launch_attr}><visual><binding template="ToastGeneric">'
        f"<text>{title}</text><text>{body}</text>{image_node}"
        "</binding></visual></toast>"
    )


def build(
    path: str | Path,
    count: int = 12,
    deleted: int = 0,
    seed: int = 0,
    start: datetime | None = None,
) -> Path:
    """Create a database holding ``count`` notifications, then delete ``deleted``.

    Deleted rows leave their payloads on freed pages, exactly as an expired
    toast does, which is what gives the carver something real to find.
    """
    path = Path(path)
    rng = random.Random(seed)
    start = start or datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc)

    connection = sqlite3.connect(path)
    try:
        _leave_deleted_rows_behind(connection)
        connection.executescript(SCHEMA)
        _populate(connection, count, rng, start)
        if deleted:
            doomed = [
                row[0]
                for row in connection.execute(
                    "SELECT [Order] FROM Notification ORDER BY ArrivalTime LIMIT ?", (deleted,)
                )
            ]
            connection.executemany(
                "DELETE FROM Notification WHERE [Order] = ?", [(v,) for v in doomed]
            )
        connection.commit()
    finally:
        connection.close()
    return path


@contextmanager
def wal_database(path: str | Path, count: int = 8, seed: int = 0):
    """Build a database that still has an un-checkpointed write-ahead log.

    SQLite checkpoints when the last connection closes, so the connection is
    held open for the duration -- copy the sidecars inside the ``with`` block.
    """
    path = Path(path)
    connection = sqlite3.connect(path)
    _leave_deleted_rows_behind(connection)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    connection.commit()

    # Fold the schema into the main database before adding any rows, so the
    # fixture matches a real store: tables checkpointed long ago, recent
    # notifications still only in the log.
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    _populate(connection, count, random.Random(seed), datetime(2026, 5, 2, tzinfo=timezone.utc))
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def _populate(
    connection: sqlite3.Connection, count: int, rng: random.Random, start: datetime
) -> None:
    for record_id, (primary_id, display, kind) in enumerate(APPS, start=1):
        created = (start - timedelta(days=30 + record_id)).strftime("%Y-%m-%d %H:%M:%S")
        connection.execute(
            "INSERT INTO NotificationHandler"
            " (RecordId, PrimaryId, HandlerType, CreatedTime, ModifiedTime)"
            " VALUES (?, ?, ?, ?, ?)",
            (record_id, primary_id, kind, created, created),
        )
        connection.execute(
            "INSERT INTO HandlerAssets (HandlerId, AssetKey, AssetValue) VALUES (?, ?, ?)",
            (record_id, "DisplayName", display),
        )

    for index in range(count):
        title, body = MESSAGES[index % len(MESSAGES)]
        arrival = start + timedelta(minutes=17 * index)
        handler_id = rng.randint(1, len(APPS))
        connection.execute(
            "INSERT INTO Notification"
            " ([Order], Id, HandlerId, Type, Payload, Tag, [Group],"
            "  ExpiryTime, ArrivalTime, PayloadType, BootId)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                index + 1,
                1000 + index,
                handler_id,
                "toast",
                toast_xml(title, f"{body} [{index}]").encode("utf-8"),
                f"tag{index}",
                "chat",
                to_filetime(arrival + timedelta(days=3)),
                to_filetime(arrival),
                "Xml",
                7,
            ),
        )
