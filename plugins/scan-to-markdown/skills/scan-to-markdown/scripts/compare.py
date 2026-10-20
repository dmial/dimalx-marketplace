#!/usr/bin/env python3
"""Compare the two transcriptions of each page with each other and with Apple Vision OCR.

For every page writes logs/scan-to-markdown/compare/<doc>/p<N>.json: every discrepancy with its exact place in
text A, the variant from text B, what Vision saw there, and enlarged crops of the scan
(in the cache). Discrepancies are decided later by the arbiter; build.py assembles the final text.

Kinds of discrepancies:
  number / flag / text  A and B differ here (flag = arrows, triangles and signs like < > ± % * !)
  cells                 table cell borders "|" differ: a value may sit in another column
  unsure                only one reader marked this place as uncertain [?…]
  punct                 the words agree, the signs between them differ: a colon, a square bracket
                        or a minus before a number
  order / moved         same tokens, different order, no cell border involved: no decision needed
  vision_check          A and B agree on a number that Vision did not see in that line
  vision_extra          Vision found a number that neither A nor B has
Vision checks skip service information (stamps, requisites, certificates, footers): two agreeing
readers are enough there. A number counts as seen in any form Vision gives it: "3, 24" as "3,24",
"I25.1" as "125.1", the "3" of "СТ3" as "СТЗ", "10^9" as "1019". Every discrepancy has a zone,
"service" when both readers' text there is service information: the arbiter decides those with a
quick look.

Crops show the spot. Where Vision shows its tokens (or, for printed text, its line), a row with the
spot framed and a close-up, where a number only Vision has is framed too; otherwise a band of the
page with the spot inside, taller for stamps, signatures and pictures. Words of stamps and
handwritten notes are never looked up in Vision's text, and only printed tokens the text has on one
line anchor a line: the doctor's name printed under her stamp, or a "12" inside a certificate
number, cannot pull a crop to another place. A spot whose line Vision does not show lies between
the nearest such anchors before and after it, so that neither a footnote far below nor table
headers repeated down the page can pull its crop away.

Table cell borders are matched by their place in the line too: a border that opens a line only
with one that opens a line, a border that closes it only with one that closes it. A row one reader
wrote without its last border ("| 5.1" against "| 5.1 |") so differs in that border, and choosing
B cannot move a border into the next row ("| | Белок | 70 |").

A discrepancy that touches a bracketed group such as [от руки: ✓], [печать: …] or (?) always
covers the whole group on both sides, so choosing one reader's variant replaces whole groups.
Punctuation glued to the end of a discrepancy joins it when the readers differ there
("[?азн:]" against "[?дан]:"), so choosing B neither loses nor doubles a colon. Words only one
reader has are cut so that choosing B leaves the punctuation B has between their neighbours:
"Тип исследования: Рентген" against "Тип Рентген" (the word moved to the next line) loses the
colon with the word, "Дата взятия: 12" against "Дата: 12" keeps it.
Choosing B everywhere gives B's tokens and B's colons, square brackets and minus signs before
numbers ("-2.1", not the dash of "5-10"). Where the text between two agreed tokens would not, as
with "Анамнез гипертония" against "Анамнез: гипертония", a "punct" discrepancy covers the two
tokens: its B variant takes B's signs and keeps A's words. Where a discrepancy lies between them
("Анамнез: гипертония" against "Анамнез гипортония"), it grows to them instead. Other
punctuation (periods, commas, dashes) is not compared and stays as reader A wrote it.
The comparison id covers the inputs and the resulting list of discrepancies: if either changes,
earlier arbiter decisions no longer apply. So a page the arbiter has decided keeps its comparison
when this logic changes, as long as its readings and OCR stay the same: the text built from the
decisions and the reviewer's marks stay valid. --redo compares such pages again.

Usage: python3 compare.py [DOC ...] [--redo]   (from the project folder)
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))  # lets `python3 -I` find common.py
from common import (
    LARGE_REACH,
    Spot,
    Token,
    alignment_keys,
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
    page_paths,
    rel,
    save_json,
    select_docs,
    service_ranges,
    tokenize,
    vision_support,
)

CONTEXT_CHARS = 60
DECIDABLE = {"number", "flag", "cells", "unsure", "punct", "text", "vision_check", "vision_extra"}
SEVERITY = {"moved": 0, "order": 1, "punct": 2, "unsure": 3, "text": 4, "cells": 5, "flag": 6, "number": 7}
# Signs that are no tokens but must come along with a reader's variant: colons, square brackets and
# a minus right before a number ("-2.1", "(-0.5"). A dash right after a letter, digit, period or
# comma is no minus: "5-10", "CS-6400".
MARK_RE = re.compile(r"[\[\]:]|(?<![\w.,])[-−–](?=\d)")
ROW_MARGIN_LINES = 2.0  # how many line heights above and below the spot a row crop shows
MAX_EDGE = 2000
# Marks for what is not printed text. Vision does not show their words, and the same words printed
# nearby (the doctor's name under her stamp) must not be taken for them.
UNSEEN_MARKS = ("от руки", "подпись", "печать", "изображение", "логотип", "штрихкод", "qr-код")
LARGE_MARKS = UNSEEN_MARKS[1:]  # stamps, signatures and pictures reach beyond the line they are written on


class Text:
    """A transcription with its tokens, line offsets and bracketed marks."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = tokenize(text)
        self.line_starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
        self.groups = bracket_groups(text)
        self.marks = []  # (start, end, kind) of every mark for what is not printed text
        for start, end in self.groups:
            inner = text[start + 1:end - 1].lstrip("?").strip().casefold()
            kind = next((m for m in UNSEEN_MARKS if inner.startswith(m)), None)
            if text[start] == "[" and kind:
                self.marks.append((start, end, kind))
        self.token_marks = [next((k for s, e, k in self.marks if s <= t.start < e), None) for t in self.tokens]
        printed = [t for t, mark in zip(self.tokens, self.token_marks) if mark is None]
        lines_of: dict[str, set[int]] = defaultdict(set)
        for t in printed:
            lines_of[t.norm].add(t.line)
        self.printed_norms = set(lines_of)
        # Printed on one line only, maybe twice there ("77/77"), with that line.
        self.once = {norm: min(lines) for norm, lines in lines_of.items() if len(lines) == 1}
        self.large_lines = frozenset(self.line_of(start) for start, _, kind in self.marks if kind in LARGE_MARKS)

    def printed(self, i1: int, i2: int) -> list[Token]:
        """The tokens among tokens[i1:i2] that are printed text, not words of a stamp or a handwritten note."""
        return [t for t, mark in zip(self.tokens[i1:i2], self.token_marks[i1:i2]) if mark is None]

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

    def glued(self, pos: int) -> str:
        """The punctuation glued to position pos, as ":" right after "Анамнез"."""
        return self.text[pos:self.glue_end(pos)]

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


