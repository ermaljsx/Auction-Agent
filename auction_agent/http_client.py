"""Polite, fail-closed HTTP client shared by every auction adapter.

- One requests.Session; urllib3 Retry handles transient 5xx and connection errors.
- 429/503 are handled here: a short Retry-After is honoured once, a long one raises
  RateLimitedError so the run stops instead of hammering the site or sleeping for hours.
- 403/405 responses and known WAF/challenge pages raise BlockedError. We never try to
  solve or bypass a challenge.
- robots.txt is fetched through the same session, then parsed; 404 means allow-all.
- A minimum interval (with jitter) separates two requests to the same host.
- Responses are always closed, and document downloads are size-capped and in memory.
"""
from __future__ import annotations

import email.utils
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Settings


class FetchError(RuntimeError):
    """Network failure or unexpected HTTP status for one URL."""


class BlockedError(FetchError):
    """The site answered with a block or challenge (WAF, captcha, 403/405)."""


class RateLimitedError(FetchError):
    """The site asked us to slow down for longer than we are willing to wait."""


class RobotsDisallowedError(FetchError):
    """robots.txt disallows this URL (or robots.txt could not be read)."""


class DocumentTooLargeError(FetchError):
    pass


_CHALLENGE_MARKERS = (
    "aws-waf-token",
    "awswafintegration",
    "/challenge-platform/",
    "cf-chl-",
    "<title>just a moment...</title>",
    "captcha-container",
)


@dataclass
class FetchResult:
    url: str
    status: int
    headers: dict[str, str]
    content: bytes
    encoding: Optional[str] = None

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")


@dataclass
class _HostState:
    last_request: float = 0.0
    robots: Optional[RobotFileParser] = None
    robots_checked: bool = False


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
        """GET a URL. Returns 2xx and 404/410 results; raises for everything else."""
        if check_robots and self.settings.respect_robots:
            self._check_robots(url)
        attempts = 0
        while True:
            attempts += 1
            result = self._request(url, referer=referer, max_bytes=max_bytes)
            if result.status in (429, 503):
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
                url, headers=headers, timeout=self.settings.request_timeout, stream=True
            ) as resp:
                chunks, total = [], 0
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise DocumentTooLargeError(f"{url}: larger than {max_bytes} bytes")
                    chunks.append(chunk)
                return FetchResult(
                    url=resp.url,
                    status=resp.status_code,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    content=b"".join(chunks),
                    encoding=resp.encoding,
                )
        except requests.RequestException as exc:
            raise FetchError(f"{url}: {exc.__class__.__name__}: {exc}") from exc
        finally:
            state.last_request = self.clock()

    def _check_robots(self, url: str) -> None:
        key, state = self._host(url)
        if not state.robots_checked:
            state.robots_checked = True
            result = self._request(f"{key}/robots.txt", referer=None, max_bytes=512 * 1024)
            if result.status in (403, 405) or _looks_like_challenge(result):
                raise BlockedError(f"{key}/robots.txt: blocked or challenged (HTTP {result.status})")
            if result.status in (404, 410):
                state.robots = None  # no robots.txt: everything allowed
            elif 200 <= result.status < 300:
                parser = RobotFileParser()
                parser.parse(result.text.splitlines())
                state.robots = parser
            else:
                # RFC 9309: an unreachable robots.txt means "assume complete disallow".
                raise RobotsDisallowedError(f"{key}/robots.txt unreadable (HTTP {result.status})")
        if state.robots is not None and not state.robots.can_fetch(self.settings.user_agent, url):
            raise RobotsDisallowedError(f"robots.txt disallows {url}")


def _retry_after_seconds(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed.timestamp() - time.time())


def _looks_like_challenge(result: FetchResult) -> bool:
    if "x-amzn-waf-action" in result.headers:
        return True
    ctype = result.headers.get("content-type", "")
    if "html" not in ctype or len(result.content) > 200_000:
        return False
    body = result.content[:200_000].decode("utf-8", errors="ignore").lower()
    return any(marker in body for marker in _CHALLENGE_MARKERS)
