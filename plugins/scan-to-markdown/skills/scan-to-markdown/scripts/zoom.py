#!/usr/bin/env python3
"""Cut out a region of a page image and enlarge it for close reading.

Usage: python3 zoom.py IMAGE TOP BOTTOM [LEFT RIGHT] [--scale N]
TOP/BOTTOM/LEFT/RIGHT are percentages of the image height and width (0-100);
LEFT/RIGHT default to the full width. Prints the path of the saved PNG (under the cache's zoom/ folder).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))  # also works under `python3 -I`
from common import ROOT, CACHE  # noqa: E402

MAX_EDGE = 2000  # larger images are shrunk by the image viewer anyway


def fmt(value: float) -> str:
    return f"{value:g}".replace(".", "_")


def main() -> int:
    parser = argparse.ArgumentParser(description="Enlarge a region of a page image.")
    parser.add_argument("image", type=Path)
    parser.add_argument("top", type=float)
    parser.add_argument("bottom", type=float)
    parser.add_argument("left", type=float, nargs="?", default=0.0)
    parser.add_argument("right", type=float, nargs="?", default=100.0)
    parser.add_argument("--scale", type=float, default=2.5)
    args = parser.parse_args()

    path = args.image if args.image.is_absolute() else (Path.cwd() / args.image)
    if not path.exists() and ROOT:
        path = ROOT / args.image
    image = Image.open(path)
    width, height = image.size
    top, bottom = sorted((max(0.0, min(100.0, args.top)), max(0.0, min(100.0, args.bottom))))
    left, right = sorted((max(0.0, min(100.0, args.left)), max(0.0, min(100.0, args.right))))
    box = (round(width * left / 100), round(height * top / 100), round(width * right / 100), round(height * bottom / 100))
    if box[2] - box[0] < 4 or box[3] - box[1] < 4:
        raise SystemExit("region is empty: check the percentages")

    crop = image.crop(box)
    scale = min(args.scale, MAX_EDGE / max(crop.size))
    if scale != 1:
        crop = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.Resampling.LANCZOS)

    out_dir = (CACHE / "zoom" if CACHE else path.parent.parent / "zoom") / path.parent.name
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{path.stem}_t{fmt(top)}_b{fmt(bottom)}_l{fmt(left)}_r{fmt(right)}_x{fmt(round(scale, 2))}.png"
    crop.save(out)
    print(f"saved: {out} ({crop.width}x{crop.height} px, scale {scale:.2f}, source px {box})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
