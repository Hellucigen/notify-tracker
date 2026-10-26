#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 toastcrumbs 导出的 JSON 整理成一张干净的本地时间表格。

toastcrumbs 的原始导出有两处不便：时间是 UTC、并且混着大量没有正文的
toastCondensed 重复项。这个脚本做后处理：

    过滤空正文 → UTC 转本地 → 补出应用名 → 抽出可跳转原文的链接 → 分成"活动表"和"已恢复"两段

用法：
    python merge_history.py                     # 读 recovered/toastcrumbs.json
    python merge_history.py -i 别的.json -o 别的.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

from notify_log import clean_text, extract_link

APPLY_RE = re.compile(r"ToastNotificationAppName=([^&\"'\s]+)")
# 深链协议 → 应用，只在恢复项（丢了应用字段）时用来兜底
SCHEME_APPS = {"ntqq-notification": "QQ", "weixin": "微信", "msteams": "Microsoft Teams"}


def app_from_launch(launch):
    match = re.match(r"([a-zA-Z][\w+.-]*):", launch or "")
    if match:
        return SCHEME_APPS.get(match.group(1).lower(), "")
    return ""


def parse_args(argv=None):
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="整理 toastcrumbs 导出结果为本地时间 CSV")
    p.add_argument("-i", "--input", default=str(here / "recovered" / "toastcrumbs.json"),
                   help="toastcrumbs 导出的 JSON")
    p.add_argument("-o", "--output", default=str(here / "recovered" / "history.csv"),
                   help="输出 CSV 路径")
    p.add_argument("--exclude", action="append", default=[], help="跳过含该关键词的记录，可重复")
    return p.parse_args(argv)


def to_local(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""


def resolve_app(item, texts):
    """活动表条目自带 app；恢复出来的只能从按钮参数或深链协议猜。"""
    app = item.get("app") or ""
    if app and app != "(unknown app)":
        return app, "系统记录"
    for action in item.get("actions") or []:
        match = APPLY_RE.search(action.get("arguments") or "")
        if match:
            return urllib.parse.unquote(match.group(1)), "按钮参数推断"
    guessed = app_from_launch(item.get("launch") or "")
    if guessed:
        return guessed, "深链协议推断"
    return "（未知）", "无法确定"


def main(argv=None) -> int:
    args = parse_args(argv)
    src = Path(args.input)
    if not src.exists():
        print(f"错误：找不到 {src}\n先生成它：python -m toastcrumbs.cli export --format json -o \"{src}\"",
              file=sys.stderr)
        return 1

    items = json.loads(src.read_text(encoding="utf-8"))
    excludes = [k.lower() for k in args.exclude if k.strip()]
    rows = []
    dropped_empty = 0
    dropped_excluded = 0

    for item in items:
        texts = [clean_text(t) for t in (item.get("text") or []) if clean_text(t)]
        if not texts:
            dropped_empty += 1
            continue
        app, app_src = resolve_app(item, texts)
        if len(texts) > 1 and texts[0] == app:
            texts = texts[1:]
        blob = " ".join([app, *texts]).lower()
        if any(k in blob for k in excludes):
            dropped_excluded += 1
            continue
        launch = item.get("launch") or ""
        rows.append({
            "时间": to_local(item.get("arrival")),
            "应用": app,
            "应用名来源": app_src,
            "标题": texts[0],
            "正文": " / ".join(texts[1:]),
            "链接": extract_link(launch, {"launch": launch}),
            "来源": "已恢复" if item.get("recovered") else "活动表",
            "状态": "截断" if item.get("partial") else "完好",
            "类型": item.get("type") or "",
        })

    timed = sorted([r for r in rows if r["时间"]], key=lambda r: r["时间"])
    undated = [r for r in rows if not r["时间"]]
    final = timed + undated

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = ["时间", "应用", "标题", "正文", "链接", "来源", "状态", "应用名来源", "类型"]
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        writer.writerows({k: r[k] for k in header} for r in final)

    print(f"读入 {len(items)} 条 → 写出 {len(final)} 条到 {out}")
    print(f"  丢弃：无正文 {dropped_empty} 条"
          + (f"，关键词过滤 {dropped_excluded} 条" if dropped_excluded else ""))
    if timed:
        print(f"  活动表（带时间）{len(timed)} 条，{timed[0]['时间']} → {timed[-1]['时间']}")
    if undated:
        print(f"  已恢复（无时间）{len(undated)} 条，排在表格末尾")
    counts = {}
    for r in final:
        counts[r["应用"]] = counts.get(r["应用"], 0) + 1
    print("  应用分布：" + "、".join(f"{a}×{n}" for a, n in
                                    sorted(counts.items(), key=lambda kv: -kv[1])[:10]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
