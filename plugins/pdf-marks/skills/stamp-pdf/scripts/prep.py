#!/usr/bin/env python3
"""Turn a phone photo of a rubber stamp into a transparent PNG ready for stamping.

Corrects uneven lighting, drops the paper it was pressed on, keeps the ink colour
of the original, and crops to the seal itself.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from collections import deque
from pathlib import Path

try:
    import numpy as np
    from PIL import Image, ImageFilter
except ImportError:
    sys.exit("Pillow and numpy required. Install with: pip3 install pillow numpy")

FLOOR = 0.22  # alpha below this is paper grain, not ink
INK_MASK = 0.35
CORE_MASK = 0.6


def load(path: Path) -> Image.Image:
    """Open the photo, converting HEIC through sips on macOS if Pillow can't."""
    try:
        return Image.open(path).convert("RGB")
    except OSError:
        if not shutil.which("sips"):
            raise SystemExit(f"Cannot read {path}. Convert it to PNG or JPEG first.")
        tmp = Path(tempfile.mkdtemp(prefix="pdf-marks-")) / "photo.png"
        subprocess.run(
            ["sips", "-s", "format", "png", str(path), "--out", str(tmp)],
            check=True, capture_output=True,
        )
        return Image.open(tmp).convert("RGB")


def ink_alpha(img: Image.Image) -> np.ndarray:
    """Per-pixel ink coverage, 0..1, with the paper flattened out."""
    rgb = np.asarray(img).astype(np.float32)
    gray = rgb.mean(axis=2)

    # Flat-field: divide by a heavily blurred copy so paper reads ~1.0 under any lighting.
    background = np.asarray(
        Image.fromarray(gray.astype(np.uint8)).filter(
            ImageFilter.GaussianBlur(max(img.size) / 40)
        )
    ).astype(np.float32)
    ratio = gray / np.maximum(background, 1.0)

    darkness = np.clip((1.0 - ratio) / 0.28, 0.0, 1.0)
    # Ink also skews blue over red; that separates it from shadows and smudges.
    blueness = (rgb[:, :, 2] - rgb[:, :, 0]) / 255.0
    tint = np.clip((blueness - 0.02) / 0.10, 0.0, 1.0)

    alpha = np.clip(darkness * 0.75 + darkness * tint * 0.45, 0.0, 1.0)
    return np.clip((alpha - FLOOR) / (1.0 - FLOOR), 0.0, 1.0)


def seal_box(mask: np.ndarray, scale: int) -> tuple[int, int, int, int]:
    """Bounding box of the largest ink blob that does not touch the border.

    The border test is what separates the seal from the desk, the shadow and the
    edge of the sheet — those always run off the frame, the seal never does.
    """
    small = Image.fromarray((mask * 255).astype(np.uint8)).resize(
        (max(mask.shape[1] // scale, 1), max(mask.shape[0] // scale, 1)), Image.BOX
    )
    grid = np.asarray(small.filter(ImageFilter.MaxFilter(5))) > 40  # bridge letter gaps
    height, width = grid.shape

    seen = np.zeros(grid.shape, dtype=bool)
    best, best_box = 0, None
    for start_y in range(height):
        for start_x in range(width):
            if not grid[start_y, start_x] or seen[start_y, start_x]:
                continue
            queue = deque([(start_y, start_x)])
            seen[start_y, start_x] = True
            size, edge = 0, False
            y0 = y1 = start_y
            x0 = x1 = start_x
            while queue:
                y, x = queue.popleft()
                size += 1
                y0, y1, x0, x1 = min(y0, y), max(y1, y), min(x0, x), max(x1, x)
                edge |= y in (0, height - 1) or x in (0, width - 1)
                for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                    if 0 <= ny < height and 0 <= nx < width and grid[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        queue.append((ny, nx))
            if not edge and size > best:
                best, best_box = size, (x0, y0, x1 + 1, y1 + 1)

    if best_box is None:
        raise SystemExit(
            "No stamp-shaped blob found. Crop the photo down to the seal and retry."
        )
    return tuple(value * scale for value in best_box)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("photo", type=Path, help="photo or scan of the stamp")
    parser.add_argument("output", type=Path, help="PNG to write")
    parser.add_argument("--rotate", type=float, default=0.0,
                        help="degrees clockwise to turn the photo upright first")
    parser.add_argument("--size", type=int, default=1000, help="longest side of the PNG")
    args = parser.parse_args()

    img = load(args.photo.expanduser())
    if args.rotate:
        img = img.rotate(-args.rotate, expand=True, resample=Image.BICUBIC,
                         fillcolor=(255, 255, 255))

    alpha = ink_alpha(img)
    x0, y0, x1, y1 = seal_box(alpha > INK_MASK, max(1, max(img.size) // 300))
    pad = int((x1 - x0) * 0.03)
    x0, y0 = max(x0 - pad, 0), max(y0 - pad, 0)
    x1, y1 = min(x1 + pad, alpha.shape[1]), min(y1 + pad, alpha.shape[0])

    alpha = alpha[y0:y1, x0:x1]
    patch = np.asarray(img).astype(np.float32)[y0:y1, x0:x1]
    core = alpha > CORE_MASK
    if core.sum() < 50:
        raise SystemExit("Too little ink found — check the photo or the rotation.")
    ink = np.percentile(patch[core], 25, axis=0)

    out = np.zeros((*alpha.shape, 4), dtype=np.uint8)
    out[:, :, 0], out[:, :, 1], out[:, :, 2] = (int(value) for value in ink)
    out[:, :, 3] = (alpha * 255).astype(np.uint8)

    stamp = Image.fromarray(out, "RGBA")
    stamp.thumbnail((args.size, args.size), Image.LANCZOS)
    output = args.output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    stamp.save(output)

    print(f"{output} {stamp.size[0]}x{stamp.size[1]}")
    print(f"  ink rgb {[int(v) for v in ink]}, cropped from {x1 - x0}x{y1 - y0} px")


if __name__ == "__main__":
    main()
