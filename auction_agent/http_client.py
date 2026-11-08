"""Polite, fail-closed HTTP client shared by every auction adapter.

- One requests.Session; urllib3 Retry handles transient 5xx and connection errors.
- 429/503 are handled here: a short Retry-After is honoured once, a long one raises
  RateLimitedError so the run stops instead of hammering the site or sleeping for hours.
- 403/405 responses and known WAF/challenge pages raise BlockedError. We never try to
  solve or bypass a challenge.
- robots.txt is fetched through the same session, then parsed; 404 means allow-all.
  On the auction site, a 403/405 robots.txt is a block. On another site that the auction
  site redirected us to (its document storage on S3, whose root answers 403 AccessDenied
  XML), a plain 4xx robots.txt means "unavailable", so allowed (RFC 9309 §2.3.1.3); a
  challenge there, or a refusal of the document itself, still raises BlockedError.
- A minimum interval (with jitter) separates two requests to the same host.
- Responses are always closed, and document downloads are size-capped and in memory.
"""
from __future__ import annotations

import email.utils
import re
import random
import time
from dataclasses import dataclass, field
from datetime import timezone
from typing import Callable, Optional
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Settings
from .robots import RobotsRules, parse_robots


class FetchError(RuntimeError):
    """Network failure or unexpected HTTP status for one URL."""


class BlockedError(FetchError):
    """The site answered with a block or challenge (WAF, captcha, 403/405)."""


class RateLimitedError(FetchError):
    """The site asked us to slow down for longer than we are willing to wait."""


class RobotsDisallowedError(FetchError):
    """robots.txt disallows this URL (or robots.txt could not be read)."""


class RobotsUnreadableError(RobotsDisallowedError):
    """robots.txt could not be read (error status, too large): treated as disallow-all."""


class DocumentTooLargeError(FetchError):
    pass


# Interstitial pages: a challenge whatever the status code.
_INTERSTITIAL_TITLES = (
    "<title>just a moment...</title>",
    "<title>attention required",
    "<title>human verification",
)
# Script/markup hints that also appear on normal pages: only count with a refusal status.
_CHALLENGE_MARKERS = (
    "aws-waf-token",
    "awswafintegration",
    "/challenge-platform/",
    "cf-chl-",
    "captcha-container",
)
_REDIRECTS = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 5


@dataclass
class FetchResult:
    url: str
    status: int
    headers: dict[str, str]
    content: bytes
    encoding: Optional[str] = None

    @property
    def text(self) -> str:
        try:
            return self.content.decode(self.encoding or "utf-8", errors="replace")
        except LookupError:  # unknown charset name in the header
            return self.content.decode("utf-8", errors="replace")


@dataclass
class _HostState:
    last_request: float = 0.0
    robots: Optional[RobotsRules] = None
    robots_checked: bool = False
    # Set when robots.txt answered a 4xx that only counts as "unavailable" on a redirect
    # to another site: a direct request to this host is then judged as strictly as before.
    robots_unavailable: Optional[int] = None


