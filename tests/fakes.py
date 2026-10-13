"""Test doubles: an HTTP client that serves fixtures by URL, and counts every request."""
from __future__ import annotations

from collections import Counter
from typing import Callable, Optional, Union
from urllib.parse import urlsplit

from auction_agent.http_client import FetchError, FetchResult

Route = Union[str, bytes, Exception, Callable[[], Union[str, bytes]]]


class FakeHttp:
    def __init__(self, routes: Optional[dict[str, Route]] = None):
        self.routes: dict[str, Route] = dict(routes or {})
        self.calls: Counter[str] = Counter()

    @staticmethod
    def _key(url: str) -> str:
        parts = urlsplit(url)
        return parts.path + (f"?{parts.query}" if parts.query else "")

    def _resolve(self, url: str) -> Union[str, bytes, None]:
        key = self._key(url)
        self.calls[key] += 1
        route = self.routes.get(key)
        if route is None:
            route = self.routes.get(urlsplit(url).path)
        if callable(route) and not isinstance(route, Exception):
            route = route()
        if isinstance(route, Exception):
            raise route
        return route

    def get_text(self, url: str, *, referer: Optional[str] = None) -> str:
        body = self._resolve(url)
        if body is None:
            raise FetchError(f"{url}: HTTP 404")
        return body.decode("utf-8") if isinstance(body, bytes) else body

    def get(self, url: str, *, referer: Optional[str] = None, max_bytes: Optional[int] = None,
            check_robots: bool = True) -> FetchResult:
        body = self._resolve(url)
        if body is None:
            return FetchResult(url=url, status=404, headers={}, content=b"")
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        ctype = "application/pdf" if data.startswith(b"%PDF") else "text/html; charset=utf-8"
        return FetchResult(url=url, status=200, headers={"content-type": ctype}, content=data)

    def count(self, prefix: str) -> int:
        return sum(n for k, n in self.calls.items() if k.startswith(prefix))

    def close(self) -> None:
        pass

    def __enter__(self) -> "FakeHttp":
        return self

    def __exit__(self, *exc: object) -> None:
        pass
