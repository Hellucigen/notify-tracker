"""Reading the live tables out of a notification database.

Two things matter here beyond writing the right SQL.

**The database is never modified.** Opening a SQLite database that has a
write-ahead log makes SQLite replay and checkpoint it, which modifies all three
files. So the database and its ``-wal``/``-shm`` sidecars are copied to a
workspace first and every query runs against the copy.

**The sidecars matter too.** A ``-wal`` file holds committed pages
not yet folded into the main database. Collect ``wpndatabase.db`` on its own and
you can silently lose the most recent notifications.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .model import Handler, Notification, from_filetime

SIDECAR_SUFFIXES = ("-wal", "-shm")

HANDLER_QUERY = """
    SELECT h.RecordId, h.PrimaryId, h.HandlerType, h.CreatedTime,
           h.ModifiedTime, h.ParentId,
           (SELECT AssetValue FROM HandlerAssets a
             WHERE a.HandlerId = h.RecordId AND a.AssetKey = 'DisplayName') AS DisplayName
      FROM NotificationHandler h
"""

NOTIFICATION_QUERY = """
    SELECT [Order], Id, HandlerId, Type, Payload, Tag, [Group],
           ExpiryTime, ArrivalTime, BootId
      FROM Notification
     ORDER BY ArrivalTime
"""


class DatabaseError(RuntimeError):
    """Raised when a file is not a usable notification database."""


def sidecars(path: Path) -> list[Path]:
    """Existing ``-wal``/``-shm`` files belonging to ``path``."""
    return [
        candidate
        for suffix in SIDECAR_SUFFIXES
        if (candidate := path.with_name(path.name + suffix)).exists()
    ]


@contextmanager
def open_copy(path: str | Path, include_wal: bool = True):
    """Yield a read-only connection to a throwaway copy of the database.

    With ``include_wal`` false the sidecars are left behind, so the connection
    sees only what had been checkpointed into the main file. Comparing the two
    views is how you tell which notifications live solely in the log.
    """
    path = Path(path)
    if not path.exists():
        raise DatabaseError(f"{path}: no such file")

    with tempfile.TemporaryDirectory(prefix="toastcrumbs-") as tmp:
        workspace = Path(tmp) / path.name
        shutil.copy2(path, workspace)
        if include_wal:
            for sidecar in sidecars(path):
                shutil.copy2(sidecar, workspace.with_name(sidecar.name))

        connection = sqlite3.connect(workspace)
        connection.row_factory = sqlite3.Row
        try:
            _assert_notification_database(connection, path)
            yield connection
        finally:
            connection.close()


def _assert_notification_database(connection: sqlite3.Connection, path: Path) -> None:
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    except sqlite3.DatabaseError as exc:
        raise DatabaseError(f"{path}: not a SQLite database ({exc})") from exc

    missing = {"Notification", "NotificationHandler"} - tables
    if missing:
        raise DatabaseError(f"{path}: not a notification database (missing {', '.join(sorted(missing))})")


def read_handlers(connection: sqlite3.Connection) -> dict[int, Handler]:
    handlers = {}
    for row in connection.execute(HANDLER_QUERY):
        handlers[row["RecordId"]] = Handler(
            record_id=row["RecordId"],
            primary_id=row["PrimaryId"],
            handler_type=row["HandlerType"],
            created=_parse_datetime(row["CreatedTime"]),
            modified=_parse_datetime(row["ModifiedTime"]),
            parent_id=row["ParentId"],
            display_name=row["DisplayName"],
        )
    return handlers


def read_notifications(
    connection: sqlite3.Connection, handlers: dict[int, Handler] | None = None
) -> list[Notification]:
    if handlers is None:
        handlers = read_handlers(connection)

    found = []
    for row in connection.execute(NOTIFICATION_QUERY):
        found.append(
            Notification(
                payload=_decode(row["Payload"]),
                order=row["Order"],
                notification_id=row["Id"],
                handler_id=row["HandlerId"],
                kind=row["Type"],
                arrival=from_filetime(row["ArrivalTime"]),
                expiry=from_filetime(row["ExpiryTime"]),
                tag=row["Tag"],
                group=row["Group"],
                boot_id=row["BootId"],
                handler=handlers.get(row["HandlerId"]),
            )
        )
    return found


def _decode(payload: bytes | str | None) -> str:
    """Payloads are XML text stored in a BLOB column, occasionally UTF-16."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if payload[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return payload.decode("utf-16", errors="replace")
    return payload.decode("utf-8", errors="replace")


def _parse_datetime(value: str | None) -> datetime | None:
    """Handler timestamps are ``YYYY-MM-DD HH:MM:SS`` text, in UTC."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
