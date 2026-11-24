#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""桌宠形象层：程序化小黑猫 + AI 立绘接入。

两种形象**二选一，随时可换**：

1. **AI 立绘（优先）**：`assets/pet_black_cat.png`（透明 PNG，由生图工具产出）。
   启动时一次性做色键合成 + **预生成一整套形变帧**，运行时只切帧、不做图像运算：
   - 绒毛边用 **4×4 Bayer 有序抖动**按 alpha 决定「留毛尖 / 抠掉」，
     保留细密毛流渐变；不再像旧版那样把半透明区一刀切压成纯深色硬边。
   - 呼吸是**非等比缩放**（竖向起伏 + 横向微收），不是整张图左右晃；
   - 眨眼是**眼部条带被上眼皮覆盖**（额头毛色下压），不是画两道黑线；
   - 尾巴 / 耳朵 / 头部各自用渐变 mask 做**局部位移**，多频率叠加避免机械感。
2. **程序化黑猫（回退）**：全部 Canvas 图元，零图片、零依赖。

这一层**只画画**：不产生文字、不判意图、不碰模型、不联网。
所有自主小动作由调用方按时间安排，本模块只负责把某一帧画出来。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPRITE_PATH = ROOT / "assets" / "pet_black_cat.png"

CHROMA = "#FF00FF"
CHROMA_RGB = (255, 0, 255)
COAT = "#2B2A34"
COAT_LIT = "#4A4757"
COAT_DARK = "#17161D"
COAT_DARK_RGB = (23, 22, 29)
EAR_IN = "#A2687B"
EYE = "#F3C65B"
EYE_DARK = "#2A2314"
BLUSH = "#7A4350"
WHISKER = "#9A94A6"
BUBBLE_BG = "#FFFCF6"

# 两级小动作：微动作高频（让猫一直「活着」），大动作低频。
# 都由调用方按时间安排，本模块只认名字。
MICRO_ACTIONS = ("blink", "ear_twitch", "tail_flick",
                 "glance_left", "glance_right")
MACRO_ACTIONS = ("stretch", "yawn", "shake", "look_away", "pat")
IDLE_ACTIONS = MICRO_ACTIONS + MACRO_ACTIONS

# 兼容旧引用；新帧库不再靠整体旋转做动作，只留极小幅度的重心倾斜
FRAME_ANGLES = (-1.2, -0.6, 0.0, 0.6, 1.2)

DEFAULT_SPRITE_SIZE = 212          # 基准画幅 260 内留 24px 边距
BAYER4 = ((0, 8, 2, 10), (12, 4, 14, 6),
          (3, 11, 1, 9), (15, 7, 13, 5))


def action_params(t: float, action: str, phase: float,
                  blink_until: float = 0.0) -> dict:
    """把「当前动作」翻译成一组绘制参数。两种形象共用，保证行为一致。"""
    p = {
        "ox": 0.0, "oy": 1.8 * math.sin(t * 1.1),
        "ear": 0.0, "tail": 1.0, "mouth": 0.0, "blink": False,
        "eye_dx": 0.0, "head_dx": 0.0, "squash": 0.0, "paw": 0.0,
    }
    if t < blink_until or (t % 4.6) < 0.17:
        p["blink"] = True
    if action == "stretch":
        p.update(oy=p["oy"] + 7, squash=9, paw=13, mouth=5, blink=True)
    elif action == "yawn":
        p.update(mouth=11, blink=True, ear=-3)
    elif action == "ear_twitch":
        p["ear"] = 7 * math.sin(phase * 24)
    elif action == "tail_flick":
        p["tail"] = 2.6
    elif action == "look_away":
        p.update(eye_dx=5, head_dx=3)
    elif action == "shake":
        p["ox"] = 2.5 * math.sin(phase * 40)
    elif action == "poke_react":
        p.update(ox=3 * math.sin(phase * 30), ear=5 * math.sin(phase * 20))
    elif action == "glance_left":
        p.update(eye_dx=-4, head_dx=-3)
    elif action == "glance_right":
        p.update(eye_dx=4, head_dx=3)
    elif action == "pat":
        p.update(blink=True, ear=-2, oy=p["oy"] - 2)
    return p


