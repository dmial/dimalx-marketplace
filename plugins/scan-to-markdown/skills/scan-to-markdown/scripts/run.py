#!/usr/bin/env python3
"""The agent's single entry point: runs every code step that can run now and names the one next
thing that needs the agent or the person.

Usage (from the project folder):
  python3 run.py init [--inbox NAME] [--documents NAME]
                                    set up the inbox, the documents folder and logs/scan-to-markdown/ here
                                    (default names: inbox/ and documents/)
  python3 run.py status [DOC ...]   what is in the inbox and the stage of every document
  python3 run.py advance [DOC ...]  do what code can, then print one of:
      ACTION read | ACTION arbitrate | ACTION corrections   the agent acts, then runs advance again
      WAIT review                                           the person reviews pages
      DONE                                                  every page is verified
      STOP                                                  an error to report, not to work around
Exit codes of `advance`: 0 DONE, 10 ACTION, 20 WAIT, 1 STOP. DOC is any part of a document name.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # lets `python3 -I` find common.py
from common import (  # noqa: E402
    CACHE,
    DEFAULTS,
    DOCUMENTS,
    INBOX,
    LOG,
    LOG_SUBDIR,
    ROOT,
    SETTINGS,
    SETTINGS_NAME,
    SKILL_DIR,
    code_version,
    load_json,
    load_manifest,
    page_paths,
    rel,
    require_project,
    select_docs,
    sha256_file,
)

SCRIPTS = SKILL_DIR / "scripts"
GUIDE = SKILL_DIR / "review" / "guide.md"
EXIT = {"DONE": 0, "ACTION": 10, "WAIT": 20, "STOP": 1}


def script(name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / name), *args], cwd=ROOT, capture_output=True, text=True)


def say(kind: str, title: str, lines: list[str]) -> int:
    print(f"\n{kind} {title}")
    for line in lines:
        print(f"  {line}")
    return EXIT[kind]


# --- init -----------------------------------------------------------------------

def init(inbox: str | None, documents: str | None) -> int:
    here = Path.cwd()
    settings = here / LOG_SUBDIR / SETTINGS_NAME
    current = load_json(settings, {"paths": dict(DEFAULTS["paths"])})
    paths = current.setdefault("paths", dict(DEFAULTS["paths"]))
    for key, name in (("inbox", inbox), ("documents", documents)):
        if name and paths.get(key) != name:
            if (here / paths.get(key, "")).is_dir() and any((here / paths[key]).iterdir()):
                print(f"note: {paths[key]}/ is not empty — move its files to {name}/ (git mv) yourself")
            paths[key] = name
    if not settings.exists() or inbox or documents:
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"settings: {LOG_SUBDIR / SETTINGS_NAME} (inbox: {paths['inbox']}/, documents: {paths['documents']}/)")
    for folder in (paths["inbox"], paths["documents"]):
        if not (here / folder).exists():
            (here / folder).mkdir(parents=True)
            print(f"created {folder}/")
    ignore = here / ".gitignore"
    lines = ignore.read_text(encoding="utf-8").splitlines() if ignore.exists() else []
    wanted = [f"{LOG_SUBDIR.as_posix()}/cache/", f"{LOG_SUBDIR.as_posix()}/plan/", "__pycache__/"]
    missing = [w for w in wanted if w not in lines]
    if missing:
        ignore.write_text("\n".join(lines + missing) + "\n", encoding="utf-8")
        print(f".gitignore: added {', '.join(missing)}")
    print(f"New scans go to {paths['inbox']}/; results appear in {paths['documents']}/.")
    print(f"To keep the whole processing log out of git, add {LOG_SUBDIR.as_posix()}/ to .gitignore.")
    tools = {"pdfinfo": "poppler (brew install poppler)", "pdfimages": "poppler", "pdftoppm": "poppler",
             "swiftc": "Xcode command line tools (xcode-select --install)"}
    absent = [f"{tool} — {how}" for tool, how in tools.items() if not shutil.which(tool)]
    try:
        import PIL  # noqa: F401
    except ImportError:
        absent.append("Pillow — pip install pillow")
    for item in absent:
        print(f"missing: {item}")
    return 1 if absent else 0


# --- state ------------------------------------------------------------------------

def inbox_pdfs(selectors: list[str]) -> list[Path]:
    return [pdf for pdf in sorted(INBOX.glob("*.pdf")) if not selectors or any(sel in pdf.stem for sel in selectors)]


def changed_pdfs(manifest: dict, selectors: list[str]) -> list[Path]:
    """Inbox scans that are new or were replaced since they were prepared."""
    # Compared by file name, so renaming the inbox folder in the settings does not make old scans "new".
    known = {Path(d["pdf"]).name: d["sha256"] for d in manifest["documents"].values()}
    return [pdf for pdf in inbox_pdfs(selectors) if known.get(pdf.name) != sha256_file(pdf)]


def name_clashes(selectors: list[str]) -> list[str]:
    """Inbox scans whose name is already taken in documents/: moving them would overwrite a document."""
    return [pdf.name for pdf in inbox_pdfs(selectors) if (DOCUMENTS / pdf.name).exists()]


def stale_compare(doc: str, page: int, logic: str) -> bool:
    paths = page_paths(doc, page)
    if not (paths["read_a"].exists() and paths["read_b"].exists()):
        return False
    data = load_json(paths["compare"])
    if data is None or data.get("logic") != logic:
        return True
    newest_input = max(paths[k].stat().st_mtime for k in ("read_a", "read_b", "ocr"))
    return newest_input > paths["compare"].stat().st_mtime


def review_pages(docs: list[str]) -> list[tuple[str, dict]]:
    data = load_json(CACHE / "review-data.json", {"documents": []})
    return [(d["doc"], p) for d in data["documents"] if d["doc"] in docs for p in d["pages"]]


def status(selectors: list[str]) -> int:
    require_project()
    manifest = load_manifest()
    docs = select_docs(manifest, selectors)
    reviewed = {(doc, p["page"]): p for doc, p in review_pages(docs)}
    print(f"{'document':58} pages  read  arbiter  built  verified  comments")
    for doc in docs:
        pages = manifest["documents"][doc]["pages"]
        read = arbitrated = built = verified = comments = 0
        for entry in pages:
            paths = page_paths(doc, entry["page"])
            read += paths["read_a"].exists() and paths["read_b"].exists()
            compare = load_json(paths["compare"])
            decisions = load_json(paths["decisions"], {})
            if compare and (compare["summary"]["open"] == 0 or decisions.get("compare_id") == compare["compare_id"]):
                arbitrated += 1
            built += paths["final"].exists()
            page = reviewed.get((doc, entry["page"]), {})
            verified += page.get("review", {}).get("state") == "verified"
            comments += bool(page.get("review", {}).get("comment_open"))
        n = len(pages)
        print(f"{doc[:58]:58} {n:5}  {read}/{n}   {arbitrated}/{n}      {built}/{n}    {verified}/{n}       {comments}")
    waiting = inbox_pdfs(selectors)
    print(f"\ninbox ({rel(INBOX)}/): {len(waiting)} scan(s), {len(changed_pdfs(manifest, selectors))} not prepared yet")
    return 0


# --- advance ------------------------------------------------------------------------

def advance(selectors: list[str]) -> int:
    require_project()
    test = script("selftest.py")
    if test.returncode != 0:
        return say("STOP", "self-test failed: the comparison/assembly logic is broken", (test.stdout + test.stderr).strip().splitlines()[-15:])

    manifest = load_manifest()
    clashes = name_clashes(selectors)
    if clashes:
        return say("STOP", f"these inbox scans have the same name as files in {rel(DOCUMENTS)}/", [
            *clashes, "Rename the new scan (or remove the old document) and run advance again."])
    new = changed_pdfs(manifest, selectors)
    if new:
        print(f"preparing {len(new)} PDF(s)…", flush=True)
        prep = script("prepare.py", *[str(p) for p in new])
        if prep.returncode != 0:
            return say("STOP", "prepare.py failed", (prep.stdout + prep.stderr).strip().splitlines()[-15:])
        manifest = load_manifest()
    if not manifest["documents"]:
        return say("STOP", f"the inbox {rel(INBOX)}/ is empty", ["put the scans there and run advance again"])
    docs = select_docs(manifest, selectors)

    plan = script("plan.py", "read", *docs)
    if plan.returncode != 0:
        return say("STOP", "plan.py read failed", plan.stderr.strip().splitlines()[-10:])
    read_plan = load_json(LOG / "plan" / "read.json")
    if read_plan["pages"]:
        readings = sum(p["need_a"] + p["need_b"] for p in read_plan["pages"])
        return say("ACTION", "read", [
            f"{len(read_plan['pages'])} page(s) need reading ({readings} readings).",
            f"Workflow: scriptPath = {SKILL_DIR / 'workflows' / 'read.js'}",
            f"          args = the JSON in {LOG / 'plan' / 'read.json'} (pass it as a JSON value)",
            "When it finishes, run advance again.",
        ])

    logic = code_version("compare.py", "common.py")
    stale = sorted({doc for doc in docs for e in manifest["documents"][doc]["pages"] if stale_compare(doc, e["page"], logic)})
    if stale:
        print(f"comparing {len(stale)} document(s)…", flush=True)
        cmp = script("compare.py", *stale)
        if cmp.returncode != 0:
            return say("STOP", "compare.py failed", (cmp.stdout + cmp.stderr).strip().splitlines()[-15:])

    plan = script("plan.py", "arbitrate", *docs)
    if plan.returncode != 0:
        return say("STOP", "plan.py arbitrate failed", plan.stderr.strip().splitlines()[-10:])
    arb_plan = load_json(LOG / "plan" / "arbitrate.json")
    if arb_plan["pages"]:
        spots = sum(p["open"] for p in arb_plan["pages"])
        return say("ACTION", "arbitrate", [
            f"{len(arb_plan['pages'])} page(s) have {spots} open discrepancies.",
            f"Workflow: scriptPath = {SKILL_DIR / 'workflows' / 'arbitrate.js'}",
            f"          args = the JSON in {LOG / 'plan' / 'arbitrate.json'} (pass it as a JSON value)",
            "When it finishes, run advance again.",
        ])

    built = script("build.py", *docs)
    output = (built.stdout + built.stderr).strip().splitlines()
    if built.returncode != 0:
        failed_fixes = [line for line in output if "правка человека" in line]
        if failed_fixes:
            return say("ACTION", "corrections", ["A correction no longer matches the page text; rewrite it:", *failed_fixes])
        return say("STOP", "build.py reported problems", output[-20:])

    pages = review_pages(docs)
    open_comments = [(doc, p) for doc, p in pages if p["review"].get("comment_open")]
    if open_comments:
        lines = [f"{len(open_comments)} page(s) have reviewer comments to act on:"]
        for doc, p in open_comments:
            lines.append(f"- {doc} p{p['page']}: «{p['review']['comment']}»")
            lines.append(f"  corrections file: {page_paths(doc, p['page'])['corrections']}")
        lines.append("Look at the scan, then write the corrections file (format in SKILL.md) and run advance again.")
        return say("ACTION", "corrections", lines)

    unverified = [(doc, p) for doc, p in pages if p["review"]["state"] != "verified"]
    if unverified:
        outdated = sum(p["review"]["state"] == "outdated" for _, p in unverified)
        port = SETTINGS.get("review_port", 8770)
        return say("WAIT", "review", [
            f"{len(unverified)} of {len(pages)} page(s) are not verified by the person"
            + (f" ({outdated} changed after verification)" if outdated else "") + ".",
            f"Start the review page in the background: python3 {SCRIPTS / 'review_server.py'}  →  http://127.0.0.1:{port}/",
            "Give the person the link and this guide, then stop until they come back:",
            *[f"| {line}" for line in GUIDE.read_text(encoding="utf-8").strip().splitlines()],
        ])
    return say("DONE", "all pages are verified", [f"Every scan lies with its Markdown in {rel(DOCUMENTS)}/."])


def main() -> int:
    parser = argparse.ArgumentParser(description="scan-to-markdown dispatcher for the agent.")
    parser.add_argument("command", choices=["init", "status", "advance"])
    parser.add_argument("docs", nargs="*", help="parts of document names (default: all)")
    parser.add_argument("--inbox", help="init: name of the inbox folder (default: inbox)")
    parser.add_argument("--documents", help="init: name of the documents folder (default: documents)")
    args = parser.parse_args()
    if args.command == "init":
        return init(args.inbox, args.documents)
    if args.command == "status":
        return status(args.docs)
    return advance(args.docs)


if __name__ == "__main__":
    raise SystemExit(main())
