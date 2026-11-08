"""`python -m auction_agent preview`: the site on 127.0.0.1 with a local snapshot (no network)."""
import json
import urllib.error
import urllib.request

import pytest

from auction_agent import cli
from auction_agent.preview import (
    CONTENT_TYPES,
    MAX_BYTES,
    WEB_DIR,
    folder_source,
    make_server,
    serve_in_thread,
    site_headers,
    static_file,
)


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - local test server
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


@pytest.fixture
def preview(tmp_path, monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    server = make_server(folder_source(tmp_path), port=0)
    serve_in_thread(server)
    yield f"http://127.0.0.1:{server.server_address[1]}", tmp_path
    server.shutdown()
    server.server_close()


def _write_snapshot(folder, data: bytes):
    target = folder / "snapshot" / "latest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def test_page_and_assets_are_served_with_the_site_headers(preview):
    base, _ = preview
    status, headers, body = _get(f"{base}/")
    assert status == 200 and b"<html" in body.lower()
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    csp = dict(site_headers())["Content-Security-Policy"]
    assert headers["Content-Security-Policy"] == csp and "script-src 'self'" in csp
    assert headers["X-Content-Type-Options"] == "nosniff"
    status, headers, _ = _get(f"{base}/app.js")
    assert status == 200 and headers["Content-Type"] == "text/javascript; charset=utf-8"  # never text/plain
    assert _get(f"{base}/styles.css")[1]["Content-Type"] == "text/css; charset=utf-8"


def test_snapshot_codes_match_the_vercel_function(preview):
    base, folder = preview
    status, headers, body = _get(f"{base}/api/snapshot?t=123")
    assert (status, json.loads(body)) == (503, {"status": "no_snapshot"})
    assert headers["Cache-Control"] == "private, no-store"
    _write_snapshot(folder, b"{not json")
    assert json.loads(_get(f"{base}/api/snapshot")[2]) == {"status": "snapshot_unreadable"}
    _write_snapshot(folder, b" " * (MAX_BYTES + 1))
    assert json.loads(_get(f"{base}/api/snapshot")[2]) == {"status": "snapshot_too_large"}
    _write_snapshot(folder, json.dumps({"sale": None, "top": []}).encode("utf-8"))
    status, _, body = _get(f"{base}/api/snapshot")
    assert status == 200 and json.loads(body)["top"] == []  # re-read on every request


@pytest.mark.parametrize("path", [
    "/api/snapshot.py", "/api/", "/vercel.json", "/../README.md", "/%2e%2e/README.md",
    "/..%5cREADME.md", "/.env", "/x/../../.env", "/nope.html",
])
def test_nothing_outside_the_public_files_is_served(preview, path):
    base, _ = preview
    status, _, body = _get(f"{base}{path}")
    assert status == 404 and b"BLOB" not in body and b"import" not in body


def test_static_file_rules():
    assert static_file(WEB_DIR, "/") == (WEB_DIR / "index.html").resolve()
    assert static_file(WEB_DIR, "/about.html") == (WEB_DIR / "about.html").resolve()
    assert static_file(WEB_DIR, "/api/snapshot.py") is None
    assert static_file(WEB_DIR, "/vercel.json") is None  # JSON only from the dictionaries' folder
    assert static_file(WEB_DIR, "/..%2F..%2Fauction_agent%2Fcli.py") is None


def test_fonts_dictionaries_and_folder_pages_are_served(tmp_path):
    """Redesign v2: self-hosted fonts, the FR/EN dictionaries and /en/ (served as Vercel does)."""
    for rel, data in {"fonts/a.woff2": b"wOF2", "i18n/en.json": b"{}", "i18n/x/deep.json": b"{}",
                      "en/index.html": b"<html>", "vercel.json": b"{}", "data.json": b"{}"}.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(data)
    assert static_file(tmp_path, "/fonts/a.woff2") == (tmp_path / "fonts" / "a.woff2").resolve()
    assert CONTENT_TYPES[".woff2"] == "font/woff2"
    assert static_file(tmp_path, "/i18n/en.json") == (tmp_path / "i18n" / "en.json").resolve()
    assert static_file(tmp_path, "/en/") == (tmp_path / "en" / "index.html").resolve()
    for private in ("/vercel.json", "/data.json", "/i18n/x/deep.json", "/api/", "/fonts/"):
        assert static_file(tmp_path, private) is None, private


def test_preview_listens_only_on_this_computer():
    with pytest.raises(ValueError):
        make_server(lambda: (200, b"{}"), port=0, host="0.0.0.0")


def test_blob_mode_uses_the_sites_own_function_without_a_token(monkeypatch):
    from auction_agent.preview import blob_source
    load = blob_source(token="")
    assert load() == (503, json.dumps({"status": "not_configured"}).encode("utf-8"))


def test_preview_command_reports_a_busy_port(settings, monkeypatch, capsys):
    import argparse
    from auction_agent import preview as preview_module

    def must_not_serve(self, *args, **kwargs):
        raise AssertionError("bound a port another server already listens on")

    monkeypatch.setattr(preview_module.PreviewServer, "serve_forever", must_not_serve)  # fail, never hang
    busy = make_server(lambda: (200, b"{}"), port=0)
    try:
        args = argparse.Namespace(blob=False, snapshot=None, port=busy.server_address[1], open=False)
        assert cli.cmd_preview(args, settings) == 2
        assert "not free" in capsys.readouterr().out
    finally:
        busy.server_close()


def test_stats_prints_the_sale_day_in_paris(settings, capsys):
    import argparse
    from auction_agent import db
    with db.open_db(settings.db_path) as conn:
        with db.write_tx(conn):
            conn.execute(
                "INSERT INTO sales (house, external_id, kind, url, starts_at, status, first_seen_at)"
                " VALUES ('alcopa', '10209', 'room', 'https://www.alcopa-auction.fr/x', "
                "'2026-11-08T23:00:00Z', 'upcoming', '2026-11-07T09:00:00Z')")
    cli.cmd_stats(argparse.Namespace(), settings)
    out = capsys.readouterr().out
    assert "2026-11-09" in out and "2026-11-08" not in out


# ------------------------------------------------------------------ independent review findings

def _raw_get(base, path, host):
    import http.client
    from urllib.parse import urlsplit
    parts = urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    try:
        conn.putrequest("GET", path, skip_host=True)
        if host is not None:
            conn.putheader("Host", host)
        conn.endheaders()
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


@pytest.mark.parametrize("host_for", [
    lambda port: "attacker.example:%d" % port,   # DNS rebinding: a web page's own name on 127.0.0.1
    lambda port: "127.0.0.1",                    # wrong port
    lambda port: "127.0.0.1.nip.io:%d" % port,
    lambda port: None,                           # no Host header at all
])
def test_requests_for_another_host_name_are_refused(preview, host_for):
    base, folder = preview
    _write_snapshot(folder, b'{"top": []}')
    port = int(base.rsplit(":", 1)[1])
    status, body = _raw_get(base, "/api/snapshot", host_for(port))
    assert status == 403 and b"top" not in body


@pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "LOCALHOST:{port}"])
def test_this_computers_own_names_are_answered(preview, host):
    base, folder = preview
    _write_snapshot(folder, b'{"top": []}')
    port = int(base.rsplit(":", 1)[1])
    assert _raw_get(base, "/api/snapshot", host.format(port=port))[0] == 200


