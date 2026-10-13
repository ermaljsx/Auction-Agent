"""PoliteClient against a real local HTTP server (no external network)."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from auction_agent.http_client import (
    BlockedError,
    DocumentTooLargeError,
    PoliteClient,
    RateLimitedError,
    RobotsDisallowedError,
)


class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}
    hits: dict = {}

    def do_GET(self):  # noqa: N802
        _Handler.hits[self.path] = _Handler.hits.get(self.path, 0) + 1
        route = _Handler.routes.get(self.path)
        if callable(route):
            route = route(_Handler.hits[self.path])
        if route is None:
            status, headers, body = 404, {}, b"not found"
        else:
            status, headers, body = route
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    _Handler.routes, _Handler.hits = {}, {}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def client(settings, monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    sleeps = []
    c = PoliteClient(settings, sleep=sleeps.append)
    c.sleeps = sleeps
    yield c
    c.close()


HTML = {"Content-Type": "text/html; charset=utf-8"}


def test_get_ok_and_robots_allow(server, client):
    _Handler.routes = {
        "/robots.txt": (200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /private/\n"),
        "/page": (200, HTML, "<p>Bonjour é</p>".encode("utf-8")),
    }
    result = client.get(f"{server}/page")
    assert result.status == 200 and "Bonjour é" in result.text
    with pytest.raises(RobotsDisallowedError):
        client.get(f"{server}/private/x")
    assert _Handler.hits["/robots.txt"] == 1  # cached per host


def test_missing_robots_allows_everything(server, client):
    _Handler.routes = {"/page": (200, HTML, b"ok")}
    assert client.get(f"{server}/page").status == 200


def test_unreadable_robots_fails_closed(server, client):
    _Handler.routes = {"/robots.txt": (500, {}, b"boom"), "/page": (200, HTML, b"ok")}
    with pytest.raises(RobotsDisallowedError):
        client.get(f"{server}/page")


def test_waf_challenge_is_blocked(server, client):
    _Handler.routes = {
        "/robots.txt": (200, {}, b""),
        "/page": (405, HTML, b"<html>challenge</html>"),
        "/soft": (200, {**HTML, "x-amzn-waf-action": "captcha"}, b"<html></html>"),
        "/cf": (200, HTML, b"<html><title>Just a moment...</title></html>"),
    }
    for path in ("/page", "/soft", "/cf"):
        with pytest.raises(BlockedError):
            client.get(f"{server}{path}")


def test_short_retry_after_is_honoured_once(server, client):
    _Handler.routes = {
        "/robots.txt": (200, {}, b""),
        "/slow": lambda n: (429, {"Retry-After": "2"}, b"") if n == 1 else (200, HTML, b"ok"),
    }
    assert client.get(f"{server}/slow").status == 200
    assert 2.0 in client.sleeps


def test_long_retry_after_fails_closed(server, client):
    _Handler.routes = {"/robots.txt": (200, {}, b""), "/slow": (503, {"Retry-After": "3600"}, b"")}
    with pytest.raises(RateLimitedError):
        client.get(f"{server}/slow")
    assert 3600.0 not in client.sleeps


def test_retry_on_transient_5xx(server, client):
    _Handler.routes = {
        "/robots.txt": (200, {}, b""),
        "/flaky": lambda n: (502, {}, b"") if n < 3 else (200, HTML, b"ok"),
    }
    assert client.get(f"{server}/flaky").status == 200
    assert _Handler.hits["/flaky"] == 3


def test_document_size_cap(server, client):
    _Handler.routes = {"/robots.txt": (200, {}, b""), "/big.bin": (200, {}, b"%PDF-" + b"x" * 5000)}
    with pytest.raises(DocumentTooLargeError):
        client.get(f"{server}/big.bin", max_bytes=1000)


def test_404_is_returned_not_raised(server, client):
    _Handler.routes = {"/robots.txt": (200, {}, b"")}
    assert client.get(f"{server}/gone").status == 404
