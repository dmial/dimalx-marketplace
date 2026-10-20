#!/usr/bin/env python3
"""Prepare scanned PDFs for reading: page images, reading strips and Apple Vision OCR.

Usage: python3 prepare.py [PDF ...] [--force]   (run from the project folder)
Without arguments processes every PDF in the inbox. Unchanged PDFs (same sha256) are skipped.

Writes (<log> is logs/scan-to-markdown):
  <log>/cache/pages/<doc>/p<N>.png        page image at the scan's native resolution (lossless)
  <log>/cache/strips/<doc>/p<N>_s<K>.png  overlapping horizontal strips for reader B
  <log>/ocr/<doc>/p<N>.json               Apple Vision OCR (text + boxes), with a block printed
                                          the other way up read in a second pass
  <log>/manifest.json                     documents, pages, checksums, where each scan and result lives
"""
from __future__ import annotations

import argparse
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageStat

sys.path.insert(0, str(Path(__file__).resolve().parent))  # lets `python3 -I` find common.py
from common import (
    MANIFEST_PATH,
    INBOX,
    SKILL_DIR,
    CACHE,
    SURE,
    load_config,
    load_json,
    load_manifest,
    page_paths,
    rel,
    require_project,
    save_json,
    sha256_file,
    turned_lines,
)

OCR_SRC = SKILL_DIR / "scripts" / "vision_ocr.swift"
# Named by its source: an updated vision_ocr.swift always gets its own build.
OCR_BIN = CACHE / "bin" / f"vision_ocr-{sha256_file(OCR_SRC)[:12]}" if CACHE else None
# Mean grey-level difference (0-255) above which the embedded image is not trusted to equal the page.
RENDER_MISMATCH_LIMIT = 15


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def ensure_ocr_binary() -> None:
    if OCR_BIN.exists():
        return
    OCR_BIN.parent.mkdir(parents=True, exist_ok=True)
    print("compiling Apple Vision OCR tool…")
    run(["swiftc", "-O", str(OCR_SRC), "-o", str(OCR_BIN)])


def read_turned_text(pages: list[tuple[Path, Path]]) -> None:
    """Add to each page's OCR the lines of a block printed the other way up (`turned_lines`).
    Vision reads a page in the orientation most of its text has; it reads again a copy of the page
    with the lines it read surely painted white, on which such a block is most of the text."""
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        jobs: list[str] = []
        for n, (image_path, ocr_path) in enumerate(pages):
            image = Image.open(image_path).convert("RGB")
            draw = ImageDraw.Draw(image)
            for obs in load_json(ocr_path)["observations"]:
                if obs["confidence"] >= SURE:
                    x, y, w, h = obs["bbox"]
                    draw.rectangle((x - 4, y - 4, x + w + 4, y + h + 4), fill="white")
            image.save(tmp / f"{n}.png")
            jobs += [str(tmp / f"{n}.png"), str(tmp / f"{n}.json")]
        run([str(OCR_BIN), *jobs])
        for n, (_, ocr_path) in enumerate(pages):
            ocr = load_json(ocr_path)
            turned = turned_lines(ocr["observations"], load_json(tmp / f"{n}.json")["observations"])
            if turned:
                ocr["observations"] += turned
                save_json(ocr_path, ocr)
                print(f"{rel(ocr_path)}: {len(turned)} lines printed the other way up")


def pdf_pages(pdf: Path) -> list[tuple[float, float]]:
    """Page sizes in points."""
    count = int(re.search(r"^Pages:\s+(\d+)", run(["pdfinfo", str(pdf)]), re.M).group(1))
    info = run(["pdfinfo", "-f", "1", "-l", str(count), str(pdf)])
    sizes = re.findall(r"^Page\s+\d+\s+size:\s+([\d.]+) x ([\d.]+) pts", info, re.M)
    return [(float(w), float(h)) for w, h in sizes]


