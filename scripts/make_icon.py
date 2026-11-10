"""生成「慕心」程序图标：深色圆角底 + 金色圆 + 慕字，与界面配色一致。"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "muxin.ico"
FONT_PATH = "C:/Windows/Fonts/msyhbd.ttc"

BG = (18, 19, 23, 255)
ACCENT = (233, 180, 88, 255)
INK = (18, 19, 23, 255)

SIZE = 512


def build() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=int(SIZE * 0.22), fill=BG)

    margin = int(SIZE * 0.13)
    d.ellipse([margin, margin, SIZE - margin, SIZE - margin], fill=ACCENT)

    font = ImageFont.truetype(FONT_PATH, int(SIZE * 0.46))
    bbox = d.textbbox((0, 0), "慕", font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(
        ((SIZE - w) / 2 - bbox[0], (SIZE - h) / 2 - bbox[1]),
        "慕",
        font=font,
        fill=INK,
    )
    return img


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img = build()
    img.save(OUT, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    # 同时留一张 png 方便查看
    img.resize((256, 256), Image.LANCZOS).save(OUT.with_suffix(".png"))
    print(f"icon -> {OUT}")


if __name__ == "__main__":
    main()
