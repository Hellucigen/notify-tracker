#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看一眼记录器现在的状态：日志里有多少条、最后几条是什么、有没有报错。"""

import csv
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"
CSV_PATH = LOG_DIR / "notifications.csv"
ERR_PATH = LOG_DIR / "error.log"
STATE_PATH = LOG_DIR / ".notify_log_state.json"


def main():
    if not CSV_PATH.exists():
        print("还没有日志文件，说明记录器一次都没运行过。")
        return 1

    with open(CSV_PATH, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    print(f"日志条数: {len(rows)}")
    if rows:
        print(f"时间范围: {rows[0]['时间']} -> {rows[-1]['时间']}")
        newest = max(r["时间"] for r in rows)
        try:
            age = (datetime.now() - datetime.strptime(newest, "%Y-%m-%d %H:%M:%S")).total_seconds()
            print(f"最新一条距今: {age / 60:.1f} 分钟")
        except ValueError:
            pass
        print("最近 3 条:")
        for row in rows[-3:]:
            print(f"  [{row['时间']}] {row['应用']} | {row['标题'][:40]}"
                  + (f" | {row['正文'][:40]}" if row["正文"] else ""))
        apps = Counter(r["应用"] for r in rows).most_common(6)
        print("应用分布: " + "、".join(f"{a}×{n}" for a, n in apps))

    if STATE_PATH.exists():
        print(f"读取游标: {STATE_PATH.read_text(encoding='utf-8')}")
    if ERR_PATH.exists() and ERR_PATH.stat().st_size:
        tail = ERR_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
        print("error.log 有内容：")
        for line in tail:
            print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