def _fur(cv, cx: float, cy: float, rx: float, ry: float,
         ox: float, oy: float, seed: int, count: int = 44):
    """沿椭圆轮廓放射短毛刺。角度固定（不随时间变），所以每帧重画也不闪烁。"""
    for i in range(count):
        a = 2 * math.pi * i / count
        # 用确定性抖动代替随机，保证离线自测可复现
        jitter = ((seed * 37 + i * 91) % 11) / 11.0
        length = 3.5 + jitter * 4.5
        x = cx + rx * math.cos(a)
        y = cy + ry * math.sin(a)
        nx, ny = math.cos(a), math.sin(a)
        col = (COAT, COAT_LIT, COAT_DARK)[i % 3]
        cv.create_line(x + ox, y + oy,
                       x + ox + nx * length, y + oy + ny * length,
                       fill=col, width=1.4, capstyle="round", tag="cat")


def draw_vector_cat(cv, *, t: float, action: str, phase: float,
                    blink_until: float = 0.0) -> int:
    """程序化小黑猫。返回画出的图元数，供自检断言。"""
    cv.delete("cat")
    p = action_params(t, action, phase, blink_until)
    ox, oy = 10 + p["ox"], 70 + p["oy"]          # 原 240 坐标 → 260×300 窗口
    squash, paw = p["squash"], p["paw"]
    sway = math.sin(t * 1.9) * p["tail"]
    hd = p["head_dx"]

    def X(v):
        return v + ox

    def Y(v):
        return v + oy

    # 尾巴（翘高 + 摆动，画在身体后面）
    cv.create_line(
        [X(158), Y(188), X(188), Y(180 - 10 * sway),
         X(212), Y(154 - 18 * sway), X(202), Y(128 - 11 * sway)],
        width=15, fill=COAT_DARK, capstyle="round", smooth=True, tag="cat",
    )
    cv.create_oval(X(194), Y(120 - 11 * sway), X(212), Y(138 - 11 * sway),
                   fill=COAT_LIT, outline="", tag="cat")

    # 丰满圆润的身体（带绒毛边）
    _fur(cv, 120, 166 + squash / 2, 54 + squash / 2, 48 - squash / 2,
         ox, oy, seed=3, count=48)
    cv.create_oval(X(66 + squash), Y(118 + squash), X(174 - squash), Y(214),
                   fill=COAT, outline=COAT_DARK, width=2, tag="cat")
    # 胸口一撮浅毛，增加层次
    cv.create_oval(X(100), Y(160), X(140), Y(206),
                   fill=COAT_LIT, outline="", tag="cat")
    cv.create_oval(X(104), Y(164), X(136), Y(202),
                   fill=COAT, outline="", tag="cat")
    # 圆爪
    cv.create_oval(X(84 - paw), Y(194 + paw / 3), X(114 - paw), Y(220),
                   fill=COAT, outline=COAT_DARK, width=2, tag="cat")
    cv.create_oval(X(126 + paw), Y(194 + paw / 3), X(156 + paw), Y(220),
                   fill=COAT, outline=COAT_DARK, width=2, tag="cat")

    # 耳朵（一只微歪，拽）
    e = p["ear"]
    cv.create_polygon(X(66), Y(80), X(70 + e), Y(30), X(108), Y(62),
                      fill=COAT, outline=COAT_DARK, width=2, tag="cat")
    cv.create_polygon(X(174), Y(80), X(168 - e), Y(32), X(132), Y(62),
                      fill=COAT, outline=COAT_DARK, width=2, tag="cat")
    cv.create_polygon(X(77), Y(72), X(79 + e / 2), Y(44), X(98), Y(63),
                      fill=EAR_IN, outline="", tag="cat")
    cv.create_polygon(X(163), Y(72), X(160 - e / 2), Y(46), X(142), Y(63),
                      fill=EAR_IN, outline="", tag="cat")

    # 大头（幼态 = 可爱）
    hx = 120 + hd
    _fur(cv, hx, 106, 62, 54, ox, oy, seed=7, count=54)
    cv.create_oval(X(58 + hd), Y(52), X(182 + hd), Y(160),
                   fill=COAT, outline=COAT_DARK, width=2, tag="cat")

    # 半眯琥珀眼（拽）+ 单边挑眉
    for ex in (88, 136):
        x0, y0, x1, y1 = X(ex + p["eye_dx"]), Y(94), X(ex + 18 + p["eye_dx"]), Y(116)
        cv.create_oval(x0, y0, x1, y1, fill=EYE, outline="", tag="cat")
        cv.create_oval(x0 + 5, y0 + 3, x1 - 5, y1 - 3,
                       fill=EYE_DARK, outline="", tag="cat")
        # 上眼睑盖掉上半 → 半眯
        cv.create_polygon(x0 - 1, y0 - 2, x1 + 1, y0 - 2,
                          x1 + 1, y0 + 9, x0 - 1, y0 + 9,
                          fill=COAT, outline="", tag="cat")
        cv.create_line(x0 - 2, y0 + 9, x1 + 2, y0 + 9,
                       fill=COAT_DARK, width=2, tag="cat")
    cv.create_line(X(84), Y(84), X(106), Y(80),
                   fill=COAT_LIT, width=2, tag="cat")     # 挑起的眉
    cv.create_line(X(136), Y(82), X(156), Y(84),
                   fill=COAT_LIT, width=2, tag="cat")

    # 腮红 / 鼻 / 微扬的嘴角 / 胡须
    cv.create_oval(X(72), Y(118), X(92), Y(128), fill=BLUSH, outline="", tag="cat")
    cv.create_oval(X(148), Y(118), X(168), Y(128), fill=BLUSH, outline="", tag="cat")
    cv.create_polygon(X(115), Y(116), X(125), Y(116), X(120), Y(123),
                      fill=EAR_IN, outline="", tag="cat")
    if p["mouth"] > 0:
        cv.create_oval(X(113), Y(126), X(127), Y(126 + p["mouth"]),
                       fill="#3A2029", outline=EAR_IN, width=1, tag="cat")
    else:
        cv.create_arc(X(108), Y(120), X(120), Y(132), start=200, extent=140,
                      style="arc", outline=WHISKER, width=2, tag="cat")
        cv.create_arc(X(120), Y(120), X(133), Y(131), start=200, extent=150,
                      style="arc", outline=WHISKER, width=2, tag="cat")
    for yy in (116, 124):
        cv.create_line(X(58), Y(yy), X(84), Y(yy - 3),
                       fill=WHISKER, width=1, tag="cat")
        cv.create_line(X(156), Y(yy - 3), X(182), Y(yy),
                       fill=WHISKER, width=1, tag="cat")

    if p["blink"]:
        # 眨眼时压一条深色弧线，比直接闭眼更软
        cv.create_line(X(86), Y(104), X(106), Y(104),
                       fill=COAT_DARK, width=3, capstyle="round", tag="cat")
        cv.create_line(X(134), Y(104), X(154), Y(104),
                       fill=COAT_DARK, width=3, capstyle="round", tag="cat")
    return len(cv.find_withtag("cat"))


