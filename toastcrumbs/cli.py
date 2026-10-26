"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from . import __version__
from .carve import carve_all, live_fingerprints
from .database import DatabaseError, open_copy, read_handlers, read_notifications, sidecars
from .model import Notification
from .report import to_csv, to_html, to_json
from .toast import try_parse_payload

DEFAULT_PATH = Path.home() / "AppData/Local/Microsoft/Windows/Notifications/wpndatabase.db"


def _force_utf8_output() -> None:
    """Print notification text without mangling it.

    The Windows console defaults to a legacy code page, and notification bodies
    are full of emoji, accents and non-Latin scripts. Left alone this either
    replaces characters silently or raises UnicodeEncodeError partway through a
    timeline.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


DATABASE_LOCATION = r"%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db"

OVERVIEW = f"""\
Every toast Windows shows passes through a SQLite database in your profile,
and the notification text is stored, not just the fact that one appeared. Chat
messages, mail subjects, calendar reminders: a readable log of what each
application told you.

Windows expires notifications aggressively, but deleting a row does not scrub
the page it sat on. Most of the history is usually still on disk, which is
what this recovers.

Default location, used when no path is given:
    {DATABASE_LOCATION}

Collect the -wal and -shm sidecars alongside the database. The write-ahead log
holds committed pages not yet folded in and is routinely larger than the
database itself; without it you can lose the most recent notifications.

A typical run:
    toastcrumbs info                    what is in it, and what is recoverable
    toastcrumbs list                    the timeline, recovered rows marked *
    toastcrumbs export -o report.html   a self-contained report

The database is never modified: it is copied before anything opens it.
"""


class _Formatter(argparse.RawDescriptionHelpFormatter):
    """Keep hand-wrapped text intact and widen the option column."""

    def __init__(self, prog):
        super().__init__(prog, max_help_position=30)


def _example(*lines: str) -> str:
    return "examples:\n" + "".join(f"  {line}\n" for line in lines)


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="toastcrumbs",
        description="Recover Windows notification history, including deleted toasts.",
        epilog=OVERVIEW,
        formatter_class=_Formatter,
    )
    parser.add_argument("--version", action="version", version=f"toastcrumbs {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    def command(name: str, help_text: str, description: str, epilog: str = ""):
        return sub.add_parser(
            name, help=help_text, description=description,
            epilog=epilog, formatter_class=_Formatter,
        )

    def add_source(p):
        p.add_argument(
            "path", type=Path, nargs="?", default=DEFAULT_PATH,
            help="wpndatabase.db to read (default: the current user's)",
        )

    add_source(command(
        "info",
        "summarise the database and what is recoverable from it",
        """\
Report how many handlers are registered, how many notifications are live, and
how much more can be carved out of freed pages and the write-ahead log.

Each source file is listed separately, so a missing sidecar is obvious.""",
        _example("toastcrumbs info", "toastcrumbs info ./captures/wpndatabase.db"),
    ))

    p_list = command(
        "list",
        "print the notification timeline",
        """\
Print notifications oldest first, one per line.

Recovered entries are marked with * and sorted after the timeline rather than
placed inside it. They have no recorded arrival time, so giving them a
position would mean inventing one.""",
        _example(
            "toastcrumbs list",
            "toastcrumbs list --limit 40",
            "toastcrumbs list ./captures/wpndatabase.db --no-carve",
        ),
    )
    add_source(p_list)
    p_list.add_argument(
        "--no-carve", action="store_true",
        help="read the live tables only, skipping recovery",
    )
    p_list.add_argument(
        "--limit", type=int, default=0, metavar="N",
        help="show at most N notifications, 0 for all (default: %(default)s)",
    )

    p_export = command(
        "export",
        "write a report",
        """\
Write the timeline to a file.

The HTML report is self-contained -- no CDN, no external fonts -- so it
survives being attached to a ticket or opened on an air-gapped machine.
Recovered entries are shaded and explained in a footer. JSON and CSV are
there for feeding other tools.""",
        _example(
            "toastcrumbs export -o timeline.html",
            "toastcrumbs export ./captures/wpndatabase.db -o timeline.csv --format csv",
        ),
    )
    add_source(p_export)
    p_export.add_argument("-o", "--output", type=Path, required=True, help="file to write")
    p_export.add_argument(
        "--format", choices=("html", "json", "csv"), default="html",
        help="output format (default: %(default)s)",
    )
    p_export.add_argument(
        "--no-carve", action="store_true",
        help="read the live tables only, skipping recovery",
    )

    p_apps = command(
        "apps",
        "list registered notification handlers",
        """\
List every application registered to raise notifications, oldest first.

