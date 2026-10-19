"""Shared helpers for scan-to-markdown: project paths and settings, tokens, Apple Vision OCR, zones."""
from __future__ import annotations

import bisect
import contextlib
import fcntl
import functools
import hashlib
import json
import os
import re
import statistics
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent  # scripts/, prompts/, workflows/, review/ live here
LOG_SUBDIR = Path("logs") / "scan-to-markdown"  # the tool's own folder inside a project
SETTINGS_NAME = "settings.json"  # <project>/logs/scan-to-markdown/settings.json marks a project root


# --- project and settings ----------------------------------------------------
#
# A project holds three things:
#   inbox/                   new scans waiting to be transcribed
#   documents/               each transcribed scan with its Markdown next to it (the lasting content)
#   logs/scan-to-markdown/   this tool's settings, its processing log and a cache of images
# The documents do not depend on the log: it can be deleted or kept out of git.

def find_root() -> Path | None:
    """$SCAN2MD_ROOT, or the nearest folder from the current one upward with logs/scan-to-markdown/settings.json."""
    env = os.environ.get("SCAN2MD_ROOT")
    if env:
        return Path(env).resolve()
    here = Path.cwd().resolve()
    for folder in (here, *here.parents):
        if (folder / LOG_SUBDIR / SETTINGS_NAME).exists():
            return folder
    return None


def deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    """Written next to the target and then moved over it, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def file_lock(path: Path):
    """An exclusive lock on path shared by all processes of the tool, held on <name>.lock next to it:
    the review page and the build both rewrite the review data."""
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


DEFAULTS = load_json(SKILL_DIR / "config.default.json")
ROOT = find_root()
LOG = ROOT / LOG_SUBDIR if ROOT else None  # manifest, readings, comparisons, decisions, review marks
SETTINGS = deep_merge(DEFAULTS, load_json(LOG / SETTINGS_NAME, {})) if ROOT else DEFAULTS
INBOX = ROOT / SETTINGS["paths"]["inbox"] if ROOT else None
DOCUMENTS = ROOT / SETTINGS["paths"]["documents"] if ROOT else None
CACHE = LOG / "cache" if ROOT else None  # page images, strips, crops: rebuilt on demand, never in git
MANIFEST_PATH = LOG / "manifest.json" if ROOT else None


def require_project() -> None:
    if ROOT is None:
        raise SystemExit(f"no {LOG_SUBDIR / SETTINGS_NAME} in {Path.cwd()} or above: run `run.py init` in the project root first")


def load_config() -> dict[str, Any]:
    """Settings: the skill's defaults with the project's logs/scan-to-markdown/settings.json on top."""
    return SETTINGS


# --- files ------------------------------------------------------------------

def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def load_text(path: Path) -> str:
    """Transcription as NFC text with Unix newlines; all character offsets refer to this form."""
    text = path.read_text(encoding="utf-8")
    return nfc(text.replace("\r\n", "\n").replace("\r", "\n"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def page_paths(doc: str, page: int) -> dict[str, Path]:
    return {
        "image": CACHE / "pages" / doc / f"p{page}.png",
        "strips_dir": CACHE / "strips" / doc,
        "crops_dir": CACHE / "crops" / doc,
        "ocr": LOG / "ocr" / doc / f"p{page}.json",
        "read_a": LOG / "reads" / doc / f"p{page}.A.md",
        "read_b": LOG / "reads" / doc / f"p{page}.B.md",
        "notes_a": LOG / "reads" / doc / f"p{page}.A.notes.md",  # what reader A noticed, for the reviewer
        "notes_b": LOG / "reads" / doc / f"p{page}.B.notes.md",
        "compare": LOG / "compare" / doc / f"p{page}.json",
        "decisions": LOG / "decisions" / doc / f"p{page}.json",
        "corrections": LOG / "corrections" / doc / f"p{page}.json",
        "final": LOG / "final" / doc / f"p{page}.md",
    }


def comparison_id(a_text: str, b_text: str, ocr_text: str, discrepancies: list[dict[str, Any]]) -> str:
    """Id of a page's comparison: its inputs and what the arbiter decides about, not where a spot
    is on the scan and not the code version."""
    content = [{k: v for k, v in d.items() if k not in ("bbox", "crops", "zone")} for d in discrepancies]
    return sha256_bytes(json.dumps([a_text, b_text, ocr_text, content], ensure_ascii=False, sort_keys=True).encode())[:16]


def inputs_changed(paths: dict[str, Path], compare: dict[str, Any]) -> bool:
    """Has a reading or the OCR changed since the comparison was made? Its id covers them, which
    unlike file times survives a git checkout."""
    texts = load_text(paths["read_a"]), load_text(paths["read_b"]), paths["ocr"].read_text(encoding="utf-8")
    return comparison_id(*texts, compare["discrepancies"]) != compare["compare_id"]


def decided(paths: dict[str, Path], compare: dict[str, Any]) -> bool:
    """Has the arbiter decided this comparison, or is there nothing to decide?

    Such a comparison is kept when the comparison logic changes: its decisions, the text built from
    them and the reviewer's marks stay valid. Only new or changed readings get the new logic.
    """
    if not any(d["needs_decision"] for d in compare["discrepancies"]):
        return True
    return (load_json(paths["decisions"]) or {}).get("compare_id") == compare["compare_id"]


def load_manifest() -> dict[str, Any]:
    return load_json(MANIFEST_PATH, {"version": 1, "documents": {}})


def select_docs(manifest: dict[str, Any], selectors: list[str]) -> list[str]:
    """Documents whose name contains any selector (all documents when no selectors given)."""
    docs = sorted(manifest["documents"])
    if not selectors:
        return docs
    chosen = [d for d in docs if any(s in d for s in selectors)]
    missing = [s for s in selectors if not any(s in d for d in docs)]
    if missing:
        raise SystemExit(f"not in manifest (run prepare.py first): {', '.join(missing)}")
    return chosen


def code_version(*names: str) -> str:
    """Checksum of the given scripts: results made by older logic are recomputed (see `decided`)."""
    digest = hashlib.sha256()
    for name in names:
        digest.update((SKILL_DIR / "scripts" / name).read_bytes())
    return digest.hexdigest()[:12]


# --- tokens -----------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"(?P<unsure>\[\?)"
    r"|(?P<num>\d+(?:[.,]\d+)*)"
    r"|(?P<sup>[⁰¹²³⁴⁵⁶⁷⁸⁹]+)"
    r"|(?P<times>×)"
    r"|(?P<word>[^\W\d_⁰¹²³⁴⁵⁶⁷⁸⁹]+)"
    r"|(?P<sym>[↑↓→←▲▼✓<>≤≥±+%?*!])"
    r"|(?P<cell>\|)"
)
SUPERSCRIPTS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_SUPERSCRIPT = str.maketrans(SUPERSCRIPTS, "0123456789")
# Latin letters that look like Cyrillic ones map to Cyrillic, so "T3" (Latin T) equals "Т3".
_HOMOGLYPHS = str.maketrans("abcehkmoptxy", "авсенкмортху")
# Letters that Apple Vision takes for digits and back: it reads "I25.1" as "125.1" and "СТ3" as "СТЗ".
# Codes glued from letters and digits are compared with these letters turned into digits.
_LOOKALIKE = str.maketrans({"i": "1", "l": "1", "і": "1", "о": "0", "з": "3", "б": "6"})
_CODE_RE = re.compile(r"[^\W_]+(?:[.,][^\W_]+)*")  # letters and digits written together: "I25.1", "СТ3"


@dataclass(frozen=True)
class Token:
    kind: str  # "num" | "word" | "sym" | "cell"; a superscript glued to a word ("см³") is a "word"
    raw: str
    norm: str
    start: int
    end: int
    line: int


def tokenize(text: str) -> list[Token]:
    """Comparable tokens of a text: numbers, words, meaningful signs (flags such as ▲ or *), the "[?"
    uncertainty mark and table cell borders "|"; other punctuation and markup are ignored."""
    line_starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
    tokens: list[Token] = []
    for match in _TOKEN_RE.finditer(text):
        group, raw = match.lastgroup, match.group()
        if group == "unsure":
            kind, norm = "sym", "[?"
        elif group == "num":
            kind, norm = "num", raw.replace(",", ".")
        elif group == "sup":
            # Glued to a word, a superscript belongs to the unit ("см³"): a value it is not.
            glued = match.start() > 0 and text[match.start() - 1].isalpha()
            kind, norm = "word" if glued else "num", raw.translate(_SUPERSCRIPT)
        elif group == "times":
            kind, norm = "word", "х"
        elif group == "word":
            kind, norm = "word", raw.casefold().replace("ё", "е").translate(_HOMOGLYPHS)
        elif group == "cell":
            kind, norm = "cell", "|"
        else:
            kind, norm = "sym", {"≤": "<=", "≥": ">="}.get(raw, raw)
        line = bisect.bisect_right(line_starts, match.start()) - 1
        tokens.append(Token(kind, raw, norm, match.start(), match.end(), line))
    return tokens


# --- Apple Vision OCR -------------------------------------------------------

def load_ocr(path: Path) -> dict[str, Any]:
    """Vision observations with normalized tokens; `numbers` covers best readings and alternatives."""
    data = load_json(path)
    if data is None:
        raise SystemExit(f"no OCR file: {rel(path)}")
    numbers: set[str] = set()
    for obs in data["observations"]:
        best = tokenize(nfc(obs["text"]))
        obs["best_numbers"] = [t.norm for t in best if t.kind == "num"]
        obs["alt_numbers"] = [
            [t.norm for t in tokenize(nfc(alt)) if t.kind == "num"] for alt in obs.get("alternatives", [])
        ]
        obs["norms"] = {t.norm for t in best}
        obs["codes"] = codes(nfc(obs["text"]))
        for alt in obs.get("alternatives", []):
            obs["norms"] |= {t.norm for t in tokenize(nfc(alt))}
            obs["codes"] |= codes(nfc(alt))
        numbers.update(obs["best_numbers"])
        for alt in obs["alt_numbers"]:
            numbers.update(alt)
    data["numbers"] = numbers
    heights = [o["bbox"][3] for o in data["observations"]]
    data["line_height"] = statistics.median(heights) if heights else 30.0
    return data


def code_form(run: str) -> str:
    return run.casefold().replace("ё", "е").translate(_HOMOGLYPHS).translate(_LOOKALIKE).replace(",", ".")


def codes(text: str) -> set[str]:
    """Every run of letters and digits in a text, in the form that ignores Vision's look-alikes."""
    return {code_form(m.group()) for m in _CODE_RE.finditer(text)}


@functools.lru_cache(maxsize=32)
def code_runs(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in _CODE_RE.finditer(text)]


def glued_code(text: str, tok: Token) -> str | None:
    """The code a number is glued into ("СТ3", "I25.1"), in look-alike form; None for a bare number."""
    runs = code_runs(text)
    i = bisect.bisect_right(runs, (tok.start, len(text))) - 1
    if i < 0 or runs[i][1] < tok.end:
        return None
    run = text[runs[i][0]:runs[i][1]]
    return code_form(run) if any(ch.isalpha() for ch in run) else None


def decimal_joins(text: str, tokens: list[Token], k: int) -> set[str]:
    """The number as printed with a space after the decimal comma: "3, 24" is also "3.24"."""
    joins = set()
    for i, j in ((k - 1, k), (k, k + 1)):
        if 0 <= i and j < len(tokens) and tokens[i].kind == tokens[j].kind == "num" \
                and text[tokens[i].end:tokens[j].start] == ", " and "." not in tokens[i].norm + tokens[j].norm:
            joins.add(f"{tokens[i].norm}.{tokens[j].norm}")
    return joins


def power_joins(text: str, tokens: list[Token], k: int) -> set[str]:
    """A power in a unit as Vision may read it, with the caret taken for a "1": "10^9" as "1019"."""
    joins = set()
    for i, j in ((k - 1, k), (k, k + 1)):
        if 0 <= i and j < len(tokens) and tokens[i].kind == tokens[j].kind == "num" \
                and text[tokens[i].end:tokens[j].start] == "^":
            joins.add(f"{tokens[i].norm}1{tokens[j].norm}")
    return joins


def number_forms(text: str, tokens: list[Token]) -> set[str]:
    """Every way the numbers of a text can show up in Vision's reading: as written, joined over a
    decimal comma with a space, a power "10^9" as "1019", and codes such as "I25.1" with look-alike
    letters read as digits."""
    forms = {t.norm for t in tokens if t.kind == "num" or t.raw[0] in SUPERSCRIPTS}
    for k, tok in enumerate(tokens):
        if tok.kind == "num":
            forms |= decimal_joins(text, tokens, k) | power_joins(text, tokens, k)
            code = glued_code(text, tok)
            if code and re.fullmatch(r"[\d.]+", code):
                forms.add(code)
    return forms


def center_y(obs: dict[str, Any]) -> float:
    x, y, w, h = obs["bbox"]
    return y + h / 2


def anchor_y(observations: list[dict[str, Any]], anchor_norms: list[str]) -> float | None:
    """Median height of nearby tokens that occur in exactly one OCR observation."""
    ys = [center_y(o) for o in seen_once(observations, anchor_norms)]
    return statistics.median(ys) if ys else None


def seen_once(observations: list[dict[str, Any]], norms: list[str]) -> list[dict[str, Any]]:
    """For each token Vision saw in exactly one observation, that observation, in the order given."""
    found = []
    for norm in norms:
        hits = [o for o in observations if norm in o["norms"]]
        if len(hits) == 1:
            found.append(hits[0])
    return found


def one_line(observations: list[dict[str, Any]], tolerance: float) -> list[dict[str, Any]]:
    """The largest group of the observations that lie on one line (centres within tolerance)."""
    groups = [[o for o in observations if abs(center_y(o) - center_y(p)) <= tolerance] for p in observations]
    return max(groups, key=len)


def union_box(observations: list[dict[str, Any]]) -> list[float]:
    left = min(o["bbox"][0] for o in observations)
    top = min(o["bbox"][1] for o in observations)
    right = max(o["bbox"][0] + o["bbox"][2] for o in observations)
    bottom = max(o["bbox"][1] + o["bbox"][3] for o in observations)
    return [left, top, right - left, bottom - top]


def locate(
    ocr: dict[str, Any],
    interest: set[str],
    around: tuple[list[str], list[str]],
    fallback_ratio: float,
    height: int,
    line_norms: list[str] | None = None,
) -> tuple[list[float] | None, float, tuple[float, float]]:
    """Where a spot is on the page: (OCR box or None, estimated y, estimated (top, bottom)), in pixels.

    If Vision saw other tokens of the spot's line exactly once, the line is known: the box is the
    observation on that line with the tokens of interest, or else the whole line, never a line
    elsewhere with the same word. Otherwise the place is estimated from nearby tokens that Vision
    saw exactly once (`around`: before and after the spot, nearest first); without a box, the spot
    lies between the nearest of them.
    """
    observations = ocr["observations"]
    overlap = lambda o: len(o["norms"] & interest)
    line_anchors = seen_once(observations, line_norms or [])
    if line_anchors:
        line = one_line(line_anchors, ocr["line_height"])  # a word seen once elsewhere does not move the line
        y = statistics.median(center_y(o) for o in line)
        hits = [o for o in observations if overlap(o) and abs(center_y(o) - y) <= ocr["line_height"]]
        if hits:
            return min(hits, key=lambda o: (-overlap(o), abs(center_y(o) - y)))["bbox"], y, (y, y)
        return union_box(line), y, (y, y)
    before, after = around
    anchors = seen_once(observations, before + after)
    if anchors:
        y = statistics.median(center_y(o) for o in anchors)
        hits = [o for o in observations if overlap(o) and abs(center_y(o) - y) <= 0.15 * height]
        if hits:
            return min(hits, key=lambda o: (-overlap(o), abs(center_y(o) - y)))["bbox"], y, (y, y)
        nearest = [center_y(o) for o in seen_once(observations, before)[:1] + seen_once(observations, after)[:1]]
        top, bottom = min(nearest), max(nearest)
        if bottom - top > 0.25 * height:  # the text jumps to another block: trust the line before the spot
            top = bottom = nearest[0]
        return None, (top + bottom) / 2, (top, bottom)
    y = fallback_ratio * height
    hits = [o for o in observations if overlap(o)]
    if not hits:
        return None, y, (y, y)
    best = min(hits, key=lambda o: (-overlap(o), abs(center_y(o) - y)))
    return best["bbox"], center_y(best), (center_y(best), center_y(best))


