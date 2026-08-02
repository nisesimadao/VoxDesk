"""アイコンを作る。

絵文字を描いた角丸の四角。Windows は Segoe UI Emoji、macOS は Apple Color Emoji、
Linux は Noto Color Emoji を使う。見つからなければ図形だけで描く。

    python packaging/make_icon.py
"""

from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
EMOJI = "🎤"
BACKGROUND = (24, 26, 34)
ACCENT = (108, 92, 231)
SIZE = 1024

EMOJI_FONTS = [
    r"C:\Windows\Fonts\seguiemj.ttf",
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
]


def _emoji_font(size: int):
    for path in EMOJI_FONTS:
        if not os.path.exists(path):
            continue
        for candidate in (size, 109, 137, 160):  # 絵文字フォントは固定サイズのことがある
            try:
                return ImageFont.truetype(path, candidate)
            except OSError:
                continue
    return None


def _rounded_background(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = int(size * 0.22)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=BACKGROUND)
    # 上端にうっすら差し色を入れて、ただの黒い四角に見えないようにする
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius,
                           outline=ACCENT, width=max(2, size // 64))
    return image


def _draw_fallback(image: Image.Image) -> None:
    """絵文字フォントが無いときは、マイクの形を自前で描く。"""
    size = image.size[0]
    draw = ImageDraw.Draw(image)
    cx = size / 2
    head_w, head_h = size * 0.26, size * 0.42
    top = size * 0.20
    draw.rounded_rectangle((cx - head_w / 2, top, cx + head_w / 2, top + head_h),
                           radius=head_w / 2, fill=(236, 240, 248))
    arc_box = (cx - head_w * 0.95, top + head_h * 0.35,
               cx + head_w * 0.95, top + head_h * 1.05)
    draw.arc(arc_box, start=0, end=180, fill=ACCENT, width=int(size * 0.045))
    draw.line((cx, top + head_h * 1.05, cx, size * 0.80), fill=ACCENT, width=int(size * 0.045))
    draw.line((cx - size * 0.11, size * 0.80, cx + size * 0.11, size * 0.80),
              fill=ACCENT, width=int(size * 0.045))


def build() -> list[str]:
    os.makedirs(ASSETS, exist_ok=True)
    image = _rounded_background(SIZE)

    font = _emoji_font(int(SIZE * 0.62))
    drawn = False
    if font is not None:
        layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        try:
            draw.text((SIZE / 2, SIZE / 2), EMOJI, font=font, anchor="mm",
                      embedded_color=True)
            box = layer.getbbox()
            if box:
                emoji = layer.crop(box)
                target = int(SIZE * 0.60)
                scale = target / max(emoji.size)
                emoji = emoji.resize((max(1, int(emoji.width * scale)),
                                      max(1, int(emoji.height * scale))), Image.LANCZOS)
                image.alpha_composite(
                    emoji, ((SIZE - emoji.width) // 2, (SIZE - emoji.height) // 2))
                drawn = True
        except Exception:
            drawn = False
    if not drawn:
        _draw_fallback(image)

    written = []
    for size in (16, 32, 48, 64, 128, 256, 512):
        path = os.path.join(ASSETS, f"icon_{size}.png")
        image.resize((size, size), Image.LANCZOS).save(path)
        written.append(path)

    ico = os.path.join(ASSETS, "icon.ico")
    image.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    written.append(ico)

    icns_source = os.path.join(ASSETS, "icon_512.png")
    written.append(icns_source)
    return written


if __name__ == "__main__":
    for path in build():
        print(f"{os.path.relpath(path, ROOT)}  {os.path.getsize(path) / 1024:.1f} KB")
    print("絵文字フォント:", "見つかった" if _emoji_font(64) else "見つからず（図形で描画）",
          file=sys.stderr)
