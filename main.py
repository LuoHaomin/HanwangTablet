#!/usr/bin/env python3
"""汉王 N10 Plus 手写板（白板模式）。

通过 adb 读取设备的 pen_touch 输入节点，实时绘制到本地窗口。
坐标系 0-1872 x 0-1404，压感 0-1024。
"""

import argparse
import queue
import subprocess
import sys
import threading
import time

import pygame

PEN_MAX_X = 1872
PEN_MAX_Y = 1404
PEN_MAX_PRESSURE = 1024

INPUT_DEVICE = "/dev/input/event7"

BG_COLOR = (250, 248, 244)
TOOLBAR_H = 52
BASE_WINDOW_H = 842  # width=1123 时的窗口高度，笔宽缩放以此为基准


def find_device_serial() -> str | None:
    out = subprocess.run(
        ["adb", "devices"], capture_output=True, text=True, timeout=5
    ).stdout
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) == 2 and parts[1] == "device":
            # 优先选 IP 连接（当前唯一稳定通道）
            if "." in parts[0]:
                return parts[0]
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) == 2 and parts[1] == "device":
            return parts[0]
    return None


class PenEvent:
    __slots__ = ("x", "y", "pressure", "down", "rubber")

    def __init__(self, x: int, y: int, pressure: int, down: bool, rubber: bool):
        self.x = x
        self.y = y
        self.pressure = pressure
        self.down = down
        self.rubber = rubber


