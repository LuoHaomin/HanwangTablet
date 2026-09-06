#!/usr/bin/env python3
"""汉王 N10 Plus 手写板（白板模式）。

通过 adb 读取设备的 pen_touch 输入节点，实时绘制到本地窗口。
坐标系 0-1872 x 0-1404，压感 0-1024。
"""

import argparse
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

import pygame


def resolve_adb() -> str:
    """Finder 启动的 .app 没有 shell PATH，补上常见安装位置。"""
    adb = shutil.which("adb")
    if adb:
        return adb
    for p in ("/opt/homebrew/bin/adb", "/usr/local/bin/adb",
              os.path.expanduser("~/homebrew/bin/adb")):
        if os.path.exists(p):
            return p
    raise FileNotFoundError("找不到 adb，请安装: brew install android-platform-tools")

PEN_MAX_X = 1872
PEN_MAX_Y = 1404
PEN_MAX_PRESSURE = 1024

INPUT_DEVICE = "/dev/input/event7"

BG_COLOR = (250, 248, 244)
TOOLBAR_H = 52
BASE_WINDOW_H = 842  # width=1123 时的窗口高度，笔宽缩放以此为基准

# 圈选擦除的判定阈值：闭合缺口须小于圈直径的此比例，且点数足够
LASSO_MIN_POINTS = 15
LASSO_MAX_GAP_RATIO = 0.30


def _adb(*args, timeout=5) -> str:
    return subprocess.run([resolve_adb(), *args], capture_output=True,
                          text=True, timeout=timeout).stdout


def find_device_serial() -> str | None:
    def scan() -> str | None:
        out = _adb("devices")
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) == 2 and parts[1] == "device":
                if "." in parts[0]:  # 优先 IP 连接（当前唯一稳定通道）
                    return parts[0]
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) == 2 and parts[1] == "device":
                return parts[0]
        return None

    dev = scan()
    if dev:
        return dev

    # 没有已连接设备时，用 mDNS 自动发现无线调试端口并连接
    # （设备每次重启/休眠唤醒后端口会变，手动抄不可靠）
    try:
        for line in _adb("mdns", "services").splitlines():
            parts = line.split()
            # 格式: adb-XXXX _adb-tls-connect._tcp. 10.x.x.x:port
            if len(parts) >= 3 and "_adb-tls-connect._tcp" in parts[1]:
                _adb("connect", parts[2], timeout=3)
    except Exception:  # noqa: BLE001
        pass
    return scan()


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
                    [resolve_adb(), "-s", self.serial, "shell",
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
                # 设备休眠唤醒/重启后无线调试端口会变，重新发现
                try:
                    found = find_device_serial()
                    if found:
                        self.serial = found
                except Exception:  # noqa: BLE001
                    pass
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


class PressureCurve:
    """PCHIP 单调三次插值压感曲线（控制点 + Fritsch-Carlson 切线）。

    保证单调不过冲：输入输出都在 0-1，适合压感映射。
    """

    def __init__(self, control_points: list[tuple[float, float]]):
        self.xs = [p[0] for p in control_points]
        self.ys = [p[1] for p in control_points]
        self.ms = self._tangents()

    def _tangents(self):
        xs, ys = self.xs, self.ys
        n = len(xs)
        d = [(ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]) for i in range(n - 1)]
        m = [0.0] * n
        m[0] = d[0]
        m[-1] = d[-1]
        for i in range(1, n - 1):
            if d[i - 1] * d[i] <= 0:
                m[i] = 0.0
            else:
                m[i] = 2.0 / (1.0 / d[i - 1] + 1.0 / d[i])  # 调和平均
        return m

    def __call__(self, p: float) -> float:
        p = min(max(p, 0.0), 1.0)
        xs, ys, m = self.xs, self.ys, self.ms
        i = 0
        while i < len(xs) - 2 and p > xs[i + 1]:
            i += 1
        h = xs[i + 1] - xs[i]
        t = (p - xs[i]) / h
        t2, t3 = t * t, t * t * t
        return (2 * t3 - 3 * t2 + 1) * ys[i] + (t3 - 2 * t2 + t) * h * m[i] \
            + (-2 * t3 + 3 * t2) * ys[i + 1] + (t3 - t2) * h * m[i + 1]


