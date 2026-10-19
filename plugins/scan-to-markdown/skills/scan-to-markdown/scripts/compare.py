#!/usr/bin/env python3
"""Compare the two transcriptions of each page with each other and with Apple Vision OCR.

For every page writes logs/scan-to-markdown/compare/<doc>/p<N>.json: every discrepancy with its exact place in
text A, the variant from text B, what Vision saw there, and enlarged crops of the scan
(in the cache). Discrepancies are decided later by the arbiter; build.py assembles the final text.

Kinds of discrepancies:
  number / flag / text  A and B differ here (flag = arrows, triangles and signs like < > ± % * !)
  cells                 table cell borders "|" differ: a value may sit in another column
  unsure                only one reader marked this place as uncertain [?…]
  order / moved         same tokens, different order, no cell border involved: no decision needed
  vision_check          A and B agree on a number that Vision did not see in that line
  vision_extra          Vision found a number that neither A nor B has
Vision checks skip service information (stamps, requisites, certificates, footers): two agreeing
readers are enough there. A number counts as seen in any form Vision gives it: "3, 24" as "3,24",
"I25.1" as "125.1", the "3" of "СТ3" as "СТЗ". Every discrepancy has a zone, "service" when both
readers' text there is service information: the arbiter decides those with a quick look.

A discrepancy that touches a bracketed group such as [от руки: ✓], [печать: …] or (?) always
covers the whole group on both sides, so choosing one reader's variant replaces whole groups.
Punctuation glued to the end of a discrepancy joins it when the readers differ there
("[?азн:]" against "[?дан]:"), so choosing B neither loses nor doubles a colon.
The comparison id covers the inputs and the resulting list of discrepancies: if either changes,
earlier arbiter decisions no longer apply. So a page the arbiter has decided keeps its comparison
when this logic changes, as long as its readings and OCR stay the same: the text built from the
decisions and the reviewer's marks stay valid. --redo compares such pages again.

Usage: python3 compare.py [DOC ...] [--redo]   (from the project folder)
"""
from __future__ import annotations

import argparse
import difflib
import sys
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))  # lets `python3 -I` find common.py
from common import (
    Token,
    bracket_groups,
    code_version,
    comparison_id,
    decided,
    distinctive,
    in_ranges,
    inputs_changed,
    number_forms,
    require_project,
    load_config,
    load_json,
    load_manifest,
    load_ocr,
    load_text,
    locate,
    neighbour_norms,
    neighbours,
    page_paths,
    rel,
    save_json,
    select_docs,
    service_ranges,
    tokenize,
    vision_support,
)

CONTEXT_CHARS = 60
DECIDABLE = {"number", "flag", "cells", "unsure", "text", "vision_check", "vision_extra"}
SEVERITY = {"moved": 0, "order": 1, "unsure": 2, "text": 3, "cells": 4, "flag": 5, "number": 6}
ROW_MARGIN_LINES = 2.0  # how many line heights above and below the spot a row crop shows
MAX_EDGE = 2000


