#!/usr/bin/env python3
"""Stamp a stored handwritten signature onto a PDF, anchored to the signature block.

Finds the signature line automatically: locates the name line at the bottom of the
page (e.g. "(Jane Doe)"), takes the empty gap above it, and places the PNG there —
sitting on the drawn rule if there is one.

Signature images live in the user's own folder, never inside this plugin:
  ~/Documents/Signatures/
Override with the SIGNATURES_DIR environment variable.
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

DEFAULT_WIDTH = 165.0


def signature_rect(page, anchor: dict, img_ratio: float, width: float) -> fitz.Rect:
    """Compute where to draw the signature: in the gap, sitting on the rule."""
    gap_top, gap_bottom = marks.gap(anchor)
    gap_height = gap_bottom - gap_top

    width = min(width, page.rect.width * 0.45)
    height = width / img_ratio
    # Allow a little overflow past the gap — a real signature crosses the line.
    if height > gap_height * 1.15:
        height = gap_height * 1.15
        width = height * img_ratio

    rule_y, x_left, _ = marks.baseline(page, anchor)
    if rule_y is not None:
        # Sit on the rule, letting the descenders cross it slightly.
        y0 = rule_y - height * 0.88
    else:
        y0 = gap_top + (gap_height - height) / 2
    y0 = max(y0, gap_top - height * 0.05)

    x0 = x_left + 3
    return fitz.Rect(x0, y0, x0 + width, y0 + height)


def sign(pdf: Path, image: Path, args) -> Path:
    doc = fitz.open(pdf)
    page = marks.page_of(doc, args.page)
    ratio = marks.image_ratio(image)

    if args.x is not None and args.y is not None:
        width = min(args.width, page.rect.width * 0.45)
        rect = fitz.Rect(args.x, args.y, args.x + width, args.y + width / ratio)
    else:
        rect = signature_rect(page, marks.find_anchor(page, args.anchor), ratio, args.width)

    page.insert_image(rect, filename=str(image), keep_proportion=True, overlay=True)
    out = marks.save(doc, pdf, args.output, args.suffix, args.force)
    doc.close()

    print(f"{out}")
    print(f"  page {args.page}, rect ({rect.x0:.0f}, {rect.y0:.0f}) "
          f"{rect.width:.0f}x{rect.height:.0f} pt")
    if args.preview:
        print(f"  preview: {marks.preview(out, args.page, rect)}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stamp a stored signature onto a PDF, anchored to the signature block."
    )
    parser.add_argument("pdfs", nargs="*", type=Path, help="PDF files to sign")
    parser.add_argument("-p", "--person", help="signature name, e.g. jane / john")
    parser.add_argument("--image", type=Path, help="use this image instead of the store")
    parser.add_argument("--light", action="store_true",
                        help="use the base signature instead of the darker '-dark' variant")
    parser.add_argument("--width", type=float, default=DEFAULT_WIDTH,
                        help=f"signature width in points (default {DEFAULT_WIDTH:.0f})")
    parser.add_argument("--page", type=int, default=1, help="page number, 1-indexed")
    parser.add_argument("--anchor", help="exact text to anchor to, e.g. '(Jane Doe)'")
    parser.add_argument("--x", type=float, help="manual left edge in points (top-left origin)")
    parser.add_argument("--y", type=float, help="manual top edge in points (top-left origin)")
    parser.add_argument("--output", help="output directory (default: alongside the input)")
    parser.add_argument("--suffix", default="-signed", help="output filename suffix")
    parser.add_argument("--preview", action="store_true",
                        help="also render a PNG crop of the signed area for visual check")
    parser.add_argument("--force", action="store_true", help="overwrite an existing output")
    parser.add_argument("--list", action="store_true", dest="list_assets",
                        help="list signatures in the store and exit")
    parser.add_argument("--register", type=Path, metavar="IMAGE",
                        help="copy this image into the store under --person, then exit")
    args = parser.parse_args()

    if args.list_assets:
        marks.SIGNATURES.print_listing()
        return

    if args.register:
        if not args.person:
            parser.error("--register needs --person to name the signature")
        print(f"stored: {marks.SIGNATURES.register(args.register.expanduser(), args.person)}")
        return

    if not args.pdfs:
        parser.error("no PDF given")
    if not args.person and not args.image:
        parser.error("pass --person (or --image)")

    if args.image:
        image = args.image.expanduser()
        if not image.is_file():
            raise SystemExit(f"{image} not found")
    else:
        image, resolved = marks.SIGNATURES.resolve(args.person, prefer_dark=not args.light)
        print(f"signature: {resolved}")

    for pdf in args.pdfs:
        pdf = pdf.expanduser()
        if not pdf.is_file():
            raise SystemExit(f"{pdf} not found")
        sign(pdf, image, args)


if __name__ == "__main__":
    main()