def gap(text: Text, i: int, j: int) -> tuple[int, int]:
    """Character range between the neighbours of tokens[i:j]: from the end of tokens[i-1] to the
    start of tokens[j]."""
    start = text.tokens[i - 1].end if i > 0 else 0
    end = text.tokens[j].start if j < len(text.tokens) else len(text.text)
    return start, end


def marks(text: str) -> str:
    """Everything but the spaces: the punctuation and brackets a stretch of text carries."""
    return "".join(text.split())


def take_glued_punctuation(units: list[Unit], a: Text, b: Text) -> list[Unit]:
    """Grow a unit over the punctuation glued to its end when A and B differ there, as in "[?азн:]"
    against "[?дан]:". The text between units comes from A: without this, choosing B would drop
    B's colon (or keep A's next to B's own one in the mirrored case).

    Words only one reader has are cut so that choosing B leaves between their neighbours the same
    punctuation as B has there. Words only in A take the punctuation glued to them along when
    that is so: "Тип исследования: Рентген" against "Тип Рентген" loses the colon with the word,
    "Дата взятия: 12" against "Дата: 12" keeps it. Words only in B go in before or after the
    punctuation A has at that place and bring their own where it is missing: "Анамнез: нет" into
    "Анамнез:", "взятия" into "Дата: 12". When no cut gives B's punctuation, nothing changes.
    """
    grown = []
    for n, u in enumerate(units):
        ga, gb = a.glue_end(u.ea), b.glue_end(u.eb)
        following = units[n + 1] if n + 1 < len(units) else None
        fits = lambda ea, eb: following is None or (ea <= following.sa and eb <= following.sb)
        (left, right), (b_left, b_right) = gap(a, u.i1, u.i2), gap(b, u.j1, u.j2)
        if u.sa < u.ea and u.sb < u.eb:
            if a.text[u.ea:ga] != b.text[u.eb:gb] and u.i2 < len(a.tokens) and u.j2 < len(b.tokens):
                # Glue that runs into the next token on one side takes along the spaces the other
                # side has there: "10:20" against "11 20" gives "11 20" with B, not "1120".
                if ga == right and b.text[gb:b_right].isspace():
                    gb = b_right
                elif gb == b_right and a.text[ga:right].isspace():
                    ga = right
            if a.text[u.ea:ga] != b.text[u.eb:gb] and fits(ga, gb):
                u = replace(u, ea=ga, eb=gb)
        elif u.sa < u.ea:  # only in A
            if marks(a.text[left:u.sa] + a.text[ga:right]) == marks(b.text[b_left:b_right]) and fits(ga, u.eb):
                u = replace(u, ea=ga)
        elif u.sb < u.eb:  # only in B: in at u.sa as before, with its own punctuation, or after A's
            want = marks(b.text[b_left:u.sb]), marks(b.text[u.eb:b_right])
            for pos, end in ((u.sa, u.eb), (u.sa, gb), (ga, gb)):
                if (marks(a.text[left:pos]), marks(b.text[u.eb:end] + a.text[pos:right])) == want:
                    if fits(pos, end):
                        u = replace(u, sa=pos, ea=pos, eb=end)
                    break
        grown.append(u)
    return grown