class Text:
    """A transcription with its tokens, line offsets and bracketed marks."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = tokenize(text)
        self.line_starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
        self.groups = bracket_groups(text)

    @property
    def line_count(self) -> int:
        return len(self.line_starts)

    def line_span(self, line: int) -> tuple[int, int]:
        """Character range of a line without its newline."""
        start = self.line_starts[line]
        end = self.line_starts[line + 1] - 1 if line + 1 < self.line_count else len(self.text)
        return start, end

    def line_of(self, pos: int) -> int:
        return max(i for i, start in enumerate(self.line_starts) if start <= pos)

    def whole_lines(self, i1: int, i2: int) -> bool:
        """True if tokens[i1:i2] are exactly all tokens of the lines they occupy."""
        if i1 >= i2:
            return False
        toks = self.tokens
        first_ok = i1 == 0 or toks[i1 - 1].line != toks[i1].line
        last_ok = i2 == len(toks) or toks[i2].line != toks[i2 - 1].line
        return first_ok and last_ok

    def insertion_point(self, i: int) -> int:
        """Just after tokens[i-1] and any closing brackets right behind it, so a gap after
        "(4.1 - 5.6)" is not taken for a gap inside the brackets."""
        if i == 0:
            return 0
        pos = self.tokens[i - 1].end
        while pos < len(self.text) and self.text[pos] in ")]":
            pos += 1
        return pos

    def expand(self, start: int, end: int) -> tuple[int, int]:
        """Grow a character range to whole bracketed marks it touches."""
        changed = True
        while changed:
            changed = False
            for left, right in self.groups:
                touches = (left < end and start < right) if start < end else (left < start < right)
                if touches and (left < start or right > end):
                    start, end = min(start, left), max(end, right)
                    changed = True
        return start, end

    def widen(self, start: int, end: int, i1: int, i2: int) -> tuple[int, int]:
        """Token range [i1, i2) grown by every token lying inside [start, end)."""
        inside = [k for k, t in enumerate(self.tokens) if start <= t.start and t.end <= end]
        if not inside:
            return i1, i2
        return min(i1, inside[0]), max(i2, inside[-1] + 1)

    def char_span(self, i1: int, i2: int, start: int, end: int) -> tuple[int, int]:
        """Range covering tokens[i1:i2] and the current range; a bare insertion point is replaced."""
        if i1 >= i2:
            return start, end
        first, last = self.tokens[i1].start, self.tokens[i2 - 1].end
        if start == end:
            return first, last
        return min(start, first), max(end, last)

    def glue_end(self, end: int) -> int:
        """End of the punctuation glued to position end: up to a space, a token or a new mark."""
        starts = {t.start for t in self.tokens}
        pos = end
        while pos < len(self.text) and not self.text[pos].isspace() and pos not in starts and self.text[pos] not in "[(|":
            pos += 1
        return pos

    def context(self, start: int, end: int) -> str:
        left = self.text[max(0, start - CONTEXT_CHARS):start]
        right = self.text[end:end + CONTEXT_CHARS]
        return f"{left}⟦{self.text[start:end]}⟧{right}".replace("\n", " ⏎ ")


@dataclass
class Unit:
    """One discrepancy: token ranges and character spans in A and B, plus the original diff chunk."""

    kind: str
    i1: int
    i2: int
    j1: int
    j2: int
    sa: int
    ea: int
    sb: int
    eb: int
    chunk: tuple[int, int, int, int]


def settle(u: Unit, a: Text, b: Text, a2b: dict[int, int], b2a: dict[int, int]) -> Unit:
    """Grow a unit until both sides cover whole marks and the same agreed tokens."""
    while True:
        sa, ea = a.expand(u.sa, u.ea)
        sb, eb = b.expand(u.sb, u.eb)
        i1, i2 = a.widen(sa, ea, u.i1, u.i2)
        j1, j2 = b.widen(sb, eb, u.j1, u.j2)
        for k in range(i1, i2):
            if k in a2b:
                j1, j2 = min(j1, a2b[k]), max(j2, a2b[k] + 1)
        for k in range(j1, j2):
            if k in b2a:
                i1, i2 = min(i1, b2a[k]), max(i2, b2a[k] + 1)
        sa, ea = a.char_span(i1, i2, sa, ea)
        sb, eb = b.char_span(j1, j2, sb, eb)
        grown = replace(u, i1=i1, i2=i2, j1=j1, j2=j2, sa=sa, ea=ea, sb=sb, eb=eb)
        if grown == u:
            return u
        u = grown


def spans_overlap(s1: int, e1: int, s2: int, e2: int) -> bool:
    if s1 < e1 and s2 < e2:
        return s1 < e2 and s2 < e1
    if s1 == e1 and s2 < e2:
        return s2 < s1 < e2
    if s2 == e2 and s1 < e1:
        return s1 < s2 < e1
    return False


def touches_cells(u: Unit, a: Text, b: Text) -> bool:
    return any(t.kind == "cell" for t in a.tokens[u.i1:u.i2] + b.tokens[u.j1:u.j2])


def line_range(text: Text, start: int, end: int) -> tuple[int, int]:
    return text.line_of(start), text.line_of(max(start, end - 1))


def must_merge(p: Unit, q: Unit, a: Text, b: Text) -> bool:
    """Overlapping units merge; so do units on one table row when either moves a cell border,
    because choosing A for one and B for the other could add or lose a cell."""
    if spans_overlap(p.sa, p.ea, q.sa, q.ea) or spans_overlap(p.sb, p.eb, q.sb, q.eb):
        return True
    if not (touches_cells(p, a, b) or touches_cells(q, a, b)):
        return False
    meet = lambda r1, r2: r1[0] <= r2[1] and r2[0] <= r1[1]
    return meet(line_range(a, p.sa, p.ea), line_range(a, q.sa, q.ea)) or meet(line_range(b, p.sb, p.eb), line_range(b, q.sb, q.eb))


def merge_overlapping(units: list[Unit], a: Text, b: Text, a2b: dict, b2a: dict) -> list[Unit]:
    while True:
        pair = next(
            ((p, q) for p in range(len(units)) for q in range(p + 1, len(units)) if must_merge(units[p], units[q], a, b)),
            None,
        )
        if pair is None:
            return sorted(units, key=lambda u: (u.sa, u.ea))
        p, q = units[pair[0]], units[pair[1]]
        i1, i2, j1, j2 = min(p.i1, q.i1), max(p.i2, q.i2), min(p.j1, q.j1), max(p.j2, q.j2)
        # Spans restart from the merged tokens: an old insertion point must not drag in the gap before it.
        sa, ea = (a.tokens[i1].start, a.tokens[i2 - 1].end) if i1 < i2 else (min(p.sa, q.sa), max(p.ea, q.ea))
        sb, eb = (b.tokens[j1].start, b.tokens[j2 - 1].end) if j1 < j2 else (min(p.sb, q.sb), max(p.eb, q.eb))
        merged = Unit(
            kind=max(p.kind, q.kind, key=SEVERITY.get),
            i1=i1, i2=i2, j1=j1, j2=j2, sa=sa, ea=ea, sb=sb, eb=eb,
            chunk=(min(p.chunk[0], q.chunk[0]), max(p.chunk[1], q.chunk[1]), min(p.chunk[2], q.chunk[2]), max(p.chunk[3], q.chunk[3])),
        )
        units = [u for k, u in enumerate(units) if k not in pair] + [settle(merged, a, b, a2b, b2a)]


def take_glued_punctuation(units: list[Unit], a: Text, b: Text) -> list[Unit]:
    """Grow a unit over the punctuation glued to its end when A and B differ there, as in "[?азн:]"
    against "[?дан]:". The text between units comes from A: without this, choosing B would drop
    B's colon (or keep A's next to B's own one in the mirrored case)."""
    grown = []
    for n, u in enumerate(units):
        if u.sa < u.ea and u.sb < u.eb:
            ga, gb = a.glue_end(u.ea), b.glue_end(u.eb)
            following = units[n + 1] if n + 1 < len(units) else None
            fits = following is None or (ga <= following.sa and gb <= following.sb)
            if a.text[u.ea:ga] != b.text[u.eb:gb] and fits:
                u = replace(u, ea=ga, eb=gb)
        grown.append(u)
    return grown


