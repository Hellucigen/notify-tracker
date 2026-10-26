#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持续录制屏幕右下角一小块区域（Windows 通知弹出的位置）。

用法示例：
    python notify_recorder.py --preview     # 先预览，确认录制范围
    python notify_recorder.py               # 持续录制，Ctrl+C 停止
    python notify_recorder.py --motion      # 只在画面变化时录，省磁盘

录制按 --segment 秒切分成多个 mp4 文件，默认存到脚本同级的 recordings/ 目录。
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import mss
import numpy as np

PREVIEW_TITLE = "Notification recorder - preview"
FILE_PREFIX = "notif_"


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="持续录制屏幕右下角小区域（Windows 通知）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--monitor", type=int, default=1,
                   help="显示器编号：1=主屏，0=所有屏幕拼接；用 --list-monitors 查看")
    p.add_argument("--width", type=int, default=520, help="录制区域宽度（像素）")
    p.add_argument("--height", type=int, default=620, help="录制区域高度（像素）")
    p.add_argument("--right", type=int, default=0, help="距屏幕右边缘的距离（像素）")
    p.add_argument("--bottom", type=int, default=64, help="距屏幕下边缘的距离（像素，默认避开任务栏）")
    p.add_argument("--fps", type=float, default=5.0, help="采集帧率（通知动画 5fps 足够，越低越省 CPU/磁盘）")
    p.add_argument("--segment", type=float, default=120.0, help="单个视频文件时长（秒）")
    p.add_argument("--scale", type=float, default=1.0,
                   help="画面缩放比例，0.5 表示只存一半分辨率（更省磁盘，字会变小）")
    p.add_argument("--quiet", action="store_true", help="不打印实时状态行")
    p.add_argument("--out", default=None, help="保存目录，默认 <脚本目录>/recordings")
    p.add_argument("--codec", default="mp4v", help="OpenCV FOURCC，例如 mp4v / avc1 / XVID")
    p.add_argument("--max-gb", type=float, default=10.0,
                   help="录制目录总大小上限（GB），超出后删除最旧的文件；0 表示不限制")
    p.add_argument("--motion", action="store_true",
                   help="仅在画面有变化时录制（适合长时间挂机，省磁盘）")
    p.add_argument("--threshold", type=float, default=2.5, help="运动检测阈值，越大越不敏感")
    p.add_argument("--hold", type=float, default=5.0, help="画面停止变化后继续录制的秒数")
    p.add_argument("--pre-roll", type=float, default=2.0, help="检测到变化前额外保留的秒数")
    p.add_argument("--preview", action="store_true", help="显示实时预览窗口，Esc 退出")
    p.add_argument("--duration", type=float, default=0.0, help="总录制时长（秒），0 表示一直录到 Ctrl+C")
    p.add_argument("--list-monitors", action="store_true", help="列出显示器信息并退出")
    return p.parse_args(argv)