def _snap_chroma(img):
    """缩放 / 旋转 / 位移插值会在洋红边缘混出浅紫，Tk 的 -transparentcolor 只认纯色。

    把「洋红系」像素刷回精确 CHROMA_RGB。两档判定：
    - 强档：明显偏洋红（含 g 被毛色抬到 ~115 的插值色，旧版阈值卡在 115 漏掉了，
      尾巴右侧就留下一块紫红色矩形）；
    - 弱档：偏暗的紫红，额外要求 b-g 与 r-g 都够大，避开粉内耳、鼻头、琥珀眼。
    只用 Pillow 通道运算，不引入 numpy。
    """
    from PIL import Image, ImageChops

    def AND(*masks):
        out = masks[0]
        for m in masks[1:]:
            out = ImageChops.multiply(out, m)
        return out

    r, g, b = img.split()
    strong = AND(r.point(lambda v: 255 if v > 175 else 0),
                 g.point(lambda v: 255 if v < 165 else 0),
                 b.point(lambda v: 255 if v > 165 else 0))
    # 插值把洋红的 g 通道从 0 抬到 ~120，会在尾巴右侧留一道亮紫细线。
    # 这类颜色 r、b 都很高；粉内耳 / 鼻头的 b 只有 ~120，不会被误伤。
    mid = AND(r.point(lambda v: 255 if v > 185 else 0),
              b.point(lambda v: 255 if v > 185 else 0),
              g.point(lambda v: 255 if v < 175 else 0),
              ImageChops.subtract(b, g).point(lambda v: 255 if v > 25 else 0))
    # 更暗的紫红（洋红与黑毛混合）；内耳 g≈104 不满足 g<90，蓝灰毛边 r-g<0
    dark = AND(b.point(lambda v: 255 if v > 110 else 0),
               g.point(lambda v: 255 if v < 90 else 0),
               ImageChops.subtract(b, g).point(lambda v: 255 if v > 40 else 0),
               ImageChops.subtract(r, g).point(lambda v: 255 if v > 40 else 0))
    # 放大后仍看得见的一小撮淡紫毛尖（洋红与浅色毛的插值）。
    # 用「b、r 都高于 g，且 b 与 r 接近」锁定紫色，粉内耳 b-g≈19、
    # 冷蓝毛边 r-g<0、琥珀眼 g 高，都不会被误伤。
    pale = AND(r.point(lambda v: 255 if v > 120 else 0),
               b.point(lambda v: 255 if v > 130 else 0),
               g.point(lambda v: 255 if v < 165 else 0),
               ImageChops.subtract(b, g).point(lambda v: 255 if v > 25 else 0),
               ImageChops.subtract(r, g).point(lambda v: 255 if v > 15 else 0),
               ImageChops.subtract(b, r).point(lambda v: 255 if v < 60 else 0))
    mask = ImageChops.lighter(
        ImageChops.lighter(strong, mid), ImageChops.lighter(dark, pale))
    img.paste(Image.new("RGB", img.size, CHROMA_RGB), (0, 0), mask)
    return img