def extract_page(pdf: Path, page: int, size_pt: tuple[float, float], dest: Path) -> str:
    """Save one page as PNG; returns how it was obtained."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        run(["pdfimages", "-png", "-f", str(page), "-l", str(page), str(pdf), str(tmp / "img")])
        run(["pdftoppm", "-r", "24", "-png", "-f", str(page), "-l", str(page), str(pdf), str(tmp / "preview")])
        images = sorted(tmp.glob("img-*.png"))
        preview = Image.open(next(tmp.glob("preview*.png"))).convert("L")
        if len(images) == 1:
            embedded = Image.open(images[0])
            shrunk = embedded.convert("L").resize(preview.size, Image.Resampling.LANCZOS)
            diff = ImageStat.Stat(ImageChops.difference(shrunk, preview)).mean[0]
            if diff <= RENDER_MISMATCH_LIMIT:
                shutil.copyfile(images[0], dest)
                return "embedded image (lossless)"
        # Fallback: render the page so its long edge is about 2500 px.
        dpi = round(72 * 2500 / max(size_pt))
        run(["pdftoppm", "-r", str(dpi), "-png", "-singlefile", "-f", str(page), "-l", str(page), str(pdf), str(tmp / "render")])
        shutil.copyfile(tmp / "render.png", dest)
        return f"rendered at {dpi} dpi"


def make_strips(image_path: Path, strips_dir: Path, page: int, height: int, min_overlap: int) -> list[dict]:
    image = Image.open(image_path)
    width, total = image.size
    count = 1 if total <= height else math.ceil((total - min_overlap) / (height - min_overlap))
    starts = [0] if count == 1 else [round(i * (total - height) / (count - 1)) for i in range(count)]
    strips_dir.mkdir(parents=True, exist_ok=True)
    for old in strips_dir.glob(f"p{page}_s*.png"):
        old.unlink()
    strips = []
    for k, y0 in enumerate(starts, 1):
        y1 = min(total, y0 + height)
        path = strips_dir / f"p{page}_s{k}.png"
        image.crop((0, y0, width, y1)).save(path)
        strips.append({"path": rel(path), "y0": y0, "y1": y1})
    return strips


def prepare_pdf(pdf: Path, config: dict, manifest: dict, force: bool) -> None:
    doc = pdf.stem
    digest = sha256_file(pdf)
    entry = manifest["documents"].get(doc)
    if entry and entry["sha256"] == digest and not force:
        outputs_ok = all(
            (page_paths(doc, p["page"])["image"]).exists() and (page_paths(doc, p["page"])["ocr"]).exists()
            for p in entry["pages"]
        )
        if outputs_ok:
            print(f"skip (unchanged): {doc}")
            return

    name = re.match(config["name_pattern"], doc) if config.get("name_pattern") else None
    pages = []
    ocr_jobs: list[str] = []
    for number, size_pt in enumerate(pdf_pages(pdf), 1):
        paths = page_paths(doc, number)
        method = extract_page(pdf, number, size_pt, paths["image"])
        width, height = Image.open(paths["image"]).size
        strips = make_strips(
            paths["image"], paths["strips_dir"], number, config["strips"]["height_px"], config["strips"]["min_overlap_px"]
        )
        ocr_jobs += [str(paths["image"]), str(paths["ocr"])]
        pages.append({
            "page": number,
            "image": rel(paths["image"]),
            "image_sha256": sha256_file(paths["image"]),
            "width": width,
            "height": height,
            "extraction": method,
            "strips": strips,
            "ocr": rel(paths["ocr"]),
        })
        print(f"{doc} p{number}: {width}x{height}, {method}, {len(strips)} strips")

    run([str(OCR_BIN), *ocr_jobs])
    read_turned_text([(page_paths(doc, p["page"])["image"], page_paths(doc, p["page"])["ocr"]) for p in pages])
    fields = name.groupdict() if name else {}
    manifest["documents"][doc] = {
        "pdf": rel(pdf),
        "sha256": digest,
        "date": fields.get("date") or "",
        "source": fields.get("source") or "",
        "title": fields.get("title") or doc,
        "pages": pages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare scanned PDFs: page images, strips, Apple Vision OCR.")
    parser.add_argument("pdfs", nargs="*", type=Path, help="PDF files (default: every PDF in the inbox)")
    parser.add_argument("--force", action="store_true", help="redo even if the PDF is unchanged")
    args = parser.parse_args()
    require_project()

    config = load_config()
    pdfs = [p.resolve() for p in args.pdfs] or sorted(INBOX.glob("*.pdf"))
    manifest = load_manifest()
    ensure_ocr_binary()
    for pdf in pdfs:
        prepare_pdf(pdf, config, manifest, args.force)
        manifest["documents"] = dict(sorted(manifest["documents"].items()))
        save_json(MANIFEST_PATH, manifest)
    print(f"manifest: {rel(MANIFEST_PATH)} ({len(manifest['documents'])} documents)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