# 压感曲线预设：轻压输出高 = 柔软，重压才出粗笔 = 硬笔
CURVES = {
    "柔软": PressureCurve([(0, 0.12), (0.25, 0.5), (0.55, 0.8), (1, 1)]),
    "适中": PressureCurve([(0, 0.0), (0.5, 0.5), (1, 1)]),
    "硬笔": PressureCurve([(0, 0.0), (0.45, 0.18), (0.8, 0.5), (1, 1)]),
}

# 平滑级别: 显示名 -> 贝塞尔稳定器强度（0 关闭，越大越稳）
SMOOTH_LEVELS = [("关", 0.0), ("轻", 0.35), ("中", 0.6), ("强", 0.8)]

# 采样管线参数（移植自 Lorien）
DEAD_ZONE = 2.0          # 距上一接受点 <= 此距离（设备单位）的点丢弃
OPT_MIN_DIST = 4.0       # 优化器: 距离阈值
OPT_ANGLE_DEG = 0.5      # 优化器: 方向变化阈值
PRESSURE_MAX_DIFF = 0.05 # 压感限速: 相邻点压感最大变化
PRESSURE_MIN = 0.1       # 压感下限
VELOCITY_TAPER = 60.0    # 速度变细: 每事件移动 60 单位压感扣满 0.33
DOT_MAX_LEN = 8.0        # 轻点判定: 总路程 <= 此值为点而非线


def cubic_bezier(p0, p1, p2, p3, t):
    """三次贝塞尔（Lorien 稳定器核心）。"""
    u = 1.0 - t
    b0 = u * u * u
    b1 = 3 * u * u * t
    b2 = 3 * u * t * t
    b3 = t * t * t
    return (b0 * p0[0] + b1 * p1[0] + b2 * p2[0] + b3 * p3[0],
            b0 * p0[1] + b1 * p1[1] + b2 * p2[1] + b3 * p3[1])


def point_in_polygon(px, py, poly) -> bool:
    """射线法判断点是否在多边形内。"""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > py) != (yj > py):
            x_cross = xi + (py - yi) * (xj - xi) / (yj - yi)
            if px < x_cross:
                inside = not inside
        j = i
    return inside


class Stroke:
    """一笔。点存设备坐标（0-1872/0-1404），窗口缩放后重绘不失真。"""

    __slots__ = ("color_idx", "pen_size", "min_mult", "max_mult",
                 "curve", "points", "erasing_trace")

    def __init__(self, color_idx: int, pen_size: float, min_mult: float,
                 max_mult: float, curve: PressureCurve):
        self.color_idx = color_idx
        self.pen_size = pen_size
        self.min_mult = min_mult
        self.max_mult = max_mult
        self.curve = curve
        self.erasing_trace = False
        self.points: list[tuple[int, int, int]] = []  # (dev_x, dev_y, pressure)

    def add(self, x, y, pressure):
        self.points.append((x, y, pressure))


