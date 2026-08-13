#!/usr/bin/env python3
"""Stamp a stored company seal onto a PDF, anchored to the signature block.

By default the seal lands the way it is pressed by hand: overlapping the left end
of the signature and straddling the rule under it, slightly tilted and slightly
translucent, so the text underneath stays readable.

Seal images live in the user's own folder, never inside this plugin — a company
seal committed to a repository is a forgeable asset:
  ~/Documents/Stamps/
Override with the STAMPS_DIR environment variable.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import marks  # noqa: E402
from marks import fitz  # noqa: E402

DEFAULT_SIZE = 115.0  # points ≈ 40 mm, the usual diameter of a round company seal
DEFAULT_OPACITY = 0.85
DEFAULT_ROTATE = -6.0
MARGIN = 36.0


def stamp_rect(page, args, ratio: float, growth: float) -> fitz.Rect:
    """Where the seal goes, in the placement mode asked for."""
    width = min(args.size, page.rect.width * 0.5) * growth
    height = width / ratio

    if args.place == "corner":
        x1 = page.rect.width - MARGIN
        y1 = page.rect.height - MARGIN
        return fitz.Rect(x1 - width, y1 - height, x1, y1)

    anchor = marks.find_anchor(page, args.anchor)
    name_top = anchor["line"]["bbox"].y0
    rule_y, x_left, x_right = marks.baseline(page, anchor)
    if rule_y is None:
        rule_y = name_top - 6

    if args.place == "beside":
        # Clear of the signature: past the end of the rule, where the page is empty.
        # There is never room to the left — the block already starts at the margin.
        room = page.rect.width - MARGIN - (x_right + 12)
        if room < width:
            raise SystemExit(
                f"Only {room:.0f} pt free beside the signature block, seal needs "
                f"{width:.0f} pt. Use a smaller --size, --place over, or --x/--y."
            )
        x0 = x_right + 12
    else:
        # Overlap the first strokes of the signature, as a hand-pressed seal does.
        x0 = x_left - width * 0.08
    x0 = min(max(x0, MARGIN), page.rect.width - MARGIN - width)

    # Cross the rule from above, but never cover the printed name under it.
    bottom = min(rule_y + height * 0.18, name_top - 3)
    y0 = min(max(bottom - height, 0.0), page.rect.height - height)
    return fitz.Rect(x0, y0, x0 + width, y0 + height)


def apply(pdf: Path, image: Path, args) -> Path:
    doc = fitz.open(pdf)
    page = marks.page_of(doc, args.page)
    data, ratio, growth = marks.transform(image, args.opacity, args.rotate)

    if args.x is not None and args.y is not None:
        width = min(args.size, page.rect.width * 0.5) * growth
        rect = fitz.Rect(args.x, args.y, args.x + width, args.y + width / ratio)
    else:
        rect = stamp_rect(page, args, ratio, growth)

    page.insert_image(rect, stream=data, keep_proportion=True, overlay=True)
    out = marks.save(doc, pdf, args.output, args.suffix, args.force)
    doc.close()

    print(f"{out}")
    print(f"  page {args.page}, {args.place} placement, rect ({rect.x0:.0f}, {rect.y0:.0f}) "
          f"{rect.width:.0f}x{rect.height:.0f} pt")
    if args.preview:
        print(f"  preview: {marks.preview(out, args.page, rect, margins=(40, 40, 260, 60))}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stamp a stored company seal onto a PDF, anchored to the signature block."
    )
    parser.add_argument("pdfs", nargs="*", type=Path, help="PDF files to stamp")
    parser.add_argument("-s", "--stamp", help="stamp name, e.g. acme")
    parser.add_argument("--image", type=Path, help="use this image instead of the store")
    parser.add_argument("--place", choices=("over", "beside", "corner"), default="over",
                        help="over the signature (default), beside it, or page corner")
    parser.add_argument("--size", type=float, default=DEFAULT_SIZE,
                        help=f"seal diameter in points (default {DEFAULT_SIZE:.0f} ≈ 40 mm)")
    parser.add_argument("--opacity", type=float, default=DEFAULT_OPACITY,
                        help=f"0..1, lower lets more text through (default {DEFAULT_OPACITY})")
    parser.add_argument("--rotate", type=float, default=DEFAULT_ROTATE,
                        help=f"tilt in degrees, counter-clockwise (default {DEFAULT_ROTATE:.0f})")
    parser.add_argument("--page", type=int, default=1, help="page number, 1-indexed")
    parser.add_argument("--anchor", help="exact text to anchor to, e.g. '(Jane Doe)'")
    parser.add_argument("--x", type=float, help="manual left edge in points (top-left origin)")
    parser.add_argument("--y", type=float, help="manual top edge in points (top-left origin)")
    parser.add_argument("--output", help="output directory (default: alongside the input)")
    parser.add_argument("--suffix", default="-stamped", help="output filename suffix")
    parser.add_argument("--preview", action="store_true",
                        help="also render a PNG crop of the stamped area for visual check")
    parser.add_argument("--force", action="store_true", help="overwrite an existing output")
    parser.add_argument("--list", action="store_true", dest="list_assets",
                        help="list stamps in the store and exit")
    parser.add_argument("--register", type=Path, metavar="IMAGE",
                        help="copy this image into the store under --stamp, then exit")
    args = parser.parse_args()

    if args.list_assets:
        marks.STAMPS.print_listing()
        return

    if args.register:
        if not args.stamp:
            parser.error("--register needs --stamp to name the seal")
        print(f"stored: {marks.STAMPS.register(args.register.expanduser(), args.stamp)}")
        return

    if not args.pdfs:
        parser.error("no PDF given")
    if not args.stamp and not args.image:
        parser.error("pass --stamp (or --image)")
    if not 0.0 < args.opacity <= 1.0:
        parser.error("--opacity must be between 0 and 1")

    if args.image:
        image = args.image.expanduser()
        if not image.is_file():
            raise SystemExit(f"{image} not found")
    else:
        image, resolved = marks.STAMPS.resolve(args.stamp)
        print(f"stamp: {resolved}")

    for pdf in args.pdfs:
        pdf = pdf.expanduser()
        if not pdf.is_file():
            raise SystemExit(f"{pdf} not found")
        apply(pdf, image, args)


if __name__ == "__main__":
    main()
