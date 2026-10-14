"""Regression tests for the Phase 1 review findings on HTTP, robots.txt, the CLI and identity keys."""
import argparse
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from auction_agent import cli
from auction_agent.http_client import (
    FetchResult,
    RobotsDisallowedError,
    _charset,
    _retry_after_seconds,
)
from auction_agent.privacy import MIN_KEY_BYTES, load_identity_key
from auction_agent.robots import parse_robots
from tests.fakes import FakeHttp
from tests.test_http import HTML, _Handler, client, server  # noqa: F401  (pytest fixtures)

# ------------------------------------------------------------------ robots matcher (security-net-3, conformance-4)

ROBOTS = """
User-agent: *
Disallow: /private/
Allow: /private/public-*
Disallow: /*.pdf$
Disallow: /search?

User-agent: OtherBot
Disallow: /

User-agent: *
Disallow: /second-group/
"""


@pytest.mark.parametrize("path,allowed", [
    ("/private/x", False),
    ("/private/public-page", True),          # longer Allow wins
    ("/docs/file.pdf", False),               # * and $ wildcards
    ("/docs/file.pdf?x=1", True),            # $ anchors the end of the path
    ("/search?q=clio", False),
    ("/second-group/x", False),              # repeated '*' groups are merged
    ("/voiture-occasion/a/b-123", True),
])
def test_robots_matcher_follows_rfc_9309(path, allowed):
    rules = parse_robots(ROBOTS)
    assert rules.can_fetch("AuctionAgent", f"https://x.fr{path}") is allowed


def test_robots_allow_wins_a_tie_and_own_group_beats_star():
    rules = parse_robots("User-agent: *\nDisallow: /page\nAllow: /page\n\n"
                         "User-agent: AuctionAgent\nDisallow: /only-us/\n")
    assert rules.can_fetch("Someone", "https://x.fr/page") is True
    assert rules.can_fetch("AuctionAgent", "https://x.fr/only-us/a") is False
    assert rules.can_fetch("AuctionAgent", "https://x.fr/page") is True  # our group has no /page rule
    assert parse_robots("User-agent: OtherBot\nDisallow: /\n").can_fetch("AuctionAgent", "https://x.fr/a")


def test_robots_token_is_configurable(server, client):  # noqa: F811
    _Handler.routes = {
        "/robots.txt": (200, {}, b"User-agent: *\nAllow: /\n\nUser-agent: AuctionAgent\nDisallow: /\n"),
        "/page": (200, HTML, b"ok"),
    }
    with pytest.raises(RobotsDisallowedError):
        client.get(f"{server}/page")
    client.settings = client.settings.replace(robots_token="SomeOtherToken")
    assert client.get(f"{server}/page").status == 200


# ------------------------------------------------------------------ robots fetch failures (security-net-4, 5)

def test_failed_robots_fetch_is_not_cached_as_allow_all(server, client):  # noqa: F811
    _Handler.routes = {
        "/robots.txt": lambda n: (418, {}, b"") if n == 1 else (200, {}, b"User-agent: *\nDisallow: /private/\n"),
        "/private/x": (200, HTML, b"secret"),
    }
    with pytest.raises(RobotsDisallowedError):
        client.get(f"{server}/private/x")
    with pytest.raises(RobotsDisallowedError):  # retried, now read and enforced
        client.get(f"{server}/private/x")
    assert _Handler.hits["/robots.txt"] == 2 and "/private/x" not in _Handler.hits


def test_oversized_robots_fails_closed(server, client):  # noqa: F811
    _Handler.routes = {"/robots.txt": (200, {}, b"#" * (600 * 1024)), "/page": (200, HTML, b"ok")}
    with pytest.raises(RobotsDisallowedError):
        client.get(f"{server}/page")


# ------------------------------------------------------------------ redirects (security-net-8)

def test_redirect_to_a_disallowed_path_is_refused(server, client):  # noqa: F811
    _Handler.routes = {
        "/robots.txt": (200, {}, b"User-agent: *\nDisallow: /private/\n"),
        "/go": (302, {"Location": "/private/x"}, b""),
        "/moved": (301, {"Location": "/page"}, b""),
        "/page": (200, HTML, b"ok"),
        "/loop": (302, {"Location": "/loop"}, b""),
    }
    with pytest.raises(RobotsDisallowedError):
        client.get(f"{server}/go")
    assert "/private/x" not in _Handler.hits
    assert client.get(f"{server}/moved").text == "ok"
    from auction_agent.http_client import FetchError
    with pytest.raises(FetchError, match="too many redirects"):
        client.get(f"{server}/loop")


# ------------------------------------------------------------------ challenge detection (security-net-6)

def test_normal_page_with_challenge_script_is_not_blocked(server, client):  # noqa: F811
    body = b'<html><script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>Lots</html>'
    _Handler.routes = {"/robots.txt": (200, {}, b""), "/page": (200, HTML, body)}
    assert client.get(f"{server}/page").status == 200


# ------------------------------------------------------------------ charset (parsing-7)

def test_html_without_charset_is_decoded_as_utf8(server, client):  # noqa: F811
    _Handler.routes = {
        "/robots.txt": (200, {}, b""),
        "/page": (200, {"Content-Type": "text/html"}, "<p>Numéro de série</p>".encode("utf-8")),
        "/latin": (200, {"Content-Type": "text/html"},
                   '<html><meta charset="iso-8859-1"><p>Numéro</p>'.encode("latin-1")),
    }
    assert "Numéro de série" in client.get(f"{server}/page").text
    assert "Numéro" in client.get(f"{server}/latin").text


