#!/usr/bin/env python3
"""汉王 N10 Plus 鼠标模式（无压感）。

把笔的绝对坐标映射到主显示器，笔尖接触/抬起 = 鼠标左键按下/抬起，
笔尾橡皮 = 鼠标右键。Ctrl+C 退出。

注意：CGEvent 注入需要在 系统设置 → 隐私与安全性 → 辅助功能 中
授权运行本程序的终端 App。
"""

import argparse
import sys
import time

from Quartz import (
    CGEventCreateMouseEvent,
    CGEventGetLocation,
    CGEventPost,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseDragged,
    kCGEventLeftMouseUp,
    kCGEventMouseMoved,
    kCGEventRightMouseDown,
    kCGEventRightMouseDragged,
    kCGEventRightMouseUp,
    kCGHIDEventTap,
    CGMainDisplayID,
    CGDisplayScreenSize,
    CGDisplayBounds,
)
from Quartz.CoreGraphics import CGDisplayPixelsWide, CGDisplayPixelsHigh

from main import PEN_MAX_X, PEN_MAX_Y, PenEvent, PenReader, find_device_serial

# N10 Plus 屏幕物理比例 1872x1404 (10.3", 4:3)。主显示器通常 16:9，
# 按面积等比缩放笔区，让移动距离手感一致，而不是强制铺满全屏。
SCALE_MM = 1.0


class MouseMapper:
    def __init__(self, full_screen: bool):
        self.full_screen = full_screen
        display_id = CGMainDisplayID()
        self.screen_w = CGDisplayPixelsWide(display_id)
        self.screen_h = CGDisplayPixelsHigh(display_id)
        self.bounds = CGDisplayBounds(display_id)
        # 设备可写区域按 4:3 保持比例居中于屏幕
        if full_screen:
            self.area_w, self.area_h = self.screen_w, self.screen_h
        else:
            mm = CGDisplayScreenSize(display_id)  # (w_mm, h_mm)
            area_ratio = 1872 / 1404
            screen_ratio = self.screen_w / self.screen_h
            if screen_ratio > area_ratio:
                self.area_h = self.screen_h
                self.area_w = self.screen_h * area_ratio
            else:
                self.area_w = self.screen_w
                self.area_h = self.screen_w / area_ratio
        self.off_x = self.bounds.origin.x + (self.screen_w - self.area_w) / 2
        self.off_y = self.bounds.origin.y + (self.screen_h - self.area_h) / 2
        self.left_down = False
        self.right_down = False

    def to_screen(self, ev: PenEvent):
        x = self.off_x + ev.x / PEN_MAX_X * self.area_w
        y = self.off_y + ev.y / PEN_MAX_Y * self.area_h
        return x, y

    def post(self, kind, x, y, button=0):
        e = CGEventCreateMouseEvent(None, kind, (x, y), button)
        CGEventPost(kCGHIDEventTap, e)

    def handle(self, ev: PenEvent):
        x, y = self.to_screen(ev)
        if ev.rubber:
            kind = (kCGEventRightMouseDragged if self.right_down
                    else kCGEventRightMouseDown)
            self.post(kind, x, y, 1)
            self.right_down = True
            return
        if self.right_down:  # 橡皮翻回笔尖，先抬起右键
            self.post(kCGEventRightMouseUp, x, y, 1)
            self.right_down = False
        if ev.down:
            kind = (kCGEventLeftMouseDragged if self.left_down
                    else kCGEventLeftMouseDown)
            self.post(kind, x, y)
            self.left_down = True
        else:
            if self.left_down:
                self.post(kCGEventLeftMouseUp, x, y)
                self.left_down = False
            else:
                self.post(kCGEventMouseMoved, x, y)


def main():
    ap = argparse.ArgumentParser(description="汉王 N10 Plus 鼠标模式（无压感）")
    ap.add_argument("--serial", help="adb 设备序列号，默认自动检测")
    ap.add_argument("--full-screen", action="store_true",
                    help="笔区铺满整个屏幕（默认按 4:3 居中映射）")
    args = ap.parse_args()

    serial = args.serial or find_device_serial()
    if not serial:
        print("未发现 adb 设备，请先: adb connect <设备IP>:<端口>", file=sys.stderr)
        sys.exit(1)

    mapper = MouseMapper(args.full_screen)
    mode = "铺满屏幕" if args.full_screen else f"映射区 {mapper.area_w:.0f}x{mapper.area_h:.0f} 居中"
    print(f"设备: {serial} | 屏幕: {mapper.screen_w}x{mapper.screen_h} | {mode}")
    print("笔尖=左键拖动，笔尾=右键。Ctrl+C 退出。")

    import queue
    events: queue.Queue = queue.Queue()
    reader = PenReader(serial, events)
    reader.start()

    try:
        while True:
            try:
                mapper.handle(events.get(timeout=1))
            except queue.Empty:
                if not reader.connected.is_set():
                    print("连接断开，重试中...", file=sys.stderr)
    except KeyboardInterrupt:
        print("\n退出")
    finally:
        reader.stop()


if __name__ == "__main__":
    main()