def edit_for_unit(a: Text, b: Text, u: Unit) -> dict:
    """Where text A changes if B's reading wins, and what goes there."""
    a_words, b_words = a.tokens[u.i1:u.i2], b.tokens[u.j1:u.j2]
    if u.kind == "punct" and [t.norm for t in a_words] == [t.norm for t in b_words]:
        # The words agree: B's variant is B's signs between A's words.
        pieces, cursor = [], u.sb
        for mine, theirs in zip(a_words, b_words):
            pieces += [b.text[cursor:theirs.start], mine.raw]
            cursor = theirs.end
        return {"mode": "inline", "a_span": [u.sa, u.ea], "b_text": "".join(pieces) + b.text[cursor:u.eb]}
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
        # A word or mark right behind ("лечебно-" before "профилактического") keeps the space before.
        word_next = end < len(a.text) and not a.text[end].isspace() and a.glue_end(end) == end
        if start > 0 and a.text[start - 1] == " " and not word_next:
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
    # A's punctuation right here that B has right after the new words ends them: no space before it.
    ends_them = a.glued(pos) != "" and a.glued(pos) == b.glued(u.eb)
    # New words B glues to the sign both readers have before them follow it glued: "|---|---" gets "|".
    glued_on = u.sb > 0 and before == b.text[u.sb - 1] and not before.isalnum()
    inserted = ("" if before.isspace() or glued_on else " ") + b.text[u.sb:u.eb] + ("" if after.isspace() or ends_them else " ")
    return {"mode": "inline", "a_span": [pos, pos], "b_text": inserted}


def landmarks(text: str) -> list[str]:
    """The tokens of a text and its MARK_RE signs, in order; every kind of minus counts as "-"."""
    signs = [(m.start(), "-" if m.group() in "−–" else m.group()) for m in MARK_RE.finditer(text)]
    return [s for _, s in sorted(set(signs + [(t.start, t.norm) for t in tokenize(text)]))]


def rebuilt(a: Text, start: int, end: int, edits: list[dict]) -> str | None:
    """Text A from start to end with B's variant of the given edits, or None if one reaches outside."""
    pieces, cursor = [], start
    for edit in sorted(edits, key=lambda e: e["a_span"]):
        s, e = edit["a_span"]
        if not cursor <= s <= e <= end:
            return None
        pieces += [a.text[cursor:s], edit["b_text"]]
        cursor = e
    return "".join(pieces) + a.text[cursor:end]


def chunk_of(units: list[Unit]) -> tuple[int, int, int, int]:
    """The original diff chunks of the units together."""
    return (min(u.chunk[0] for u in units), max(u.chunk[1] for u in units),
            min(u.chunk[2] for u in units), max(u.chunk[3] for u in units))