class PenReader(threading.Thread):
    """后台线程：保持 adb getevent 流存活，解析事件，断线自动重连。"""

    def __init__(self, serial: str, out: queue.Queue[PenEvent]):
        super().__init__(daemon=True)
        self.serial = serial
        self.out = out
        self.stop_flag = threading.Event()
        self.connected = threading.Event()
        self.last_error: str | None = None

    def run(self):
        while not self.stop_flag.is_set():
            proc = None
            try:
                proc = subprocess.Popen(
                    ["adb", "-s", self.serial, "shell",
                     "getevent", "-lt", INPUT_DEVICE],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                self.connected.set()
                self.last_error = None
                self._parse_stream(proc)
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
            finally:
                self.connected.clear()
                if proc is not None:
                    proc.terminate()
            if not self.stop_flag.is_set():
                time.sleep(1.0)  # 断线后 1s 重试

    def _parse_stream(self, proc: subprocess.Popen):
        x = y = pressure = 0
        down = False
        rubber = False
        assert proc.stdout is not None
        for line in proc.stdout:
            if self.stop_flag.is_set():
                return
            # 两种格式（设备路径列有无取决于是否指定单个设备）:
            #   [ 1265.155563] /dev/input/event7: EV_ABS  ABS_X  0000015b
            #   [ 1265.155563] EV_ABS  ABS_X  0000015b
            parts = line.split()
            ev_type = code = value = ""
            for i, tok in enumerate(parts):
                if tok.startswith("EV_"):
                    ev_type = tok
                    code = parts[i + 1] if i + 1 < len(parts) else ""
                    value = parts[i + 2] if i + 2 < len(parts) else ""
                    break
            if not ev_type:
                continue
            if ev_type == "EV_ABS":
                if code == "ABS_X":
                    x = int(value, 16)
                elif code == "ABS_Y":
                    y = int(value, 16)
                elif code == "ABS_PRESSURE":
                    pressure = int(value, 16)
            elif ev_type == "EV_KEY":
                if code == "BTN_TOUCH":
                    down = value == "DOWN"
                elif code == "BTN_TOOL_RUBBER":
                    rubber = value == "DOWN"
            elif ev_type == "EV_SYN" and code == "SYN_REPORT":
                self.out.put(PenEvent(x, y, pressure, down, rubber))

    def stop(self):
        self.stop_flag.set()


class Stroke:
    """一笔。点存设备坐标（0-1872/0-1404），窗口缩放后重绘不失真。"""

    __slots__ = ("erasing", "color_idx", "pen_size", "points")

    def __init__(self, erasing: bool, color_idx: int, pen_size: float):
        self.erasing = erasing
        self.color_idx = color_idx
        self.pen_size = pen_size
        self.points: list[tuple[int, int, int]] = []  # (dev_x, dev_y, pressure)

    def add(self, x, y, pressure):
        self.points.append((x, y, pressure))


class Whiteboard:
    COLORS = [(30, 30, 34), (200, 40, 40), (30, 90, 200), (20, 120, 60)]
    SIZES = [3.0, 6.0, 10.0, 16.0]

    def __init__(self, width: int, min_pressure: int, pen_size: float):
        self.min_pressure = min_pressure
        self.window_w = width
        self.window_h = round(width * PEN_MAX_Y / PEN_MAX_X)

        pygame.init()
        pygame.display.set_caption("汉王手写板 — 笔尾即橡皮 | Z撤销 C清空 S保存")
        self.screen = pygame.display.set_mode(
            (self.window_w, self.window_h), pygame.RESIZABLE
        )
        self.font = pygame.font.SysFont(
            "pingfangsc,hiraginosansgb,arialunicodems,stheitisc", 16
        )
        self.small_font = pygame.font.SysFont(
            "pingfangsc,hiraginosansgb", 13
        )

        self.strokes: list[Stroke] = []
        self.current: Stroke | None = None
        self.color_idx = 0
        self.pen_size = pen_size if pen_size in self.SIZES else self.SIZES[1]
        self.erasing = False
        self.status = "就绪"
        self.show_pressure = 0
        self.pen_down = False
        self.buttons: list[tuple[pygame.Rect, callable, str]] = []

        self._resize_canvas(self.window_w, self.window_h)

    # ---- 坐标与绘制 ----

    def _resize_canvas(self, w, h):
        self.canvas_w, self.canvas_h = w, h - TOOLBAR_H
        self.canvas = pygame.Surface((self.canvas_w, self.canvas_h))
        self.redraw_all()

    def map_point(self, dx: int, dy: int) -> tuple[float, float]:
        return (dx / PEN_MAX_X * self.canvas_w,
                dy / PEN_MAX_Y * self.canvas_h)

    def _seg(self, pt_a, pt_b, stroke):
        """把一条线段画到 canvas。pt 为 (dev_x, dev_y, pressure)。"""
        scale = self.canvas_h / BASE_WINDOW_H
        if stroke.erasing:
            color = BG_COLOR
            width = 40 * scale
        else:
            color = self.COLORS[stroke.color_idx]
            pa = max(pt_a[2] - self.min_pressure, 0) / PEN_MAX_PRESSURE
            pb = max(pt_b[2] - self.min_pressure, 0) / PEN_MAX_PRESSURE
            w_a = stroke.pen_size * scale * (0.25 + 1.75 * pa)
            w_b = stroke.pen_size * scale * (0.25 + 1.75 * pb)
            width = max((w_a + w_b) / 2, 1)
        ax, ay = self.map_point(*pt_a[:2])
        bx, by = self.map_point(*pt_b[:2])
        if stroke.erasing:
            pygame.draw.circle(self.canvas, color, (bx, by), width / 2)
        else:
            pygame.draw.line(self.canvas, color, (ax, ay), (bx, by),
                             max(round(width), 1))
            # 宽度渐变时用圆补两端，避免锯齿断裂
            wa = stroke.pen_size * scale * (0.25 + 1.75 * pa)
            pygame.draw.circle(self.canvas, color, (ax, ay), wa / 2)
            pygame.draw.circle(self.canvas, color, (bx, by), width / 2)

    def redraw_all(self):
        self.canvas.fill(BG_COLOR)
        for s in self.strokes:
            for i in range(1, len(s.points)):
                self._seg(s.points[i - 1], s.points[i], s)
            if s.points:
                self._seg(s.points[0], s.points[0], s)

    # ---- 笔事件 ----

    def handle_pen(self, ev: PenEvent):
        self.show_pressure = ev.pressure
        if ev.down:
            self.pen_down = True
            erasing = self.erasing or ev.rubber
            if self.current is None:
                self.current = Stroke(erasing, self.color_idx, self.pen_size)
            self.current.add(ev.x, ev.y, ev.pressure)
            n = len(self.current.points)
            if n >= 2:
                self._seg(self.current.points[n - 2], self.current.points[n - 1],
                          self.current)
            else:
                self._seg(self.current.points[0], self.current.points[0],
                          self.current)
        else:
            self.pen_down = False
            if self.current is not None:
                if len(self.current.points) >= 1:
                    self.strokes.append(self.current)
                self.current = None

    def undo(self):
        if self.strokes:
            self.strokes.pop()
            self.redraw_all()
            self.status = "已撤销"
        else:
            self.status = "没有可撤销的笔画"

    def save(self):
        path = f"note-{time.strftime('%Y%m%d-%H%M%S')}.png"
        pygame.image.save(self.canvas, path)
        self.status = f"已保存 {path}"

    # ---- 工具栏 ----

    def _make_buttons(self):
        self.buttons = []
        h = self.window_h
        x = 12
        y = h - TOOLBAR_H / 2
        for i, c in enumerate(self.COLORS):
            r = pygame.Rect(x, y - 14, 28, 28)
            self.buttons.append((r, lambda i=i: self._pick_color(i), "color"))
            x += 38
        x += 12
        for s in self.SIZES:
            r = pygame.Rect(x, y - 16, 32, 32)
            self.buttons.append((r, lambda s=s: self._pick_size(s), "size"))
            x += 42
        x += 12
        for label, fn in (("撤销", self.undo),
                          ("清空", lambda: self._clear()),
                          ("保存", self.save)):
            r = pygame.Rect(x, y - 16, 52, 32)
            self.buttons.append((r, fn, "text:" + label))
            x += 60
        eraser_label = "画笔" if self.erasing else "橡皮"
        r = pygame.Rect(x, y - 16, 52, 32)
        self.buttons.append((r, self._toggle_eraser, "text:" + eraser_label))
        return x

    def _pick_color(self, i):
        self.color_idx = i
        self.erasing = False
        self.status = f"颜色 {i + 1}"

    def _pick_size(self, s):
        self.pen_size = s
        self.status = f"笔宽 {s:.0f}"

    def _toggle_eraser(self):
        self.erasing = not self.erasing
        self.status = "橡皮擦" if self.erasing else "画笔"

    def _clear(self):
        self.strokes.clear()
        self.redraw_all()
        self.status = "已清空"

    def draw_toolbar(self):
        bar = pygame.Rect(0, self.window_h - TOOLBAR_H, self.window_w, TOOLBAR_H)
        pygame.draw.rect(self.screen, (240, 238, 233), bar)
        pygame.draw.line(self.screen, (210, 208, 203), bar.topleft, bar.topright, 1)
        end_x = self._make_buttons()
        for rect, fn, kind in self.buttons:
            cy = rect.centery
            if kind == "color":
                idx = [b[0].x for b in self.buttons[:4]].index(rect.x)
                pygame.draw.circle(self.screen, self.COLORS[idx], rect.center, 12)
                if idx == self.color_idx and not self.erasing:
                    pygame.draw.circle(self.screen, (80, 80, 80), rect.center, 15, 2)
            elif kind == "size":
                idx = [b[0].x for b in self.buttons[4:8]].index(rect.x)
                size = self.SIZES[idx]
                if size == self.pen_size:
                    pygame.draw.rect(self.screen, (180, 210, 180), rect, border_radius=6)
                scale = self.canvas_h / BASE_WINDOW_H
                pygame.draw.circle(self.screen, (60, 60, 60), rect.center,
                                   max(size * scale / 2, 1.5))
            elif kind.startswith("text:"):
                label = kind[5:]
                bg = (210, 235, 210) if label in ("撤销", "清空", "保存") else (235, 225, 210)
                pygame.draw.rect(self.screen, bg, rect, border_radius=6)
                t = self.small_font.render(label, True, (50, 50, 50))
                self.screen.blit(t, t.get_rect(center=rect.center))
        _ = end_x

    def handle_click(self, pos):
        for rect, fn, kind in self.buttons:
            if rect.collidepoint(pos):
                fn()
                return

    # ---- 主循环 ----

    def run(self, events: queue.Queue[PenEvent], reader: PenReader):
        clock = pygame.time.Clock()
        while True:
            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    reader.stop()
                    pygame.quit()
                    return
                if e.type == pygame.VIDEORESIZE:
                    self.window_w, self.window_h = e.w, e.h
                    self._resize_canvas(e.w, e.h)
                if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                    if e.pos[1] >= self.window_h - TOOLBAR_H:
                        self.handle_click(e.pos)
                if e.type == pygame.KEYDOWN:
                    if e.key == pygame.K_z:
                        self.undo()
                    elif e.key == pygame.K_c:
                        self._clear()
                    elif e.key == pygame.K_s:
                        self.save()
                    elif e.key == pygame.K_e:
                        self._toggle_eraser()
                    elif pygame.K_1 <= e.key <= pygame.K_4:
                        self._pick_color(e.key - pygame.K_1)

            try:
                while True:
                    self.handle_pen(events.get_nowait())
            except queue.Empty:
                pass

            if not reader.connected.is_set():
                self.status = f"连接断开，重试中... {reader.last_error or ''}"

            self.screen.blit(self.canvas, (0, 0))
            hud = (f"[{'●' if self.pen_down else '○'}] 压感 {self.show_pressure:4d} | "
                   f"{self.status}")
            self.screen.blit(self.font.render(hud, True, (120, 120, 120)), (10, 8))
            self.draw_toolbar()
            pygame.display.flip()
            clock.tick(120)


def main():
    ap = argparse.ArgumentParser(description="汉王 N10 Plus 无线手写板")
    ap.add_argument("--serial", help="adb 设备序列号，默认自动检测")
    ap.add_argument("--width", type=int, default=1123, help="窗口宽度（默认 1123）")
    ap.add_argument("--min-pressure", type=int, default=50,
                    help="低于此压感不画线（过滤悬空噪声，默认 50）")
    ap.add_argument("--pen-size", type=float, default=6.0, help="基准笔宽（默认 6）")
    args = ap.parse_args()

    serial = args.serial or find_device_serial()
    if not serial:
        print("未发现 adb 设备，请先: adb connect <设备IP>:<端口>", file=sys.stderr)
        sys.exit(1)
    print(f"使用设备: {serial}")

    events: queue.Queue[PenEvent] = queue.Queue()
    reader = PenReader(serial, events)
    reader.start()

    Whiteboard(args.width, args.min_pressure, args.pen_size).run(events, reader)


if __name__ == "__main__":
    main()