def edit_for_unit(a: Text, b: Text, u: Unit) -> dict:
    """Where text A changes if B's reading wins, and what goes there."""
    if u.sa < u.ea and u.sb < u.eb:
        return {"mode": "inline", "a_span": [u.sa, u.ea], "b_text": b.text[u.sb:u.eb]}
    if u.sa < u.ea:  # only in A
        if a.whole_lines(u.i1, u.i2):
            start, _ = a.line_span(a.tokens[u.i1].line)
            _, end = a.line_span(a.tokens[u.i2 - 1].line)
            if end < len(a.text):
                end += 1  # remove the newline together with the lines
            elif start > 0:
                start -= 1
            return {"mode": "lines", "a_span": [start, end], "b_text": ""}
        start, end = u.sa, u.ea
        if start > 0 and a.text[start - 1] == " ":
            start -= 1  # take one neighbouring space along, so no double space is left
        elif end < len(a.text) and a.text[end] == " ":
            end += 1
        return {"mode": "inline", "a_span": [start, end], "b_text": ""}
    # only in B
    if b.whole_lines(u.j1, u.j2):
        b_start, _ = b.line_span(b.tokens[u.j1].line)
        _, b_end = b.line_span(b.tokens[u.j2 - 1].line)
        lines = b.text[b_start:b_end]
        if u.i1 > 0:
            _, pos = a.line_span(a.tokens[u.i1 - 1].line)
            return {"mode": "lines", "a_span": [pos, pos], "b_text": "\n" + lines}
        return {"mode": "lines", "a_span": [0, 0], "b_text": lines + "\n"}
    pos = u.sa
    before = a.text[pos - 1] if pos > 0 else "\n"
    after = a.text[pos] if pos < len(a.text) else "\n"
    inserted = ("" if before.isspace() else " ") + b.text[u.sb:u.eb] + ("" if after.isspace() else " ")
    return {"mode": "inline", "a_span": [pos, pos], "b_text": inserted}