class Whiteboard:
    COLORS = [(30, 30, 34), (200, 40, 40), (30, 90, 200), (20, 120, 60)]
    SIZES = [3.0, 6.0, 10.0, 16.0]

    def __init__(self, width: int, min_pressure: int, pen_size: float,
                 rotation: int = 0, min_mult: float = 0.2,
                 max_mult: float = 2.0, curve_name: str = "适中",
                 smooth_idx: int = 1):
        self.min_pressure = min_pressure
        self.rotation = rotation
        self.min_mult = min_mult
        self.max_mult = max_mult
        self.curve_name = curve_name
        # 自定义曲线的控制点（0-1 归一化），可用曲线编辑器拖拽
        self.custom_points = [(0.0, 0.0), (0.35, 0.35), (0.65, 0.65), (1.0, 1.0)]
        self.custom_curve = PressureCurve(self.custom_points)
        self.show_curve_editor = False
        self.drag_cp: int | None = None
        self.smooth_idx = smooth_idx
        # 采样管线状态（Lorien 式）：stab 保留最近 3 个接受点
        self.stab: list[tuple[float, float]] = []
        self.last_press = 0.5
        self.prev_angle: float | None = None
        self.window_w = width
        self.window_h = round(width * PEN_MAX_Y / PEN_MAX_X)
        if rotation in (90, 270):  # 竖拿时窗口用竖向比例
            self.window_w, self.window_h = self.window_h, self.window_w

        pygame.init()
        pygame.display.set_caption("汉王手写板 — 笔尾即橡皮/画圈圈选删除 | Z撤销 C清空 S保存")
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
        # 操作命令栈：每个操作记录 (操作前笔画表, 操作后笔画表)，对象共享，代价低
        self.history: list[tuple[list, list]] = []
        self.h_idx = 0
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

    def rotate(self, dx: int, dy: int) -> tuple[int, int]:
        """把设备坐标旋转到窗口朝向（0/90/180/270）。"""
        if self.rotation == 90:
            return PEN_MAX_Y - dy, dx
        if self.rotation == 180:
            return PEN_MAX_X - dx, PEN_MAX_Y - dy
        if self.rotation == 270:
            return dy, PEN_MAX_X - dx
        return dx, dy

    def map_point(self, dx: int, dy: int) -> tuple[float, float]:
        rx, ry = self.rotate(dx, dy)
        # 旋转 90/270 后坐标轴对调，分母也要跟着换，否则映射超界
        if self.rotation in (90, 270):
            max_x, max_y = PEN_MAX_Y, PEN_MAX_X
        else:
            max_x, max_y = PEN_MAX_X, PEN_MAX_Y
        return (rx / max_x * self.canvas_w,
                ry / max_y * self.canvas_h)

    def _pressure_width(self, stroke, pressure) -> float:
        """压感 → 宽度倍率：压力已归一化(0-1)，过 PCHIP 曲线后映射到 [min_mult, max_mult]。"""
        f = stroke.curve(min(max(pressure, 0.0), 1.0))
        return stroke.min_mult + (stroke.max_mult - stroke.min_mult) * f

    def _seg(self, pt_a, pt_b, stroke):
        """变宽笔迹渲染：四边形条带（Lorien 的 Line2D 条带思路）+ 关节圆。"""
        scale = self.canvas_h / BASE_WINDOW_H
        color = self.COLORS[stroke.color_idx]
        base = stroke.pen_size * scale
        wa = max(base * self._pressure_width(stroke, pt_a[2]), 1)
        wb = max(base * self._pressure_width(stroke, pt_b[2]), 1)
        ax, ay = self.map_point(pt_a[0], pt_a[1])
        bx, by = self.map_point(pt_b[0], pt_b[1])
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length < 1e-6:
            pygame.draw.circle(self.canvas, color, (ax, ay), wa / 2)
            return
        nx, ny = -dy / length, dx / length  # 法向
        quad = [(ax + nx * wa / 2, ay + ny * wa / 2),
                (bx + nx * wb / 2, by + ny * wb / 2),
                (bx - nx * wb / 2, by - ny * wb / 2),
                (ax - nx * wa / 2, ay - ny * wa / 2)]
        pygame.draw.polygon(self.canvas, color, quad)
        # 关节圆填补相邻四边形间的缝隙
        pygame.draw.circle(self.canvas, color, (ax, ay), wa / 2)
        pygame.draw.circle(self.canvas, color, (bx, by), wb / 2)

    def redraw_all(self):
        self.canvas.fill(BG_COLOR)
        for s in self.strokes:
            for i in range(1, len(s.points)):
                self._seg(s.points[i - 1], s.points[i], s)
            if s.points:
                self._seg(s.points[0], s.points[0], s)

    def _erase_dot(self, pt):
        """橡皮拖动时的实时视觉反馈（画底色圆）。"""
        scale = self.canvas_h / BASE_WINDOW_H
        x, y = self.map_point(pt[0], pt[1])
        pygame.draw.circle(self.canvas, BG_COLOR, (x, y), 20 * scale)

    # ---- 笔事件 ----

    def _pen_sample(self, ev: PenEvent):
        """Lorien 式采样管线：死区 → 贝塞尔稳定器 → 速度变细 →
        压感限速 → 点优化器。返回接受后的 (x, y, p_norm) 或 None。"""
        x, y = float(ev.x), float(ev.y)
        p = ev.pressure / PEN_MAX_PRESSURE
        if not self.stab:  # 每笔第一个点直接透传，无启动延迟
            self.stab = [(x, y)]
            self.last_press = max(p, PRESSURE_MIN)
            self.prev_angle = None
            return x, y, max(p, PRESSURE_MIN)

        lx, ly = self.stab[-1]
        dist = ((x - lx) ** 2 + (y - ly) ** 2) ** 0.5
        if dist <= DEAD_ZONE:
            return None

        # 贝塞尔稳定器：把新点拉向已有折线，快线不抖
        strength = SMOOTH_LEVELS[self.smooth_idx][1]
        if strength >= 0.01 and len(self.stab) >= 3:
            t = 0.5 + (1.0 - strength) * 0.5
            x, y = cubic_bezier(self.stab[-3], self.stab[-2],
                                self.stab[-1], (x, y), t)
            dist = ((x - lx) ** 2 + (y - ly) ** 2) ** 0.5

        # 速度变细：运笔越快墨越淡（模拟真实笔锋）
        p -= min(dist / VELOCITY_TAPER, 0.33)

        # 压感限速：相邻点压感最多变化 0.05，消灭跳变
        dp = p - self.last_press
        if abs(dp) > PRESSURE_MAX_DIFF:
            p = self.last_press + (PRESSURE_MAX_DIFF if dp > 0
                                   else -PRESSURE_MAX_DIFF)
        p = min(max(p, PRESSURE_MIN), 1.0)

        # 点优化器：近距离且方向几乎不变的点不存
        angle = math.atan2(y - ly, x - lx)
        if dist < OPT_MIN_DIST and self.prev_angle is not None:
            da = abs(angle - self.prev_angle)
            da = min(da, 2 * math.pi - da)
            if math.degrees(da) < OPT_ANGLE_DEG:
                return None
        self.prev_angle = angle

        self.stab.append((x, y))
        self.last_press = p
        return x, y, p

    def handle_pen(self, ev: PenEvent):
        self.show_pressure = ev.pressure
        if ev.down:
            self.pen_down = True
            sampled = self._pen_sample(ev)
            if sampled is None:
                return
            x, y, p = sampled
            erasing = self.erasing or ev.rubber
            if self.current is None:
                # 橡皮轨迹不作为笔画存储，只用于切割
                self.current = Stroke(0 if erasing else self.color_idx,
                                      self.pen_size, self.min_mult,
                                      self.max_mult, self._current_curve())
                self.current.erasing_trace = erasing
            self.current.add(x, y, p)
            if erasing:
                self._erase_dot((x, y, p))
            else:
                n = len(self.current.points)
                if n >= 2:
                    self._seg(self.current.points[n - 2],
                              self.current.points[n - 1], self.current)
                else:
                    self._seg(self.current.points[0],
                              self.current.points[0], self.current)
        else:
            self.pen_down = False
            self.stab = []
            self.prev_angle = None
            if self.current is not None:
                if len(self.current.points) >= 1:
                    self._finish_stroke(self.current)
                self.current = None

    def _finish_stroke(self, stroke: Stroke):
        if getattr(stroke, "erasing_trace", False):
            before = self._begin_op()
            if self._is_closed_lasso(stroke):
                removed = self._lasso_erase(stroke)
                self.status = f"圈选擦除 {removed} 笔"
            else:
                split, gone = self._cut_erase(stroke)
                self.status = f"擦除：切割 {split} 笔，删 {gone} 笔"
            self.redraw_all()
            self._end_op(before)
            return
        # 轻点判定：总路程极短的一笔渲染为一个圆点（Lorien 的 dot 处理）
        pts = stroke.points
        if len(pts) <= 6:
            total = sum(math.hypot(pts[i][0] - pts[i - 1][0],
                                   pts[i][1] - pts[i - 1][1])
                        for i in range(1, len(pts)))
            if total <= DOT_MAX_LEN:
                cx = sum(pt[0] for pt in pts) / len(pts)
                cy = sum(pt[1] for pt in pts) / len(pts)
                stroke.points = [(cx - 1.5, cy, 0.5), (cx, cy, 0.5),
                                 (cx + 1.5, cy, 0.5)]
        before = self._begin_op()
        self.strokes.append(stroke)
        self._end_op(before)

    def _is_closed_lasso(self, stroke: Stroke) -> bool:
        pts = stroke.points
        if len(pts) < LASSO_MIN_POINTS:
            return False
        (x0, y0, _), (x1, y1, _) = pts[0], pts[-1]
        gap = ((x0 - x1) ** 2 + (y0 - y1) ** 2) ** 0.5
        # 手绘圈的缺口阈值随圈大小自适应：小圈要求闭合得更紧
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        diameter = max(max(xs) - min(xs), max(ys) - min(ys))
        if diameter < 50:  # 太小算不上"圈"，按普通擦除处理
            return False
        return gap < LASSO_MAX_GAP_RATIO * diameter

    def _lasso_erase(self, lasso: Stroke) -> int:
        poly = [(p[0], p[1]) for p in lasso.points]
        kept, removed = [], 0
        for s in self.strokes:
            hits = sum(point_in_polygon(p[0], p[1], poly) for p in s.points)
            if len(s.points) and hits / len(s.points) > 0.5:
                removed += 1
            else:
                kept.append(s)
        self.strokes = kept
        return removed

    def _erase_radius_screen(self) -> float:
        return 20 * self.canvas_h / BASE_WINDOW_H

    def _cut_erase(self, eraser: Stroke):
        """真正的对象切割：删除橡皮路径半径内的点，笔画在擦除处断开成多段。"""
        r = self._erase_radius_screen()
        r2 = r * r
        ex_pts = [self.map_point(p[0], p[1]) for p in eraser.points]
        # 橡皮轨迹的包围盒（屏幕坐标），粗筛加速
        exs = [p[0] for p in ex_pts]
        eys = [p[1] for p in ex_pts]
        bbox = (min(exs) - r, min(eys) - r, max(exs) + r, max(eys) + r)

        def erased(pt) -> bool:
            x, y = self.map_point(pt[0], pt[1])
            if not (bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]):
                return False
            for ex, ey in ex_pts:
                if (x - ex) ** 2 + (y - ey) ** 2 <= r2:
                    return True
            return False

        new_strokes, split, gone = [], 0, 0
        for s in self.strokes:
            runs = []
            run = []
            for pt in s.points:
                if erased(pt):
                    if run:
                        runs.append(run)
                        run = []
                else:
                    run.append(pt)
            if run:
                runs.append(run)
            if len(runs) > 1:
                split += 1
            if not runs:
                gone += 1
            for run_pts in runs:
                ns = Stroke(s.color_idx, s.pen_size, s.min_mult, s.max_mult,
                            s.curve)
                ns.points = run_pts
                new_strokes.append(ns)
        self.strokes = new_strokes
        return split, gone

    def _begin_op(self) -> list:
        return list(self.strokes)

    def _end_op(self, before: list):
        self.history = self.history[:self.h_idx]  # 丢弃被撤销分支
        self.history.append((before, list(self.strokes)))
        self.h_idx = len(self.history)

    def undo(self):
        if self.h_idx > 0:
            self.h_idx -= 1
            self.strokes = list(self.history[self.h_idx][0])
            self.redraw_all()
            self.status = "已撤销"
        else:
            self.status = "没有可撤销的操作"

    def redo(self):
        if self.h_idx < len(self.history):
            self.strokes = list(self.history[self.h_idx][1])
            self.h_idx += 1
            self.redraw_all()
            self.status = "已重做"
        else:
            self.status = "没有可重做的操作"

    def save(self):
        desktop = os.path.expanduser("~/Desktop")
        path = os.path.join(desktop, f"note-{time.strftime('%Y%m%d-%H%M%S')}.png")
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
                          ("保存", self.save),
                          ("旋转", self._cycle_rotation),
                          ("曲线", self._cycle_curve),
                          ("曲线编辑", self._toggle_curve_editor),
                          (f"平滑:{SMOOTH_LEVELS[self.smooth_idx][0]}",
                           self._cycle_smooth)):
            r = pygame.Rect(x, y - 16, 58, 32)
            self.buttons.append((r, fn, "text:" + label))
            x += 64
        eraser_label = "画笔" if self.erasing else "橡皮"
        r = pygame.Rect(x, y - 16, 52, 32)
        self.buttons.append((r, self._toggle_eraser, "text:" + eraser_label))

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

    def _cycle_rotation(self):
        old = self.rotation
        self.rotation = (self.rotation + 90) % 360
        # 横竖切换时把窗口转成对应纵横比，画布与可视区一致
        if old % 180 != self.rotation % 180:
            self.window_w, self.window_h = self.window_h, self.window_w
            pygame.display.set_mode((self.window_w, self.window_h),
                                    pygame.RESIZABLE)
        self._resize_canvas(self.window_w, self.window_h)
        self.status = f"旋转 {self.rotation}°"

    def _current_curve(self) -> PressureCurve:
        if self.curve_name == "自定义":
            return self.custom_curve
        return CURVES[self.curve_name]

    def _cycle_curve(self):
        names = list(CURVES) + ["自定义"]
        self.curve_name = names[(names.index(self.curve_name) + 1) % len(names)]
        if self.curve_name == "自定义":
            self.show_curve_editor = True
        self.status = f"曲线: {self.curve_name}"

    def _toggle_curve_editor(self):
        self.show_curve_editor = not self.show_curve_editor
        if self.show_curve_editor:
            self.curve_name = "自定义"

    def _cycle_smooth(self):
        self.smooth_idx = (self.smooth_idx + 1) % len(SMOOTH_LEVELS)
        self.status = f"平滑: {SMOOTH_LEVELS[self.smooth_idx][0]}"

    def _curve_panel(self) -> pygame.Rect:
        return pygame.Rect(self.window_w - 330, 36, 312, 232)

    def _cp_to_px(self, i) -> tuple[float, float]:
        r = self._curve_panel()
        px, py = self.custom_points[i]
        return r.x + 12 + px * (r.w - 24), r.y + r.h - 30 - py * (r.h - 50)

    def _px_to_cp(self, pos) -> tuple[float, float]:
        r = self._curve_panel()
        x = (pos[0] - r.x - 12) / (r.w - 24)
        y = (r.y + r.h - 30 - pos[1]) / (r.h - 50)
        return min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)

    def _curve_editor_mousedown(self, pos) -> bool:
        r = self._curve_panel()
        if not r.collidepoint(pos):
            return False
        for i in range(len(self.custom_points)):
            cx, cy = self._cp_to_px(i)
            if (pos[0] - cx) ** 2 + (pos[1] - cy) ** 2 < 12 ** 2:
                self.drag_cp = i
                return True
        self.drag_cp = None  # 点到面板空白：结束拖拽
        return True

    def _curve_editor_drag(self, pos):
        if self.drag_cp is None:
            return
        x, y = self._px_to_cp(pos)
        i = self.drag_cp
        n = len(self.custom_points)
        if i == 0:
            x = 0.0
        elif i == n - 1:
            x = 1.0
        else:
            # 保持控制点 x 单调递增，PCHIP 才是函数
            x = min(max(x, self.custom_points[i - 1][0] + 0.03),
                    self.custom_points[i + 1][0] - 0.03)
        self.custom_points[i] = (x, y)
        self.custom_curve = PressureCurve(self.custom_points)

    def draw_curve_editor(self):
        r = self._curve_panel()
        pygame.draw.rect(self.screen, (255, 255, 255), r, border_radius=8)
        pygame.draw.rect(self.screen, (160, 160, 160), r, 2, border_radius=8)
        t = self.small_font.render("自定义压感曲线（拖动控制点，U 关闭）",
                                   True, (90, 90, 90))
        self.screen.blit(t, (r.x + 12, r.y + 8))
        # 网格与曲线
        for gx in range(5):
            x = r.x + 12 + gx * (r.w - 24) / 4
            pygame.draw.line(self.screen, (230, 230, 230), (x, r.y + 34),
                             (x, r.y + r.h - 30))
        pts = []
        for i in range(61):
            p = i / 60
            v = self.custom_curve(p)
            pts.append((r.x + 12 + p * (r.w - 24),
                        r.y + r.h - 30 - v * (r.h - 50)))
        pygame.draw.lines(self.screen, (200, 60, 60), False, pts, 2)
        for i, (cx, cy) in enumerate(self._cp_to_px(i) for i in
                                     range(len(self.custom_points))):
            color = (220, 120, 40) if i == self.drag_cp else (60, 60, 60)
            pygame.draw.circle(self.screen, color, (cx, cy), 6)
            pygame.draw.circle(self.screen, (255, 255, 255), (cx, cy), 3)

    def _clear(self):
        if not self.strokes:
            return
        before = self._begin_op()
        self.strokes.clear()
        self._end_op(before)
        self.redraw_all()
        self.status = "已清空"

    def draw_toolbar(self):
        bar = pygame.Rect(0, self.window_h - TOOLBAR_H, self.window_w, TOOLBAR_H)
        pygame.draw.rect(self.screen, (240, 238, 233), bar)
        pygame.draw.line(self.screen, (210, 208, 203), bar.topleft, bar.topright, 1)
        self._make_buttons()
        color_rects = [b[0] for b in self.buttons[:4]]
        size_rects = [b[0] for b in self.buttons[4:8]]
        for rect, fn, kind in self.buttons:
            if kind == "color":
                idx = color_rects.index(rect)
                pygame.draw.circle(self.screen, self.COLORS[idx], rect.center, 12)
                if idx == self.color_idx and not self.erasing:
                    pygame.draw.circle(self.screen, (80, 80, 80), rect.center, 15, 2)
            elif kind == "size":
                idx = size_rects.index(rect)
                if self.SIZES[idx] == self.pen_size:
                    pygame.draw.rect(self.screen, (180, 210, 180), rect,
                                     border_radius=6)
                scale = self.canvas_h / BASE_WINDOW_H
                pygame.draw.circle(self.screen, (60, 60, 60), rect.center,
                                   max(self.SIZES[idx] * scale / 2, 1.5))
            elif kind.startswith("text:"):
                label = kind[5:]
                bg = (215, 232, 215)
                pygame.draw.rect(self.screen, bg, rect, border_radius=6)
                t = self.small_font.render(label, True, (50, 50, 50))
                self.screen.blit(t, t.get_rect(center=rect.center))

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
                    if self.show_curve_editor and \
                            self._curve_editor_mousedown(e.pos):
                        pass  # 曲线编辑器优先
                    elif e.pos[1] >= self.window_h - TOOLBAR_H:
                        self.handle_click(e.pos)
                if e.type == pygame.MOUSEBUTTONUP and e.button == 1:
                    self.drag_cp = None
                if e.type == pygame.MOUSEMOTION and self.drag_cp is not None:
                    self._curve_editor_drag(e.pos)
                if e.type == pygame.KEYDOWN:
                    if e.key == pygame.K_z and e.mod & pygame.KMOD_SHIFT:
                        self.redo()
                    elif e.key == pygame.K_z:
                        self.undo()
                    elif e.key == pygame.K_u:
                        self.show_curve_editor = not self.show_curve_editor
                    elif e.key == pygame.K_c:
                        self._clear()
                    elif e.key == pygame.K_s:
                        self.save()
                    elif e.key == pygame.K_e:
                        self._toggle_eraser()
                    elif e.key == pygame.K_r:
                        self._cycle_rotation()
                    elif e.key == pygame.K_p:
                        self._cycle_curve()
                    elif e.key == pygame.K_m:
                        self._cycle_smooth()
                    elif e.key == pygame.K_LEFTBRACKET:
                        self.min_mult = max(self.min_mult - 0.05, 0.05)
                        self.status = f"最细 {self.min_mult:.2f}x"
                    elif e.key == pygame.K_RIGHTBRACKET:
                        self.min_mult = min(self.min_mult + 0.05, self.max_mult)
                        self.status = f"最细 {self.min_mult:.2f}x"
                    elif e.key == pygame.K_MINUS:
                        self.max_mult = max(self.max_mult - 0.1, self.min_mult)
                        self.status = f"最粗 {self.max_mult:.2f}x"
                    elif e.key == pygame.K_EQUALS:
                        self.max_mult = min(self.max_mult + 0.1, 5.0)
                        self.status = f"最粗 {self.max_mult:.2f}x"
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
                   f"{self.rotation}° 曲线{self.curve_name} 平滑"
                   f"{SMOOTH_LEVELS[self.smooth_idx][0]} "
                   f"{self.min_mult:.2f}-{self.max_mult:.2f}x | {self.status}")
            self.screen.blit(self.font.render(hud, True, (120, 120, 120)), (10, 8))
            self.draw_toolbar()
            if self.show_curve_editor:
                self.draw_curve_editor()
            pygame.display.flip()
            clock.tick(120)


