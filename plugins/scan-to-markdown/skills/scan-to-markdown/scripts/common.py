"""Shared helpers for scan-to-markdown: project paths and settings, tokens, Apple Vision OCR, zones."""
from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
import statistics
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
        "compare": LOG / "compare" / doc / f"p{page}.json",
        "decisions": LOG / "decisions" / doc / f"p{page}.json",
        "corrections": LOG / "corrections" / doc / f"p{page}.json",
        "final": LOG / "final" / doc / f"p{page}.md",
    }


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
    """Checksum of the given scripts: results made by older logic are recomputed."""
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
    r"|(?P<sym>[↑↓→←▲▼✓<>≤≥±+%?])"
    r"|(?P<cell>\|)"
)
_SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
# Latin letters that look like Cyrillic ones map to Cyrillic, so "T3" (Latin T) equals "Т3".
_HOMOGLYPHS = str.maketrans("abcehkmoptxy", "авсенкмортху")


@dataclass(frozen=True)
class Token:
    kind: str  # "num" | "word" | "sym" | "cell"
    raw: str
    norm: str
    start: int
    end: int
    line: int


def tokenize(text: str) -> list[Token]:
    """Comparable tokens of a text: numbers, words, meaningful signs, the "[?" uncertainty mark and
    table cell borders "|"; other punctuation and markup are ignored."""
    line_starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
    tokens: list[Token] = []
    for match in _TOKEN_RE.finditer(text):
        group, raw = match.lastgroup, match.group()
        if group == "unsure":
            kind, norm = "sym", "[?"
        elif group == "num":
            kind, norm = "num", raw.replace(",", ".")
        elif group == "sup":
            kind, norm = "num", raw.translate(_SUPERSCRIPT)
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
        for alt in obs.get("alternatives", []):
            obs["norms"] |= {t.norm for t in tokenize(nfc(alt))}
        numbers.update(obs["best_numbers"])
        for alt in obs["alt_numbers"]:
            numbers.update(alt)
    data["numbers"] = numbers
    heights = [o["bbox"][3] for o in data["observations"]]
    data["line_height"] = statistics.median(heights) if heights else 30.0
    return data


def center_y(obs: dict[str, Any]) -> float:
    x, y, w, h = obs["bbox"]
    return y + h / 2


def anchor_y(observations: list[dict[str, Any]], anchor_norms: list[str]) -> float | None:
    """Median height of nearby tokens that occur in exactly one OCR observation."""
    ys = []
    for norm in anchor_norms:
        hits = [o for o in observations if norm in o["norms"]]
        if len(hits) == 1:
            ys.append(center_y(hits[0]))
    return statistics.median(ys) if ys else None


def locate(
    observations: list[dict[str, Any]],
    interest: set[str],
    anchor_norms: list[str],
    fallback_ratio: float,
    height: int,
    line_norms: list[str] | None = None,
) -> tuple[list[float] | None, float]:
    """Best OCR box for tokens of interest near the place suggested by anchor tokens.

    The place is estimated from other tokens of the same line if Vision saw any of them exactly
    once, otherwise from nearby tokens. Returns (bbox or None, estimated y of the place in pixels).
    """
    y_est = anchor_y(observations, line_norms or [])
    if y_est is None:
        y_est = anchor_y(observations, anchor_norms)
    has_anchor = y_est is not None
    if y_est is None:
        y_est = fallback_ratio * height
    candidates = [o for o in observations if o["norms"] & interest]
    if has_anchor:
        near = [o for o in candidates if abs(center_y(o) - y_est) <= 0.15 * height]
        candidates = near or candidates
    if not candidates:
        return None, y_est
    best = min(candidates, key=lambda o: (-len(o["norms"] & interest), abs(center_y(o) - y_est)))
    return best["bbox"], y_est


def distinctive(t: Token) -> bool:
    return t.kind == "num" or (t.kind == "word" and len(t.norm) >= 4)


def neighbour_norms(tokens: list[Token], i1: int, i2: int, count: int = 6) -> list[str]:
    """Distinctive tokens around tokens[i1:i2] (numbers and words of 4+ letters)."""
    before = [t.norm for t in reversed(tokens[:i1]) if distinctive(t)][:count]
    after = [t.norm for t in tokens[i2:] if distinctive(t)][:count]
    return before + after


def vision_support(ocr: dict[str, Any], tokens: list[Token], k: int, height: int) -> bool:
    """Does Vision show the number tokens[k] at the same place on the page?

    The place is the height of the token's line, estimated from other tokens of that line that
    Vision saw exactly once; failing that, from nearby tokens. Only when the place cannot be
    estimated at all does any occurrence on the page count.
    """
    tok = tokens[k]
    observations = ocr["observations"]
    hits = [o for o in observations if tok.norm in o["norms"]]
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
    """Parts of a page text with service information rather than medical data: marks such as
    [печать: …] or [логотип: …], whole lines with requisites (licences, certificates, tax numbers,
    phones, page footers) and single links or e-mail addresses (a medical line may contain a link).
    Rules come from settings["service_zone"]."""
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
    return sorted(ranges)


def in_ranges(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(s <= start and end <= e for s, e in ranges)
