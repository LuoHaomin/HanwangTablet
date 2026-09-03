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


class Whiteboard:
    def __init__(self, width: int, min_pressure: int, pen_size: float):
        self.min_pressure = min_pressure
        self.pen_size = pen_size
        self.window_w = width
        self.window_h = round(width * PEN_MAX_Y / PEN_MAX_X)

        pygame.init()
        pygame.display.set_caption("汉王手写板 — 笔尾翻转即橡皮 | C清空 S保存 E橡皮 1-4颜色")
        self.screen = pygame.display.set_mode(
            (self.window_w, self.window_h), pygame.RESIZABLE
        )
        self.canvas = pygame.Surface((self.window_w, self.window_h))
        self.canvas.fill((250, 248, 244))  # 纸感底色
        self.font = pygame.font.SysFont(
            "pingfangsc,hiraginosansgb,arialunicodems,stheitisc", 16
        )

        self.colors = [(30, 30, 34), (200, 40, 40), (30, 90, 200), (20, 120, 60)]
        self.color_idx = 0
        self.erasing = False
        self.last_pt = None       # (win_x, win_y, width)
        self.pen_down = False
        self.status = "启动中..."
        self.show_pressure = 0

    def map_point(self, ev: PenEvent) -> tuple[float, float]:
        w, h = self.canvas.get_size()
        return ev.x / PEN_MAX_X * w, ev.y / PEN_MAX_Y * h

    def stroke_width(self, pressure: int) -> float:
        p = max(pressure - self.min_pressure, 0) / PEN_MAX_PRESSURE
        return self.pen_size * (0.25 + 1.75 * p)

    def draw_to(self, ev: PenEvent):
        wx, wy = self.map_point(ev)
        width = self.stroke_width(ev.pressure)
        erasing = self.erasing or ev.rubber
        color = (250, 248, 244) if erasing else self.colors[self.color_idx]
        if erasing:
            width = 40
        if self.last_pt is None:
            pygame.draw.circle(self.canvas, color, (wx, wy), width / 2)
        else:
            lx, ly, lw = self.last_pt
            # 相邻两点宽度不同时用圆补两端，避免锯齿断裂
            pygame.draw.line(self.canvas, color, (lx, ly), (wx, wy), max(round(width), 1))
            pygame.draw.circle(self.canvas, color, (lx, ly), lw / 2)
            pygame.draw.circle(self.canvas, color, (wx, wy), width / 2)
        self.last_pt = (wx, wy, width)

    def save(self):
        path = f"note-{time.strftime('%Y%m%d-%H%M%S')}.png"
        pygame.image.save(self.canvas, path)
        self.status = f"已保存 {path}"

    def run(self, events: queue.Queue[PenEvent], reader: PenReader):
        clock = pygame.time.Clock()
        while True:
            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    reader.stop()
                    pygame.quit()
                    return
                if e.type == pygame.VIDEORESIZE:
                    old = pygame.transform.scale(self.canvas, (e.w, e.h))
                    self.canvas = old
                if e.type == pygame.KEYDOWN:
                    if e.key == pygame.K_c:
                        self.canvas.fill((250, 248, 244))
                        self.status = "已清空"
                    elif e.key == pygame.K_s:
                        self.save()
                    elif e.key == pygame.K_e:
                        self.erasing = not self.erasing
                        self.status = "橡皮擦" if self.erasing else "画笔"
                    elif pygame.K_1 <= e.key <= pygame.K_4:
                        self.color_idx = e.key - pygame.K_1
                        self.status = f"颜色 {self.color_idx + 1}"

            got_any = False
            try:
                while True:
                    ev = events.get_nowait()
                    got_any = True
                    self.show_pressure = ev.pressure
                    if ev.down:
                        self.pen_down = True
                        self.draw_to(ev)
                    else:
                        self.pen_down = False
                        self.last_pt = None
            except queue.Empty:
                pass

            if not reader.connected.is_set():
                self.status = f"连接断开，重试中... {reader.last_error or ''}"

            self.screen.blit(self.canvas, (0, 0))
            hud = f"[{'●' if self.pen_down else '○'}] 压感 {self.show_pressure:4d} | {self.status}"
            self.screen.blit(self.font.render(hud, True, (120, 120, 120)), (10, 8))
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
