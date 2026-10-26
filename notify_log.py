#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Windows 通知记成文字日志——不截屏、不录屏，几乎零占用。

原理：Windows 自己会把每一条通知（含正文 XML）写进
    %LOCALAPPDATA%\\Microsoft\\Windows\\Notifications\\wpndatabase.db
本脚本只读方式轮询这个 SQLite 库，把新到达的通知追加到 CSV / JSONL 日志。
被「勿扰模式」吞掉、没在屏幕上显示过的通知同样能被记录。

用法：
    python notify_log.py                 # 持续监听，Ctrl+C 停止
    python notify_log.py --dump 30       # 查看最近的 30 条历史
    python notify_log.py --once          # 只拉取一次（可配合计划任务）
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB = Path(os.path.expandvars(
    r"%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db"))
FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
TEXT_RE = re.compile(r"<text[^>]*>(.*?)</text>", re.S | re.I)
URL_RE = re.compile(r"https?://[^\s\"'<>]+")

QUERY = """
SELECT n."Order", n.Id, n.Type, n.Payload, n.PayloadType, n.ArrivalTime,
       n."Group", n.Tag, h.PrimaryId, h.HandlerType, a.DisplayName
FROM Notification n
LEFT JOIN NotificationHandler h ON n.HandlerId = h.RecordId
LEFT JOIN (SELECT HandlerId, AssetValue AS DisplayName
           FROM HandlerAssets WHERE AssetKey = 'DisplayName') a
       ON a.HandlerId = h.RecordId
WHERE n.Id > ? AND n.Type IN ({types}){since}
ORDER BY n.Id ASC LIMIT 500
"""


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="读取 Windows 通知数据库，把通知记成文字日志（无需截屏）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--db", default=None, help=f"wpndatabase.db 路径，默认 {DEFAULT_DB}")
    p.add_argument("--out", default=None, help="日志目录，默认 <脚本目录>/logs")
    p.add_argument("--interval", type=float, default=0.5,
                   help="检查通知数据库是否有写入的间隔（秒）；只有变化时才真正查询")
    p.add_argument("--types", default="toast", help="记录的通知类型，逗号分隔，如 toast,raw,tile")
    p.add_argument("--csv", dest="fmt_csv", action="store_true", default=True, help="写 notifications.csv")
    p.add_argument("--no-csv", dest="fmt_csv", action="store_false", help="不写 CSV")
    p.add_argument("--json", dest="fmt_json", action="store_true", default=True, help="写 notifications.jsonl（含原始 XML）")
    p.add_argument("--no-json", dest="fmt_json", action="store_false", help="不写 JSONL")
    p.add_argument("--dump", type=int, metavar="N", default=None, help="打印最近 N 条历史后退出")
    p.add_argument("--replay", action="store_true", help="首次运行时把库里已有的通知也补记一遍")
    p.add_argument("--since", default=None, metavar="日期",
                   help='只处理该日期之后的通知，如 --since today 或 --since 2026-10-20 08:00')
    p.add_argument("--once", action="store_true", help="只拉取一次新通知后退出")
    p.add_argument("--carve-every", type=float, default=10.0, metavar="分钟",
                   help="每隔多少分钟做一次刻录，把已被系统删除、但字节还在的通知也存下来；0 关闭")
    p.add_argument("--exclude", action="append", default=[], metavar="关键词",
                   help="跳过应用名/标题/正文含该关键词的通知，可重复使用")
    p.add_argument("--quiet", action="store_true", help="不在控制台打印，只写日志")
    return p.parse_args(argv)


def filetime_to_local(value):
    """FILETIME（100ns，UTC）转本地时间。"""
    if not isinstance(value, int) or value <= 0:
        return None
    try:
        return (FILETIME_EPOCH + timedelta(microseconds=value / 10)).astimezone()
    except (OverflowError, OSError, ValueError):
        return None


def local_to_filetime(dt_local):
    """本地时间转 FILETIME（100ns，UTC 起点 1601）。"""
    delta = dt_local.astimezone(timezone.utc) - FILETIME_EPOCH
    return int(delta.total_seconds() * 10_000_000)


def parse_since(text):
    """解析 --since：today / 2026-10-21 / '2026-10-21 08:00'。"""
    if not text:
        return None
    text = text.strip()
    if text.lower() in ("today", "今天"):
        base = datetime.now()
        return datetime(base.year, base.month, base.day)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m-%d", "%H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise SystemExit(f"错误：无法识别的日期 {text!r}（支持 today / 2026-10-21 / '2026-10-21 08:00'）")