def test_charset_helper_and_unknown_charset():
    assert _charset("text/html; charset=windows-1252", b"") == "windows-1252"
    assert _charset("application/pdf", b"%PDF-") is None
    bad = FetchResult(url="u", status=200, headers={}, content="é".encode("utf-8"), encoding="no-such-codec")
    assert bad.text == "é"


# ------------------------------------------------------------------ Retry-After parsing (security-net-7)

def test_retry_after_rejects_non_ascii_digits():
    assert _retry_after_seconds("²") is None
    assert _retry_after_seconds("12") == 12.0


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset (POSIX)")
def test_retry_after_date_without_timezone_is_gmt(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")  # a local zone 9 h away from GMT
    time.tzset()
    try:
        target = datetime.now(timezone.utc) + timedelta(seconds=40)
        value = target.strftime("%a, %d %b %Y %H:%M:%S")  # no zone suffix
        wait = _retry_after_seconds(value)
        assert wait is not None and 30 <= wait <= 41
    finally:
        monkeypatch.undo()
        time.tzset()


# ------------------------------------------------------------------ CLI capture (security-net-9)

def test_capture_filenames_are_sanitised_and_contained(settings, tmp_path, monkeypatch):
    vin = "VR3UPHNEKNT084478"
    fake = FakeHttp({"/x/..%2F..%2Fevil": f"<html>VIN {vin}</html>", "/sale": "<html>ok</html>"})
    monkeypatch.setattr(cli, "PoliteClient", lambda _settings: fake)
    out = tmp_path / "captures"
    args = argparse.Namespace(url="/sale", detail=["/x/..%2F..%2Fevil", "/x/..%2F..%2Fevil?b=1"], out=str(out))
    assert cli.cmd_capture(args, settings) == 0
    files = sorted(out.iterdir())
    assert len(files) == 3  # same tail, different URLs: digest keeps them apart
    for path in files:
        assert path.parent == out and re.fullmatch(r"(sale|detail)-[A-Za-z0-9._-]+-[0-9a-f]{8}\.html", path.name)
        assert vin not in path.read_text(encoding="utf-8")
    assert not (tmp_path / "evil").exists()


# ------------------------------------------------------------------ cleanup-legacy (storage-10, security-net-1)

def _make_legacy_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(path))) as conn:
        conn.executescript("CREATE TABLE vehicles (id INTEGER PRIMARY KEY, detail_url TEXT, vin TEXT);"
                           "INSERT INTO vehicles VALUES (1, 'u', 'VF3ABCDEFGH123456');")
        conn.commit()


def test_cleanup_legacy_scans_both_folders_and_deletes_the_new_backup(settings, tmp_path, monkeypatch):
    project = tmp_path / "project"
    monkeypatch.setattr(cli, "PROJECT_DIR", project)
    _make_legacy_db(settings.db_path)
    old_pdfs = project / "data" / "documents" / "lot_12"
    new_pdfs = settings.data_dir / "documents" / "veh_3"
    for folder in (old_pdfs, new_pdfs):
        folder.mkdir(parents=True)
        (folder / "ct.pdf").write_bytes(b"%PDF-1.4")
    keep = settings.data_dir / "documents" / "notes"
    keep.mkdir()

    dry = argparse.Namespace(yes=False, delete_backups=True)
    assert cli.cmd_cleanup_legacy(dry, settings) == 0
    assert settings.db_path.exists() and old_pdfs.exists() and new_pdfs.exists()

    apply = argparse.Namespace(yes=True, delete_backups=True)
    assert cli.cmd_cleanup_legacy(apply, settings) == 0
    assert not settings.db_path.exists() and not old_pdfs.exists() and not new_pdfs.exists()
    assert list(settings.db_path.parent.glob("auction.v1-*.db")) == []
    assert keep.exists()  # only the prototype's lot_*/veh_* folders are touched


def test_cleanup_legacy_keeps_the_scrubbed_backup_by_default(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "PROJECT_DIR", tmp_path / "project")
    _make_legacy_db(settings.db_path)
    assert cli.cmd_cleanup_legacy(argparse.Namespace(yes=True, delete_backups=False), settings) == 0
    backups = list(settings.db_path.parent.glob("auction.v1-*.db"))
    assert len(backups) == 1 and b"VF3ABCDEFGH123456" not in backups[0].read_bytes()


# ------------------------------------------------------------------ identity key (security-net-2)

def test_short_identity_keys_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("IDENTITY_HMAC_KEY", "abc")
    with pytest.raises(RuntimeError, match="too short"):
        load_identity_key(tmp_path / "identity.key")
    monkeypatch.delenv("IDENTITY_HMAC_KEY")
    short_file = tmp_path / "short.key"
    short_file.write_text("00ff", encoding="utf-8")
    with pytest.raises(RuntimeError, match="too short"):
        load_identity_key(short_file)


def test_hex_env_key_is_decoded_and_file_key_is_stable(tmp_path, monkeypatch):
    hex_key = "ab" * MIN_KEY_BYTES
    monkeypatch.setenv("IDENTITY_HMAC_KEY", hex_key)
    assert load_identity_key(tmp_path / "unused.key") == bytes.fromhex(hex_key)
    assert not (tmp_path / "unused.key").exists()
    monkeypatch.delenv("IDENTITY_HMAC_KEY")
    path = tmp_path / "data" / "identity.key"
    first = load_identity_key(path)
    assert len(first) == MIN_KEY_BYTES and load_identity_key(path) == first
