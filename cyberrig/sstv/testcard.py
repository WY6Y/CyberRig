"""A synthetic SSTV test card, for verifying the encoder without a camera.

Colour bars catch channel-order mistakes (a red/blue swap is obvious), the
grey ramp catches the level-to-frequency mapping, and the sync wedges at the
edges make slant visible at a glance in whatever decoder you check against.
"""

from __future__ import annotations

import numpy as np

BARS = [
    (255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0),
    (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0),
]


def test_card(width: int, height: int, label: str = "") -> np.ndarray:
    img = np.zeros((height, width, 3), dtype=np.uint8)

    # Top 55%: colour bars.
    bar_bottom = int(height * 0.55)
    for i, c in enumerate(BARS):
        x0 = int(round(i * width / len(BARS)))
        x1 = int(round((i + 1) * width / len(BARS)))
        img[:bar_bottom, x0:x1] = c

    # Middle 20%: full black-to-white ramp.
    ramp_bottom = int(height * 0.75)
    ramp = np.linspace(0, 255, width).astype(np.uint8)
    img[bar_bottom:ramp_bottom] = ramp[None, :, None]

    # Bottom: primaries at half and full, plus a 1-pixel checker for detail.
    third = (height - ramp_bottom) // 3
    y = ramp_bottom
    for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255)):
        half = tuple(v // 2 for v in c)
        img[y:y + third, :width // 2] = half
        img[y:y + third, width // 2:] = c
        y += third
    img[y:, :] = 0
    checker = np.indices((max(1, height - y), width)).sum(axis=0) % 2
    img[y:, :] = (checker * 255)[:, :, None]

    # Corner wedges: any slant shows as these leaning.
    w = max(2, width // 40)
    img[:w, :] = 255
    img[-w:, :] = 255
    img[:, :w] = 255
    img[:, -w:] = 255

    if label:
        _draw_label(img, label)
    return img


def _draw_label(img: np.ndarray, text: str):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return
    im = Image.fromarray(img)
    d = ImageDraw.Draw(im)
    h = img.shape[0]
    d.rectangle([0, h // 2 - 14, img.shape[1], h // 2 + 14], fill=(0, 0, 0))
    d.text((8, h // 2 - 8), text, fill=(255, 255, 255))
    img[:] = np.asarray(im, dtype=np.uint8)
