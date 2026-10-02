"""Read-only trace viewer server.

Serves the static call graph (``trace/graph.json``) produced by
``trace/builder.py`` together with local project source files. The server
never executes project code and never writes anything; it only reads the
graph and ``.py`` files that live inside the project root.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
GRAPH_PATH = os.path.join(HERE, "graph.json")
WEB_DIR = os.path.join(HERE, "web")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _safe_source_path(rel):
    if not rel or not rel.endswith(".py"):
        return None
    rel = rel.replace("\\", "/").lstrip("/")
    candidate = os.path.abspath(os.path.join(ROOT, rel))
    root_prefix = os.path.abspath(ROOT) + os.sep
    if not (candidate == ROOT or candidate.startswith(root_prefix)):
        return None
    if os.path.commonpath([candidate, ROOT]) != ROOT:
        return None
    return candidate


class TraceHandler(BaseHTTPRequestHandler):
    server_version = "TraceViewer/1.0"

    def log_message(self, fmt, *args):  # quieter logging
        print("[server] " + fmt % args)

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, payload):
        self._send(status, json.dumps(payload, ensure_ascii=False))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])
        if path == "/api/health":
            return self._send_json(200, {"ok": True})
        if path == "/api/graph":
            return self._serve_graph()
        if path == "/api/source":
            return self._serve_source(query.get("file", [""])[0])
        self._send_json(404, {"error": "not found", "path": path})

    def _serve_graph(self):
        if not os.path.exists(GRAPH_PATH):
            return self._send_json(503, {
                "error": "trace/graph.json not found; run: python trace/builder.py",
            })
        try:
            with open(GRAPH_PATH, "r", encoding="utf-8") as handle:
                graph_text = handle.read()
        except OSError as exc:
            return self._send_json(500, {"error": str(exc)})
        self._send(200, graph_text)

    def _serve_source(self, rel):
        target = _safe_source_path(rel)
        if target is None or not os.path.isfile(target):
            return self._send_json(404, {"error": "source file not found or outside project root"})
        try:
            with open(target, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            return self._send_json(500, {"error": str(exc)})
        return self._send_json(200, {"file": rel, "lines": text.splitlines()})

    def _serve_static(self, rel):
        rel = rel.replace("\\", "/")
        if not rel or "/" in rel or ".." in rel:
            return self._send_json(404, {"error": "not found"})
        target = os.path.abspath(os.path.join(WEB_DIR, rel))
        if not target.startswith(os.path.abspath(WEB_DIR) + os.sep):
            return self._send_json(404, {"error": "not found"})
        if not os.path.isfile(target):
            return self._send_json(404, {"error": "not found"})
        ext = os.path.splitext(rel)[1]
        content_type = CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(target, "r", encoding="utf-8") as handle:
            self._send(200, handle.read(), content_type)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve the read-only call-graph viewer")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    if not os.path.exists(GRAPH_PATH):
        print("[server] trace/graph.json missing; building it now ...")
        from trace.builder import CallGraphBuilder  # type: ignore

        state = os.path.join(HERE, ".graph-state.json")
        CallGraphBuilder(ROOT, GRAPH_PATH, state).run()

    httpd = ThreadingHTTPServer((args.host, args.port), TraceHandler)
    print(f"[server] trace viewer on http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] shutting down")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
