#!/usr/bin/env python3
"""Derive the shipped brand assets from the source logo artwork.

The founder supplied the logo as four JPEGs (black-on-white and white-on-black,
1024 px and 2048 px) which live in ``assets/brand/``. JPEG has no alpha channel
and a hard background, so it is unusable in the UI: the white rectangle around
the black lockup is visible on every dark surface, and vice versa.

This script regenerates the assets the app actually loads — transparent PNGs in
both ink colours, the mark on its own (no wordmark) for favicons, and a solid
apple-touch icon — from the single highest-resolution source. Everything it
writes is committed, so you only need to re-run it when the artwork changes::

    python3 scripts/gen_brand_assets.py

Requires Pillow (``pip install pillow``); it is a tooling-only dependency and is
deliberately not part of the backend runtime requirements.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover - tooling-only guard
    sys.exit("Pillow is required: python3 -m pip install --user pillow")

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "assets" / "brand" / "audr-logo-black-2048.jpg"
BRAND_OUT = REPO / "assets" / "brand"
PUBLIC_OUT = REPO / "frontend" / "public" / "brand"

# Ink colours for the two themes. The artwork is pure black line art, so the
# alpha mask is shared and only the fill colour changes.
INK_LIGHT = (13, 13, 13)  # near-black lockup, for light backgrounds
INK_DARK = (255, 255, 255)  # white lockup, for dark backgrounds
BACKDROP = (13, 13, 13)  # apple-touch-icon background

LOCKUP_WIDTHS = (512, 1024)
MARK_SIZES = (64, 180, 256, 512)
FAVICON_SIZES = (16, 32, 48, 64)


def alpha_mask(source: Path) -> Image.Image:
    """Return the artwork as an alpha mask: opaque where the ink is."""
    grey = Image.open(source).convert("L")
    # Black ink (0) becomes fully opaque, white paper (255) fully transparent.
    return grey.point(lambda v: 255 - v)


def tint(mask: Image.Image, ink: tuple[int, int, int]) -> Image.Image:
    out = Image.new("RGBA", mask.size, ink + (0,))
    out.putalpha(mask)
    return out


def trim(mask: Image.Image, image: Image.Image) -> Image.Image:
    box = mask.getbbox()
    return image.crop(box) if box else image


def split_mark(mask: Image.Image) -> Image.Image:
    """Crop the mountain mark off the top of the lockup, dropping the wordmark.

    The lockup is the mark stacked above the ``audr`` wordmark with a band of
    blank paper between them. Finding the widest fully-blank horizontal band
    gives the split point without hard-coding pixel offsets, so the crop keeps
    working if the artwork is re-exported at another size.
    """
    box = mask.getbbox()
    if box is None:
        raise ValueError("source artwork is blank")
    left, top, right, bottom = box
    body = mask.crop(box)
    width, height = body.size

    row_has_ink = [
        any(body.getpixel((x, y)) > 8 for x in range(0, width, 4)) for y in range(height)
    ]

    best_start = best_len = 0
    run_start = None
    for y, inked in enumerate(row_has_ink):
        if inked:
            if run_start is not None and y - run_start > best_len:
                best_start, best_len = run_start, y - run_start
            run_start = None
        elif run_start is None:
            run_start = y
    if run_start is not None and height - run_start > best_len:
        best_start, best_len = run_start, height - run_start

    if best_len < height * 0.02:
        raise ValueError("could not find the gap between the mark and the wordmark")

    return mask.crop((left, top, right, top + best_start))


def save_png(
    image: Image.Image, path: Path, width: int | None = None, height: int | None = None
) -> None:
    out = image
    if width is not None:
        scale = width / image.width
        out = image.resize((width, max(1, round(image.height * scale))), Image.LANCZOS)
    elif height is not None:
        scale = height / image.height
        out = image.resize((max(1, round(image.width * scale)), height), Image.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.save(path, "PNG", optimize=True)
    print(f"  {path.relative_to(REPO)}  {out.width}x{out.height}")


def square(
    image: Image.Image, size: int, background: tuple[int, int, int] | None = None
) -> Image.Image:
    """Fit the image into a transparent (or filled) square with a small margin."""
    inner = round(size * 0.86)
    scale = min(inner / image.width, inner / image.height)
    scaled = image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS
    )
    canvas = Image.new("RGBA", (size, size), (background + (255,)) if background else (0, 0, 0, 0))
    canvas.alpha_composite(scaled, ((size - scaled.width) // 2, (size - scaled.height) // 2))
    return canvas


def main() -> int:
    if not SOURCE.exists():
        sys.exit(f"source artwork missing: {SOURCE}")

    mask = alpha_mask(SOURCE)
    mark_mask = split_mark(mask)

    lockup_light = trim(mask, tint(mask, INK_LIGHT))
    lockup_dark = trim(mask, tint(mask, INK_DARK))
    mark_light = trim(mark_mask, tint(mark_mask, INK_LIGHT))
    mark_dark = trim(mark_mask, tint(mark_mask, INK_DARK))

    print("brand assets:")
    for width in LOCKUP_WIDTHS:
        save_png(lockup_light, BRAND_OUT / f"audr-logo-light-{width}.png", width=width)
        save_png(lockup_dark, BRAND_OUT / f"audr-logo-dark-{width}.png", width=width)
    for size in MARK_SIZES:
        save_png(mark_light, BRAND_OUT / f"audr-mark-light-{size}.png", height=size)
        save_png(mark_dark, BRAND_OUT / f"audr-mark-dark-{size}.png", height=size)

    print("frontend public assets:")
    save_png(lockup_light, PUBLIC_OUT / "audr-logo-light.png", width=512)
    save_png(lockup_dark, PUBLIC_OUT / "audr-logo-dark.png", width=512)
    save_png(mark_light, PUBLIC_OUT / "audr-mark-light.png", height=256)
    save_png(mark_dark, PUBLIC_OUT / "audr-mark-dark.png", height=256)

    # Favicon: the white mark, which stays legible on the dark chrome of every
    # browser theme as well as on a light tab strip.
    icon = PUBLIC_OUT / "favicon.ico"
    square(mark_dark, 256).save(icon, "ICO", sizes=[(s, s) for s in FAVICON_SIZES])
    print(f"  {icon.relative_to(REPO)}  {'/'.join(str(s) for s in FAVICON_SIZES)}")

    touch = PUBLIC_OUT / "apple-touch-icon.png"
    square(mark_dark, 180, background=BACKDROP).convert("RGB").save(touch, "PNG", optimize=True)
    print(f"  {touch.relative_to(REPO)}  180x180")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