def classify(tokens: list[Token]) -> str:
    if tokens and all(t.norm == "[?" for t in tokens):
        return "unsure"
    if tokens and all(t.kind == "cell" for t in tokens):
        return "cells"
    if any(t.kind == "num" for t in tokens):
        return "number"
    if any(t.kind == "sym" for t in tokens):
        return "flag"
    return "text"


def side(text: Text, start: int, end: int) -> dict:
    return {"text": text.text[start:end], "line": text.line_of(start) + 1, "context": text.context(start, end)}


class Cropper:
    """Crops of the page around a spot: a full-width row (with the spot framed) and a close-up."""

    def __init__(self, image_path: Path, out_dir: Path, page: int) -> None:
        self.image = Image.open(image_path).convert("RGB")
        self.out_dir = out_dir
        self.page = page
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.glob(f"p{page}_*.png"):
            old.unlink()

    def save(self, item_id: str, bbox: list[float] | None, span: tuple[float, float], row_only: bool = False) -> list[str]:
        """With a box: the row (box framed) and, unless row_only, a close-up; without: the band
        between the estimated top and bottom of the spot."""
        width, height = self.image.size
        paths = []
        if bbox and row_only:
            x, y, w, h = bbox
            line_h = max(h, 18)
            top = max(0, round(y - ROW_MARGIN_LINES * line_h))
            bottom = min(height, round(y + h + ROW_MARGIN_LINES * line_h))
            paths.append(self._write(self.image.crop((0, top, width, bottom)), f"p{self.page}_{item_id}_row.png"))
        elif bbox:
            x, y, w, h = bbox
            line_h = max(h, 18)
            top = max(0, round(y - ROW_MARGIN_LINES * line_h))
            bottom = min(height, round(y + h + ROW_MARGIN_LINES * line_h))
            row = self.image.crop((0, top, width, bottom))
            ImageDraw.Draw(row).rectangle((x - 6, y - top - 6, x + w + 6, y - top + h + 6), outline=(220, 30, 30), width=3)
            paths.append(self._write(row, f"p{self.page}_{item_id}_row.png"))
            pad_x, pad_y = max(3 * line_h, 80), 1.5 * line_h
            zoom = self.image.crop((
                max(0, round(x - pad_x)), max(0, round(y - pad_y)),
                min(width, round(x + w + pad_x)), min(height, round(y + h + pad_y)),
            ))
            scale = min(3.0, MAX_EDGE / max(zoom.size))
            zoom = zoom.resize((round(zoom.width * scale), round(zoom.height * scale)), Image.Resampling.LANCZOS)
            paths.append(self._write(zoom, f"p{self.page}_{item_id}_zoom.png"))
        else:
            top = max(0, round(min(span[0] - 60, (span[0] + span[1]) / 2 - 90)))
            bottom = min(height, round(max(span[1] + 60, (span[0] + span[1]) / 2 + 90)))
            paths.append(self._write(self.image.crop((0, top, width, bottom)), f"p{self.page}_{item_id}_band.png"))
        return paths

    def _write(self, image: Image.Image, name: str) -> str:
        path = self.out_dir / name
        image.save(path)
        return rel(path)