def decode_payload(blob):
    if not blob:
        return ""
    if isinstance(blob, str):
        return blob
    for encoding in ("utf-8", "utf-16-le", "utf-16-be", "gbk"):
        try:
            return blob.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return blob.decode("utf-8", "replace")


def parse_text_fields(xml_text):
    """从 Toast XML 里取出文本行、按钮、<toast> 属性。解析失败时退回正则。"""
    texts = []
    buttons = []
    attrs = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        texts = [re.sub(r"\s+", " ", t).strip() for t in TEXT_RE.findall(xml_text)]
        header = re.search(r"<toast([^>]*)>", xml_text, re.I)
        if header:
            for key, value in re.findall(r'([\w-]+)="([^"]*)"', header.group(1)):
                attrs[key] = value
        return texts, buttons, attrs
    if root.tag.lower() == "toast":
        attrs = dict(root.attrib)
    for node in root.iter():
        tag = node.tag.lower()
        if tag == "text":
            value = "".join(node.itertext()).strip()
            if value:
                texts.append(value)
        elif tag == "button":
            value = (node.get("Content") or "").strip()
            if value:
                buttons.append(value)
    return texts, buttons, attrs


def db_stamp(paths):
    """数据库及其 WAL 文件的 (大小, 修改时间) 指纹，用来判断有没有新写入。"""
    sig = []
    for path in paths:
        try:
            st = path.stat()
            sig.append((st.st_size, st.st_mtime_ns))
        except OSError:
            sig.append((0, 0))
    return tuple(sig)


def open_db(path: Path):
    """只读打开；WPN 服务正在写也能读（WAL 模式）。"""
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=1.0)
    con.text_factory = lambda b: b.decode("utf-8", "replace") if isinstance(b, bytes) else b
    return con


def fetch(con, after_id, types_sql, since_ft=None):
    params = [after_id]
    if since_ft:
        params.append(since_ft)
    return con.execute(QUERY.format(types=types_sql, since=" AND n.ArrivalTime >= ?" if since_ft else ""),
                       params).fetchall()


