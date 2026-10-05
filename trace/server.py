"""本地静态/数据服务。

只读取两类本地文件：
- trace/graph.json（AST 调用图）
- 项目内被 graph.json 引用的 .py 源码

不导入、不执行任何被分析模块；不访问网络。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACE_DIR = os.path.join(ROOT, "trace")
WEB_DIR = os.path.join(TRACE_DIR, "web")
GRAPH_PATH = os.path.join(TRACE_DIR, "graph.json")


def _load_graph():
    try:
        with open(GRAPH_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None


def _safe_join(base, *parts):
    target = os.path.abspath(os.path.join(base, *parts))
    base_abs = os.path.abspath(base)
    if target != base_abs and not target.startswith(base_abs + os.sep):
        return None
    return target


class TraceHandler(BaseHTTPRequestHandler):
    server_version = "TraceHTTP/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[trace] " + (fmt % args) + "\n")

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            return self._serve_static("index.html", "text/html; charset=utf-8")

        if path == "/api/graph":
            graph = _load_graph()
            if graph is None:
                return self._send(404, json.dumps(
                    {"error": "trace/graph.json 不存在，请先运行 python trace/analyzer.py"},
                    ensure_ascii=False))
            return self._send(200, json.dumps(graph, ensure_ascii=False))

        if path == "/api/source":
            return self._serve_source(parsed)

        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            return self._serve_static(rel)

        self._send(404, json.dumps({"error": "not found"}, ensure_ascii=False))

    def _serve_source(self, parsed):
        query = dict(item.split("=", 1) for item in parsed.query.split("&")
                     if "=" in item)
        rel = query.get("file", "")
        # 仅允许 graph.json 中出现过的源码文件
        graph = _load_graph()
        if graph is None:
            return self._send(404, json.dumps({"error": "graph missing"},
                                              ensure_ascii=False))
        allowed = {node["file"] for node in graph.get("nodes", [])}
        if rel not in allowed:
            return self._send(403, json.dumps(
                {"error": "file not referenced by graph"},
                ensure_ascii=False))
        target = _safe_join(ROOT, rel.replace("/", os.sep))
        if not target or not target.endswith(".py") or not os.path.isfile(target):
            return self._send(404, json.dumps({"error": "source missing"},
                                              ensure_ascii=False))
        with open(target, "r", encoding="utf-8") as handle:
            source = handle.read()
        return self._send(200, json.dumps({"file": rel, "source": source},
                                          ensure_ascii=False))

    def _serve_static(self, rel, content_type=None):
        target = _safe_join(WEB_DIR, rel.replace("/", os.sep))
        if not target or not os.path.isfile(target):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        if content_type is None:
            content_type = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".svg": "image/svg+xml",
            }.get(os.path.splitext(target)[1], "application/octet-stream")
        with open(target, "rb") as handle:
            return self._send(200, handle.read(), content_type)


def main(argv=None):
    parser = argparse.ArgumentParser(description="调用图追踪页面服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    if not os.path.exists(GRAPH_PATH):
        print("[trace] graph.json 不存在，先执行一次全量扫描……")
        import analyzer  # type: ignore
        analyzer.scan(ROOT, TRACE_DIR, force_full=True)

    httpd = ThreadingHTTPServer((args.host, args.port), TraceHandler)
    print("[trace] 服务已启动: http://%s:%d" % (args.host, args.port))
    print("[trace] 数据源: trace/graph.json + 项目内 .py 源码（只读）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[trace] 已停止")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