def enable_dpi_awareness():
    """让进程按物理像素工作，避免缩放比例下录到偏移的区域。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def compute_region(monitor: dict, args) -> dict:
    """根据右下角锚点计算实际录制区域，并保证宽高为偶数（编码器要求）。"""
    left = monitor["left"] + monitor["width"] - args.width - args.right
    top = monitor["top"] + monitor["height"] - args.height - args.bottom
    left = max(monitor["left"], left)
    top = max(monitor["top"], top)
    width = min(args.width, monitor["left"] + monitor["width"] - left)
    height = min(args.height, monitor["top"] + monitor["height"] - top)
    width -= width % 2
    height -= height % 2
    if width <= 0 or height <= 0:
        raise SystemExit("录制区域大小为 0，请检查 --width / --height / --right / --bottom 参数")
    return {"left": int(left), "top": int(top), "width": int(width), "height": int(height)}


class SegmentWriter:
    """一个分段的视频文件；负责开文件、写帧、关闭并清理超出配额的老文件。"""

    def __init__(self, out_dir: Path, fps: float, size: tuple[int, int],
                 codec_code: str, max_bytes: int):
        self.out_dir = out_dir
        self.fps = fps
        self.size = size
        self.codec_code = codec_code
        self.max_bytes = max_bytes
        self.ext = ".avi" if codec_code.upper() in ("XVID", "DIVX", "MJPG") else ".mp4"
        self.writer = None
        self.path: Path | None = None
        self.frames = 0
        self.started_at = 0.0
        self._fourcc = cv2.VideoWriter_fourcc(*codec_code)

    def open(self):
        base = f"{FILE_PREFIX}{datetime.now():%Y%m%d_%H%M%S}"
        path = self.out_dir / f"{base}{self.ext}"
        index = 2
        while path.exists():
            path = self.out_dir / f"{base}_{index}{self.ext}"
            index += 1

        self.writer = cv2.VideoWriter(str(path), self._fourcc, self.fps, self.size)
        if not self.writer.isOpened():
            self.writer = None
            raise RuntimeError(
                f"无法创建视频文件 {path} —— 编码器 {self.codec_code} 可能不可用，"
                f"可以试试 --codec avc1 或 --codec XVID"
            )
        self.path = path
        self.frames = 0
        self.started_at = time.perf_counter()
        return path

    def write(self, frame):
        self.writer.write(frame)
        self.frames += 1

    def size_mb(self) -> float:
        try:
            return self.path.stat().st_size / (1024 * 1024)
        except (OSError, AttributeError):
            return 0.0

    def close(self):
        if self.writer is None:
            return
        self.writer.release()
        self.writer = None
        duration = time.perf_counter() - self.started_at
        print(
            f"\n[保存] {self.path.name}  {self.frames} 帧 / {duration:.1f}s / {self.size_mb():.1f} MB",
            flush=True,
        )
        self._enforce_quota()

    def _enforce_quota(self):
        if self.max_bytes <= 0:
            return
        files = [p for p in self.out_dir.iterdir()
                 if p.is_file() and p.name.startswith(FILE_PREFIX)
                 and p.suffix.lower() in (".mp4", ".avi")]
        files.sort(key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for old in files:
            if total <= self.max_bytes:
                break
            if old == self.path:
                break
            try:
                total -= old.stat().st_size
                old.unlink()
                print(f"[清理] 删除旧文件 {old.name}", flush=True)
            except OSError:
                pass


def set_topmost(title: str):
    """把预览窗口置顶，方便一边校验区域一边调参数。"""
    try:
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if hwnd:
            ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)
    except Exception:
        pass


def main(argv=None) -> int:
    args = parse_args(argv)
    enable_dpi_awareness()

    with mss.MSS() as sct:
        monitors = sct.monitors
        if args.list_monitors:
            for i, mon in enumerate(monitors):
                label = "所有屏幕" if i == 0 else f"显示器 {i}"
                print(f"{i}: {label}  {mon['width']}x{mon['height']} @ ({mon['left']}, {mon['top']})")
            return 0

        if args.monitor < 0 or args.monitor >= len(monitors):
            print(f"错误：--monitor {args.monitor} 不存在，可用范围 0..{len(monitors) - 1}", file=sys.stderr)
            return 2

        monitor = monitors[args.monitor]
        region = compute_region(monitor, args)
        if len(args.codec) != 4:
            print("错误：--codec 必须是 4 个字符，例如 mp4v", file=sys.stderr)
            return 2

        out_dir = Path(args.out) if args.out else Path(__file__).resolve().parent / "recordings"
        out_dir = out_dir.expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        max_bytes = int(args.max_gb * 1024 ** 3) if args.max_gb > 0 else 0

        scale = min(1.0, max(0.2, args.scale))
        native = (region["width"], region["height"])
        size = (max(2, int(native[0] * scale) // 2 * 2), max(2, int(native[1] * scale) // 2 * 2))
        resizing = size != native
        segment = SegmentWriter(out_dir, args.fps, size, args.codec, max_bytes)
        period = 1.0 / args.fps
        small_size = (160, max(2, int(round(160 * size[1] / size[0]))))

        print(f"显示器 {args.monitor}: {monitor['width']}x{monitor['height']} @ ({monitor['left']}, {monitor['top']})")
        print(f"录制区域: {region['width']}x{region['height']} @ ({region['left']}, {region['top']})")
        print(f"保存目录: {out_dir}")
        mode = (f"运动触发（阈值 {args.threshold}，保持 {args.hold}s，前摇 {args.pre_roll}s）"
                if args.motion else "持续录制")
        print(f"模式: {mode} | {args.fps:g} fps | 每 {args.segment:g} 秒一个文件"
              + (f" | 输出 {size[0]}x{size[1]}（缩放 {scale:g}）" if resizing else ""))
        if args.duration > 0:
            print(f"本次录制时长: {args.duration:g} 秒")
        print("按 Ctrl+C 停止" + ("，预览窗口按 Esc 也可以退出" if args.preview else ""))

        if args.preview:
            cv2.namedWindow(PREVIEW_TITLE, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(PREVIEW_TITLE, size[0], size[1])
            set_topmost(PREVIEW_TITLE)

        start = time.perf_counter()
        start_wall = datetime.now()
        next_tick = start
        prev_gray = None
        pre_roll: deque = deque(maxlen=max(1, int(round(args.pre_roll * args.fps))))
        active_until = 0.0
        writer: SegmentWriter | None = None
        seg_start = 0.0
        frames = 0
        segments_saved = 0
        last_status = start
        grab_errors = 0
        exit_code = 0

        def start_segment(now: float):
            nonlocal writer, seg_start
            segment.open()
            writer = segment
            seg_start = now

        def stop_segment():
            nonlocal writer, segments_saved
            if writer is not None:
                segments_saved += 1
                segment.close()
                writer = None

        try:
            while True:
                now = time.perf_counter()
                if args.duration > 0 and now - start >= args.duration:
                    break

                try:
                    shot = sct.grab(region)
                    frame = np.ascontiguousarray(np.asarray(shot)[:, :, :3])
                    if resizing:
                        frame = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
                    grab_errors = 0
                except Exception as exc:
                    grab_errors += 1
                    if grab_errors >= 20:
                        raise
                    print(f"\n[警告] 抓屏失败（第 {grab_errors} 次）：{exc}", flush=True)
                    time.sleep(0.5)
                    next_tick = time.perf_counter()
                    continue

                now = time.perf_counter()
                if args.motion:
                    small = cv2.resize(frame, small_size, interpolation=cv2.INTER_AREA)
                    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                    changed = False
                    if prev_gray is not None:
                        diff = float(cv2.absdiff(gray, prev_gray).mean())
                        changed = diff >= args.threshold
                    prev_gray = gray

                    if changed:
                        active_until = now + args.hold
                    active = changed or now < active_until

                    if active:
                        if writer is None:
                            start_segment(now)
                            for old_frame in pre_roll:
                                segment.write(old_frame)
                        elif now - seg_start >= args.segment:
                            stop_segment()
                            start_segment(now)
                        segment.write(frame)
                    elif writer is not None:
                        stop_segment()
                    pre_roll.append(frame)
                else:
                    if writer is None:
                        start_segment(now)
                    elif now - seg_start >= args.segment:
                        stop_segment()
                        start_segment(now)
                    segment.write(frame)

                frames += 1

                if args.preview:
                    cv2.imshow(PREVIEW_TITLE, frame)
                    if cv2.waitKey(1) & 0xFF == 27:
                        break
                    try:
                        if cv2.getWindowProperty(PREVIEW_TITLE, cv2.WND_PROP_VISIBLE) < 1:
                            break
                    except cv2.error:
                        break

                if not args.quiet and now - last_status >= 1.0:
                    last_status = now
                    elapsed = max(1e-6, now - start)
                    if writer is not None:
                        status = f"录制中 {segment.path.name}  {segment.size_mb():.1f} MB"
                    else:
                        status = "空闲，等待画面变化"
                    print(f"\r[{datetime.now():%H:%M:%S}] {frames / elapsed:4.1f} fps | {status}   ",
                          end="", flush=True)

                next_tick += period
                delay = next_tick - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_tick = time.perf_counter()
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            print(f"\n[错误] {exc}", file=sys.stderr, flush=True)
            exit_code = 1
        finally:
            stop_segment()
            if args.preview:
                cv2.destroyAllWindows()
            elapsed = time.perf_counter() - start
            print(
                f"\n结束：{start_wall:%Y-%m-%d %H:%M:%S} 起，共 {elapsed:.0f} 秒，"
                f"保存 {segments_saved} 个文件到 {out_dir}",
                flush=True,
            )
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