Handlers outlive the toasts they produced -- a machine with a couple of dozen
live notifications can carry hundreds of handlers, so the table is a record of
what was installed and when it first registered.""",
        _example("toastcrumbs apps", "toastcrumbs apps --desktop"),
    )
    add_source(p_apps)
    p_apps.add_argument(
        "--desktop", action="store_true",
        help="only desktop applications, which are usually the interesting subset",
    )

    p_synth = command(
        "synth",
        "generate a synthetic database for testing",
        """\
Build a database to the real schema, then delete some of its rows.

A real notification store is a transcript of somebody's messages, so none is
committed and no test touches one. The deleted rows leave their payloads on
freed pages exactly as an expired toast does, giving the carver something
genuine to find.""",
        _example(
            "toastcrumbs synth -o test.db",
            "toastcrumbs synth -o test.db --count 40 --deleted 15",
        ),
    )
    p_synth.add_argument("-o", "--output", type=Path, required=True,
                         help="database path to write")
    p_synth.add_argument("--count", type=int, default=14, metavar="N",
                         help="notifications to create (default: %(default)s)")
    p_synth.add_argument("--deleted", type=int, default=5, metavar="N",
                         help="how many to then delete (default: %(default)s)")
    p_synth.add_argument("--seed", type=int, default=0, metavar="N",
                         help="random seed (default: %(default)s)")

    args = parser.parse_args(argv)
    handlers = {
        "info": _cmd_info,
        "list": _cmd_list,
        "export": _cmd_export,
        "apps": _cmd_apps,
        "synth": _cmd_synth,
    }
    try:
        return handlers[args.command](args)
    except DatabaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def collect(path: Path, carve: bool = True) -> list[Notification]:
    """Live notifications, plus anything recoverable, oldest first.

    Carved records sort last: they carry no arrival time, so placing them in the
    timeline would mean inventing one.
    """
    with open_copy(path) as connection:
        live = read_notifications(connection)

    for notification in live:
        try_parse_payload(notification)

    if not carve:
        return live

    recovered, _ = carve_all(path, live_fingerprints(live))
    live.sort(key=lambda n: (n.arrival is None, n.arrival))
    return live + recovered


def _cmd_info(args) -> int:
    print(f"{args.path}")
    for sidecar in sidecars(args.path):
        print(f"  sidecar: {sidecar.name} ({sidecar.stat().st_size:,} bytes)")

    with open_copy(args.path) as connection:
        handlers = read_handlers(connection)
        live = read_notifications(connection, handlers)

    print(f"\n{len(handlers)} registered handlers, {len(live)} live notifications")
    kinds = Counter(n.kind for n in live)
    for kind, count in kinds.most_common():
        print(f"    {count:>6}  {kind}")

    types = Counter(h.handler_type for h in handlers.values())
    print()
    for kind, count in types.most_common():
        print(f"    {count:>6}  {kind}")

    recovered, reports = carve_all(args.path, live_fingerprints(live))
    print()
    for report in reports:
        print(f"  {report.summary()}")
    print(f"\n{len(recovered)} notifications recoverable beyond the live tables")
    return 0


def _cmd_list(args) -> int:
    found = collect(args.path, carve=not args.no_carve)
    shown = found[: args.limit] if args.limit else found

    for notification in shown:
        when = (
            notification.arrival.strftime("%Y-%m-%d %H:%M:%S")
            if notification.arrival
            else "  (recovered)     "
        )
        flag = "*" if notification.recovered else " "
        print(f"{flag} {when}  {notification.app[:32]:<32}  {notification.summary[:78]}")

    if len(shown) < len(found):
        print(f"\n... {len(found) - len(shown)} more")
    print(f"\n{len(found)} notifications "
          f"({sum(1 for n in found if n.recovered)} recovered, marked *)")
    return 0


def _cmd_export(args) -> int:
    found = collect(args.path, carve=not args.no_carve)
    writer = {"html": lambda n, p: to_html(n, p, str(args.path)), "json": to_json, "csv": to_csv}
    writer[args.format](found, args.output)
    print(f"wrote {len(found)} notifications to {args.output}")
    return 0


def _cmd_apps(args) -> int:
    with open_copy(args.path) as connection:
        handlers = read_handlers(connection)

    chosen = [h for h in handlers.values() if not args.desktop or h.is_desktop_app]
    chosen.sort(key=lambda h: (h.created or h.label, h.label))

    for handler in chosen:
        created = handler.created.strftime("%Y-%m-%d") if handler.created else "unknown   "
        print(f"{created}  {(handler.handler_type or '?'):<14}  {handler.label[:60]}")
    print(f"\n{len(chosen)} handlers")
    return 0


def _cmd_synth(args) -> int:
    from .synth import build

    build(args.output, count=args.count, deleted=args.deleted, seed=args.seed)
    print(
        f"wrote {args.output} with {args.count} notifications, "
        f"{args.deleted} of them deleted but still carvable"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
