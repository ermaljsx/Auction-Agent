"""Local preview of the private site: ``python -m auction_agent preview``.

Serves ``web/`` on 127.0.0.1 only, with the headers from ``web/vercel.json``, and answers
``/api/snapshot`` from the folder ``publish --out`` wrote (or, with ``--blob``, from the
private Vercel Blob store through the site's own function). The status codes match the
Vercel function, so the page behaves exactly as online. Standard library only.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urlsplit

from .config import PROJECT_DIR

WEB_DIR = PROJECT_DIR / "web"
SNAPSHOT_PATH = "snapshot/latest.json"  # same key as blobstore.SNAPSHOT_PATH
MAX_BYTES = 2_000_000  # same cap as web/api/snapshot.py
# Only the files the site really serves; never the function's source or anything else.
# Fixed types: on Windows, mimetypes reads the registry, which can map .js to text/plain,
# and the page's "nosniff" header would then make the browser refuse the script.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
    ".woff2": "font/woff2",
    ".json": "application/json; charset=utf-8",  # the page's dictionaries only: see JSON_DIRS
}
# JSON is served only from these folders (web/i18n/fr.json…): vercel.json stays private.
JSON_DIRS = ("i18n",)

SnapshotSource = Callable[[], tuple]  # () -> (HTTP status, JSON body bytes)


def _error(status: int, code: str) -> tuple:
    return status, json.dumps({"status": code}).encode("utf-8")


def folder_source(folder: Path) -> SnapshotSource:
    """Read <folder>/snapshot/latest.json on every request (a new publish shows at once)."""

    def load() -> tuple:
        path = Path(folder) / SNAPSHOT_PATH
        try:
            with path.open("rb") as fh:
                body = fh.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return _error(503, "no_snapshot")
        except OSError:
            return _error(502, "storage_unavailable")
        if len(body) > MAX_BYTES:
            return _error(502, "snapshot_too_large")
        try:
            json.loads(body)
        except ValueError:
            return _error(502, "snapshot_unreadable")
        return 200, body

    return load


def blob_source(token: Optional[str] = None) -> SnapshotSource:
    """Read the live snapshot through web/api/snapshot.py, exactly as the site does."""
    spec = importlib.util.spec_from_file_location("_site_snapshot_api", WEB_DIR / "api" / "snapshot.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    secret = token if token is not None else os.environ.get("BLOB_READ_WRITE_TOKEN", "")
    return lambda: module.load_snapshot(secret)


def site_headers(web_dir: Path = WEB_DIR) -> list[tuple[str, str]]:
    """The headers vercel.json applies to every path (CSP, noindex…)."""
    try:
        config = json.loads((web_dir / "vercel.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for rule in config.get("headers", []):
        if rule.get("source") == "/(.*)":
            out += [(h["key"], h["value"]) for h in rule.get("headers", []) if "key" in h and "value" in h]
    return out


def static_file(web_dir: Path, url_path: str) -> Optional[Path]:
    """The file under web_dir for a URL path, or None (outside, hidden, api/, other types).

    A path ending in "/" is its folder's index.html, as Vercel serves it ("/en/" is the English page).
    """
    path = unquote(url_path) or "/"
    if path.endswith("/"):
        path += "index.html"
    if "\\" in path or "\x00" in path:
        return None
    parts = [p for p in path.split("/") if p]
    if not parts or parts[0] == "api" or any(p.startswith(".") for p in parts):
        return None
    root = web_dir.resolve()
    target = root.joinpath(*parts).resolve()
    suffix = target.suffix.lower()
    if root not in target.parents or not target.is_file() or suffix not in CONTENT_TYPES:
        return None
    if suffix == ".json" and (len(parts) != 2 or parts[0] not in JSON_DIRS):
        return None
    return target


class PreviewServer(ThreadingHTTPServer):
    """A busy port must fail: on Windows, SO_REUSEADDR would let this server share a port
    another program already listens on, so it binds with SO_EXCLUSIVEADDRUSE there."""

    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)  # skips HTTPServer's slow reverse-DNS lookup
        self.server_name, self.server_port = str(self.server_address[0]), int(self.server_address[1])

    def handle_error(self, request, client_address) -> None:
        """A browser closing a tab mid-request is normal: no traceback for that."""
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_server(source: SnapshotSource, port: int = 8765, web_dir: Path = WEB_DIR,
                host: str = "127.0.0.1") -> ThreadingHTTPServer:
    headers = site_headers(web_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "AuctionAgentPreview"
        sys_version = ""

        def _send(self, status: int, body: bytes, content_type: str, cache: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", str(len(body)))
            for key, value in headers:
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            # DNS rebinding: a web page could point its own name at 127.0.0.1 and read the
            # snapshot as "same origin". Only this computer's own addresses are answered.
            port = self.server.server_address[1]
            if (self.headers.get("Host") or "").strip().lower() not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                self._send(403, b"forbidden", "text/plain; charset=utf-8", "no-store")
                return
            path = urlsplit(self.path).path
            if path == "/api/snapshot":
                status, body = source()
                self._send(status, body, "application/json; charset=utf-8", "private, no-store")
                return
            target = static_file(web_dir, path)
            if target is None:
                self._send(404, b"not found", "text/plain; charset=utf-8", "no-store")
                return
            self._send(200, target.read_bytes(), CONTENT_TYPES[target.suffix.lower()], "no-store")

        do_HEAD = do_GET

        def log_message(self, *args):  # quiet: the console shows only the address
            pass

    if host != "127.0.0.1":
        raise ValueError("the preview only listens on this computer (127.0.0.1)")
    return PreviewServer((host, port), Handler)


def serve_in_thread(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread
