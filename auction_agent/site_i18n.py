"""The site's two languages from one page (redesign v2, BRIEF §5.5).

``web/index.html`` is the French page; every text it shows carries a ``data-i18n`` key (or
``data-i18n-ph`` / ``-label`` / ``-content`` for a placeholder, an aria-label or a meta
content). Its texts are written from ``web/i18n/fr.json``, and ``web/en/index.html`` is the
same page written from ``web/i18n/en.json``, so the two pages keep the same structure and
the dictionaries stay the single source of the copy. The tests check both files are as this
module writes them; after a copy change, run::

    python -m auction_agent.site_i18n
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

from .config import PROJECT_DIR

WEB_DIR = PROJECT_DIR / "web"
# An element whose whole content is one dictionary text (no tags inside).
_TEXT = re.compile(r'(<(?P<tag>[a-z0-9]+)\b[^<>]*?\sdata-i18n="(?P<key>[\w.]+)"[^<>]*>)(?P<body>[^<]*)(</(?P=tag)>)')
_ATTR = {"ph": "placeholder", "label": "aria-label", "content": "content"}
_ATTR_KEY = re.compile(r'<[a-z0-9]+\b[^<>]*\sdata-i18n-(?P<kind>ph|label|content)="(?P<key>[\w.]+)"[^<>]*>')

# What else differs on the English page: (French, English), each found exactly once.
EN_SWAPS = [
    ('<html lang="fr">', '<html lang="en">'),
    ('<a class="brand" href="/" ', '<a class="brand" href="/en/" '),
    ('<a class="nav-sales" href="/" aria-current="page"', '<a class="nav-sales" href="/en/" aria-current="page"'),
    ('<a id="lang-fr" href="/" lang="fr" hreflang="fr" aria-label="Français" aria-current="true">FR</a>',
     '<a id="lang-fr" href="/" lang="fr" hreflang="fr" aria-label="Français">FR</a>'),
    ('<a id="lang-en" href="/en/" lang="en" hreflang="en" aria-label="English">EN</a>',
     '<a id="lang-en" href="/en/" lang="en" hreflang="en" aria-label="English" aria-current="true">EN</a>'),
    # The method page is French only for now: its links say so to assistive technology.
    ('<a href="/about.html" data-i18n="nav.method">', '<a href="/about.html" hreflang="fr" data-i18n="nav.method">'),
    ('<a href="/about.html" data-i18n="foot.method">', '<a href="/about.html" hreflang="fr" data-i18n="foot.method">'),
    ('<a class="cov-link" href="/about.html#couverture" data-i18n="cov.link">',
     '<a class="cov-link" href="/about.html#couverture" hreflang="fr" data-i18n="cov.link">'),
]
# The French page's notice for English readers is not on the English page.
_HINT = re.compile(r'\s*<p id="lang-hint"[^\n]*</p>')


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def render(page: str, strings: dict[str, str], lang: str) -> str:
    """The page with every keyed text taken from ``strings`` (a KeyError names a missing key)."""
    def text(m: re.Match) -> str:
        return f"{m.group(1)}{_escape(strings[m.group('key')])}</{m.group('tag')}>"

    def attr(m: re.Match) -> str:
        tag, name = m.group(0), _ATTR[m.group("kind")]
        value = html.escape(strings[m.group("key")], quote=True)
        new, n = re.subn(rf'\s{name}="[^"]*"', f' {name}="{value}"', tag, count=1)
        if not n:
            raise ValueError(f"{name} missing on the element keyed {m.group('key')}")
        return new

    out = _TEXT.sub(text, page)
    out = _ATTR_KEY.sub(attr, out)
    if lang == "en":
        for fr, en in EN_SWAPS:
            if out.count(fr) != 1:
                raise ValueError(f"expected once in the French page: {fr}")
            out = out.replace(fr, en)
        out, n = _HINT.subn("", out)
        if n != 1:
            raise ValueError("the English-reader notice is missing from the French page")
    return out


def load(lang: str, web_dir: Path = WEB_DIR) -> dict[str, str]:
    return json.loads((web_dir / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))


def pages(web_dir: Path = WEB_DIR) -> dict[Path, str]:
    """{path: content} for the French and the English page, as they should be."""
    source = (web_dir / "index.html").read_text(encoding="utf-8")
    return {web_dir / "index.html": render(source, load("fr", web_dir), "fr"),
            web_dir / "en" / "index.html": render(source, load("en", web_dir), "en")}


def main() -> int:
    for path, content in pages().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old != content:
            path.write_text(content, encoding="utf-8", newline="\n")
            print(f"wrote {path.relative_to(PROJECT_DIR)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
