#!/usr/bin/env python3
"""Assemble the final text of every page, write the Markdown files and the data for the review page.

The final text is reader A's transcription with exactly the edits the arbiter decided, applied by
this script, and then the reviewer's corrections: every character outside those places stays as
reader A wrote it. Pages with missing or invalid decisions or corrections are reported, and their
document is not written. A written document goes to documents/ as <name>.md, and its scan moves
there from the inbox, so the two lie side by side.

Usage: python3 build.py [DOC ...]   (from the project folder)
"""
from __future__ import annotations

import argparse
import difflib
import re
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # lets `python3 -I` find common.py
from common import (
    CACHE,
    DOCUMENTS,
    INBOX,
    LOG,
    MANIFEST_PATH,
    ROOT,
    distinctive,
    in_ranges,
    load_config,
    load_json,
    load_manifest,
    load_ocr,
    load_text,
    locate,
    neighbour_norms,
    nfc,
    page_paths,
    rel,
    require_project,
    save_json,
    select_docs,
    service_ranges,
    sha256_bytes,
    tokenize,
    vision_support,
)

TOOL = "scan-to-markdown"
REVIEW_DATA = CACHE / "review-data.json" if CACHE else None  # derived, rebuilt by this script
REVIEW_STATUS = LOG / "review-status.json" if LOG else None
VERDICTS = {
    "number": {"A", "B", "own", "illegible"},
    "flag": {"A", "B", "own", "illegible"},
    "text": {"A", "B", "own", "illegible"},
    "unsure": {"A", "B", "own", "illegible"},
    "cells": {"A", "B", "own", "illegible"},
    "vision_check": {"confirmed", "own", "illegible"},
    "vision_extra": {"already_present", "ocr_noise", "missing"},
}
# Marks in the text that a human has to look at, whoever wrote them.
ATTENTION_RE = re.compile(r"\[\?[^\]\n]*\]|\[неразборчиво\]|\[обрезано\]|\[от руки: неразборчиво\]")
NUMBER_STATUS_BY_SEGMENT = {
    "confirmed": "confirmed",
    "own": "attention",
    "illegible": "attention",
    "inserted": "attention",
    "undecided": "attention",
    "human": "human",
}
# Statuses of medical numbers that still need a human look.
NEEDS_LOOK = ("attention", "resolved_unconfirmed", "unconfirmed")


@dataclass
class Edit:
    start: int
    end: int
    text: str
    status: str  # resolved | confirmed | own | illegible | inserted | undecided
    item_id: str
    region: list[float] | None = None  # where the arbiter looked: [top, bottom, left, right] in %
    kept: list[tuple[int, int]] = field(default_factory=list)  # ranges in `text` both readers agreed on


def agreed_ranges(a_text: str, b_text: str) -> set[tuple[int, int]]:
    """Character ranges of tokens in text A that reader B wrote the same way (same diff as compare.py)."""
    a_tokens, b_tokens = tokenize(a_text), tokenize(b_text)
    matcher = difflib.SequenceMatcher(None, [t.norm for t in a_tokens], [t.norm for t in b_tokens], autojunk=False)
    return {
        (a_tokens[k].start, a_tokens[k].end)
        for tag, i1, i2, _, _ in matcher.get_opcodes() if tag == "equal" for k in range(i1, i2)
    }


def mark_kept(edit: Edit, a_text: str, agreed: set[tuple[int, int]]) -> Edit:
    """Find the tokens of the new text that repeat agreed tokens of A unchanged."""
    original = [t for t in tokenize(a_text) if edit.start <= t.start and t.end <= edit.end]
    new = tokenize(edit.text)
    matcher = difflib.SequenceMatcher(None, [t.norm for t in original], [t.norm for t in new], autojunk=False)
    for tag, i1, i2, j1, _ in matcher.get_opcodes():
        if tag == "equal":
            for off in range(i2 - i1):
                if (original[i1 + off].start, original[i1 + off].end) in agreed:
                    edit.kept.append((new[j1 + off].start, new[j1 + off].end))
    return edit


def whitespace_frame(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if not stripped:
        return text, ""
    return text[: len(text) - len(text.lstrip())], text[len(text.rstrip()):]


def uncertain(guess: str) -> str:
    """A reading marked as uncertain: "4.87" -> "[?4.87]", "[от руки: ✓]" -> "[?от руки: ✓]"."""
    if not guess:
        return "[неразборчиво]"
    if guess.startswith("[?") or guess == "[неразборчиво]":
        return guess
    if guess.startswith("[") and guess.endswith("]") and guess.count("[") == 1:
        return "[?" + guess[1:]
    return f"[?{guess}]"


def valid_region(region: object) -> list[float] | None:
    """[top, bottom, left, right] in percent of the page, or None if malformed."""
    if not isinstance(region, list) or len(region) != 4:
        return None
    try:
        top, bottom, left, right = (float(v) for v in region)
    except (TypeError, ValueError):
        return None
    if not (0 <= top < bottom <= 100 and 0 <= left < right <= 100):
        return None
    return [top, bottom, left, right]


def region_bbox(region: list[float] | None, width: int, height: int) -> list[float] | None:
    if not region:
        return None
    top, bottom, left, right = region
    return [round(width * left / 100), round(height * top / 100), round(width * (right - left) / 100), round(height * (bottom - top) / 100)]


def plan_edits(a_text: str, compare: dict, decisions: dict[str, dict]) -> tuple[list[Edit], list[str], list[str]]:
    """Edits of text A from the arbiter's decisions; returns (edits, problems, warnings)."""
    edits: list[Edit] = []
    problems: list[str] = []
    warnings: list[str] = []
    for item in compare["discrepancies"]:
        if not item["needs_decision"]:
            continue
        decision = decisions.get(item["id"])
        kind = item["kind"]
        if decision is None:
            problems.append(f"{item['id']}: нет решения арбитра")
            if "edit" in item:
                s, e = item["edit"]["a_span"]
                edits.append(Edit(s, e, a_text[s:e], "undecided", item["id"]))
            continue
        verdict = decision.get("verdict")
        if verdict not in VERDICTS[kind]:
            problems.append(f"{item['id']}: недопустимое решение «{verdict}» для {kind}")
            continue
        text = nfc(str(decision.get("text") or "")).strip()
        region = valid_region(decision.get("region"))
        if region is None and kind != "vision_extra":
            warnings.append(f"{item['id']}: арбитр не указал место на скане (region)")

        if kind == "vision_extra":
            if verdict == "missing":
                anchor = nfc(str(decision.get("insert_after") or ""))
                count = a_text.count(anchor) if anchor else 0
                if count != 1 or not text:
                    problems.append(f"{item['id']}: вставка невозможна — фрагмент «{anchor}» встречается в тексте A {count} раз")
                    continue
                pos = a_text.index(anchor) + len(anchor)
                edits.append(Edit(pos, pos, " " + text, "inserted", item["id"], region))
            continue

        s, e = item["edit"]["a_span"]
        original = a_text[s:e]
        lead, trail = whitespace_frame(original if s < e else item["edit"]["b_text"])
        if verdict == "A":
            edits.append(Edit(s, e, original, "resolved", item["id"], region))
        elif verdict == "confirmed":
            edits.append(Edit(s, e, original, "confirmed", item["id"], region))
        elif verdict == "B":
            edits.append(Edit(s, e, item["edit"]["b_text"], "resolved", item["id"], region))
        elif verdict == "own":
            if not text:
                problems.append(f"{item['id']}: решение «own» без текста")
                continue
            edits.append(Edit(s, e, lead + text + trail, "own", item["id"], region))
        else:  # illegible
            guess = text or original.strip() or item["edit"]["b_text"].strip()
            edits.append(Edit(s, e, lead + uncertain(guess) + trail, "illegible", item["id"], region))
    return edits, problems, warnings


def apply_edits(a_text: str, edits: list[Edit], width: int, height: int) -> tuple[str, list[dict], list[str]]:
    """Text A with the edits applied; segments give the final-text range of every edit."""
    out: list[str] = []
    segments: list[dict] = []
    problems: list[str] = []
    cursor = length = 0
    for edit in sorted(edits, key=lambda x: (x.start, x.end)):
        if edit.start < cursor:
            problems.append(f"{edit.item_id}: правка пересекается с соседней и не применена")
            continue
        kept = a_text[cursor:edit.start]
        out.append(kept)
        length += len(kept)
        out.append(edit.text)
        core_start = length + len(edit.text) - len(edit.text.lstrip())
        core_end = length + len(edit.text.rstrip())
        if core_end > core_start:
            segments.append({
                "start": core_start, "end": core_end, "status": edit.status, "id": edit.item_id,
                "bbox": region_bbox(edit.region, width, height),
                "kept": [[length + s, length + e] for s, e in edit.kept],
            })
        length += len(edit.text)
        cursor = edit.end
    out.append(a_text[cursor:])
    return "".join(out), segments, problems


def apply_corrections(text: str, segments: list[dict], corrections: list[dict], width: int, height: int) -> tuple[str, list[dict], list[str]]:
    """The reviewer's corrections, applied last: each replaces one exact, unique fragment of the page.

    Arbiter segments that a correction overlaps are dropped: the person's reading wins there.
    """
    problems: list[str] = []
    for n, fix in enumerate(corrections, 1):
        find, replace = nfc(str(fix.get("find", ""))), nfc(str(fix.get("replace", "")))
        count = text.count(find) if find else 0
        if count != 1:
            problems.append(f"правка человека h{n} не применена: фрагмент «{find}» встречается в тексте {count} раз")
            continue
        start = text.index(find)
        end = start + len(find)
        delta = len(replace) - len(find)
        moved = []
        for seg in segments:
            if seg["end"] <= start:
                moved.append(seg)
            elif seg["start"] >= end:
                moved.append({**seg, "start": seg["start"] + delta, "end": seg["end"] + delta,
                              "kept": [[s + delta, e + delta] for s, e in seg.get("kept", [])]})
        lead = len(replace) - len(replace.lstrip())
        if replace.strip():
            moved.append({"start": start + lead, "end": start + len(replace.rstrip()), "status": "human", "id": f"h{n}",
                          "bbox": region_bbox(valid_region(fix.get("region")), width, height), "kept": []})
        segments = sorted(moved, key=lambda s: (s["start"], s["end"]))
        text = text[:start] + replace + text[end:]
    return text, segments, problems


def number_entries(final: str, segments: list[dict], ocr: dict, height: int, service: list[tuple[int, int]]) -> list[dict]:
    tokens = tokenize(final)
    line_total = final.count("\n") + 1
    entries = []
    for k, tok in enumerate(tokens):
        if tok.kind != "num":
            continue
        segment = next((s for s in segments if s["start"] <= tok.start and tok.end <= s["end"] and s["status"] != "marker"), None)
        seen = vision_support(ocr, tokens, k, height)
        kept = segment is not None and [tok.start, tok.end] in segment.get("kept", [])
        if segment is None:
            status = "ok" if seen else "unconfirmed"
        elif kept:  # both readers agreed and the decision left it as it was; the arbiter saw it in place
            status = "ok" if seen else "confirmed"
        elif segment["status"] == "resolved":
            status = "resolved" if seen else "resolved_unconfirmed"
        else:
            status = NUMBER_STATUS_BY_SEGMENT[segment["status"]]
        same_line = [t.norm for i, t in enumerate(tokens) if i != k and t.line == tok.line and distinctive(t)]
        bbox, y_est = locate(
            ocr["observations"], {tok.norm}, neighbour_norms(tokens, k, k + 1), (tok.line + 0.5) / line_total, height, same_line
        )
        if not (seen and bbox) and segment is not None and segment.get("bbox"):
            bbox = segment["bbox"]  # Vision did not see it here: show where the arbiter or the reviewer looked
        elif not seen:
            bbox = None  # a box Vision found for the same digits elsewhere would point to the wrong place
        entries.append({
            "start": tok.start, "end": tok.end, "status": status, "bbox": bbox, "y": round(y_est),
            "zone": "service" if in_ranges(tok.start, tok.end, service) else "medical",
        })
    return entries


def review_state(status: dict, key: str, final_sha: str, handled: set[str], replies: dict[str, str]) -> dict:
    mark = status.get(key)
    if not mark:
        return {"state": "pending", "comment": "", "comment_open": False}
    state = "pending"
    if mark.get("verified"):
        state = "verified" if mark.get("final_sha256") == final_sha else "outdated"
    comment = mark.get("comment", "").strip()
    return {
        "state": state,
        "comment": comment,
        "comment_open": bool(comment) and comment not in handled,
        "reply": replies.get(comment, ""),
        "updated_at": mark.get("updated_at", ""),
    }


def build_page(doc: str, entry: dict, status: dict) -> dict:
    page = entry["page"]
    paths = page_paths(doc, page)
    compare = load_json(paths["compare"])
    if compare is None:
        return {"page": page, "problems": ["нет сравнения: запусти compare.py"], "ready": False}
    width, height = entry["width"], entry["height"]
    a_text = load_text(paths["read_a"])
    decisions_doc = load_json(paths["decisions"], {})
    problems: list[str] = []
    open_items = [d for d in compare["discrepancies"] if d["needs_decision"]]
    if open_items and decisions_doc.get("compare_id") != compare["compare_id"]:
        problems.append("решения арбитра отсутствуют или сделаны для другой версии сравнения")
        decisions_doc = {}
    decisions = {d.get("id"): d for d in decisions_doc.get("decisions", [])}
    unknown = sorted(set(decisions) - {d["id"] for d in open_items})
    if unknown:
        problems.append(f"решения для несуществующих расхождений: {', '.join(map(str, unknown))}")

    edits, edit_problems, warnings = plan_edits(a_text, compare, decisions)
    agreed = agreed_ranges(a_text, load_text(paths["read_b"]))
    edits = [mark_kept(e, a_text, agreed) for e in edits]
    final, segments, apply_problems = apply_edits(a_text, edits, width, height)
    corrections_doc = load_json(paths["corrections"], {})
    final, segments, correction_problems = apply_corrections(final, segments, corrections_doc.get("corrections", []), width, height)
    problems += edit_problems + apply_problems + correction_problems

    service = service_ranges(final)
    for match in ATTENTION_RE.finditer(final):
        segments.append({"start": match.start(), "end": match.end(), "status": "marker", "id": ""})
    for seg in segments:
        seg["zone"] = "service" if in_ranges(seg["start"], seg["end"], service) else "medical"
    segments.sort(key=lambda s: (s["start"], s["end"]))

    ocr = load_ocr(paths["ocr"])
    numbers = number_entries(final, segments, ocr, height, service)
    final_sha = sha256_bytes(final.encode())
    counts = Counter(n["status"] for n in numbers if n["zone"] == "medical")
    counts["markers"] = sum(1 for s in segments if s["status"] == "marker" and s["zone"] == "medical")
    counts["service"] = sum(1 for n in numbers if n["zone"] == "service" and n["status"] in NEEDS_LOOK) + sum(
        1 for s in segments if s["status"] == "marker" and s["zone"] == "service")
    a_service = service_ranges(a_text)
    items = []
    for item in compare["discrepancies"]:
        shown = {k: v for k, v in item.items() if k != "edit"}
        shown["decision"] = decisions.get(item["id"])
        if shown["decision"]:
            shown["region_bbox"] = region_bbox(valid_region(shown["decision"].get("region")), width, height)
        span = item.get("edit", {}).get("a_span")
        shown["zone"] = "service" if span and in_ranges(span[0], max(span[0], span[1]), a_service) else "medical"
        items.append(shown)

    handled = {str(x.get("comment", "")).strip() for x in corrections_doc.get("corrections", []) + corrections_doc.get("replies", [])}
    replies = {str(x.get("comment", "")).strip(): str(x.get("reply", "")) for x in corrections_doc.get("replies", [])}
    ready = not problems
    if ready:
        paths["final"].parent.mkdir(parents=True, exist_ok=True)
        paths["final"].write_text(final, encoding="utf-8")
    return {
        "page": page,
        "ready": ready,
        "image": entry["image"],
        "width": width,
        "height": height,
        "final": final,
        "final_sha256": final_sha,
        "segments": segments,
        "numbers": numbers,
        "counts": dict(counts),
        "discrepancies": items,
        "extra_findings": decisions_doc.get("extra_findings", []),
        "corrections": corrections_doc.get("corrections", []),
        "problems": problems,
        "warnings": warnings,
        "review": review_state(status, f"{doc}#p{page}", final_sha, handled, replies),
    }


def made_by_this_tool(path: Path) -> bool:
    """A Markdown file this tool may overwrite: one it wrote itself (current or older format)."""
    head = path.read_text(encoding="utf-8")[:2000]
    return f'transcribed_by: "{TOOL} ' in head or "transcription_pipeline:" in head


def render_markdown(meta: dict, pages: list[dict], config: dict) -> str:
    """The lasting document. Its properties describe the document, not the tool: only
    `transcribed_by` and `transcription_log` say how it was made."""
    verified_pages = [p for p in pages if p["review"]["state"] == "verified"]
    verified = len(verified_pages) == len(pages)
    verified_at = max(p["review"].get("updated_at", "") for p in verified_pages)[:10] if verified else ""
    open_marks = sum(len(ATTENTION_RE.findall(p["final"])) for p in pages)  # [?…], [неразборчиво], [обрезано] left in the text
    scan = Path(meta["pdf"]).name  # the scan lies next to this file
    q = lambda s: '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    lines = ["---", f"title: {q(meta['title'])}"]
    if meta["date"]:
        lines.append(f"date: {meta['date']}")
    if meta["source"]:
        lines.append(f"source: {q(meta['source'])}")
    lines += [
        f"scan: {q(scan)}",
        f"scan_sha256: {meta['sha256']}",
        f"pages: {len(pages)}",
        f"verified: {'true' if verified else 'false'}",
        f"verified_pages: {q(f'{len(verified_pages)}/{len(pages)}')}",
    ]
    if verified:
        lines.append(f"verified_at: {verified_at}")
    lines += [
        f"open_marks: {open_marks}",
        f"transcribed_by: {q(TOOL + ' ' + config['pipeline_version'])}",
        f"transcription_log: {q(rel(LOG))}",
        "---",
        "",
        f"# {meta['title']}" + (f" — {meta['date']}" if meta["date"] else ""),
        "",
        f"Дословная транскрипция скана [{scan}](<{scan}>).",
        "",
    ]
    for p in pages:
        if len(pages) > 1:
            lines += [f"## Страница {p['page']}", ""]
        lines += [p["final"].rstrip("\n"), ""]
    return "\n".join(lines)


def settle_scan(meta: dict) -> str | None:
    """Move the scan from the inbox next to its Markdown; returns a problem, or None.

    The scan is looked up where the log last saw it, then by name in the documents folder and the
    inbox, so renaming those folders in the settings does not lose it.
    """
    name = Path(meta["pdf"]).name
    candidates = [ROOT / meta["pdf"], DOCUMENTS / name, INBOX / name]
    source = next((c for c in candidates if c.exists()), candidates[0])
    target = DOCUMENTS / name
    if source.resolve() == target.resolve():
        meta["pdf"] = rel(target)
        return None
    if target.exists():
        return f"в {rel(DOCUMENTS)}/ уже есть {target.name}: скан оставлен в {meta['pdf']}"
    if not source.exists():
        return f"скан {name} не найден ни в {rel(DOCUMENTS)}/, ни в {rel(INBOX)}/"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    meta["pdf"] = rel(target)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble final texts, Markdown files and review data.")
    parser.add_argument("docs", nargs="*", help="parts of document names (default: all)")
    args = parser.parse_args()
    require_project()

    manifest = load_manifest()
    config = load_config()
    status = load_json(REVIEW_STATUS, {})
    review = load_json(REVIEW_DATA, {"documents": []})
    by_doc = {d["doc"]: d for d in review["documents"]}
    failed = 0

    for doc in select_docs(manifest, args.docs):
        meta = manifest["documents"][doc]
        pages = [build_page(doc, entry, status) for entry in meta["pages"]]
        if any("final" not in p for p in pages):
            failed += 1
            for p in pages:
                for problem in p["problems"]:
                    print(f"  ✗ {doc} p{p['page']}: {problem}")
            continue
        by_doc[doc] = {
            "doc": doc,
            "title": meta["title"],
            "date": meta["date"],
            "source": meta["source"],
            "pdf": meta["pdf"],
            "pages": pages,
        }
        totals = Counter()
        for p in pages:
            totals.update(p["counts"])
        print(f"{doc}: {dict(sorted(totals.items()))}")
        for p in pages:
            for warning in p["warnings"]:
                print(f"  ! p{p['page']}: {warning}")
        problems = [f"p{p['page']}: {problem}" for p in pages for problem in p["problems"]]
        out = DOCUMENTS / f"{doc}.md"
        if out.exists() and not made_by_this_tool(out):
            problems.append(f"{rel(out)} сделан не этим инструментом: не перезаписан")
        for problem in problems:
            print(f"  ✗ {problem}")
        if problems:
            failed += 1
            print(f"  Markdown not written: {rel(out)}")
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        text = render_markdown(meta, pages, config)
        out.write_text(text, encoding="utf-8")
        moved = settle_scan(meta)
        if moved:
            failed += 1
            print(f"  ✗ {moved}")
        by_doc[doc]["pdf"] = meta["pdf"]
        meta["result"] = {"markdown": rel(out), "markdown_sha256": sha256_bytes(text.encode()), "counts": dict(sorted(totals.items()))}
        print(f"  → {rel(out)} (scan: {meta['pdf']})")

    save_json(MANIFEST_PATH, manifest)
    review["documents"] = sorted(by_doc.values(), key=lambda d: (d["date"], d["doc"]))
    save_json(REVIEW_DATA, review)
    print(f"review data: {rel(REVIEW_DATA)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
