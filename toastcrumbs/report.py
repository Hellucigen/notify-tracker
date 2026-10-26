"""Output formats.

A timeline reads better than a table of blobs, so the HTML report is the primary
output and is deliberately self-contained -- no CDN, no fonts to fetch -- so it
survives being attached to a ticket or opened on an air-gapped machine.

Recovered notifications are marked wherever they appear. A carved payload has no
timestamp and no verified handler, and letting it sit unlabelled next to a live
row would invite attributing it to a time it was never shown to have.
"""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from .model import Notification

STYLE = """
:root{
  color-scheme:light dark;
  --bg:#f5f7f8; --surface:#fff; --ink:#15181c; --muted:#5c6672;
  --line:#e1e6ea; --rail:#26788c; --accent:#20697a;
  --carved:#8a5a0d; --carved-bg:#fbf3e4; --carved-line:#e8d3a8;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
@media (prefers-color-scheme:dark){
  :root{ --bg:#101317; --surface:#181c21; --ink:#e7ecf1; --muted:#8d99a6;
         --line:#262c33; --rail:#4ea3b8; --accent:#5fb0c4;
         --carved:#d9a44e; --carved-bg:#221b10; --carved-line:#3d3018; }
}
:root[data-theme="dark"]{ --bg:#101317; --surface:#181c21; --ink:#e7ecf1;
  --muted:#8d99a6; --line:#262c33; --rail:#4ea3b8; --accent:#5fb0c4;
  --carved:#d9a44e; --carved-bg:#221b10; --carved-line:#3d3018; }
:root[data-theme="light"]{ --bg:#f5f7f8; --surface:#fff; --ink:#15181c;
  --muted:#5c6672; --line:#e1e6ea; --rail:#26788c; --accent:#20697a;
  --carved:#8a5a0d; --carved-bg:#fbf3e4; --carved-line:#e8d3a8; }

*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:16px/1.5 var(--sans);-webkit-text-size-adjust:100%}
.page{max-width:46rem;margin:0 auto;padding:1.75rem 1rem 4rem;
      display:flex;flex-direction:column;gap:1.5rem}

.masthead{display:flex;flex-direction:column;gap:.3rem}
.eyebrow{font:600 .68rem/1 var(--mono);letter-spacing:.12em;text-transform:uppercase;
         color:var(--accent)}
h1{margin:0;font-size:1.45rem;line-height:1.2;font-weight:650;text-wrap:balance}
.source{margin:0;color:var(--muted);font:.8rem/1.4 var(--mono);word-break:break-all}

.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(6.5rem,1fr));gap:.5rem}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:9px;
      padding:.6rem .7rem;display:flex;flex-direction:column;gap:.1rem}
.stat b{font:600 1.35rem/1 var(--mono);font-variant-numeric:tabular-nums}
.stat span{color:var(--muted);font-size:.72rem;letter-spacing:.02em}

.day{display:flex;align-items:center;gap:.6rem;margin:.75rem 0 -.4rem}
.day h2{margin:0;font:600 .72rem/1 var(--mono);letter-spacing:.1em;color:var(--muted)}
.day::after{content:"";flex:1;height:1px;background:var(--line)}

.feed{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.5rem}
.entry{position:relative;background:var(--surface);border:1px solid var(--line);
       border-radius:9px;padding:.7rem .8rem .7rem 1rem;
       display:flex;flex-direction:column;gap:.35rem;overflow:hidden}
.entry::before{content:"";position:absolute;inset:0 auto 0 0;width:3px;
               background:var(--rail)}
.head{display:flex;flex-wrap:wrap;align-items:baseline;gap:.5rem}
time{font:.78rem/1 var(--mono);font-variant-numeric:tabular-nums;color:var(--muted)}
.app{font-weight:640;font-size:.88rem;overflow-wrap:anywhere}
.msg{margin:0;font-size:.95rem;overflow-wrap:anywhere}
.msg.absent{color:var(--muted);font-style:italic}
.tags{display:flex;flex-wrap:wrap;gap:.28rem}
.tag{font:.66rem/1.6 var(--mono);padding:0 .38rem;border-radius:4px;
     border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.origin{font:.66rem/1.6 var(--mono);color:var(--muted);overflow-wrap:anywhere}

.entry.carved{background:var(--carved-bg);border-color:var(--carved-line)}
.entry.carved::before{background:var(--carved)}
.entry.carved .tag{border-color:var(--carved-line)}
.carved-head{display:flex;flex-direction:column;gap:.2rem;margin:1.25rem 0 -.25rem}
.carved-head h2{margin:0;font:600 .72rem/1 var(--mono);letter-spacing:.1em;
                color:var(--carved);text-transform:uppercase}
.carved-head p{margin:0;color:var(--muted);font-size:.82rem}

footer{color:var(--muted);font-size:.78rem;line-height:1.5;border-top:1px solid var(--line);
       padding-top:1rem}
@media (min-width:34rem){ .page{padding:2.5rem 1.5rem 4rem} h1{font-size:1.7rem} }
"""


