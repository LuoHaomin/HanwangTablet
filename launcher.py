#!/usr/bin/env python3
"""汉王手写板 .app 启动器：选择白板模式或鼠标模式。"""

import pygame

import main
import mousepad


def run():
    pygame.init()
    screen = pygame.display.set_mode((360, 240))
    pygame.display.set_caption("汉王手写板")
    font = pygame.font.SysFont("pingfangsc,hiraginosansgb", 18)
    small = pygame.font.SysFont("pingfangsc,hiraginosansgb", 13)

    buttons = [
        (pygame.Rect(30, 60, 300, 56), "白板模式（手写笔记/画图）", "whiteboard"),
        (pygame.Rect(30, 130, 300, 56), "鼠标模式（数位板，无压感）", "mouse"),
    ]

    while True:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                pygame.quit()
                return
            if e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                for rect, _, mode in buttons:
                    if rect.collidepoint(e.pos):
                        pygame.quit()  # 目标模式自建窗口
                        if mode == "whiteboard":
                            main.start_whiteboard()
                        else:
                            mousepad.start_mousepad()
                        return

        screen.fill((250, 248, 244))
        screen.blit(font.render("汉王 N10 Plus 手写板", True, (30, 30, 34)),
                    (30, 20))
        screen.blit(small.render("请选择模式（需已 adb connect 设备）", True,
                                 (120, 120, 120)), (30, 210))
        for rect, label, _ in buttons:
            hovered = rect.collidepoint(pygame.mouse.get_pos())
            pygame.draw.rect(screen, (200, 220, 245) if hovered else (225, 232, 240),
                             rect, border_radius=10)
            t = font.render(label, True, (30, 30, 34))
            screen.blit(t, t.get_rect(center=rect.center))
        pygame.display.flip()


if __name__ == "__main__":
    run()