@dataclass
class PoliteClient:
    settings: Settings
    session: Optional[requests.Session] = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    rng: random.Random = field(default_factory=random.Random)
    _hosts: dict[str, _HostState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()
            retry = Retry(
                total=3,
                connect=3,
                read=2,
                status=3,
                status_forcelist=(500, 502, 504),
                backoff_factor=0.5,
                backoff_jitter=0.3,
                allowed_methods=frozenset({"GET", "HEAD"}),
                respect_retry_after_header=False,
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry)
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)
        self.session.headers.update(
            {
                "User-Agent": self.settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.6,en;q=0.5",
            }
        )

    def close(self) -> None:
        if self.session is not None:
            self.session.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ----------------------------------------------------------------- public API

    def get(
        self,
        url: str,
        *,
        referer: Optional[str] = None,
        max_bytes: Optional[int] = None,
        check_robots: bool = True,
    ) -> FetchResult:
        """GET a URL. Returns 2xx and 404/410 results; raises for everything else.

        Redirects are followed by hand (at most 5) so that every hop is checked against
        that host's robots.txt and throttled like any other request.
        """
        attempts = 0
        hops = 0
        origin_site = _site(url)
        while True:
            if check_robots and self.settings.respect_robots:
                self._check_robots(url, origin_site=origin_site)
            attempts += 1
            result = self._request(url, referer=referer, max_bytes=max_bytes)
            if result.status in _REDIRECTS and result.headers.get("location"):
                hops += 1
                if hops > MAX_REDIRECTS:
                    raise FetchError(f"{url}: too many redirects")
                url = urljoin(url, result.headers["location"])
                continue
            if result.status in (429, 503) and not _looks_like_challenge(result):
                wait = _retry_after_seconds(result.headers.get("retry-after"))
                if attempts >= 2 or (wait is not None and wait > self.settings.max_retry_after_s):
                    raise RateLimitedError(f"{url}: HTTP {result.status}, Retry-After={wait}")
                self.sleep(wait if wait is not None else min(30.0, self.settings.max_retry_after_s))
                continue
            if result.status in (403, 405) or _looks_like_challenge(result):
                raise BlockedError(f"{url}: blocked or challenged (HTTP {result.status})")
            if 200 <= result.status < 300 or result.status in (404, 410):
                return result
            raise FetchError(f"{url}: unexpected HTTP {result.status}")

    def get_text(self, url: str, *, referer: Optional[str] = None) -> str:
        result = self.get(url, referer=referer)
        if result.status in (404, 410):
            raise FetchError(f"{url}: HTTP {result.status}")
        return result.text

    def robots_allows(self, url: str) -> bool:
        """Whether robots.txt lets us fetch url (robots.txt is fetched once per host).

        An unreadable robots.txt (error status, rate limit) raises instead of answering, so a
        caller stops at once rather than asking again for every path. A block also raises.
        """
        try:
            self._check_robots(url)
        except RobotsUnreadableError:
            raise
        except RobotsDisallowedError:
            return False
        return True

    # ----------------------------------------------------------------- internals

    def _host(self, url: str) -> tuple[str, _HostState]:
        parts = urlsplit(url)
        key = f"{parts.scheme}://{parts.netloc}"
        return key, self._hosts.setdefault(key, _HostState())

    def _throttle(self, state: _HostState) -> None:
        interval = self.settings.min_request_interval
        if interval <= 0:
            return
        target = interval + self.rng.uniform(0, interval * 0.5)
        elapsed = self.clock() - state.last_request
        if state.last_request and elapsed < target:
            self.sleep(target - elapsed)

    def _request(self, url: str, *, referer: Optional[str], max_bytes: Optional[int]) -> FetchResult:
        _, state = self._host(url)
        self._throttle(state)
        headers = {"Referer": referer} if referer else None
        try:
            with self.session.get(  # type: ignore[union-attr]
                url, headers=headers, timeout=self.settings.request_timeout, stream=True,
                allow_redirects=False,
            ) as resp:
                chunks, total = [], 0
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise DocumentTooLargeError(f"{url}: larger than {max_bytes} bytes")
                    chunks.append(chunk)
                headers_l = {k.lower(): v for k, v in resp.headers.items()}
                content = b"".join(chunks)
                return FetchResult(
                    url=resp.url,
                    status=resp.status_code,
                    headers=headers_l,
                    content=content,
                    encoding=_charset(headers_l.get("content-type", ""), content),
                )
        except requests.RequestException as exc:
            raise FetchError(f"{url}: {exc.__class__.__name__}: {exc}") from exc
        finally:
            state.last_request = self.clock()

    def _check_robots(self, url: str, *, origin_site: Optional[str] = None) -> None:
        """Raise unless robots.txt lets us fetch url.

        origin_site is the site of the first URL of a redirect chain; a hop to another site
        (the auction site's document storage) reads a plain 4xx robots.txt as "unavailable".
        """
        key, state = self._host(url)
        redirected_elsewhere = origin_site is not None and _site(url) != origin_site
        if state.robots_unavailable is not None and not redirected_elsewhere:
            _raise_strict_robots_error(key, state.robots_unavailable)
        if not state.robots_checked:
            # Only a definitive answer (2xx, 404/410) is cached; errors are re-raised and
            # the next request tries again, so a failed fetch never means "allow all".
            try:
                result = self._request(f"{key}/robots.txt", referer=None, max_bytes=512 * 1024)
            except DocumentTooLargeError as exc:
                raise RobotsUnreadableError(f"{key}/robots.txt larger than 512 KiB") from exc
            challenge = _looks_like_challenge(result)
            if (redirected_elsewhere and not challenge and 400 <= result.status < 500
                    and result.status not in (404, 410, 429)):
                # RFC 9309 §2.3.1.3: robots.txt "unavailable" (4xx), so the host may be
                # crawled. Only here: S3 answers 403 AccessDenied for a bucket root.
                state.robots, state.robots_unavailable, state.robots_checked = None, result.status, True
                return
            if result.status in (403, 405) or challenge:
                raise BlockedError(f"{key}/robots.txt: blocked or challenged (HTTP {result.status})")
            if result.status in (404, 410):
                state.robots = None  # no robots.txt: everything allowed
            elif 200 <= result.status < 300:
                state.robots = parse_robots(result.text)
            else:
                # RFC 9309: an unreachable robots.txt means "assume complete disallow".
                raise RobotsUnreadableError(f"{key}/robots.txt unreadable (HTTP {result.status})")
            state.robots_checked = True
        if state.robots is not None and not state.robots.can_fetch(self.settings.robots_token, url):
            raise RobotsDisallowedError(f"robots.txt disallows {url}")