def clean_text(value):
    """换成普通空格、去掉不间断/零宽字符，并压成单行（Excel 里一格一行）。"""
    value = re.sub(r"[\u034f\u200b-\u200f\u202f\u2060\ufeff\xa0\u2007]", " ", value)
    value = re.sub(r"\s*[\r\n]+\s*", " ", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    return value.strip()


def extract_link(xml_text, attrs):
    """取出可跳转原文的链接：优先 http(s)，其次 toast 的 launch 深链。"""
    decoded = urllib.parse.unquote(attrs.get("launch", "") or "")
    found = []
    for source in (decoded, xml_text):
        for url in URL_RE.findall(source):
            url = url.split(";")[0].rstrip("&;,")   # 去掉拼在后面的第二个链接
            if url and url not in found:
                found.append(url)
    if found:
        return found[0]
    return decoded


def app_name_from_actions(xml_text):
    """手机镜像通知的真实应用名藏在按钮参数 ToastNotificationAppName= 里。"""
    match = re.search(r"ToastNotificationAppName=([^&\"'\s]+)", xml_text)
    if match:
        return urllib.parse.unquote(match.group(1))
    return ""


def build_record(row):
    (order, nid, ntype, payload, payload_type, arrival, group, tag,
     primary_id, handler_type, display_name) = row
    xml_text = decode_payload(payload)
    texts, buttons, attrs = parse_text_fields(xml_text)
    buttons = [clean_text(b) for b in buttons]
    app = display_name or app_name_from_actions(xml_text) or primary_id or "?"
    # 手机镜像通知的第一行就是应用名，重复出现没意义，往下挪一位
    if len(texts) > 1 and clean_text(texts[0]) == app:
        texts = texts[1:]
    title = clean_text(texts[0]) if texts else ""
    body = " / ".join(clean_text(t) for t in texts[1:] if clean_text(t))
    when = filetime_to_local(arrival)
    return {
        "order": order,
        "id": nid,
        "time": when.strftime("%Y-%m-%d %H:%M:%S") if when else "",
        "app": app,
        "app_id": primary_id or "",
        "type": ntype,
        "title": title,
        "body": body,
        "link": extract_link(xml_text, attrs),
        "buttons": buttons,
        "group": group or "",
        "tag": tag or "",
        "duration": attrs.get("duration", ""),
        "arrival_filetime": arrival,
        "payload": xml_text,
    }


def one_line(rec):
    parts = [rec["app"], rec["title"]]
    if rec["body"]:
        parts.append(rec["body"])
    text = " | ".join(p for p in parts if p)
    stamp = rec["time"].split(" ")[-1] if rec["time"] else "--:--:--"
    return f"[{stamp}] {text}"


class LogFiles:
    def __init__(self, out_dir: Path, want_csv: bool, want_json: bool):
        out_dir.mkdir(parents=True, exist_ok=True)
        self.csv_path = out_dir / "notifications.csv"
        self.json_path = out_dir / "notifications.jsonl"
        self.want_csv = want_csv
        self.want_json = want_json
        if want_csv and not self.csv_path.exists():
            with open(self.csv_path, "w", encoding="utf-8-sig", newline="") as fh:
                csv.writer(fh).writerow(
                    ["时间", "应用", "AppId", "标题", "正文", "链接", "按钮", "类型", "通知Id", "位置序号"])

    def append(self, rec):
        """写一行；文件被 Excel/WPS 占用时重试几次，仍失败就抛给调用方（游标不前进，下轮重试）。"""
        def write_csv():
            with open(self.csv_path, "a", encoding="utf-8-sig", newline="") as fh:
                csv.writer(fh).writerow([
                    rec["time"], rec["app"], rec["app_id"], rec["title"], rec["body"],
                    rec["link"], " / ".join(rec["buttons"]), rec["type"], rec["id"], rec["order"]])

        def write_json():
            with open(self.json_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

        for attempt in range(4):
            try:
                if self.want_csv:
                    write_csv()
                if self.want_json:
                    write_json()
                return
            except OSError:
                if attempt == 3:
                    raise
                time.sleep(0.4 * (attempt + 1))


    def logged_ids(self):
        """已经写过的通知 Id，用于 --replay 时去重（Order 会被系统重编号，不能用）。"""
        ids = set()
        if self.want_json and self.json_path.exists():
            for line in self.json_path.read_text(encoding="utf-8").splitlines():
                try:
                    ids.add(int(json.loads(line)["id"]))
                except (ValueError, KeyError, TypeError):
                    continue
        return ids


def load_state(state_path: Path, db_path: Path, jsonl_path: Path):
    """读取上次进度。早期版本按 Order 记录，而 Order 会被重编号，
    所以遇到旧格式时改用 JSONL 里最大的 Id 精确续读。"""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if data.get("db") == str(db_path).lower():
            if "id" in data:
                return int(data["id"])
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    fallback = 0
    try:
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            try:
                fallback = max(fallback, int(json.loads(line)["id"]))
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        pass
    return fallback if fallback else None


def save_state(state_path: Path, db_path: Path, last_id: int):
    try:
        state_path.write_text(
            json.dumps({"db": str(db_path).lower(), "id": last_id}), encoding="utf-8")
    except OSError:
        pass


def acquire_singleton_lock():
    """命名互斥体，防止开机自启和手动启动同时跑导致日志重复。返回句柄或 None。"""
    if sys.platform != "win32":
        return object()
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\notify_log_single_instance")
    if not handle:
        return object()
    if kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return handle


def redirect_console_to_log(out_arg):
    """pythonw 启动时没有控制台（sys.stdout/stderr 为 None），把输出接到日志文件，
    否则后台实例一旦报错就什么痕迹都不留。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_dir = Path(out_arg).expanduser() if out_arg else Path(__file__).resolve().parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handle = open(log_dir / "runner.log", "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = handle
        if sys.stderr is None:
            sys.stderr = handle
    except OSError:
        pass


def fingerprint(payload):
    """把 XML 里的空白归一后取哈希，用来跨"活动表"和"刻录"两条路径去重。"""
    return hashlib.sha1(re.sub(r"\s+", " ", payload or "").strip().encode("utf-8", "replace")).hexdigest()


def load_seen(path: Path, limit: int = 20000):
    try:
        items = json.loads(path.read_text(encoding="utf-8")).get("seen", [])
        return set(items[-limit:])
    except (OSError, ValueError, AttributeError, TypeError):
        return set()


def save_seen(path: Path, seen):
    try:
        keep = list(seen)[-20000:]
        path.write_text(json.dumps({"seen": keep}), encoding="utf-8")
    except OSError:
        pass


APP_ARG_RE = re.compile(r"ToastNotificationAppName=([^&\"'\s]+)")


def carved_app(notification):
    """刻出来的记录丢了应用归属，只能从按钮参数里捞。"""
    for label, arguments in notification.actions or ():
        match = APP_ARG_RE.search(arguments or "")
        if match:
            return urllib.parse.unquote(match.group(1))
    return notification.app or ""


def carve_once(db_path: Path, out_csv: Path, seen: set, excludes, when: str):
    """扫描数据库空闲页和 WAL，把已被系统删掉、但字节还在的通知追加进 carved.csv。"""
    from toastcrumbs.carve import carve_all, live_fingerprints
    from toastcrumbs.database import open_copy, read_handlers, read_notifications
    from toastcrumbs.toast import try_parse_payload

    with open_copy(db_path) as con:                      # 复制后再开，绝不碰真库
        handlers = read_handlers(con)
        live = [try_parse_payload(n) for n in read_notifications(con, handlers)]
        known = live_fingerprints(live)
        for item in live:                                # 活着的已经由主循环记过了
            seen.add(fingerprint(item.payload))

    recovered, _stats = carve_all(db_path, known)        # 只读文件字节，不经过 SQLite
    added = 0
    new_rows = []
    for item in recovered:
        parsed = try_parse_payload(item)
        parts = [clean_text(t) for t in (parsed.text or [])]
        text = " | ".join(p for p in parts if p)
        if not text:
            continue
        fp = fingerprint(parsed.payload)
        if fp in seen:
            continue
        seen.add(fp)
        app = carved_app(parsed)
        if excludes and any(k in f"{app} {text}".lower() for k in excludes):
            continue
        new_rows.append([when, app, text, "截断" if parsed.partial else "完好",
                         parsed.source or "", extract_link(parsed.payload or "",
                                                           {"launch": parsed.launch or ""})])
        added += 1
    if new_rows:
        fresh = not out_csv.exists()
        with open(out_csv, "a", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            if fresh:
                writer.writerow(["发现时间", "应用", "内容", "完整度", "位置", "链接"])
            writer.writerows(new_rows)
    return added, len(recovered)


def main(argv=None) -> int:
    args = parse_args(argv)
    redirect_console_to_log(args.out)
    lock = acquire_singleton_lock()
    if lock is None:
        print("已经有另一个 notify_log.py 实例在运行，本次退出（避免重复记录）")
        return 0
    db_path = Path(args.db).expanduser() if args.db else DEFAULT_DB
    if not db_path.exists():
        print(f"错误：找不到通知数据库 {db_path}", file=sys.stderr)
        return 1

    out_dir = Path(args.out).expanduser() if args.out else Path(__file__).resolve().parent / "logs"
    state_path = out_dir / ".notify_log_state.json"
    type_list = [t for t in re.split(r"[,\s]+", args.types.strip()) if t]
    if not type_list:
        print("错误：--types 不能为空", file=sys.stderr)
        return 2
    types_sql = ", ".join("'" + t.replace("'", "") + "'" for t in type_list)
    since_dt = parse_since(args.since)
    since_ft = local_to_filetime(since_dt) if since_dt else None

    try:
        con = open_db(db_path)
    except sqlite3.Error as exc:
        print(f"错误：无法打开通知数据库（{exc}）", file=sys.stderr)
        return 1

    if args.dump:
        rows = fetch(con, 0, types_sql, since_ft)
        records = [build_record(r) for r in rows][-args.dump:]
        print(f"最近 {len(records)} 条通知（来自 {db_path}）：")
        for rec in records:
            print("  " + one_line(rec))
        if not records:
            print("  （库里没有记录，可能系统刚清理过，或 --types 过滤太严）")
        con.close()
        return 0

    try:
        max_id = con.execute("SELECT COALESCE(MAX(Id), 0) FROM Notification").fetchone()[0]
    except sqlite3.Error:
        max_id = 0

    logger = LogFiles(out_dir, args.fmt_csv, args.fmt_json)
    already = set()
    cursor = load_state(state_path, db_path, logger.json_path)
    if args.replay:
        cursor = 0
        already = logger.logged_ids()
        if not args.quiet and already:
            print(f"日志里已有 {len(already)} 条，补记时会自动跳过")
    elif cursor is None:
        cursor = max_id
        if not args.quiet:
            print(f"起始位置：DB 当前最大通知 Id = {max_id}，只记录之后的新通知")
    excludes = [k.lower() for k in args.exclude if k.strip()]
    carved_csv = out_dir / "carved.csv"
    carved_path = out_dir / ".carved_seen.json"
    carved_seen = load_seen(carved_path)
    carve_minutes = args.carve_every if args.carve_every > 0 and not args.once else 0
    next_carve = time.perf_counter()
    carved_total = 0
    if not args.quiet:
        print(f"通知数据库: {db_path}")
        print(f"日志目录:   {out_dir}")
        print(f"检查间隔:   {args.interval:g} 秒（数据库有写入才查询，空闲时几乎不占 CPU） "
              f"| 类型: {', '.join(type_list)}")
        if since_dt:
            print(f"时间过滤:   只记 {since_dt:%Y-%m-%d %H:%M} 之后的通知"
                  + ("（配合 --replay 补记历史）" if args.replay else ""))
        if carve_minutes:
            print(f"刻录:       每 {carve_minutes:g} 分钟扫一次空闲页/WAL，"
                  f"已被系统删掉的通知追加到 {carved_csv.name}")
        print("等待新通知… 按 Ctrl+C 停止\n")

    newest = cursor
    rows = []
    written = 0
    skipped = 0
    idle_rounds = 0
    exit_code = 0
    watch_files = [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]
    last_stamp = None
    try:
        while True:
            if carve_minutes and time.perf_counter() >= next_carve:
                next_carve = time.perf_counter() + carve_minutes * 60
                try:
                    added, scanned = carve_once(db_path, carved_csv, carved_seen, excludes,
                                                datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    carved_total += added
                    save_seen(carved_path, carved_seen)
                    if added and not args.quiet:
                        print(f"[刻录] 新发现 {added} 条已被系统删掉的通知（扫到 {scanned} 条候选）"
                              f" → {carved_csv.name}", flush=True)
                except Exception as exc:
                    if not args.quiet:
                        print(f"[刻录跳过] {exc}", flush=True)

            stamp = db_stamp(watch_files)
            if stamp == last_stamp and not args.once:
                time.sleep(args.interval)
                continue
            last_stamp = stamp
            try:
                rows = fetch(con, newest, types_sql, since_ft)
                idle_rounds = 0
            except sqlite3.Error:
                # 数据库可能被 WPN 服务重建（例如服务重启），重连一次
                idle_rounds += 1
                try:
                    con.close()
                except Exception:
                    pass
                try:
                    con = open_db(db_path)
                except sqlite3.Error:
                    if not args.quiet and idle_rounds % 30 == 1:
                        print(f"[警告] 暂时读不到通知数据库，重试中…（第 {idle_rounds} 次）", flush=True)
                if args.once:
                    break
                time.sleep(max(1.0, args.interval))
                continue

            for row in rows:
                rec = build_record(row)
                # 游标和去重都用 Id：Order 会在旧行被删后重新编号，拿它当游标会漏掉新通知
                if rec["id"] in already:
                    newest = max(newest, rec["id"])
                    continue
                if excludes and any(kw in " ".join(
                        (rec["app"], rec["app_id"], rec["title"], rec["body"])).lower()
                        for kw in excludes):
                    skipped += 1
                    newest = max(newest, rec["id"])
                    continue
                if rec["title"] or rec["body"]:
                    try:
                        logger.append(rec)
                    except OSError as exc:
                        print(f"[写入失败] {logger.csv_path.name} 正被其它程序占用"
                              f"（{exc.strerror}），这条留到下一轮重试", flush=True)
                        break
                    written += 1
                    carved_seen.add(fingerprint(rec["payload"]))
                    newest = max(newest, rec["id"])
                    if not args.quiet:
                        print(one_line(rec), flush=True)
                else:
                    newest = max(newest, rec["id"])
                    if not args.quiet:
                        print(f"[跳过] {rec['app']} 的 {rec['type']} 通知没有正文", flush=True)

            save_state(state_path, db_path, newest)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"\n[错误] {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        try:
            con.close()
        except Exception:
            pass
        if carve_minutes:
            save_seen(carved_path, carved_seen)
        if not args.quiet:
            logged = 0
            if logger.want_csv and logger.csv_path.exists():
                with open(logger.csv_path, encoding="utf-8-sig", newline="") as fh:
                    logged = max(0, sum(1 for _ in csv.reader(fh)) - 1)
            print(f"\n本次写入 {written} 条"
                  + (f"（按关键词跳过 {skipped} 条）" if skipped else "")
                  + (f"，刻录新增 {carved_total} 条" if carve_minutes else "")
                  + f"；日志累计 {logged} 条 -> "
                  f"{logger.csv_path if logger.want_csv else logger.json_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