def to_json(notifications: list[Notification], path: str | Path) -> Path:
    path = Path(path)
    path.write_text(
        json.dumps([n.as_dict() for n in notifications], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


FIELDS = [
    "arrival", "app", "primary_id", "type", "summary",
    "launch", "tag", "group", "recovered", "source",
]


def to_csv(notifications: list[Notification], path: str | Path) -> Path:
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for notification in notifications:
            writer.writerow(notification.as_dict())
    return path


def render_body(notifications: list[Notification], source: str = "") -> str:
    """The report as a `<style>` block plus page content, without a document shell.

    Split out so the same design can be written to a standalone file or embedded
    in a host page that supplies its own ``<head>``.

    The layout is an entry list rather than a table on purpose: these reports get
    read on phones, and a three-column table of long message text is unusable at
    that width.
    """
    live = [n for n in notifications if not n.recovered]
    carved = [n for n in notifications if n.recovered]

    stats = "".join(
        f'<div class="stat"><b>{value}</b><span>{label}</span></div>'
        for label, value in (
            ("records", len(notifications)),
            ("from live tables", len(live)),
            ("recovered", len(carved)),
            ("applications", len({n.app for n in notifications})),
        )
    )

    sections = []
    for day, entries in _by_day(live):
        sections.append(f'<div class="day"><h2>{html.escape(day)}</h2></div>')
        sections.append(f'<ul class="feed">{"".join(_entry(n) for n in entries)}</ul>')

    if carved:
        sections.append(
            '<div class="carved-head"><h2>Recovered</h2>'
            "<p>Carved from freed pages and the write-ahead log. These have no "
            "recorded time and no verified application, so they sit outside the "
            "timeline rather than being placed in it.</p></div>"
            f'<ul class="feed">{"".join(_entry(n) for n in carved)}</ul>'
        )

    return (
        f"<style>{STYLE}</style>"
        '<div class="page">'
        '<header class="masthead">'
        '<p class="eyebrow">Windows notification history</p>'
        "<h1>What this machine put on screen</h1>"
        f'<p class="source">{html.escape(source or "wpndatabase.db")}</p>'
        "</header>"
        f'<div class="stats">{stats}</div>'
        f'{"".join(sections)}'
        "<footer>Times are UTC, taken from the FILETIME columns on each record. "
        "Recovered entries prove that something was shown and what it said — not "
        "when, and not by which application.</footer>"
        "</div>"
    )


def to_html(notifications: list[Notification], path: str | Path, source: str = "") -> Path:
    path = Path(path)
    path.write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Notification timeline</title></head><body>"
        f"{render_body(notifications, source)}"
        "</body></html>",
        encoding="utf-8",
    )
    return path


def _by_day(notifications: list[Notification]) -> list[tuple[str, list[Notification]]]:
    days: dict[str, list[Notification]] = {}
    for notification in notifications:
        key = notification.arrival.strftime("%Y-%m-%d") if notification.arrival else "undated"
        days.setdefault(key, []).append(notification)
    return list(days.items())


def _entry(notification: Notification) -> str:
    when = notification.arrival.strftime("%H:%M:%S") if notification.arrival else "--:--:--"

    labels = [notification.kind, notification.tag, notification.group]
    if notification.partial:
        labels.append("truncated")
    if notification.unresolved_text:
        labels.append("text not stored")
    for content, _ in notification.actions:
        if content:
            labels.append(f"button: {content}")
    tags = "".join(
        f'<span class="tag">{html.escape(str(value))}</span>' for value in labels if value
    )

    absent = " absent" if notification.summary.startswith("(") else ""
    return (
        f'<li class="entry{" carved" if notification.recovered else ""}">'
        f'<div class="head"><time>{html.escape(when)}</time>'
        f'<span class="app">{html.escape(notification.app)}</span></div>'
        f'<p class="msg{absent}">{html.escape(notification.summary)}</p>'
        f'{f"<div class=tags>{tags}</div>" if tags else ""}'
        f'<span class="origin">{html.escape(notification.source)}</span>'
        "</li>"
    )
