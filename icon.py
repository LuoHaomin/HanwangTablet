#!/usr/bin/env python3
"""生成 HanwangTablet.app 图标：纸面底色 + 变宽墨迹笔画 + 红印章点。

2048 超采样绘制后缩到各档尺寸，输出 icns 所需的 iconset 目录。
用法: uv run python icon.py
"""

import math
import os
import pygame

SRC = 2048  # 超采样边长
BG = (250, 248, 244)
INK = (30, 30, 34)
RED = (198, 55, 45)


def bezier(p0, p1, p2, p3, t):
    u = 1.0 - t
    return (u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
            u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1])


def draw_icon(size: int) -> pygame.Surface:
    surf = pygame.Surface((SRC, SRC))
    surf.fill((0, 0, 0, 0)) if False else None

    # macOS squircle 近似：圆角矩形底
    radius = int(SRC * 0.225)
    pygame.draw.rect(surf, BG, (0, 0, SRC, SRC), border_radius=radius)
    # 内描边增加质感
    pygame.draw.rect(surf, (215, 211, 203), (0, 0, SRC, SRC),
                     width=int(SRC * 0.008), border_radius=radius)

    # 变宽墨迹：贝塞尔中线 + 压感宽度剖面（与白板渲染同思路）
    p0, p1 = (430, 1560), (1560, 1450)
    p2, p3 = (600, 650), (1610, 470)
    n = 160
    max_w = SRC * 0.125
    top, bot = [], []
    for i in range(n + 1):
        t = i / n
        x, y = bezier(p0, p1, p2, p3, t)
        d = bezier(p0, p1, p2, p3, min(t + 0.01, 1))
        dx, dy = d[0] - x, d[1] - y
        ln = math.hypot(dx, dy) or 1
        nx, ny = -dy / ln, dx / ln
        w = max_w * (0.12 + 0.88 * math.sin(math.pi * t) ** 0.7) / 2
        top.append((x + nx * w, y + ny * w))
        bot.append((x - nx * w, y - ny * w))
    poly = top + bot[::-1]
    pygame.draw.polygon(surf, INK, poly)
    # 端点圆
    for pt, t in ((top[0], 0), (bot[0], 0), (top[-1], 1), (bot[-1], 1)):
        w = max_w * (0.12 + 0.88 * math.sin(math.pi * t) ** 0.7) / 2
        pygame.draw.circle(surf, INK, (int(pt[0]), int(pt[1])), int(w))

    # 红印章点
    pygame.draw.circle(surf, RED, (int(SRC * 0.765), int(SRC * 0.765)),
                       int(SRC * 0.062))

    if size == SRC:
        return surf
    out = pygame.Surface((size, size))
    pygame.transform.smoothscale(surf, (size, size), out)
    return out


def main():
    pygame.init()
    iconset = "HanwangTablet.iconset"
    os.makedirs(iconset, exist_ok=True)
    for sz in (16, 32, 128, 256, 512):
        pygame.image.save(draw_icon(sz), f"{iconset}/icon_{sz}x{sz}.png")
        pygame.image.save(draw_icon(sz * 2),
                          f"{iconset}/icon_{sz}x{sz}@2x.png")
    # 1024 只有 @1x 一档（icon_512x512@2x 已覆盖 1024）
    pygame.image.save(draw_icon(1024), f"{iconset}/icon_1024x1024.png")
    print("iconset written:", iconset)


if __name__ == "__main__":
    main()
