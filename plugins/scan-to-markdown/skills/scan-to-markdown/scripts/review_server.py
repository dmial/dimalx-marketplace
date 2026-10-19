#!/usr/bin/env python3
"""Local review page: the scan on the left, the transcription on the right, a "verified" mark per page.

Usage: python3 review_server.py [--port N]   (from the project folder; default port from settings)
Listens on 127.0.0.1 only. Marks and comments are saved to logs/scan-to-markdown/review-status.json together with
the checksum of the page text they refer to, so a mark goes stale if the text changes later.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))  # lets `python3 -I` find common.py
from common import LOG, ROOT, SETTINGS, SKILL_DIR, CACHE, load_json, require_project, save_json

STATUS_PATH = LOG / "review-status.json" if LOG else None
DATA_PATH = CACHE / "review-data.json" if CACHE else None
PAGE_PATH = SKILL_DIR / "review" / "index.html"
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/":
            self.send_file(PAGE_PATH, "text/html; charset=utf-8")
        elif path == "/data":
            data = load_json(DATA_PATH, {"documents": []})
            self.send_bytes(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        elif path == "/guide":
            self.send_file(SKILL_DIR / "review" / "guide.md", "text/plain; charset=utf-8")
        elif path.startswith("/files/"):
            # Only images from the cache (pages, crops) are served.
            target = (ROOT / path[len("/files/"):]).resolve()
            if CACHE.resolve() in target.parents and target.is_file():
                self.send_file(target, mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/status":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        key = f"{body['doc']}#p{int(body['page'])}"
        mark = {
            "verified": bool(body.get("verified")),
            "comment": str(body.get("comment", "")).strip(),
            "final_sha256": str(body.get("final_sha256", "")),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with LOCK:
            status = load_json(STATUS_PATH, {})
            status[key] = mark
            save_json(STATUS_PATH, dict(sorted(status.items())))
            self.update_data(body["doc"], int(body["page"]), mark)
        self.send_bytes(json.dumps(mark, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    @staticmethod
    def update_data(doc: str, page: int, mark: dict) -> None:
        """Keep the derived review data in step, so a reload shows the new mark."""
        data = load_json(DATA_PATH)
        if not data:
            return
        for d in data["documents"]:
            if d["doc"] != doc:
                continue
            for p in d["pages"]:
                if p["page"] == page:
                    state = "pending"
                    if mark["verified"]:
                        state = "verified" if mark["final_sha256"] == p.get("final_sha256") else "outdated"
                    previous = p.get("review", {})
                    same_comment = mark["comment"] == previous.get("comment", "")
                    p["review"] = {
                        "state": state,
                        "comment": mark["comment"],
                        # A new comment waits for the agent; an unchanged one keeps its state.
                        "comment_open": bool(mark["comment"]) and (not same_comment or previous.get("comment_open", False)),
                        "reply": previous.get("reply", "") if same_comment else "",
                        "updated_at": mark["updated_at"],
                    }
        save_json(DATA_PATH, data)

    def send_file(self, path: Path, content_type: str) -> None:
        self.send_bytes(path.read_bytes(), content_type)

    def send_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the local review page.")
    parser.add_argument("--port", type=int, default=SETTINGS.get("review_port", 8770))
    args = parser.parse_args()
    require_project()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"review page: http://127.0.0.1:{args.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