def test_a_client_closing_the_connection_prints_no_traceback(capsys):
    from auction_agent.preview import PreviewServer
    server = make_server(lambda: (200, b"{}"), port=0)
    try:
        for exc in (ConnectionResetError(10054, "reset"), ConnectionAbortedError(), BrokenPipeError(), TimeoutError()):
            try:
                raise exc
            except OSError:
                server.handle_error(None, ("127.0.0.1", 1))
        assert capsys.readouterr().err == ""
        try:
            raise ValueError("a real bug")
        except ValueError:
            server.handle_error(None, ("127.0.0.1", 1))
        assert "ValueError" in capsys.readouterr().err  # real errors still show
    finally:
        server.server_close()
    assert issubclass(PreviewServer, object)


@pytest.mark.parametrize("value", ["87650", "-1", "65536", "abc"])
def test_preview_port_must_be_a_port(value, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["preview", "--port", value])
    assert exc.value.code == 2 and "not a port" in capsys.readouterr().err


def test_preview_port_bounds_are_accepted():
    assert cli.build_parser().parse_args(["preview", "--port", "0"]).port == 0
    assert cli.build_parser().parse_args(["preview", "--port", "65535"]).port == 65535


def test_favicon_is_served_as_svg(preview):
    base, _ = preview
    status, headers, body = _get(f"{base}/favicon.svg")
    assert status == 200 and headers["Content-Type"] == "image/svg+xml" and body.startswith(b"<svg")
    assert headers["Content-Security-Policy"] == dict(site_headers())["Content-Security-Policy"]
