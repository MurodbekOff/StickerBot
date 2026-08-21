"""Resize/convert arbitrary images into Telegram static-sticker-compliant PNGs.

Telegram's rule for static stickers: exactly one side must be 512px,
the other side <= 512px, image must have transparency support (PNG/WEBP).
"""
from io import BytesIO
from PIL import Image

STICKER_SIDE = 512


def to_sticker_png(image_bytes: bytes) -> BytesIO:
    img = Image.open(BytesIO(image_bytes)).convert("RGBA")

    w, h = img.size
    if w >= h:
        new_w = STICKER_SIDE
        new_h = max(1, round(h * STICKER_SIDE / w))
    else:
        new_h = STICKER_SIDE
        new_w = max(1, round(w * STICKER_SIDE / h))

    img = img.resize((new_w, new_h), Image.LANCZOS)

    out = BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    out.name = "sticker.png"
    return out