def _dither_composite(canvas):
    """RGBA → 色键 RGB：绒毛边用有序抖动保留渐变。

    `-transparentcolor` 只能精确抠掉**一种颜色**，所以边缘不能留中间色，
    否则会晕出一圈洋红。旧版把 alpha 24~110 整片刷成同一个深色，
    毛流渐变被抹平 → 远看就是一圈硬描边，正是「像贴纸 / 简笔画」的主因。

    现在按 4×4 Bayer 阈值把 alpha 转成「留毛尖原色 / 抠成纯背景」的二值点阵：
    近看是细密毛刺，远看是柔和过渡，且没有任何中间色。
    """
    from PIL import Image, ImageChops

    size = canvas.width
    rgb, a = canvas.convert("RGB"), canvas.getchannel("A")
    th = Image.new("L", (size, size))
    px = th.load()
    for y in range(size):
        row = BAYER4[y & 3]
        for x in range(size):
            px[x, y] = int((row[x & 3] + 0.5) * 16)
    core = a.point(lambda v: 255 if v >= 120 else 0)
    soft = a.point(lambda v: min(255, int((v - 16) * 255 / 104))
                   if 16 <= v < 120 else 0)
    keep = ImageChops.subtract(soft, th).point(lambda v: 255 if v > 0 else 0)
    mask = ImageChops.lighter(core, keep)
    flat = Image.new("RGB", (size, size), CHROMA_RGB)
    flat.paste(rgb, (0, 0), mask)
    return flat