def no_device_dialog() -> bool:
    """未发现设备时的提示窗。返回 True=重试，False=退出。"""
    pygame.init()
    screen = pygame.display.set_mode((420, 190))
    pygame.display.set_caption("汉王手写板")
    font = pygame.font.SysFont("pingfangsc,hiraginosansgb", 16)
    small = pygame.font.SysFont("pingfangsc,hiraginosansgb", 13)
    retry_btn = pygame.Rect(110, 110, 90, 44)
    quit_btn = pygame.Rect(220, 110, 90, 44)
    clock = pygame.time.Clock()
    while True:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                pygame.quit()
                return False
            if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                if retry_btn.collidepoint(e.pos):
                    pygame.quit()
                    return True
                if quit_btn.collidepoint(e.pos):
                    pygame.quit()
                    return False
        screen.fill((250, 248, 244))
        screen.blit(font.render("未发现汉王设备", True, (200, 60, 60)), (110, 24))
        screen.blit(small.render("请在设备上打开无线调试页面，确保已连接", True,
                                 (120, 120, 120)), (66, 62))
        screen.blit(small.render("同一网络（必要时先 adb connect / kill-server）",
                                 True, (120, 120, 120)), (58, 84))
        for rect, label, bg in ((retry_btn, "重试", (200, 225, 200)),
                                (quit_btn, "退出", (230, 210, 210))):
            pygame.draw.rect(screen, bg, rect, border_radius=8)
            t = font.render(label, True, (40, 40, 40))
            screen.blit(t, t.get_rect(center=rect.center))
        pygame.display.flip()
        clock.tick(30)


