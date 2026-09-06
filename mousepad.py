#!/usr/bin/env python3
"""汉王 N10 Plus 鼠标模式（无压感）。

把笔的绝对坐标映射到主显示器，笔尖接触/抬起 = 鼠标左键按下/抬起，
笔尾橡皮 = 鼠标右键。

带一个小状态窗口（Esc 或关窗退出），可在 .app 中无终端运行。
CGEvent 注入需要 系统设置 → 隐私与安全性 → 辅助功能 授权。
"""

import argparse
import queue
import sys

import pygame
from Quartz import (
    CGEventCreateMouseEvent,
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
    CGDisplayBounds,
)
from Quartz.CoreGraphics import CGDisplayPixelsWide, CGDisplayPixelsHigh

from main import PEN_MAX_X, PEN_MAX_Y, PenEvent, PenReader, find_device_serial


class MouseMapper:
    def __init__(self, full_screen: bool, rotation: int = 0):
        self.full_screen = full_screen
        self.rotation = rotation
        display_id = CGMainDisplayID()
        self.screen_w = CGDisplayPixelsWide(display_id)
        self.screen_h = CGDisplayPixelsHigh(display_id)
        self.bounds = CGDisplayBounds(display_id)
        if full_screen:
            self.area_w, self.area_h = self.screen_w, self.screen_h
        else:
            # N10 是 4:3，按比例居中映射保持手感
            area_ratio = PEN_MAX_X / PEN_MAX_Y
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
        dx, dy = ev.x, ev.y
        if self.rotation == 90:
            dx, dy = PEN_MAX_Y - dy, dx
        elif self.rotation == 180:
            dx, dy = PEN_MAX_X - dx, PEN_MAX_Y - dy
        elif self.rotation == 270:
            dx, dy = dy, PEN_MAX_X - dx
        x = self.off_x + dx / PEN_MAX_X * self.area_w
        y = self.off_y + dy / PEN_MAX_Y * self.area_h
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


def start_mousepad(serial: str | None = None, full_screen: bool = False,
                   rotation: int = 0):
    """带小状态窗口运行，供 CLI 和 .app 启动器共用。"""
    serial = serial or find_device_serial()
    if not serial:
        print("未发现 adb 设备，请先: adb connect <设备IP>:<端口>", file=sys.stderr)
        sys.exit(1)

    mapper = MouseMapper(full_screen, rotation)
    pygame.init()
    screen = pygame.display.set_mode((380, 150))
    pygame.display.set_caption("汉王鼠标模式")
    font = pygame.font.SysFont("pingfangsc,hiraginosansgb", 14)

    events: queue.Queue = queue.Queue()
    reader = PenReader(serial, events)
    reader.start()

    clock = pygame.time.Clock()
    while True:
        for e in pygame.event.get():
            if e.type == pygame.QUIT or \
                    (e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE):
                reader.stop()
                pygame.quit()
                return
        try:
            while True:
                mapper.handle(events.get_nowait())
        except queue.Empty:
            pass
        screen.fill((250, 248, 244))
        lines = [
            "鼠标模式运行中（Esc 或关窗退出）",
            "笔尖 = 左键按下/拖动    笔尾 = 右键",
            f"设备: {serial}",
            "已连接" if reader.connected.is_set() else "连接断开，重试中...",
        ]
        for i, line in enumerate(lines):
            screen.blit(font.render(line, True, (60, 60, 60)), (14, 14 + i * 30))
        pygame.display.flip()
        clock.tick(60)


def main():
    ap = argparse.ArgumentParser(description="汉王 N10 Plus 鼠标模式（无压感）")
    ap.add_argument("--serial", help="adb 设备序列号，默认自动检测")
    ap.add_argument("--full-screen", action="store_true",
                    help="笔区铺满整个屏幕（默认按 4:3 居中映射）")
    ap.add_argument("--rotation", type=int, default=0, choices=[0, 90, 180, 270],
                    help="设备摆放旋转角（默认 0，竖拿用 90 或 270）")
    args = ap.parse_args()
    start_mousepad(args.serial, args.full_screen, args.rotation)


if __name__ == "__main__":
    main()