def _grad_masks(size):
    """局部形变用的渐变 mask：一次生成，所有帧复用。"""
    from PIL import Image, ImageChops

    v = Image.linear_gradient("L").resize((size, size),
                                          Image.Resampling.BILINEAR)  # 上 0 → 下 255
    h = v.transpose(Image.Transpose.TRANSPOSE)                        # 左 0 → 右 255
    upper = v.point(lambda x: max(0, min(255, (150 - x) * 2)))
    lower = v.point(lambda x: max(0, min(255, (x - 96) * 2)))
    far_right = h.point(lambda x: max(0, min(255, (x - 96) * 2)))
    far_left = h.point(lambda x: max(0, min(255, (150 - x) * 2)))
    return {
        "head": upper,
        "tail": ImageChops.multiply(far_right, lower),
        "ear_l": ImageChops.multiply(far_left, upper),
        "ear_r": ImageChops.multiply(far_right, upper),
    }


def _shift_region(img, mask, dx: int, dy: int = 0):
    """把某个区域平移，用渐变 mask 与原图混合，避免硬缝。

    **不能用 `ImageChops.offset`**：那是循环位移，会把对侧的毛 / 洋红边卷进
    尾巴右侧，形成一道亮紫色横条。这里改成非循环平移，露出的部分填纯 CHROMA，
    Tk 会精确抠掉，没有任何中间色残留。
    """
    from PIL import Image, ImageChops

    dx, dy = int(round(dx)), int(round(dy))
    if not dx and not dy:
        return img
    shifted = Image.new("RGB", img.size, CHROMA_RGB)
    shifted.paste(img, (dx, dy))
    return Image.composite(shifted, img, mask)


def _eyelid(img, close: float):
    """眨眼：用眼睛**上方那几行毛色**向下拉伸盖住眼部条带。

    不是画两道黑线（那又是简笔画），而是让上眼皮真的压下来，
    条带上下用三角羽化，闭到一半也能自然衔接。
    """
    from PIL import Image

    if close <= 0:
        return img
    size = img.width
    y0, y1 = int(0.395 * size), int(0.545 * size)
    lid = img.crop((0, max(0, y0 - 3), size, y0)).resize(
        (size, y1 - y0), Image.Resampling.BILINEAR)
    band = img.crop((0, y0, size, y1))
    mixed = Image.blend(band, lid, min(1.0, close))
    feather = Image.linear_gradient("L").resize(
        (size, y1 - y0), Image.Resampling.BILINEAR)
    feather = feather.point(lambda v: max(0, 255 - abs(v - 127) * 2))
    feather = feather.point(lambda v: int(v * min(1.0, close)))
    out = img.copy()
    out.paste(mixed, (0, y0), feather)
    return out