def joined(p: Unit, q: Unit, a: Text, b: Text, a2b: dict, b2a: dict) -> Unit:
    """One unit covering both, their spans kept as they are."""
    return settle(Unit(
        kind=max(p.kind, q.kind, key=SEVERITY.get),
        i1=min(p.i1, q.i1), i2=max(p.i2, q.i2), j1=min(p.j1, q.j1), j2=max(p.j2, q.j2),
        sa=min(p.sa, q.sa), ea=max(p.ea, q.ea), sb=min(p.sb, q.sb), eb=max(p.eb, q.eb),
        chunk=chunk_of([p, q]),
    ), a, b, a2b, b2a)


def span_between(text: Text, k: int, k2: int) -> tuple[int, int, int, int]:
    """Tokens [i1, i2) and characters [start, end) from tokens[k] to tokens[k2], where -1 and
    len(tokens) stand for the text edges. A cell border at either end stays outside, so that no
    decision about a sign can move a column."""
    tokens = text.tokens
    if k < 0:
        i1, start = 0, 0
    elif tokens[k].kind == "cell":
        i1, start = k + 1, tokens[k].end
    else:
        i1, start = k, tokens[k].start
    if k2 >= len(tokens):
        i2, end = len(tokens), len(text.text)
    elif tokens[k2].kind == "cell":
        i2, end = k2, tokens[k2].start
    else:
        i2, end = k2 + 1, tokens[k2].end
    return i1, i2, start, end


def on_table_row(text: Text, start: int, end: int) -> bool:
    """True if the range touches a line of a table."""
    lines = range(text.line_of(start), text.line_of(max(start, end - 1)) + 1)
    return any(text.text[slice(*text.line_span(n))].lstrip().startswith("|") for n in lines)


def carry_marks(units: list[Unit], a: Text, b: Text, a2b: dict, b2a: dict) -> list[Unit]:
    """Make choosing B give B's tokens and MARK_RE signs between every two neighbouring agreed tokens
    that no unit covers, and between the text edges and such tokens.

    Where it would not, one unit covers both tokens and everything between them: a new "punct" unit
    when no unit lies there ("Анамнез гипертония" against "Анамнез: гипертония"), else the units
    there taken into it ("Анамнез: гипертония" against "Анамнез гипортония"; "Время 10:20" against
    "Время 11 20", where B's "11" would run into A's "20"). Such a unit is at least "punct": readers
    who differ there in more than the order of words need a decision.
    """
    for _ in range(len(a.tokens) + 2):  # each round adds at least one unit, or ends
        units = sorted(units, key=lambda u: (u.sa, u.ea))
        covered = {k for u in units for k in range(u.i1, u.i2)}
        agreed = [k for k in range(len(a.tokens)) if k in a2b and k not in covered]
        found: list[tuple[Unit, list[int]]] = []  # a new unit, and the units it takes in
        for k, k2 in zip([-1] + agreed, agreed + [len(a.tokens)]):
            m = a2b[k] if k >= 0 else -1
            m2 = a2b[k2] if k2 < len(a.tokens) else len(b.tokens)
            # Both tokens count, so that a variant running into one of them ("1120") shows.
            a_start, gap_start = (a.tokens[k].start, a.tokens[k].end) if k >= 0 else (0, 0)
            gap_end, a_end = (a.tokens[k2].start, a.tokens[k2].end) if k2 < len(a.tokens) else (len(a.text), len(a.text))
            b_start, b_gap_start = (b.tokens[m].start, b.tokens[m].end) if m >= 0 else (0, 0)
            b_gap_end, b_end = (b.tokens[m2].start, b.tokens[m2].end) if m2 < len(b.tokens) else (len(b.text), len(b.text))
            if any(spans_overlap(f.sa, f.ea, gap_start, gap_end) or spans_overlap(f.sb, f.eb, b_gap_start, b_gap_end) for f, _ in found):
                continue  # a unit found just now reaches in here (the "]" of a moved bracket): next round
            inside = [n for n, u in enumerate(units) if gap_start <= u.sa and u.ea <= gap_end]
            middle = rebuilt(a, gap_start, gap_end, [edit_for_unit(a, b, units[n]) for n in inside])
            if middle is not None and landmarks(a.text[a_start:gap_start] + middle + a.text[gap_end:a_end]) == landmarks(b.text[b_start:b_end]):
                continue
            (i1, i2, sa, ea), (j1, j2, sb, eb) = span_between(a, k, k2), span_between(b, m, m2)
            if a.text[sa:ea].count("\n") != b.text[sb:eb].count("\n") and (on_table_row(a, sa, ea) or on_table_row(b, sb, eb)):
                continue  # B's variant would join or split table rows: left as it is, for the self-test to stop
            unit = Unit("punct", i1, i2, j1, j2, sa, ea, sb, eb, (i1, i2, j1, j2))
            if inside:  # what those units differ in stays what to look for on the scan
                kind = max(["punct"] + [units[n].kind for n in inside], key=SEVERITY.get)
                unit = replace(unit, kind=kind, chunk=chunk_of([units[n] for n in inside]))
            found.append((settle(unit, a, b, a2b, b2a), inside))
        if not found:
            break
        taken = {n for _, inside in found for n in inside}
        units = [u for n, u in enumerate(units) if n not in taken]
        for unit, _ in found:
            # Take in whatever the new unit now overlaps or shares a table row with.
            others = [o for o in units if must_merge(unit, o, a, b)]
            while others:
                for other in others:
                    units.remove(other)
                    unit = joined(unit, other, a, b, a2b, b2a)
                others = [o for o in units if must_merge(unit, o, a, b)]
            units.append(unit)
    return sorted(units, key=lambda u: (u.sa, u.ea))


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

    def save(self, item_id: str, bbox: list[float] | None, span: tuple[float, float], row_only: bool = False,
             framed_zoom: bool = False) -> list[str]:
        """With a box: the row (box framed) and, unless row_only, a close-up, framed too if
        framed_zoom; without: the band between the estimated top and bottom of the spot."""
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
            left, upper = max(0, round(x - pad_x)), max(0, round(y - pad_y))
            zoom = self.image.crop((left, upper, min(width, round(x + w + pad_x)), min(height, round(y + h + pad_y))))
            scale = min(3.0, MAX_EDGE / max(zoom.size))
            zoom = zoom.resize((round(zoom.width * scale), round(zoom.height * scale)), Image.Resampling.LANCZOS)
            if framed_zoom:
                ImageDraw.Draw(zoom).rectangle(((x - 6 - left) * scale, (y - 6 - upper) * scale, (x + w + 6 - left) * scale,
                                                (y + h + 6 - upper) * scale), outline=(220, 30, 30), width=3)
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
    matcher = difflib.SequenceMatcher(None, alignment_keys(a.text, a.tokens), alignment_keys(b.text, b.tokens), autojunk=False)
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
    units = take_glued_punctuation(merge_overlapping(units, a, b, a2b, b2a), a, b)
    return carry_marks(units, a, b, a2b, b2a), a2b, matcher.ratio()