def distinctive(t: Token) -> bool:
    return t.kind == "num" or (t.kind == "word" and len(t.norm) >= 4)


def neighbours(tokens: list[Token], i1: int, i2: int, count: int = 6) -> tuple[list[str], list[str]]:
    """Distinctive tokens before and after tokens[i1:i2] (numbers and words of 4+ letters), nearest first."""
    before = [t.norm for t in reversed(tokens[:i1]) if distinctive(t)][:count]
    after = [t.norm for t in tokens[i2:] if distinctive(t)][:count]
    return before, after


def neighbour_norms(tokens: list[Token], i1: int, i2: int, count: int = 6) -> list[str]:
    """Distinctive tokens around tokens[i1:i2] (numbers and words of 4+ letters)."""
    before, after = neighbours(tokens, i1, i2, count)
    return before + after


def vision_support(ocr: dict[str, Any], text: str, tokens: list[Token], k: int, height: int) -> bool:
    """Does Vision show the number tokens[k] of text at the same place on the page?

    The number counts in any form Vision may give it: "3, 24" also as "3,24", the "3" of "СТ3" also
    as "СТЗ", the "10" and the "9" of "10^9" also as "1019". The place is the height of the token's
    line, estimated from other tokens of that line that Vision saw exactly once; failing that, from
    nearby tokens. Only when the place cannot be estimated at all does any occurrence on the page
    count.
    """
    tok = tokens[k]
    observations = ocr["observations"]
    forms = {tok.norm} | decimal_joins(text, tokens, k) | power_joins(text, tokens, k)
    code = glued_code(text, tok)
    hits = [o for o in observations if forms & o["norms"] or code is not None and code in o["codes"]]
    if not hits:
        return False
    same_line = [t.norm for i, t in enumerate(tokens) if i != k and t.line == tok.line and distinctive(t)]
    y = anchor_y(observations, same_line)
    tolerance = 2.5 * ocr["line_height"]
    if y is None:
        y = anchor_y(observations, neighbour_norms(tokens, k, k + 1))
        tolerance = 0.05 * height
    if y is None:
        return True
    return any(abs(center_y(o) - y) <= tolerance for o in hits)


# --- groups and zones ---------------------------------------------------------

def bracket_groups(text: str) -> list[tuple[int, int]]:
    """Outermost [...] and (...) groups, each within one line, as [start, end) character ranges.

    An unmatched "(" inside [...] is ignored, so a stray parenthesis cannot hide a whole mark.
    """
    groups, stack = [], []

    def close(opening: str, end: int) -> None:
        if stack and stack[-1][0] == opening:
            _, start = stack.pop()
            if not stack:
                groups.append((start, end))

    for i, ch in enumerate(text):
        if ch == "\n":
            stack.clear()
        elif ch in "[(":
            stack.append((ch, i))
        elif ch == "]":
            while stack and stack[-1][0] != "[":
                stack.pop()
            close("[", i + 1)
        elif ch == ")":
            close("(", i + 1)
    return groups


def service_ranges(text: str) -> list[tuple[int, int]]:
    """Parts of a page text with service information rather than the document's content: marks such
    as [печать: …] or [логотип: …], whole lines with requisites (licences, certificates, tax numbers,
    phones, page footers) and single links or e-mail addresses (a content line may contain a link).
    Neighbouring parts with only blank space between them form one range. Rules come from
    settings["service_zone"]."""
    rules = SETTINGS["service_zone"]
    marks = tuple(m.casefold() for m in rules["marks"])
    keywords = [k.casefold() for k in rules["line_keywords"]]
    ranges = [m.span() for pattern in rules.get("token_patterns", []) for m in re.finditer(pattern, text)]
    for start, end in bracket_groups(text):
        inner = text[start + 1:end - 1].lstrip("?").strip().casefold()
        if text[start] == "[" and inner.startswith(marks):
            ranges.append((start, end))
    pos = 0
    for line in text.split("\n"):
        if any(k in line.casefold() for k in keywords):
            ranges.append((pos, pos + len(line)))
        pos += len(line) + 1
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and not text[merged[-1][1]:start].strip():
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def in_ranges(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(s <= start and end <= e for s, e in ranges)