def _deform(base, masks, *, breath=0.0, tail=0.0, head=0.0, ear=0.0,
            close=0.0, squash=0.0, tilt=0.0):
    """把一组动作参数烘焙成一帧。运行时不再做任何图像运算。"""
    from PIL import Image

    size = base.width
    # 呼吸是竖向起伏 + 横向微收；squash 是点击/伸懒腰的弹性形变
    bx = 1 - 0.010 * breath + 0.055 * squash
    by = 1 + 0.026 * breath - 0.070 * squash
    w, h = max(8, int(size * bx)), max(8, int(size * by))
    r = base.resize((w, h), Image.Resampling.LANCZOS)
    img = Image.new("RGB", (size, size), CHROMA_RGB)
    img.paste(r, ((size - w) // 2, (size - h) // 2 + int(2 * breath)))

    # 各部位不同频率 / 不同方向，避免整只猫像一块板子在平移
    img = _shift_region(img, masks["tail"], tail * 5.5, tail * 1.2)
    if ear:
        img = _shift_region(img, masks["ear_l"], -ear * 3.5)
        img = _shift_region(img, masks["ear_r"], ear * 3.5)
    img = _shift_region(img, masks["head"], head * 3.2, -abs(head) * 0.8)
    img = _eyelid(img, close)
    if tilt:
        img = img.rotate(tilt, resample=Image.Resampling.BICUBIC,
                         expand=False, fillcolor=CHROMA_RGB)
    return _snap_chroma(img)


def _pool_specs():
    """帧表配方。返回 {动作名: [参数 dict, ...]}。"""
    pools = {}
    pools["idle"] = [
        dict(breath=math.sin(p),
             tail=0.85 * math.sin(0.73 * p + 1.1),
             head=0.55 * math.sin(0.41 * p + 2.2))
        for p in (2 * math.pi * i / 12 for i in range(12))
    ]
    pools["blink"] = [dict(close=c, breath=0.15)
                      for c in (0.95, 1.0, 0.5, 0.0)]
    pools["ear_twitch"] = [dict(ear=e) for e in (0.0, 1.0, -0.75, 0.25)]
    pools["tail_flick"] = [dict(tail=t) for t in (0.0, 1.6, -1.2, 0.75, 0.0)]
    pools["glance_left"] = [dict(head=h, ear=-0.2)
                            for h in (-0.45, -0.95, -0.8)]
    pools["glance_right"] = [dict(head=h, ear=0.2)
                             for h in (0.45, 0.95, 0.8)]
    pools["look_away"] = [dict(head=h, tail=0.3)
                          for h in (0.0, 0.8, 1.05, 0.5)]
    pools["stretch"] = [dict(squash=s, tilt=a, close=c, breath=0.3)
                        for s, a, c in ((0.15, 0, 0), (0.95, 1.2, 0.8),
                                        (0.45, 0.5, 0.3), (0.0, 0, 0))]
    pools["yawn"] = [dict(close=c, squash=s)
                     for c, s in ((0.0, 0.0), (0.9, -0.5),
                                  (1.0, -0.6), (0.2, -0.1))]
    pools["shake"] = [dict(head=h, ear=e)
                      for h, e in ((0.0, 0.0), (1.0, 0.7), (-1.0, -0.7),
                                   (0.7, 0.4), (-0.35, -0.2))]
    pools["poke_react"] = [dict(squash=s, tilt=a, head=h)
                           for s, a, h in ((1.0, -2.0, 0.2), (-0.5, 1.4, -0.2),
                                           (0.3, -0.6, 0.1), (0.0, 0.0, 0.0))]
    pools["pat"] = [dict(close=c, head=h, breath=-0.2)
                    for c, h in ((0.5, 0.0), (0.9, -0.2), (0.65, -0.15))]
    return pools


def sprite_frames(path: str | Path, size: int = DEFAULT_SPRITE_SIZE):
    """AI 立绘 → `{动作名: [PhotoImage, ...]}` 帧库。

    文件缺失 / Pillow 不可用 / 处理失败返回 None，调用方自动回退程序化黑猫。
    一次生成约 50 帧，运行时只按时间切帧，不再做缩放 / 形变。
    """
    try:
        from PIL import Image, ImageTk
    except ImportError:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        img = Image.open(p).convert("RGBA")
        box = img.getchannel("A").getbbox()
        if box:
            img = img.crop(box)
        img.thumbnail((size - 6, size - 6), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2))

        base = _dither_composite(canvas)
        masks = _grad_masks(size)
        out = {}
        for name, specs in _pool_specs().items():
            out[name] = [ImageTk.PhotoImage(_deform(base, masks, **kw))
                         for kw in specs]
        return out
    except Exception as exc:
        # 立绘是可选增强，失败一律回退程序化黑猫，但要把原因留下来便于排查
        print(f"[pet_art] 立绘加载失败，回退程序化黑猫: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def draw_sprite(cv, frames, *, t: float, action: str, phase: float,
                cx: int = 130, cy: int = 200, scale: float = 1.0) -> int:
    """立绘模式：纯切帧。不再叠加任何简笔线条，动作全部由形变帧表达。"""
    cv.delete("cat")
    idle = frames.get("idle") or []
    if not idle:
        return 0
    if action in frames:
        pool = frames[action]
        idx = max(0, min(len(pool) - 1, int(phase * len(pool))))
    else:
        # 12 帧 × 160ms ≈ 1.9 秒一个呼吸周期，配合尾巴 / 头部的不同频率
        pool, idx = idle, int(t / 0.16) % len(idle)
    p = action_params(t, action, phase)
    dy = 1.6 * math.sin(t * 1.1)
    if action == "stretch":
        dy += 4
    cv.create_image(cx + p["ox"] * scale, cy + dy * scale,
                    image=pool[idx], tag="cat")
    return len(cv.find_withtag("cat"))
