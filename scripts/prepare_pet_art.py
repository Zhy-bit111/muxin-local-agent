#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 AI 生图产出的立绘处理成桌宠可用的透明 PNG（纯本地、零联网）。

生图工具虽然给了 transparent 参数，实际返回的是浅灰白底 + 淡网格 + 角标水印，
所以必须本地抠一次。步骤：

1. 取四角中位数当背景色；
2. 按「与背景色的距离」算 alpha，保留绒毛边缘的半透明过渡，不硬切；
3. 用「深色主体膨胀若干次」当许可区，把远处的网格线 / 水印直接清零；
4. 按 alpha 的 bbox 裁切，输出 RGBA 到 `assets/pet_black_cat.png`；
5. 另存一张洋红底预览，方便肉眼确认抠图效果。

换形象时把新图放进 assets/，重跑本脚本即可。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
DST = ASSETS / "pet_black_cat.png"
PREVIEW = ASSETS / "pet_black_cat_preview.png"


def find_source() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    candidates = sorted(ASSETS.glob("A_*fluffy*black*.png"))
    if not candidates:
        raise SystemExit("assets/ 里没找到生图原始 PNG；可把路径作为第一个参数传入")
    return candidates[-1]


def main() -> int:
    src = find_source()
    img = Image.open(src).convert("RGB")
    a = np.asarray(img).astype(np.int16)
    h, w = a.shape[:2]

    corners = np.concatenate([
        a[:40, :40].reshape(-1, 3), a[:40, -40:].reshape(-1, 3),
        a[-40:, :40].reshape(-1, 3), a[-40:, -40:].reshape(-1, 3),
    ])
    bg = np.median(corners, axis=0)
    dist = np.sqrt(((a - bg) ** 2).sum(axis=2))
    alpha = np.clip((dist - 10.0) / 45.0, 0.0, 1.0)

    # 深色猫身是主体；向外膨胀几圈，保留贴着身体的绒毛，丢掉远处的网格/水印
    core = a.mean(axis=2) < 150
    mask = core.copy()
    for _ in range(6):
        grown = mask.copy()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                grown |= np.roll(np.roll(mask, dy, axis=0), dx, axis=1)
        mask = grown
    alpha = np.where(mask, alpha, 0.0)
    alpha[int(h * 0.88):, int(w * 0.82):] = 0.0      # 右下角角标，保险

    rgba = np.dstack([a.astype(np.uint8), (alpha * 255).astype(np.uint8)])
    out = Image.fromarray(rgba)
    box = out.getchannel("A").getbbox()
    if not box:
        raise SystemExit("抠图失败：整张图都变透明了")
    out = out.crop(box)
    out.save(DST)

    preview = Image.new("RGB", out.size, (255, 0, 255))
    preview.paste(out.convert("RGB"), (0, 0), out.getchannel("A"))
    preview.save(PREVIEW)

    arr = np.asarray(out.getchannel("A"))
    print(f"源图       {src.name}  {w}x{h}")
    print(f"背景色中位数 {bg.astype(int).tolist()}")
    print(f"输出       {DST.name}  {out.width}x{out.height}")
    print(f"预览       {PREVIEW.name}（洋红 = 将被抠掉的透明区）")
    print(f"alpha>200 像素占比 {float((arr > 200).mean()):.1%}，"
          f"全透明占比 {float((arr == 0).mean()):.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
