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


# ------------------------------------------------------------------ live crawl, 2026-11-07
# The CT link (alcopa-auction.fr/getDocument/ct/...) redirects to Amazon S3, whose root
# answers robots.txt with 403 AccessDenied XML. That stopped the first real crawl as "blocked".

from urllib.parse import urlsplit  # noqa: E402

from auction_agent import http_client  # noqa: E402
from auction_agent.http_client import RobotsUnreadableError, _site  # noqa: E402

S3_DENIED = (403, {"Content-Type": "application/xml", "Server": "AmazonS3"},
             b'<?xml version="1.0" encoding="UTF-8"?><Error><Code>AccessDenied</Code>'
             b"<Message>Access Denied</Message></Error>")
PDF_OK = (200, {"Content-Type": "application/pdf"}, b"%PDF-1.4 test")


@pytest.fixture
def hosts():
    """Start independent local servers: hosts({path: route}) -> (base_url, hits)."""
    started = []

    def start(routes):
        hits = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                hits[self.path] = hits.get(self.path, 0) + 1
                status, headers, body = routes.get(self.path, (404, {}, b"not found"))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}", hits

    yield start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def ports_are_sites(monkeypatch):
    """Both local servers are 127.0.0.1: let each port stand for its own site."""
    monkeypatch.setattr(http_client, "_site", lambda url: urlsplit(url).netloc)


def _site_with_storage(hosts, storage_routes, doc="/bucket/ct.pdf"):
    storage, storage_hits = hosts({"/robots.txt": S3_DENIED, **storage_routes})
    site, site_hits = hosts({
        "/robots.txt": (200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /private/\n"),
        "/getDocument/ct/x": (302, {"Location": f"{storage}{doc}"}, b""),
        "/getDocument/ct/y": (302, {"Location": f"{storage}/bucket/other.pdf"}, b""),
    })
    return site, site_hits, storage, storage_hits


def test_redirect_to_storage_whose_robots_is_403_xml_fetches_the_document(hosts, client, ports_are_sites):
    site, _, storage, storage_hits = _site_with_storage(
        hosts, {"/bucket/ct.pdf": PDF_OK, "/bucket/other.pdf": PDF_OK})
    result = client.get(f"{site}/getDocument/ct/x")
    assert result.status == 200 and result.content.startswith(b"%PDF")
    assert client.get(f"{site}/getDocument/ct/y").status == 200
    assert storage_hits["/robots.txt"] == 1  # read once, cached as "unavailable"


def test_a_direct_request_to_that_storage_host_stays_strict(hosts, client, ports_are_sites):
    site, _, storage, storage_hits = _site_with_storage(hosts, {"/bucket/ct.pdf": PDF_OK})
    assert client.get(f"{site}/getDocument/ct/x").status == 200
    with pytest.raises(BlockedError):
        client.get(f"{storage}/bucket/ct.pdf")  # not reached through the auction site
    with pytest.raises(BlockedError):
        client.robots_allows(f"{storage}/bucket/ct.pdf")
    assert storage_hits["/bucket/ct.pdf"] == 1


@pytest.mark.parametrize("robots", [
    (403, {"Content-Type": "text/html", "x-amzn-waf-action": "challenge"}, b"<html></html>"),
    (403, {"Content-Type": "text/html"}, b"<html><title>Just a moment...</title></html>"),
    (200, {"Content-Type": "text/html"}, b"<html><title>Attention Required! | Cloudflare</title></html>"),
    (405, {"Content-Type": "text/html", "cf-mitigated": "challenge"}, b"<html></html>"),
])
def test_a_challenge_on_the_storage_robots_still_blocks(hosts, client, ports_are_sites, robots):
    site, _, storage, storage_hits = _site_with_storage(hosts, {"/robots.txt": robots, "/bucket/ct.pdf": PDF_OK})
    with pytest.raises(BlockedError):
        client.get(f"{site}/getDocument/ct/x")
    assert "/bucket/ct.pdf" not in storage_hits


def test_a_refused_document_on_the_storage_host_still_blocks(hosts, client, ports_are_sites):
    site, _, _, _ = _site_with_storage(hosts, {"/bucket/ct.pdf": S3_DENIED})
    with pytest.raises(BlockedError):
        client.get(f"{site}/getDocument/ct/x")


def test_rate_limited_storage_robots_fails_closed(hosts, client, ports_are_sites):
    site, _, _, storage_hits = _site_with_storage(
        hosts, {"/robots.txt": (429, {}, b"slow down"), "/bucket/ct.pdf": PDF_OK})
    with pytest.raises(RobotsUnreadableError):
        client.get(f"{site}/getDocument/ct/x")
    assert "/bucket/ct.pdf" not in storage_hits


@pytest.mark.parametrize("status", [403, 405])
def test_the_auction_sites_own_robots_403_405_still_blocks(hosts, client, ports_are_sites, status):
    site, hits = hosts({"/robots.txt": (status, {"Content-Type": "application/xml"}, b"<Error/>"),
                        "/page": (200, HTML, b"ok")})
    with pytest.raises(BlockedError):
        client.get(f"{site}/page")
    assert "/page" not in hits


def test_a_redirect_within_the_same_site_stays_strict(hosts, client):
    """Without ports_are_sites both servers are 127.0.0.1: the same site, so 403 blocks."""
    site, _, storage, storage_hits = _site_with_storage(hosts, {"/bucket/ct.pdf": PDF_OK})
    with pytest.raises(BlockedError):
        client.get(f"{site}/getDocument/ct/x")
    assert "/bucket/ct.pdf" not in storage_hits


def test_storage_robots_rules_are_still_obeyed_when_readable(hosts, client, ports_are_sites):
    site, _, _, storage_hits = _site_with_storage(hosts, {
        "/robots.txt": (200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /bucket/\n"),
        "/bucket/ct.pdf": PDF_OK})
    with pytest.raises(RobotsDisallowedError):
        client.get(f"{site}/getDocument/ct/x")
    assert "/bucket/ct.pdf" not in storage_hits


@pytest.mark.parametrize("url,site", [
    ("https://www.alcopa-auction.fr/getDocument/ct/x", "alcopa-auction.fr"),
    ("https://alcopa-auction.fr/", "alcopa-auction.fr"),
    ("https://WWW.Alcopa-Auction.FR./x", "alcopa-auction.fr"),
    ("https://s3.eu-west-3.amazonaws.com/bucket/key.pdf?X-Amz-Signature=abc", "amazonaws.com"),
    ("http://127.0.0.1:8000/x", "127.0.0.1"),
    ("http://[::1]:8000/x", "::1"),
    ("http://localhost:1/", "localhost"),
])
def test_site_of_a_url(url, site):
    assert _site(url) == site
