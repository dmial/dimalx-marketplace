#!/usr/bin/env python3
"""Stamp a stored handwritten signature onto a PDF, anchored to the signature block.

Finds the signature line automatically: locates the name line at the bottom of the
page (e.g. "(Jane Doe)"), takes the empty gap above it, and places the PNG there —
sitting on the drawn rule if there is one.

Signature images live in the user's own folder, never inside this plugin:
  ~/Documents/Signatures/   (or ~/Documents/Assinaturas/)
Override with the SIGNATURES_DIR environment variable.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("PyMuPDF not installed. Install with: pip3 install pymupdf")

DEFAULT_STORES = [
    Path.home() / "Documents" / "Signatures",
    Path.home() / "Documents" / "Assinaturas",
]
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}

NAME_LINE = re.compile(r"^\(.+\)$")
RULE_LINE = re.compile(r"^[_\-–—\.\s]{5,}$")
DEFAULT_WIDTH = 165.0
MIN_GAP = 18.0
ANCHOR_ZONE = 0.35  # ignore name-like lines in the top third of the page


# --- signature store -------------------------------------------------------


def stores() -> list[Path]:
    """Directories to search for signature assets, most specific first."""
    override = os.environ.get("SIGNATURES_DIR")
    if override:
        return [Path(p).expanduser() for p in override.split(os.pathsep) if p]
    return DEFAULT_STORES


def primary_store() -> Path:
    """Where newly registered signatures are written."""
    return stores()[0]


def list_signatures() -> list[dict]:
    """Return every signature asset found in the configured stores."""
    found = []
    for directory in stores():
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() in IMAGE_SUFFIXES:
                found.append({"name": path.stem, "display_name": "", "path": path})
            elif path.suffix == ".json":
                # pdf-toolkit style asset: base64 image inside a JSON envelope.
                try:
                    data = json.loads(path.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                if data.get("image_data_b64"):
                    found.append(
                        {
                            "name": data.get("name", path.stem),
                            "display_name": data.get("display_name", ""),
                            "path": path,
                        }
                    )
    return found


def resolve_signature(person: str, prefer_dark: bool) -> tuple[Path, str]:
    """Resolve a person name to an image file on disk.

    Returns (image_path, resolved_name). JSON assets are decoded into a temp PNG.
    """
    available = {sig["name"]: sig for sig in list_signatures()}

    candidates = [person]
    if prefer_dark and not person.endswith("-escura"):
        candidates.insert(0, f"{person}-escura")

    for name in candidates:
        sig = available.get(name)
        if sig is None:
            continue
        path = sig["path"]
        if path.suffix != ".json":
            return path, name
        data = json.loads(path.read_text())
        suffix = ".jpg" if "jpeg" in data.get("image_mime", "") else ".png"
        tmp = Path(tempfile.mkdtemp(prefix="assinar-")) / f"{name}{suffix}"
        tmp.write_bytes(base64.b64decode(data["image_data_b64"]))
        return tmp, name

    known = ", ".join(sorted(available)) or "(none)"
    searched = ", ".join(str(d) for d in stores())
    raise SystemExit(
        f"Signature '{person}' not found. Available: {known}\nSearched: {searched}"
    )


def register(image: Path, name: str) -> Path:
    """Copy a signature image into the store under the given name."""
    if not image.is_file():
        raise SystemExit(f"{image} not found")
    if image.suffix.lower() not in IMAGE_SUFFIXES:
        raise SystemExit("Signature must be a PNG or JPEG (PNG with transparency is best).")
    store = primary_store()
    store.mkdir(parents=True, exist_ok=True)
    target = store / f"{name}{image.suffix.lower()}"
    shutil.copy2(image, target)
    return target


# --- page geometry ---------------------------------------------------------


def text_lines(page) -> list[dict]:
    """Flatten the page into text lines with bboxes, in reading order."""
    lines = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            text = "".join(span["text"] for span in line["spans"])
            text = text.replace("\xa0", " ").strip()
            if text:
                lines.append({"text": text, "bbox": fitz.Rect(line["bbox"])})
    lines.sort(key=lambda item: (round(item["bbox"].y0, 1), item["bbox"].x0))
    return lines


def find_anchor(page, anchor: str | None) -> dict:
    """Find the line the signature should sit above (the name line, by default)."""
    lines = text_lines(page)
    if not lines:
        raise SystemExit("No text on the page — use --x/--y to place the signature manually.")

    if anchor:
        matches = [ln for ln in lines if anchor.lower() in ln["text"].lower()]
        if not matches:
            raise SystemExit(f"Anchor {anchor!r} not found on the page.")
    else:
        floor = page.rect.height * ANCHOR_ZONE
        matches = [
            ln for ln in lines if ln["bbox"].y0 > floor and NAME_LINE.match(ln["text"])
        ]
        if not matches:
            raise SystemExit(
                "No name line like '(Full Name)' found below the top third. "
                "Pass --ancora with the exact text to anchor to, or --x/--y."
            )
    target = matches[-1]

    # An underscore run right above the name is the signature rule, not content.
    index = lines.index(target)
    rule = None
    while index > 0 and RULE_LINE.match(lines[index - 1]["text"]):
        rule = lines[index - 1]
        index -= 1
    previous = lines[index - 1] if index > 0 else None
    return {"line": target, "previous": previous, "rule": rule}


def horizontal_rule(page, top: float, bottom: float, min_len: float = 100.0):
    """Return the y of a drawn horizontal rule inside the gap, if there is one."""
    best = None
    for drawing in page.get_drawings():
        rect = drawing["rect"]
        if rect.height > 3 or rect.width < min_len:
            continue
        y = (rect.y0 + rect.y1) / 2
        if top <= y <= bottom and (best is None or y > best):
            best = y
    return best


def signature_rect(page, anchor: dict, img_ratio: float, width: float) -> fitz.Rect:
    """Compute where to draw the signature: in the gap, sitting on the rule."""
    name_line = anchor["line"]["bbox"]
    previous = anchor["previous"]
    text_rule = anchor["rule"]
    gap_top = (previous["bbox"].y1 if previous else name_line.y0 - 80) + 2
    gap_bottom = (text_rule["bbox"].y1 if text_rule else name_line.y0) - 1
    gap = gap_bottom - gap_top

    if gap < MIN_GAP:
        gap_top = gap_bottom - 60
        gap = gap_bottom - gap_top

    width = min(width, page.rect.width * 0.45)
    height = width / img_ratio
    # Allow a little overflow past the gap — a real signature crosses the line.
    if height > gap * 1.15:
        height = gap * 1.15
        width = height * img_ratio

    if text_rule:
        rule_y = (text_rule["bbox"].y0 + text_rule["bbox"].y1) / 2
    else:
        rule_y = horizontal_rule(page, gap_top, gap_bottom + 4)

    if rule_y is not None:
        # Sit on the rule, letting the descenders cross it slightly.
        y0 = rule_y - height * 0.88
    else:
        y0 = gap_top + (gap - height) / 2
    y0 = max(y0, gap_top - height * 0.05)

    x0 = (text_rule["bbox"].x0 if text_rule else name_line.x0) + 3
    return fitz.Rect(x0, y0, x0 + width, y0 + height)


# --- main ------------------------------------------------------------------


def sign(pdf: Path, image: Path, args) -> Path:
    doc = fitz.open(pdf)
    page = doc[args.pagina - 1]

    with fitz.open(image) as img:
        ratio = img[0].rect.width / img[0].rect.height

    if args.x is not None and args.y is not None:
        width = min(args.largura, page.rect.width * 0.45)
        rect = fitz.Rect(args.x, args.y, args.x + width, args.y + width / ratio)
    else:
        rect = signature_rect(page, find_anchor(page, args.ancora), ratio, args.largura)

    page.insert_image(rect, filename=str(image), keep_proportion=True, overlay=True)

    out_dir = Path(args.saida).expanduser() if args.saida else pdf.parent
    out = out_dir / f"{pdf.stem}{args.sufixo}.pdf"
    if out.exists() and not args.forcar:
        doc.close()
        raise SystemExit(f"{out} already exists. Use --forcar to overwrite.")
    out_dir.mkdir(parents=True, exist_ok=True)
    doc.save(out, garbage=4, deflate=True)

    print(f"{out}")
    print(f"  page {args.pagina}, rect ({rect.x0:.0f}, {rect.y0:.0f}) "
          f"{rect.width:.0f}x{rect.height:.0f} pt")

    if args.preview:
        clip = fitz.Rect(
            max(rect.x0 - 30, 0),
            max(rect.y0 - 60, 0),
            min(rect.x1 + 320, page.rect.width),
            min(rect.y1 + 40, page.rect.height),
        )
        signed = fitz.open(out)
        pix = signed[args.pagina - 1].get_pixmap(clip=clip, dpi=150)
        preview = out.with_name(f"{out.stem}-preview.png")
        pix.save(preview)
        signed.close()
        print(f"  preview: {preview}")

    doc.close()
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stamp a stored signature onto a PDF, anchored to the signature block."
    )
    parser.add_argument("pdfs", nargs="*", type=Path, help="PDF files to sign")
    parser.add_argument("-p", "--pessoa", help="signature name, e.g. ana / john")
    parser.add_argument("--imagem", type=Path, help="use this image instead of the store")
    parser.add_argument("--clara", action="store_true",
                        help="use the base signature instead of the darker '-escura' variant")
    parser.add_argument("--largura", type=float, default=DEFAULT_WIDTH,
                        help=f"signature width in points (default {DEFAULT_WIDTH:.0f})")
    parser.add_argument("--pagina", type=int, default=1, help="page number, 1-indexed")
    parser.add_argument("--ancora", help="exact text to anchor to, e.g. '(Jane Doe)'")
    parser.add_argument("--x", type=float, help="manual left edge in points (top-left origin)")
    parser.add_argument("--y", type=float, help="manual top edge in points (top-left origin)")
    parser.add_argument("--saida", help="output directory (default: alongside the input)")
    parser.add_argument("--sufixo", default="-assinada", help="output filename suffix")
    parser.add_argument("--preview", action="store_true",
                        help="also render a PNG crop of the signed area for visual check")
    parser.add_argument("--forcar", action="store_true", help="overwrite an existing output")
    parser.add_argument("--listar", action="store_true",
                        help="list signatures in the store and exit")
    parser.add_argument("--registar", type=Path, metavar="IMAGE",
                        help="copy this image into the store under --pessoa, then exit")
    args = parser.parse_args()

    if args.listar:
        print(f"store: {', '.join(str(d) for d in stores())}")
        found = list_signatures()
        if not found:
            print("(empty — add a PNG with --registar)")
        for sig in found:
            label = f" — {sig['display_name']}" if sig["display_name"] else ""
            print(f"  {sig['name']}{label}")
        return

    if args.registar:
        if not args.pessoa:
            parser.error("--registar needs --pessoa to name the signature")
        print(f"stored: {register(args.registar.expanduser(), args.pessoa)}")
        return

    if not args.pdfs:
        parser.error("no PDF given")
    if not args.pessoa and not args.imagem:
        parser.error("pass --pessoa (or --imagem)")

    if args.imagem:
        image = args.imagem.expanduser()
        if not image.is_file():
            raise SystemExit(f"{image} not found")
    else:
        image, resolved = resolve_signature(args.pessoa, prefer_dark=not args.clara)
        print(f"signature: {resolved}")

    for pdf in args.pdfs:
        pdf = pdf.expanduser()
        if not pdf.is_file():
            raise SystemExit(f"{pdf} not found")
        sign(pdf, image, args)


if __name__ == "__main__":
    main()
