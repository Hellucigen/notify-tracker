#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Notify Tracker 图形界面 —— 监听、回看、检索、刻录、归档，一个窗口搞定。

只依赖 Python 标准库（tkinter），双击 app.py 或运行：

    python app.py

底层调用同目录下的 notify_log.py / merge_history.py / toastcrumbs，
所以命令行用法和图形界面完全共用一套数据和代码。
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import zipfile
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"
REC_DIR = HERE / "recovered"
MAIN_CSV = LOG_DIR / "notifications.csv"
CARVED_CSV = LOG_DIR / "carved.csv"
STATE = LOG_DIR / ".notify_log_state.json"
STARTUP_VBS = "start_log_background.vbs"

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def run_python(args, cwd=HERE, capture=False):
    """用当前解释器跑一个子任务；GUI 打包后 sys.executable 同样有效。"""
    return subprocess.run([sys.executable, *args], cwd=str(cwd),
                          creationflags=NO_WINDOW,
                          capture_output=capture, text=True,
                          encoding="utf-8", errors="replace")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Notify Tracker —— Windows 通知记录")
        self.geometry("1080x660")
        self.minsize(880, 520)
        self.monitor = None                       # 我们启动的监听子进程
        self._lock = threading.Lock()

        self._build_toolbar()
        self._build_notebook()
        self._build_statusbar()

        self.refresh_all()
        self.after(3000, self._tick)              # 定期刷新，看到新记录进来

    # ---------- 界面 ----------

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(side=tk.TOP, fill=tk.X)

        self.btn_start = ttk.Button(bar, text="▶ 启动监听", command=self.start_monitor)
        self.btn_stop = ttk.Button(bar, text="■ 停止监听", command=self.stop_monitor, state=tk.DISABLED)
        self.btn_scan = ttk.Button(bar, text="⟳ 刷新", command=self.refresh_all)
        self.btn_carve = ttk.Button(bar, text="⛏ 立即刻录", command=self.do_carve)
        self.btn_replay = ttk.Button(bar, text="↺ 回补历史", command=self.do_replay)
        self.btn_archive = ttk.Button(bar, text="📦 归档数据", command=self.do_archive)
        for b in (self.btn_start, self.btn_stop, self.btn_scan,
                  self.btn_carve, self.btn_replay, self.btn_archive):
            b.pack(side=tk.LEFT, padx=(0, 6))

        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Label(bar, text="搜索").pack(side=tk.LEFT, padx=(0, 4))
        self.var_search = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.var_search, width=28)
        entry.pack(side=tk.LEFT)
        entry.bind("<Return>", lambda _e: self.reload_table())
        entry.bind("<KeyRelease>", lambda _e: self._debounce_reload())
        ttk.Label(bar, text="  应用").pack(side=tk.LEFT)
        self.var_app = tk.StringVar(value="全部")
        self.cmb_app = ttk.Combobox(bar, textvariable=self.var_app, values=["全部"], width=18, state="readonly")
        self.cmb_app.pack(side=tk.LEFT)
        self.cmb_app.bind("<<ComboboxSelected>>", lambda _e: self.reload_table())

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))

    def _build_notebook(self):
        # 实时记录
        self.tab_main = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_main, text="  实时记录  ")
        self.tree_main = self._make_table(self.tab_main,
                                          ["时间", "应用", "标题", "正文", "链接", "通知Id"],
                                          [140, 110, 240, 340, 150, 60])

        # 刻录恢复
        self.tab_carved = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_carved, text="  已被系统删除  ")
        self.tree_carved = self._make_table(self.tab_carved,
                                            ["发现时间", "应用", "内容", "完整度", "位置", "链接"],
                                            [140, 110, 420, 70, 150, 140])

        # 统计
        self.tab_stats = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_stats, text="  统计  ")
        self.txt_stats = tk.Text(self.tab_stats, wrap=tk.NONE, font=("Consolas", 10),
                                 bg=self.cget("bg"), relief=tk.FLAT)
        self.txt_stats.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # 设置
        self.tab_settings = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_settings, text="  设置  ")
        self._build_settings()

    def _make_table(self, parent, columns, widths):
        holder = ttk.Frame(parent)
        holder.pack(fill=tk.BOTH, expand=True)
        tree = ttk.Treeview(holder, columns=columns, show="headings", selectmode="extended")
        for name, width in zip(columns, widths):
            tree.heading(name, text=name)
            tree.column(name, width=width, anchor=tk.W)
        vsb = ttk.Scrollbar(holder, orient=tk.VERTICAL, command=tree.yview)
        hsb = ttk.Scrollbar(holder, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        tree.bind("<Double-1>", lambda _e: self.copy_selected(tree))
        tree.bind("<Control-c>", lambda _e: self.copy_selected(tree))
        return tree

    def _build_settings(self):
        box = ttk.LabelFrame(self.tab_settings, text="监听参数（下次启动监听时生效）", padding=10)
        box.pack(fill=tk.X, padx=10, pady=10)

        ttk.Label(box, text="检查间隔（秒）").grid(row=0, column=0, sticky="w")
        self.var_interval = tk.DoubleVar(value=0.5)
        ttk.Spinbox(box, from_=0.2, to=10, increment=0.1, textvariable=self.var_interval,
                    width=8).grid(row=0, column=1, sticky="w", padx=(8, 30))

        ttk.Label(box, text="刻录间隔（分钟）").grid(row=0, column=2, sticky="w")
        self.var_carve = tk.DoubleVar(value=10)
        ttk.Spinbox(box, from_=0, to=240, increment=1, textvariable=self.var_carve,
                    width=8).grid(row=0, column=3, sticky="w", padx=8)

        ttk.Label(box, text="关键词过滤（逗号分隔）").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.var_exclude = tk.StringVar()
        ttk.Entry(box, textvariable=self.var_exclude, width=50).grid(
            row=1, column=1, columnspan=3, sticky="w", padx=(8, 0), pady=(8, 0))

        row = ttk.Frame(self.tab_settings, padding=(10, 4))
        row.pack(fill=tk.X)
        ttk.Button(row, text="安装开机自启", command=self.install_autostart).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(row, text="取消开机自启", command=self.remove_autostart).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(row, text="打开数据目录", command=lambda: os.startfile(str(LOG_DIR))
                   if os.name == "nt" else None).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(row, text="导出当前表格…", command=self.export_table).pack(side=tk.LEFT)

        tip = ("提示：双击表格行可复制该行文本。\n"
               "命令行等价用法：python notify_log.py（监听）  python -m toastcrumbs.cli list（回看）\n"
               "数据都在 logs/ 和 recovered/ 下，已被 .gitignore 忽略，不会误提交。")
        ttk.Label(self.tab_settings, text=tip, justify=tk.LEFT, foreground="#666").pack(
            anchor=tk.W, padx=12, pady=(6, 0))

    def _build_statusbar(self):
        self.var_status = tk.StringVar(value="准备就绪")
        bar = ttk.Label(self, textvariable=self.var_status, anchor=tk.W, padding=(10, 4),
                        relief=tk.SUNKEN)
        bar.pack(side=tk.BOTTOM, fill=tk.X)

    # ---------- 数据 ----------

    def _rows(self, path, limit=4000):
        if not path.exists():
            return []
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        return rows[-limit:]

    def reload_table(self):
        keyword = self.var_search.get().strip().lower()
        app = self.var_app.get()
        rows = self._rows(MAIN_CSV)

        if keyword or (app and app != "全部"):
            rows = [r for r in rows
                    if (not app or app == "全部" or r.get("应用") == app)
                    and (not keyword or keyword in " ".join(
                        (r.get("应用", ""), r.get("标题", ""), r.get("正文", ""), r.get("链接", ""))).lower())]

        tree = self.tree_main
        tree.delete(*tree.get_children())
        if not rows:
            tree.insert("", tk.END, values=["", "", "还没有记录 —— 点左上角「▶ 启动监听」开始收集", "", "", ""])
        for r in reversed(rows):                            # 最新的排最上面
            tree.insert("", tk.END, values=[r.get("时间", ""), r.get("应用", ""), r.get("标题", ""),
                                            r.get("正文", ""), r.get("链接", ""), r.get("通知Id", "")])

        carved = self._rows(CARVED_CSV)
        self.tree_carved.delete(*self.tree_carved.get_children())
        if not carved:
            self.tree_carved.insert("", tk.END, values=["", "", "还没有刻录记录 —— 「⛏ 立即刻录」可以马上扫一次",
                                                        "", "", ""])
        for r in reversed(carved):
            self.tree_carved.insert("", tk.END, values=[r.get("发现时间", ""), r.get("应用", ""),
                                                        r.get("内容", ""), r.get("完整度", ""),
                                                        r.get("位置", ""), r.get("链接", "")])

        apps = sorted({r.get("应用", "") for r in self._rows(MAIN_CSV, 100000) if r.get("应用")})
        self.cmb_app.configure(values=["全部", *apps])
        self._render_stats()
        self._update_status()

    def _render_stats(self):
        rows = self._rows(MAIN_CSV, 100000)
        carved = self._rows(CARVED_CSV, 100000)
        by_day, by_app = {}, {}
        for r in rows:
            by_day[r.get("时间", "")[:10]] = by_day.get(r.get("时间", "")[:10], 0) + 1
            by_app[r.get("应用", "?")] = by_app.get(r.get("应用", "?"), 0) + 1

        lines = [f"实时记录 {len(rows)} 条　　已被系统删除（刻录）{len(carved)} 条", ""]
        if by_day:
            lines.append("按天")
            width = max(len(v) for v in by_day.values())
            for day in sorted(by_day):
                n = by_day[day]
                lines.append(f"  {day}  {str(n).rjust(width)}  {'█' * min(60, n)}")
            lines.append("")
        if by_app:
            lines.append("按应用")
            width = max(len(v) for v in by_app.values())
            for app, n in sorted(by_app.items(), key=lambda kv: -kv[1])[:20]:
                label = app if len(app) <= 26 else app[:24] + "…"
                lines.append(f"  {label.ljust(28)} {str(n).rjust(width)}  {'█' * min(60, n)}")
        self.txt_stats.delete("1.0", tk.END)
        self.txt_stats.insert("1.0", "\n".join(lines))

    def _update_status(self):
        alive, pid = self.monitor_alive()
        cursor = ""
        if STATE.exists():
            try:
                data = json.loads(STATE.read_text(encoding="utf-8"))
                cursor = f"游标 Id {data.get('id', '?')}"
                age = time.time() - STATE.stat().st_mtime
                cursor += f"（{int(age)} 秒前更新）"
            except (ValueError, OSError):
                pass
        total = len(self._rows(MAIN_CSV, 100000))
        carved = len(self._rows(CARVED_CSV, 100000))
        state = f"监听中 PID {pid}" if alive else "未监听"
        self.var_status.set(f"{state}　|　{cursor}　|　实时 {total} 条　|　刻录 {carved} 条　|　"
                            f"{datetime.now():%Y-%m-%d %H:%M:%S}")

    def _debounce_reload(self):
        if getattr(self, "_after_id", None):
            self.after_cancel(self._after_id)
        self._after_id = self.after(350, self.reload_table)

    def _tick(self):
        if self.monitor_alive()[0] or MAIN_CSV.exists():
            self._update_status()
        self.after(5000, self._tick)

    # ---------- 动作 ----------

    def start_monitor(self):
        if self.monitor_alive()[0]:
            messagebox.showinfo("已在运行", "监听已经在跑了。")
            return
        args = [str(HERE / "notify_log.py"),
                "--interval", str(self.var_interval.get()),
                "--carve-every", str(self.var_carve.get())]
        for kw in [k.strip() for k in self.var_exclude.get().split(",") if k.strip()]:
            args += ["--exclude", kw]
        try:
            self.monitor = subprocess.Popen([sys.executable, *args], cwd=str(HERE),
                                            creationflags=NO_WINDOW)
        except OSError as exc:
            messagebox.showerror("启动失败", str(exc))
            return
        self.after(1200, self._check_started)
        self.btn_start.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.var_status.set("正在启动监听…")

    def _check_started(self):
        """子进程若因单实例锁立刻退出，说明外部已经有一个在跑。"""
        if self.monitor is not None and self.monitor.poll() is not None:
            self.monitor = None
            self.btn_start.configure(state=tk.NORMAL)
            self.btn_stop.configure(state=tk.DISABLED)
            self.var_status.set("已有一个实例在运行（单实例锁生效），未重复启动")
        self._update_status()

    def stop_monitor(self):
        if self.monitor is not None and self.monitor.poll() is None:
            self.monitor.terminate()
            try:
                self.monitor.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.monitor.kill()
        self.monitor = None
        self.btn_start.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)
        self._update_status()

    def monitor_alive(self):
        if self.monitor is not None and self.monitor.poll() is None:
            return True, self.monitor.pid
        # 外部启动的实例（比如双击 vbs）：靠状态文件的新鲜度判断
        if STATE.exists():
            try:
                if time.time() - STATE.stat().st_mtime < 25:
                    return True, "外部"
            except OSError:
                pass
        return False, None

    def do_carve(self):
        self._busy("正在扫描空闲页…")
        threading.Thread(target=self._carve_worker, daemon=True).start()

    def _carve_worker(self):
        try:
            sys.path.insert(0, str(HERE))
            import notify_log
            seen = notify_log.load_seen(LOG_DIR / ".carved_seen.json")
            added, scanned = notify_log.carve_once(
                notify_log.DEFAULT_DB, CARVED_CSV, seen, [],
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            notify_log.save_seen(LOG_DIR / ".carved_seen.json", seen)
            self.after(0, lambda: (self.reload_table(),
                                   self.var_status.set(f"刻录完成：新增 {added} 条（候选 {scanned}）")))
        except Exception as exc:                          # noqa: BLE001 - GUI 里兜底显示
            self.after(0, lambda: messagebox.showerror("刻录失败", str(exc)))

    def do_replay(self):
        if not messagebox.askokcancel("回补历史",
                                      "将调用 toastcrumbs 从数据库空闲页/WAL 里\n"
                                      "把已被系统删除的通知整理成 recovered/history.csv。\n"
                                      "数据量大时可能需要十几秒，继续吗？"):
            return
        self._busy("正在回补…")
        threading.Thread(target=self._replay_worker, daemon=True).start()

    def _replay_worker(self):
        try:
            REC_DIR.mkdir(exist_ok=True)
            run_python(["-m", "toastcrumbs.cli", "export", "--format", "json",
                        "-o", str(REC_DIR / "toastcrumbs.json")])
            run_python(["-m", "toastcrumbs.cli", "export", "-o", str(REC_DIR / "notifications.html")])
            proc = run_python([str(HERE / "merge_history.py")], capture=True)
            text = (proc.stdout or "") + (proc.stderr or "")
            self.after(0, lambda: (self.reload_table(),
                                   self.var_status.set("回补完成，见 recovered/ 目录"),
                                   messagebox.showinfo("回补完成", text.strip() or "已生成 recovered/history.csv")))
        except Exception as exc:                          # noqa: BLE001
            self.after(0, lambda: messagebox.showerror("回补失败", str(exc)))

    def do_archive(self):
        """把已采集的数据打包到上级目录并从工作目录移走（发布前清理隐私）。"""
        if not MAIN_CSV.exists() and not CARVED_CSV.exists():
            messagebox.showinfo("没有数据", "logs/ 里还没有任何记录。")
            return
        target = filedialog.asksaveasfilename(
            title="归档到", initialdir=str(HERE.parent),
            defaultextension=".zip",
            initialfile=f"notify-data-{datetime.now():%Y-%m-%d}.zip",
            filetypes=[("Zip 压缩包", "*.zip")])
        if not target:
            return
        out = Path(target)
        try:
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                for folder in (LOG_DIR, REC_DIR):
                    if folder.exists():
                        for item in sorted(folder.rglob("*")):
                            if item.is_file():
                                z.write(item, item.relative_to(HERE).as_posix())
            keep = messagebox.askyesno("归档完成", f"已写入 {out.name}\n\n是否同时清空项目里的这些数据？")
            if keep:
                for folder in (LOG_DIR, REC_DIR):
                    if folder.exists():
                        for item in sorted(folder.rglob("*")):
                            if item.is_file():
                                item.unlink()
                self.reload_table()
            self.var_status.set(f"归档完成：{out}")
        except OSError as exc:
            messagebox.showerror("归档失败", str(exc))

    def install_autostart(self):
        if os.name != "nt":
            messagebox.showinfo("不支持", "开机自启仅在 Windows 下可用。")
            return
        startup = Path(os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"))
        vbs = HERE / STARTUP_VBS
        if not vbs.exists():
            messagebox.showerror("缺少文件", f"找不到 {vbs.name}")
            return
        try:
            target = startup / STARTUP_VBS
            target.write_text(vbs.read_text(encoding="utf-8"), encoding="utf-8")
            messagebox.showinfo("已安装", f"开机自启已写入：\n{target}")
        except OSError as exc:
            messagebox.showerror("安装失败", str(exc))

    def remove_autostart(self):
        startup = Path(os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"))
        target = startup / STARTUP_VBS
        if target.exists():
            try:
                target.unlink()
                messagebox.showinfo("已取消", "开机自启已移除。")
                return
            except OSError as exc:
                messagebox.showerror("移除失败", str(exc))
                return
        messagebox.showinfo("没有自启项", "当前没有安装开机自启。")

    def export_table(self):
        tree = (self.tree_main if self.notebook.index(self.notebook.select()) == 0 else self.tree_carved)
        rows = [tree.item(i, "values") for i in tree.get_children()]
        if not rows:
            messagebox.showinfo("空的", "当前表格没有内容可导出。")
            return
        path = filedialog.asksaveasfilename(
            title="导出", defaultextension=".csv",
            initialfile=f"notify-export-{datetime.now():%Y%m%d-%H%M}.csv",
            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        header = [tree.heading(c, "text") for c in tree["columns"]]
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            writer.writerows(rows)
        self.var_status.set(f"已导出 {len(rows)} 行 → {path}")

    def copy_selected(self, tree):
        items = tree.selection()
        if not items:
            return
        text = "\n".join("\t".join(str(v) for v in tree.item(i, "values")) for i in items)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.var_status.set(f"已复制 {len(items)} 行到剪贴板")

    def _busy(self, message):
        self.var_status.set(message)
        for btn in (self.btn_carve, self.btn_replay):
            btn.configure(state=tk.DISABLED)
        self.after(8000, lambda: (self.btn_carve.configure(state=tk.NORMAL),
                                  self.btn_replay.configure(state=tk.NORMAL)))

    def refresh_all(self):
        self.reload_table()


def main():
    if sys.version_info < (3, 10):
        print("需要 Python 3.10 或更高版本")
        return 1
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