def starts_line(text: Text, i: int) -> bool:
    """True if tokens[i] is the first token of its line."""
    return i < len(text.tokens) and (i == 0 or text.tokens[i - 1].line != text.tokens[i].line)


def insertion_point(text: Text, i: int, other_starts_line: bool) -> int:
    """Where material missing before tokens[i] goes: at the start of i's line if the other reader
    has it at a line start and i starts a line here, otherwise right after the previous token."""
    if other_starts_line and starts_line(text, i):
        line_start = text.line_starts[text.tokens[i].line]
        prefix = text.text[line_start:text.tokens[i].start]
        return text.tokens[i].start if "|" in prefix else line_start
    return text.insertion_point(i)


def diff_units(a: Text, b: Text) -> tuple[list[Unit], dict[int, int], float]:
    """Discrepancies between A and B, grown to whole marks and merged where they touch."""
    matcher = difflib.SequenceMatcher(None, [t.norm for t in a.tokens], [t.norm for t in b.tokens], autojunk=False)
    opcodes = matcher.get_opcodes()
    a2b, b2a = {}, {}
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            for off in range(i2 - i1):
                a2b[i1 + off] = j1 + off
                b2a[j1 + off] = i1 + off

    # Identical runs that only changed position are "moved", not misread.
    a_only = {(i1, i2): tuple(t.norm for t in a.tokens[i1:i2]) for tag, i1, i2, _, _ in opcodes if tag == "delete"}
    b_only = {(j1, j2): tuple(t.norm for t in b.tokens[j1:j2]) for tag, _, _, j1, j2 in opcodes if tag == "insert"}
    moved_a, moved_b = set(), set()
    for key_a, seq_a in a_only.items():
        for key_b, seq_b in b_only.items():
            if key_b not in moved_b and seq_a == seq_b:
                moved_a.add(key_a)
                moved_b.add(key_b)
                break

    units = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        chunk = a.tokens[i1:i2] + b.tokens[j1:j2]
        harmless = not any(t.kind == "cell" for t in chunk)  # a moved "|" moves values between columns
        if harmless and (tag == "delete" and (i1, i2) in moved_a or tag == "insert" and (j1, j2) in moved_b):
            kind = "moved"
        elif harmless and tag == "replace" and Counter(t.norm for t in a.tokens[i1:i2]) == Counter(t.norm for t in b.tokens[j1:j2]):
            kind = "order"
        else:
            kind = classify(chunk)
        sa = ea = insertion_point(a, i1, starts_line(b, j1))
        sb = eb = insertion_point(b, j1, starts_line(a, i1))
        if i1 < i2:
            sa, ea = a.tokens[i1].start, a.tokens[i2 - 1].end
        if j1 < j2:
            sb, eb = b.tokens[j1].start, b.tokens[j2 - 1].end
        units.append(settle(Unit(kind, i1, i2, j1, j2, sa, ea, sb, eb, (i1, i2, j1, j2)), a, b, a2b, b2a))
    return take_glued_punctuation(merge_overlapping(units, a, b, a2b, b2a), a, b), a2b, matcher.ratio()


