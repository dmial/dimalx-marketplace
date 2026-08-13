"""Shared plumbing for the pdf-marks skills: asset stores, page geometry, output.

Both skills stamp an image onto the signature block of a PDF — one a handwritten
signature, one a company seal. The part they share is finding that block: the name
line at the bottom of the page, the rule above it, and the empty gap in between.

Assets (signature and stamp images) live in the user's own folders, never inside
this plugin — a seal committed to a repository is a forgeable asset.
"""

from __future__ import annotations

import base64
import io
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

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
NAME_LINE = re.compile(r"^\(.+\)$")
RULE_LINE = re.compile(r"^[_\-–—\.\s]{5,}$")
MIN_GAP = 18.0
ANCHOR_ZONE = 0.35  # ignore name-like lines in the top third of the page
DARK_SUFFIXES = ("-dark", "-escura")  # -escura: assets registered before 0.2.0


# --- asset store -----------------------------------------------------------


class Store:
    """A folder of mark images in the user's Documents, overridable by env var."""

    def __init__(self, env_var: str, defaults: list[Path], label: str):
        self.env_var = env_var
        self.defaults = defaults
        self.label = label

    def dirs(self) -> list[Path]:
        override = os.environ.get(self.env_var)
        if override:
            return [Path(p).expanduser() for p in override.split(os.pathsep) if p]
        return self.defaults

    def primary(self) -> Path:
        """Where newly registered assets are written."""
        return self.dirs()[0]

    def list(self) -> list[dict]:
        """Every asset found in the configured folders."""
        found = []
        for directory in self.dirs():
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

    def resolve(self, name: str, prefer_dark: bool = False) -> tuple[Path, str]:
        """Resolve an asset name to an image on disk, decoding JSON assets to a temp file."""
        available = {asset["name"]: asset for asset in self.list()}

        candidates = [name]
        if prefer_dark and not name.endswith(DARK_SUFFIXES):
            candidates = [f"{name}{suffix}" for suffix in DARK_SUFFIXES] + candidates

        for candidate in candidates:
            asset = available.get(candidate)
            if asset is None:
                continue
            path = asset["path"]
            if path.suffix != ".json":
                return path, candidate
            data = json.loads(path.read_text())
            suffix = ".jpg" if "jpeg" in data.get("image_mime", "") else ".png"
            tmp = Path(tempfile.mkdtemp(prefix="pdf-marks-")) / f"{candidate}{suffix}"
            tmp.write_bytes(base64.b64decode(data["image_data_b64"]))
            return tmp, candidate

        known = ", ".join(sorted(available)) or "(none)"
        searched = ", ".join(str(d) for d in self.dirs())
        raise SystemExit(
            f"{self.label} '{name}' not found. Available: {known}\nSearched: {searched}"
        )

    def register(self, image: Path, name: str) -> Path:
        """Copy an image into the store under the given name."""
        if not image.is_file():
            raise SystemExit(f"{image} not found")
        if image.suffix.lower() not in IMAGE_SUFFIXES:
            raise SystemExit(f"{self.label} must be a PNG or JPEG (PNG with transparency is best).")
        directory = self.primary()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{name}{image.suffix.lower()}"
        shutil.copy2(image, target)
        return target

    def print_listing(self) -> None:
        print(f"store: {', '.join(str(d) for d in self.dirs())}")
        found = self.list()
        if not found:
            print("(empty — add a PNG with --register)")
        for asset in found:
            label = f" — {asset['display_name']}" if asset["display_name"] else ""
            print(f"  {asset['name']}{label}")


SIGNATURES = Store(
    "SIGNATURES_DIR",
    [Path.home() / "Documents" / "Signatures"],
    "Signature",
)
STAMPS = Store(
    "STAMPS_DIR",
    [Path.home() / "Documents" / "Stamps"],
    "Stamp",
)


# --- page geometry ---------------------------------------------------------


def page_of(doc, number: int):
    """The 1-indexed page, or a readable error instead of an IndexError."""
    total = doc.page_count
    if not 1 <= number <= total:
        doc.close()
        raise SystemExit(f"Page {number} is out of range — the document has {total}.")
    return doc[number - 1]


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
    """Find the line the mark should sit above (the name line, by default)."""
    lines = text_lines(page)
    if not lines:
        raise SystemExit("No text on the page — use --x/--y to place the mark manually.")

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
                "Pass --anchor with the exact text to anchor to, or --x/--y."
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


def gap(anchor: dict) -> tuple[float, float]:
    """Vertical space the mark may occupy: below the previous line, above the rule."""
    name_line = anchor["line"]["bbox"]
    previous = anchor["previous"]
    text_rule = anchor["rule"]
    top = (previous["bbox"].y1 if previous else name_line.y0 - 80) + 2
    bottom = (text_rule["bbox"].y1 if text_rule else name_line.y0) - 1
    if bottom - top < MIN_GAP:
        top = bottom - 60
    return top, bottom


def baseline(page, anchor: dict) -> tuple[float | None, float, float]:
    """The signature rule: its y (None if there is none) and the x it spans."""
    text_rule = anchor["rule"]
    if text_rule:
        rect = text_rule["bbox"]
        return (rect.y0 + rect.y1) / 2, rect.x0, rect.x1
    top, bottom = gap(anchor)
    name = anchor["line"]["bbox"]
    return horizontal_rule(page, top, bottom + 4), name.x0, name.x1


# --- image + output --------------------------------------------------------


def image_ratio(path: Path) -> float:
    with fitz.open(path) as img:
        return img[0].rect.width / img[0].rect.height


def transform(path: Path, opacity: float, rotate: float) -> tuple[bytes, float, float]:
    """Rotate and fade an image in memory.

    Returns (PNG bytes, width/height ratio, growth) — rotating expands the canvas,
    so growth is how much wider the result is than the original. Scale the target
    rectangle by it and the mark keeps the size that was asked for.
    """
    try:
        from PIL import Image
    except ImportError:
        sys.exit("Pillow not installed. Install with: pip3 install pillow")

    img = Image.open(path).convert("RGBA")
    original_width = img.width
    if rotate:
        img = img.rotate(rotate, expand=True, resample=Image.BICUBIC)
    if opacity < 1.0:
        img.putalpha(img.getchannel("A").point(lambda v: int(v * opacity)))
    buffer = io.BytesIO()
    img.save(buffer, "PNG")
    return buffer.getvalue(), img.width / img.height, img.width / original_width


def save(doc, pdf: Path, out_dir: str | None, suffix: str, force: bool) -> Path:
    """Write the marked document next to the original, never over it."""
    directory = Path(out_dir).expanduser() if out_dir else pdf.parent
    out = directory / f"{pdf.stem}{suffix}.pdf"
    if out.exists() and not force:
        doc.close()
        raise SystemExit(f"{out} already exists. Use --force to overwrite.")
    directory.mkdir(parents=True, exist_ok=True)
    doc.save(out, garbage=4, deflate=True)
    return out


def preview(out: Path, page_no: int, rect, margins=(30, 60, 320, 40)) -> Path:
    """Render a PNG crop around the mark so the placement can be checked by eye."""
    left, top, right, bottom = margins
    doc = fitz.open(out)
    page = doc[page_no - 1]
    clip = fitz.Rect(
        max(rect.x0 - left, 0),
        max(rect.y0 - top, 0),
        min(rect.x1 + right, page.rect.width),
        min(rect.y1 + bottom, page.rect.height),
    )
    path = out.with_name(f"{out.stem}-preview.png")
    page.get_pixmap(clip=clip, dpi=150).save(path)
    doc.close()
    return path
