"""GET /api/snapshot: this week's snapshot, read from the project's private Blob store.

Standard library only. The store token (BLOB_READ_WRITE_TOKEN) is injected by Vercel when
the store is connected to the project; it is never sent to the browser. The whole site sits
behind Vercel Authentication, so only signed-in members reach this function.
"""
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

SNAPSHOT_PATH = "snapshot/latest.json"
MAX_BYTES = 2_000_000
_TOKEN_PREFIX = "vercel_blob_rw_"


def blob_url(token: str, path: str = SNAPSHOT_PATH) -> str:
    """Private delivery URL of a blob; the store id is the first part of the token."""
    if not token.startswith(_TOKEN_PREFIX):
        raise ValueError("not a Blob read-write token")
    store_id, sep, secret = token[len(_TOKEN_PREFIX):].partition("_")
    store_id = store_id.removeprefix("store_").strip()
    if not sep or not store_id.isalnum() or not secret.strip("_"):
        raise ValueError("not a Blob read-write token")
    return f"https://{store_id.lower()}.private.blob.vercel-storage.com/{path}"


def _error(status: int, code: str) -> tuple:
    return status, json.dumps({"status": code}).encode("utf-8")


def load_snapshot(token: str, opener=urllib.request.urlopen, timeout: float = 10.0) -> tuple:
    """(HTTP status, JSON body) for the page. Never includes the token or the store URL."""
    try:
        url = blob_url((token or "").strip())
    except ValueError:
        return _error(503, "not_configured")
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token.strip()}"})
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return _error(503, "no_snapshot") if exc.code == 404 else _error(502, "storage_error")
    except (urllib.error.URLError, TimeoutError, OSError):
        return _error(502, "storage_unavailable")
    if len(body) > MAX_BYTES:
        return _error(502, "snapshot_too_large")
    try:
        json.loads(body)
    except ValueError:
        return _error(502, "snapshot_unreadable")
    return 200, body


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = load_snapshot(os.environ.get("BLOB_READ_WRITE_TOKEN", ""))
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