def looked_for(a: Text, b: Text, u: Unit) -> tuple[set[str], set[str]]:
    """What to look for in Vision's text to find a unit, and the kinds of marks it involves.

    Printed tokens: the numbers that differ, else the words, else any of the unit. Words of a stamp
    or a handwritten note are never looked for, as the same name printed nearby would be framed
    instead; of such a mark, only a long number the page prints nowhere else is surely the mark.
    """
    ci1, ci2, cj1, cj2 = u.chunk
    chunk_tokens = a.printed(ci1, ci2) + b.printed(cj1, cj2)
    unit_tokens = a.printed(u.i1, u.i2) + b.printed(u.j1, u.j2)
    interest = (
        {t.norm for t in chunk_tokens if t.kind == "num"}
        or {t.norm for t in chunk_tokens if t.kind == "word" and len(t.norm) >= 3}
        or {t.norm for t in unit_tokens if t.kind == "num" or (t.kind == "word" and len(t.norm) >= 3)}
        or {t.norm for t in a.tokens[ci1:ci2] + b.tokens[cj1:cj2]
            if t.kind == "num" and len(t.norm) >= 4 and t.norm not in a.printed_norms | b.printed_norms}
    )
    return interest, {mark for mark in a.token_marks[u.i1:u.i2] + b.token_marks[u.j1:u.j2] if mark}


def place(a: Text, ocr: dict, height: int, interest: set[str], i1: int, i2: int, start: int,
          marks: set[str] = frozenset()) -> tuple[list[float] | None, float, tuple[float, float]]:
    """Where a spot of text A is on the scan: (Vision's box or None, estimated y, (top, bottom)).

    The box is where Vision shows the tokens of interest. Printed text without such tokens is boxed
    by its whole line; a spot with marks for what is not printed text (a stamp, a handwritten note,
    a picture) gets no box but the range it lies in, so that its crop is a band of the page with
    the spot inside. The line comes from printed tokens that the text has on one line only and
    Vision saw once, never from words of such marks; the nearest twelve such tokens on each side
    anchor the estimate, however many repeated words (table headers) stand between. Stamps,
    signatures and pictures reach beyond the line they are written on: their range grows by
    LARGE_REACH of the page height each way.
    """
    line = a.line_of(start)
    anchor = lambda k: distinctive(a.tokens[k]) and a.token_marks[k] is None and a.tokens[k].norm in a.once
    # Text only B has is printed where B saw it, not necessarily on A's line at the insertion point.
    same_line = [a.tokens[k].norm for k in range(len(a.tokens)) if a.tokens[k].line == line and not i1 <= k < i2 and anchor(k)] if i1 < i2 else []
    before = [a.tokens[k].norm for k in range(i1 - 1, -1, -1) if anchor(k)][:12]
    after = [a.tokens[k].norm for k in range(i2, len(a.tokens)) if anchor(k)][:12]
    on_line = [k for k, t in enumerate(a.tokens) if t.line == line and t.kind in ("word", "num") and a.token_marks[k] is None]
    sides = ({a.tokens[k].norm for k in on_line if k < i1}, {a.tokens[k].norm for k in on_line if k >= i2})
    spot = Spot(line, a.once, a.large_lines, bool(marks & set(LARGE_MARKS)), sides)
    box, y, (top, bottom) = locate(ocr, interest, (before, after), (line + 0.5) / a.line_count, height, same_line,
                                   whole_line=not marks, spot=spot)
    if box is None and marks & set(LARGE_MARKS):
        top, bottom = max(0, top - LARGE_REACH * height), min(height, bottom + LARGE_REACH * height)
    return box, y, (top, bottom)


def place_check(a: Text, ocr: dict, height: int, i1: int, i2: int) -> tuple[list[float] | None, float, tuple[float, float]]:
    """Where numbers tokens[i1:i2] that both readers agree on and Vision did not see are: their line
    is found by its own printed words; a line without them gets a band, never a neighbour's box."""
    line = a.tokens[i1].line
    words = {t.norm for t in a.printed(0, len(a.tokens)) if t.line == line and t.kind == "word" and len(t.norm) >= 3}
    return place(a, ocr, height, words, i1, i2, a.tokens[i1].start)


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

    units, a2b, similarity = diff_units(a, b)
    a_service, b_service = service_ranges(a.text), service_ranges(b.text)

    def zone(u: Unit) -> str:
        """Service only if what each reader wrote there is service (stamps, requisites, footers)."""
        a_side = u.sa == u.ea or in_ranges(u.sa, u.ea, a_service)
        b_side = u.sb == u.eb or in_ranges(u.sb, u.eb, b_service)
        return "service" if a_side and b_side else "content"

    entries: list[tuple[int, dict]] = []
    for u in units:
        interest, marks = looked_for(a, b, u)
        entries.append((u.sa, {
            "kind": u.kind,
            "a": side(a, u.sa, u.ea),
            "b": side(b, u.sb, u.eb),
            "edit": edit_for_unit(a, b, u),
            "vision": {
                "a_numbers": {a.tokens[k].raw: vision_support(ocr, a.text, a.tokens, k, height) for k in range(u.i1, u.i2) if a.tokens[k].kind == "num"},
                "b_numbers": {b.tokens[k].raw: vision_support(ocr, b.text, b.tokens, k, height) for k in range(u.j1, u.j2) if b.tokens[k].kind == "num"},
            },
            "_where": place(a, ocr, height, interest, u.i1, u.i2, u.sa, marks),
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
        entries.append((start, {
            "kind": "vision_check",
            "a": side(a, start, end),
            "b": side(b, b.tokens[j1].start, b.tokens[j2 - 1].end),
            "edit": {"mode": "inline", "a_span": [start, end], "b_text": b.text[b.tokens[j1].start:b.tokens[j2 - 1].end]},
            "vision": {"a_numbers": {a.tokens[k].raw: False for k in range(i1, i2)}, "b_numbers": {}},
            "_where": place_check(a, ocr, height, i1, i2),
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
    # as "3,24" for "3, 24", "125.1" for "I25.1" or "1019" for "10^9" is no omission; service lines
    # are not checked.
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
            # The close-up of a stamp or a table holds many numbers: the frame shows which marks Vision read.
            "crops": cropper.save(item_id, obs["bbox"], (y + h / 2, y + h / 2), framed_zoom=True),
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
