#!/usr/bin/env python3
"""Build the args for the read / arbitrate workflows.

Usage (from the project folder):
  python3 plan.py read [DOC ...] [--redo]       -> <log>/plan/read.json
  python3 plan.py arbitrate [DOC ...] [--redo]  -> <log>/plan/arbitrate.json
DOC is any part of a document name. By default only pages without results are included;
--redo includes every selected page.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # lets `python3 -I` find common.py
from common import LOG, ROOT, SKILL_DIR, CACHE, load_config, load_json, load_manifest, page_paths, require_project, save_json, select_docs


def short_labels(docs: list[str]) -> dict[str, str]:
    """A short name per document for the workflow display and its failure reports: the date and
    the source, plus the part of the title that tells apart documents sharing them ("(бланк 1)")."""
    def parts(doc: str) -> tuple[str, str, str]:
        date, _, rest = doc.partition("_")
        source, _, title = rest.partition("_")
        return date, source, title or rest or doc

    labels = {doc: " ".join(filter(None, (parts(doc)[0], parts(doc)[1][:18]))) for doc in docs}
    shared = Counter(labels.values())
    for doc in docs:
        if shared[labels[doc]] > 1:
            title = parts(doc)[2]
            tail = re.search(r"\([^()]*\)\s*$", title)
            labels[doc] += " " + (tail.group().strip() if tail else title[-24:])
    shared = Counter(labels.values())
    return {doc: doc if shared[label] > 1 else label for doc, label in labels.items()}


def common_args() -> dict:
    # The workflows build file paths from these folders exactly like common.page_paths does.
    return {"root": str(ROOT), "cache": str(CACHE), "log": str(LOG), "zoom": str(SKILL_DIR / "scripts" / "zoom.py")}


def strips(entry: dict) -> list[list[int]]:
    return [[s["y0"], s["y1"]] for s in entry["strips"]]


def plan_read(docs: list[str], manifest: dict, config: dict, redo: bool) -> dict:
    pages = []
    labels = short_labels(docs)
    for doc in docs:
        for entry in manifest["documents"][doc]["pages"]:
            paths = page_paths(doc, entry["page"])
            need_a = redo or not paths["read_a"].exists()
            need_b = redo or not paths["read_b"].exists()
            if need_a or need_b:
                pages.append({
                    "doc": doc, "page": entry["page"], "label": f"{labels[doc]} p{entry['page']}",
                    "height": entry["height"], "strips": strips(entry), "need_a": need_a, "need_b": need_b,
                })
    return {
        **common_args(),
        "rules": str(SKILL_DIR / "prompts" / "reader.md"),
        "reader_a": {"model": config["reader_a"]["model"], "effort": config["reader_a"]["effort"]},
        "reader_b": {"model": config["reader_b"]["model"], "effort": config["reader_b"]["effort"]},
        "pages": pages,
    }


def plan_arbitrate(docs: list[str], manifest: dict, config: dict, redo: bool) -> dict:
    pages = []
    labels = short_labels(docs)
    for doc in docs:
        for entry in manifest["documents"][doc]["pages"]:
            paths = page_paths(doc, entry["page"])
            compare = load_json(paths["compare"])
            if compare is None:
                continue  # not read yet
            open_count = sum(1 for d in compare["discrepancies"] if d["needs_decision"])
            if open_count == 0:
                continue
            decisions = load_json(paths["decisions"])
            current = decisions is not None and decisions.get("compare_id") == compare["compare_id"]
            if current and not redo:
                continue
            pages.append({
                "doc": doc, "page": entry["page"], "label": f"{labels[doc]} p{entry['page']}",
                "strips": strips(entry), "open": open_count,
            })
    return {
        **common_args(),
        "rules": str(SKILL_DIR / "prompts" / "arbiter.md"),
        "reader_rules": str(SKILL_DIR / "prompts" / "reader.md"),
        "arbiter": {"model": config["arbiter"]["model"], "effort": config["arbiter"]["effort"]},
        "pages": pages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build workflow args.")
    parser.add_argument("step", choices=["read", "arbitrate"])
    parser.add_argument("docs", nargs="*", help="parts of document names (default: all)")
    parser.add_argument("--redo", action="store_true", help="include pages that already have results")
    args = parser.parse_args()
    require_project()

    manifest = load_manifest()
    config = load_config()
    docs = select_docs(manifest, args.docs)
    builder = plan_read if args.step == "read" else plan_arbitrate
    plan = builder(docs, manifest, config, args.redo)
    out = LOG / "plan" / f"{args.step}.json"
    save_json(out, plan)
    print(f"{out.relative_to(ROOT)}: {len(plan['pages'])} pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