_IP_HOST_RE = re.compile(r"^[\d.]+$|:")


def _site(url: str) -> str:
    """The site a URL belongs to: its last two host labels ('www.alcopa-auction.fr' →
    'alcopa-auction.fr'), or the whole host for an IP address or a one-label name."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if _IP_HOST_RE.search(host):
        return host
    return ".".join(host.split(".")[-2:])


def _raise_strict_robots_error(key: str, status: int) -> None:
    """How a 4xx robots.txt is judged when it is not a redirect to another site."""
    if status in (403, 405):
        raise BlockedError(f"{key}/robots.txt: blocked or challenged (HTTP {status})")
    raise RobotsUnreadableError(f"{key}/robots.txt unreadable (HTTP {status})")


def _retry_after_seconds(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return float(value)
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:  # HTTP dates are always GMT
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, parsed.timestamp() - time.time())


_META_CHARSET_RE = re.compile(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9_\-]+)", re.IGNORECASE)


def _charset(content_type: str, content: bytes) -> Optional[str]:
    """Explicit header charset, else <meta charset>, else UTF-8 for HTML.

    requests would otherwise assume ISO-8859-1 for "text/html" without a charset and
    turn "Numéro" into "NumÃ©ro", which breaks label matching and redaction.
    """
    m = re.search(r"charset=([\w\-]+)", content_type, re.IGNORECASE)
    if m:
        return m.group(1)
    if "html" in content_type.lower() or content.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        meta = _META_CHARSET_RE.search(content[:4096])
        return meta.group(1).decode("ascii") if meta else "utf-8"
    return None


def _looks_like_challenge(result: FetchResult) -> bool:
    headers = result.headers
    if headers.get("x-amzn-waf-action") or headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if "html" not in headers.get("content-type", "").lower():
        return False
    body = result.content[:200_000].decode("utf-8", errors="ignore").lower()
    if any(title in body for title in _INTERSTITIAL_TITLES):
        return True
    if result.status in (202, 403, 405, 429, 503):
        return any(marker in body for marker in _CHALLENGE_MARKERS)
    return False