def compare_page(doc: str, entry: dict, version: str) -> dict:
    page = entry["page"]
    paths = page_paths(doc, page)
    for key in ("read_a", "read_b"):
        if not paths[key].exists():
            raise SystemExit(f"missing {rel(paths[key])}: run the read workflow first")
    a_raw, b_raw = load_text(paths["read_a"]), load_text(paths["read_b"])
    a, b = Text(a_raw), Text(b_raw)
    ocr = load_ocr(paths["ocr"])
    observations = ocr["observations"]
    height = entry["height"]
    cropper = Cropper(paths["image"], paths["crops_dir"], page)

    def where(interest: set[str], i1: int, i2: int, start: int) -> tuple[list[float] | None, float, tuple[float, float]]:
        line = a.line_of(start)
        # Text only B has is printed where B saw it, not necessarily on A's line at the insertion point.
        same_line = [t.norm for k, t in enumerate(a.tokens) if t.line == line and not i1 <= k < i2 and distinctive(t)] if i1 < i2 else []
        return locate(ocr, interest, neighbours(a.tokens, i1, i2), (line + 0.5) / a.line_count, height, same_line)

    units, a2b, similarity = diff_units(a, b)
    a_service, b_service = service_ranges(a.text), service_ranges(b.text)

    def zone(u: Unit) -> str:
        """Service only if what each reader wrote there is service (stamps, requisites, footers)."""
        a_side = u.sa == u.ea or in_ranges(u.sa, u.ea, a_service)
        b_side = u.sb == u.eb or in_ranges(u.sb, u.eb, b_service)
        return "service" if a_side and b_side else "content"

    entries: list[tuple[int, dict]] = []
    for u in units:
        ci1, ci2, cj1, cj2 = u.chunk
        chunk_tokens = a.tokens[ci1:ci2] + b.tokens[cj1:cj2]
        unit_tokens = a.tokens[u.i1:u.i2] + b.tokens[u.j1:u.j2]
        interest = (
            {t.norm for t in chunk_tokens if t.kind == "num"}
            or {t.norm for t in chunk_tokens if t.kind == "word" and len(t.norm) >= 3}
            or {t.norm for t in unit_tokens if t.kind == "num" or (t.kind == "word" and len(t.norm) >= 3)}
        )
        entries.append((u.sa, {
            "kind": u.kind,
            "a": side(a, u.sa, u.ea),
            "b": side(b, u.sb, u.eb),
            "edit": edit_for_unit(a, b, u),
            "vision": {
                "a_numbers": {a.tokens[k].raw: vision_support(ocr, a.text, a.tokens, k, height) for k in range(u.i1, u.i2) if a.tokens[k].kind == "num"},
                "b_numbers": {b.tokens[k].raw: vision_support(ocr, b.text, b.tokens, k, height) for k in range(u.j1, u.j2) if b.tokens[k].kind == "num"},
            },
            "_where": where(interest, u.i1, u.i2, u.sa),
            "zone": zone(u),
        }))

    # Numbers both readers agree on but Vision does not show in that line: checked by the arbiter.
    # In service information (requisites, certificates, footers) two agreeing readers are enough.
    covered = [(u.sa, u.ea) for u in units]
    checks: list[list[int]] = []
    for k, tok in enumerate(a.tokens):
        if tok.kind != "num" or k not in a2b or any(s <= tok.start < e for s, e in covered):
            continue
        if in_ranges(tok.start, tok.end, a_service) or vision_support(ocr, a.text, a.tokens, k, height):
            continue
        if checks and checks[-1][1] == k and a.tokens[k - 1].line == tok.line:
            checks[-1][1] = k + 1  # adjacent unconfirmed numbers on one line form one check
        else:
            checks.append([k, k + 1])
    for i1, i2 in checks:
        start, end = a.tokens[i1].start, a.tokens[i2 - 1].end
        j1, j2 = a2b[i1], a2b[i2 - 1] + 1
        line_words = {t.norm for t in a.tokens if t.line == a.tokens[i1].line and t.kind == "word" and len(t.norm) >= 3}
        entries.append((start, {
            "kind": "vision_check",
            "a": side(a, start, end),
            "b": side(b, b.tokens[j1].start, b.tokens[j2 - 1].end),
            "edit": {"mode": "inline", "a_span": [start, end], "b_text": b.text[b.tokens[j1].start:b.tokens[j2 - 1].end]},
            "vision": {"a_numbers": {a.tokens[k].raw: False for k in range(i1, i2)}, "b_numbers": {}},
            "_where": where(line_words or set(neighbour_norms(a.tokens, i1, i2)), i1, i2, start),
            "zone": "content",
        }))

    discrepancies = []
    for n, (_, item) in enumerate(sorted(entries, key=lambda e: e[0]), 1):
        item_id = f"d{n:02d}"
        bbox, _, span = item.pop("_where")
        needs = item["kind"] in DECIDABLE
        discrepancies.append({
            "id": item_id,
            "kind": item["kind"],
            "needs_decision": needs,
            **item,
            "bbox": bbox,
            # A check's box is found from other words of its line, so show the whole line, not a close-up.
            "crops": cropper.save(item_id, bbox, span, row_only=item["kind"] == "vision_check") if needs else [],
        })

    # Numbers that Vision saw but neither reader wrote: possible omissions by both. A reading such
    # as "3,24" for "3, 24" or "125.1" for "I25.1" is no omission; service lines are not checked.
    ab_nums = number_forms(a.text, a.tokens) | number_forms(b.text, b.tokens)
    for obs in observations:
        missing = [num for num in obs["best_numbers"] if num not in ab_nums and not any(num in other for other in ab_nums)]
        if not missing or in_ranges(0, len(obs["text"]), service_ranges(obs["text"])):
            continue
        if any(alt and all(num in ab_nums for num in alt) for alt in obs["alt_numbers"]):
            continue  # Vision itself had a reading that matches the readers
        item_id = f"d{len(discrepancies) + 1:02d}"
        x, y, w, h = obs["bbox"]
        discrepancies.append({
            "id": item_id,
            "kind": "vision_extra",
            "needs_decision": True,
            "vision": {"text": obs["text"], "missing_numbers": missing},
            "zone": "content",
            "bbox": obs["bbox"],
            "crops": cropper.save(item_id, obs["bbox"], (y + h / 2, y + h / 2)),
        })

    kinds = Counter(d["kind"] for d in discrepancies)
    return {
        # Pages whose inputs and discrepancies stay the same keep their decisions after an update.
        "compare_id": comparison_id(a_raw, b_raw, paths["ocr"].read_text(encoding="utf-8"), discrepancies),
        "logic": code_version("compare.py", "common.py"),
        "doc": doc,
        "page": page,
        "read_a": rel(paths["read_a"]),
        "read_b": rel(paths["read_b"]),
        "summary": {
            "a_tokens": len(a.tokens),
            "b_tokens": len(b.tokens),
            "a_numbers": sum(t.kind == "num" for t in a.tokens),
            "b_numbers": sum(t.kind == "num" for t in b.tokens),
            "similarity": round(similarity, 4),
            "open": sum(d["needs_decision"] for d in discrepancies),
            "by_kind": dict(sorted(kinds.items())),
        },
        "discrepancies": discrepancies,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare reader A and B with each other and with Vision OCR.")
    parser.add_argument("docs", nargs="*", help="parts of document names (default: all)")
    parser.add_argument("--redo", action="store_true", help="also compare pages the arbiter has already decided")
    args = parser.parse_args()
    require_project()

    manifest = load_manifest()
    version = load_config()["pipeline_version"]
    for doc in select_docs(manifest, args.docs):
        for entry in manifest["documents"][doc]["pages"]:
            paths = page_paths(doc, entry["page"])
            if not (paths["read_a"].exists() and paths["read_b"].exists()):
                print(f"skip (not read yet): {doc} p{entry['page']}")
                continue
            stored = load_json(paths["compare"])
            if stored and not args.redo and not inputs_changed(paths, stored) and decided(paths, stored):
                print(f"kept (decided): {doc} p{entry['page']}")
                continue
            result = compare_page(doc, entry, version)
            save_json(paths["compare"], result)
            s = result["summary"]
            print(f"{doc} p{entry['page']}: similarity {s['similarity']}, open {s['open']}, {s['by_kind']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