def start_whiteboard(serial: str | None = None, width: int = 1123):
    """供 CLI 和 .app 启动器共用的入口。"""
    serial = serial or find_device_serial()
    while serial is None:
        if not no_device_dialog():
            sys.exit(0)
        serial = find_device_serial()
    print(f"使用设备: {serial}")

    events: queue.Queue[PenEvent] = queue.Queue()
    reader = PenReader(serial, events)
    reader.start()

    Whiteboard(width, 50, 6.0).run(events, reader)


def main():
    ap = argparse.ArgumentParser(description="汉王 N10 Plus 无线手写板")
    ap.add_argument("--serial", help="adb 设备序列号，默认自动检测")
    ap.add_argument("--width", type=int, default=1123, help="窗口宽度（默认 1123）")
    ap.add_argument("--min-pressure", type=int, default=50,
                    help="低于此压感不画线（过滤悬空噪声，默认 50）")
    ap.add_argument("--pen-size", type=float, default=6.0, help="基准笔宽（默认 6）")
    ap.add_argument("--rotation", type=int, default=0, choices=[0, 90, 180, 270],
                    help="设备摆放旋转角（默认 0，竖拿用 90 或 270）")
    ap.add_argument("--min-mult", type=float, default=0.2,
                    help="最轻压感时笔宽倍率（默认 0.2）")
    ap.add_argument("--max-mult", type=float, default=2.0,
                    help="最重压感时笔宽倍率（默认 2.0）")
    ap.add_argument("--curve", default="适中", choices=list(CURVES) + ["自定义"],
                    help="压感曲线预设（默认 适中）")
    ap.add_argument("--smooth", default="轻", choices=[n for n, _ in SMOOTH_LEVELS],
                    help="笔迹平滑级别（默认 轻）")
    args = ap.parse_args()

    serial = args.serial or find_device_serial()
    if not serial:
        print("未发现 adb 设备，请先: adb connect <设备IP>:<端口>", file=sys.stderr)
        sys.exit(1)
    print(f"使用设备: {serial}")

    events: queue.Queue[PenEvent] = queue.Queue()
    reader = PenReader(serial, events)
    reader.start()

    smooth_idx = [n for n, _ in SMOOTH_LEVELS].index(args.smooth)
    Whiteboard(args.width, args.min_pressure, args.pen_size,
               args.rotation, args.min_mult, args.max_mult,
               args.curve, smooth_idx).run(events, reader)


if __name__ == "__main__":
    main()
