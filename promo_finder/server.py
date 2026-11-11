"""Serve the editor and selected run artifacts over loopback only."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


def _make_handler(out: Path, web: Path | None = None):
    """Build a handler with a closed list of routes; no source-file serving."""
    out = Path(out).resolve()
    web = (Path(web) if web is not None else Path(__file__).resolve().parent / "web").resolve()
    routes = {
        "/": (web, "index.html", "text/html; charset=utf-8"),
        "/index.html": (web, "index.html", "text/html; charset=utf-8"),
        "/app.js": (web, "app.js", "text/javascript; charset=utf-8"),
        "/style.css": (web, "style.css", "text/css; charset=utf-8"),
        "/demo.json": (web, "demo.json", "application/json; charset=utf-8"),
        "/data.json": (out, "data.json", "application/json; charset=utf-8"),
        "/ranked_promos.csv": (out, "ranked_promos.csv", "text/csv; charset=utf-8"),
        "/run_log.json": (out, "run_log.json", "application/json; charset=utf-8"),
        "/rejections.json": (out, "rejections.json", "application/json; charset=utf-8"),
        "/cutlist-prototype.zip": (Path(__file__).resolve().parent.parent, "cutlist-prototype.zip", "application/zip"),
    }

    class EditorHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._send_file(include_body=True)

        def do_HEAD(self):
            self._send_file(include_body=False)

        def _send_file(self, *, include_body: bool):
            try:
                route = routes.get(unquote(urlsplit(self.path).path))
            except ValueError:
                route = None
            if route is None:
                self.send_error(404, "Not found")
                return
            root, name, content_type = route
            path = (root / name).resolve()
            # Exact route names already prevent traversal. This check also avoids
            # following an artifact symlink outside its selected directory.
            if path.parent != root or not path.is_file():
                self.send_error(404, "Not found")
                return
            try:
                body = path.read_bytes()
            except OSError:
                self.send_error(404, "Not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if name in {"ranked_promos.csv", "cutlist-prototype.zip"}:
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def log_message(self, format, *args):
            # Keep terminal output readable while retaining ordinary access logs.
            super().log_message(format, *args)

    return EditorHandler


def serve(out: Path, port: int = 8765):
    """Serve a completed run until interrupted; never expose the input folder."""
    out = Path(out).resolve()
    if not out.is_dir():
        raise FileNotFoundError(f"Output directory does not exist: {out}")
    with ThreadingHTTPServer(("127.0.0.1", port), _make_handler(out)) as server:
        server.daemon_threads = True
        print(f"Editor: http://127.0.0.1:{server.server_port}/", flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
