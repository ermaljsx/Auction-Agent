"""The private site's static files (web/): markup, script and style rules, contrast, favicon.

No browser here: the rules that keep the page safe under its CSP (no inline code or styles,
no HTML from the data) and accessible (landmarks, focus, contrast) are checked on the files.
The page's pure helpers run under Node when it is installed (CI); locally they are skipped.

Redesign v2 « Relevé » (docs/redesign/v2/BRIEF.md): the v1 bans on @font-face, url(),
uppercase, letter-spacing and box-shadow are now the narrower rules of BRIEF §3, and the
tests that pinned v1 copy, fonts and tokens check their v2 equivalents (owner decision,
2026-11-08: port them, keep every safety check at least as strict).
"""
import json
import re
import shutil
import struct
import subprocess

import pytest

from auction_agent import site_i18n
from auction_agent.preview import static_file
from tests.conftest import PROJECT_ROOT

WEB = PROJECT_ROOT / "web"
PAGES = ("index.html", "about.html", "en/index.html")
LANG = {"index.html": "fr", "about.html": "fr", "en/index.html": "en"}
CATALOGUES = ("index.html", "en/index.html")


def _read(name):
    return (WEB / name).read_text(encoding="utf-8")


def _dict(lang):
    return json.loads(_read(f"i18n/{lang}.json"))


# ------------------------------------------------------------------ markup

@pytest.mark.parametrize("page", PAGES)
def test_markup_rules(page):
    html = _read(page)
    assert not re.search(r"\sstyle\s*=", html, re.I)  # CSP style-src 'self': no inline styles
    assert "<style" not in html.lower()
    for tag in re.findall(r"<script\b[^>]*>", html, re.I):
        assert re.search(r'\ssrc="/[a-z]+\.js"', tag)  # no inline script
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)  # no inline event handlers
    assert 'class="skip"' in html and "<header" in html and "<main" in html and "<footer" in html
    assert '<link rel="icon" href="/favicon.svg" type="image/svg+xml">' in html
    assert '<link rel="icon" href="/favicon-32.png" sizes="32x32" type="image/png">' in html
    assert '<meta name="color-scheme" content="light dark">' in html
    assert f'<html lang="{LANG[page]}">' in html and '<meta name="robots" content="noindex, nofollow">' in html
    assert "http://" not in html and "https://" not in html  # no external resources or links
    skip_target = re.search(r'class="skip" href="#([a-z-]+)"', html).group(1)
    assert f'id="{skip_target}"' in html
    # Both languages are announced (noindex stays), and the variable font is preloaded from the site.
    assert '<link rel="alternate" hreflang="fr" href="/">' in html and '<link rel="alternate" hreflang="en" href="/en/">' in html
    assert '<link rel="preload" href="/fonts/archivo-latin-wdth-wght.woff2" as="font" type="font/woff2" crossorigin>' in html


@pytest.mark.parametrize("page", PAGES)
def test_every_page_has_the_language_switch(page):
    html = _read(page)
    switch = re.search(r'<div class="lang" role="group"[^>]*>(.*?)</div>', html, re.S).group(1)
    links = re.findall(r"<a\b[^>]*>", switch)
    assert len(links) == 2
    fr, en = links
    assert 'lang="fr" hreflang="fr" aria-label="Français"' in fr and 'lang="en" hreflang="en" aria-label="English"' in en
    current = fr if LANG[page] == "fr" else en
    assert [link for link in links if 'aria-current="true"' in link] == [current]


def test_inline_svg_is_decoration():
    for page in PAGES:
        for tag in re.findall(r"<svg\b[^>]*>", _read(page)):
            assert 'aria-hidden="true"' in tag and 'focusable="false"' in tag, (page, tag)


def test_local_links_and_anchors_resolve():
    """Every local link of the pages and of the script reaches a file the site serves, and its anchor."""
    sources = {page: _read(page) for page in PAGES}
    links = [(page, href) for page, html in sources.items() for href in re.findall(r'href="(/[^"]*)"', html)]
    links += [("app.js", href) for href in re.findall(r'href: "(/[^"]*)"', _read("app.js"))]
    assert links
    for page, href in links:
        path, _, anchor = href.partition("#")
        target = static_file(WEB, path)
        assert target is not None, (page, href)
        if anchor:
            assert f'id="{anchor}"' in target.read_text(encoding="utf-8"), (page, href)


def test_the_hero_says_what_the_site_does():
    """A general headline for the platform (BRIEF v2 §5.1): it names no sale, house or room."""
    for page, words in (("index.html", "Les voitures aux enchères, décodées avant la vente."),
                        ("en/index.html", "Auction cars, decoded before the sale.")):
        html = _read(page)
        h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", html, re.S).group(1)
        text = re.sub(r"<[^>]+>", "", h1)
        assert text == words, page
        assert not re.search(r"Alcopa|Beauvais|lundi|Monday|novembre|November", text)
        assert '<span class="hl"' in h1  # the one decorative use of yellow
        hero = re.search(r'<section class="hero-main".*?</section>', html, re.S).group(0)
        assert 'id="hero-search"' in hero and 'role="search"' in hero  # search sits in the hero


def test_no_page_promises_an_all_in_price():
    # The estimate leaves out fixed per-lot fees, transport and registration: never "tout compris".
    for name in PAGES + ("app.js", "i18n/fr.json", "i18n/en.json"):
        text = _read(name)
        assert "tout compris" not in text and "all-in" not in text.lower(), name


def test_the_footer_links_only_pages_that_exist():
    for page in PAGES:
        html = _read(page)
        nav = re.search(r'<nav class="foot-nav".*?</nav>', html, re.S).group(0)
        assert re.findall(r'href="([^"]+)"', nav) == ["/about.html"]
        strings = _dict(LANG[page])
        for key in ("foot.legal", "foot.terms", "foot.privacy", "foot.contact"):
            assert f'<li class="soon" data-i18n="{key}">{strings[key]}</li>' in nav, (page, key)


def _labelled(html, field):
    """True when the control with this id sits inside a <label> (or has one pointing at it)."""
    if f'for="{field}"' in html:
        return True
    return any(f'id="{field}"' in label for label in re.findall(r"<label\b.*?</label>", html, re.S))


@pytest.mark.parametrize("page", CATALOGUES)
def test_index_has_the_catalogue_landmarks_and_controls(page):
    html = _read(page)
    strings = _dict(LANG[page])
    assert re.search(r'<p id="count"[^>]*role="status"', html)
    assert 'aria-controls="f-more" aria-expanded="false"' in html
    assert 'id="catalogue"' in html and 'aria-busy="true"' in html
    assert 'role="search"' in html and f'aria-label="{strings["filters.label"]}"' in html
    for field in ("f-q", "f-budget", "f-fuel", "f-gearbox", "f-km", "f-year", "f-ct", "f-engine", "f-sort", "h-q", "h-budget"):
        assert f'id="{field}"' in html and _labelled(html, field), field  # every control is labelled
    assert "<form" in html and 'autocomplete="off"' in html
    assert 'role="img"' in re.search(r'<div id="waffle"[^>]*>', html).group(0)  # the waffle is one image


def test_every_element_the_script_looks_up_exists():
    app = _read("app.js")
    made_by_script = {"sheet-title", "sheet-masked", "sheet-status"}
    ids = set(re.findall(r'\$\("([a-z0-9-]+)"\)', app))
    assert {"index-list", "f-n", "no-match-text", "waffle", "legend", "sheet-layer"} <= ids
    for page in CATALOGUES:
        html = _read(page)
        # The notice for English readers is on the French page only (the script checks it exists).
        optional = {"lang-hint", "lang-hint-x"} if LANG[page] == "en" else set()
        assert ids and not {i for i in ids if f'id="{i}"' not in html} - made_by_script - optional, page
        for key in ("top", "cards", "rest"):  # looked up through templates
            for template in (f"sec-{key}", f"n-{key}", f"{key}-list", f"more-{key}"):
                assert f'id="{template}"' in html, (page, template)


def test_both_languages_are_one_page():
    """The English page is the French page written from en.json (auction_agent/site_i18n.py),
    and the French page's texts are fr.json's: run `python -m auction_agent.site_i18n` after a change."""
    for path, content in site_i18n.pages(WEB).items():
        assert path.read_text(encoding="utf-8") == content, f"{path.name}: run python -m auction_agent.site_i18n"
    fr_ids = re.findall(r'\sid="([^"]+)"', _read("index.html"))
    en_ids = re.findall(r'\sid="([^"]+)"', _read("en/index.html"))
    assert set(en_ids) == set(fr_ids) - {"lang-hint", "lang-hint-x"}


# ------------------------------------------------------------------ strings (FR / EN)

def test_dictionaries_have_the_same_keys():
    fr, en = _dict("fr"), _dict("en")
    missing_en = sorted(set(fr) - set(en))
    assert not missing_en, f"English keys missing (they would show in French): {missing_en}"
    assert not sorted(set(en) - set(fr))


def test_every_key_the_page_uses_exists():
    fr = _dict("fr")
    app = _read("app.js")
    used = set(re.findall(r'\bt\("([\w.]+)"', app)) | set(re.findall(r'\btOr\("([\w.]+)"', app))
    plural = set(re.findall(r'\btn\("([\w.]+)"', app))
    need = used | {f"{k}_one" for k in plural} | {f"{k}_other" for k in plural}
    for page in PAGES:
        need |= set(re.findall(r'data-i18n(?:-ph|-label|-content)?="([\w.]+)"', _read(page)))
    assert not sorted(k for k in need if k not in fr)
    # Keys built from data: every value the code can form exists.
    for prefix, values in (("legend.", ("fav", "major", "crit", "none")), ("ct.word_", ("fav", "major", "crit", "unread", "none", "young")),
                           ("tier.", ("green", "yellow", "red")), ("tier_long.", ("green", "yellow", "red")),
                           ("fn.", tuple(str(i) for i in range(9))), ("ai.", ("off", "error", "missing")),
                           ("cost.at_", ("start", "shown", "bid")), ("cost.label_", ("start", "shown", "bid")),
                           ("cost.row_", ("start", "shown", "bid")), ("status.ai_", ("off", "error", "partial")),
                           ("src.", ("ct", "listing", "comment", "kb", "cohort", "llm", "battery"))):
        for v in values:
            assert prefix + v in fr, prefix + v


def test_french_typography_lives_in_the_dictionary():
    """No break inside « », before ; : ! ? or between a figure and its unit (BRIEF v2 §5.5)."""
    for key, value in _dict("fr").items():
        assert not re.search(r"« | »", value), key  # a plain space inside the guillemets
        assert not re.search(r" [;:!?]", value), key  # a plain space before high punctuation
        assert not re.search(r"(\d|\}) (km|€|%|ans|jours?|lots?|Mo)\b", value), key
    for key, value in _dict("en").items():
        assert "«" not in value and "»" not in value, key


def test_copy_that_must_stay_is_in_the_dictionaries():
    fr, en = _dict("fr"), _dict("en")
    assert "peut se tromper" in fr["ai.label"] and "may be wrong" in en["ai.label"]
    assert fr["a11y.new_tab"] == "(nouvel onglet)" and en["a11y.new_tab"] == "(new tab)"
    assert "selon nos critères" in fr["brief.weights"]
    assert fr["sheet.listing"] == "Voir l’annonce sur {house}" and en["sheet.listing"].endswith("(in French)")
    assert fr["sheet.no_listing"] == "Annonce introuvable." and fr["sheet.sale_catalogue"]
    # Never a bidding ceiling we chose (docs/BIDDING_CEILING.md): the simulator is the buyer's arithmetic.
    for strings in (fr, en):
        assert not any(re.search(r"plafond d.ench|bidding ceiling|maximum bid we", v, re.I) for v in strings.values())
    assert "pas une estimation" in fr["sim.tag"] and "ne dit pas ce que vaut la voiture" in fr["sim.step_unknown"]


# ------------------------------------------------------------------ script

SVG_NS = 'const SVG_NS = "http://www.w3.org/2000/svg";'


def test_app_js_rules():
    app = _read("app.js")
    assert app.lstrip().splitlines()[0].startswith("//") and '"use strict";' in app
    # The SVG namespace is a name for createElementNS, not a URL: it is the only "http://" allowed.
    assert app.count(SVG_NS) == 1 and app.count("http://") == 1
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function", "document.write",
                   'setAttribute("style"', ".style.cssText", "http://", "document.cookie", "indexedDB"):
        assert banned not in app.replace(SVG_NS, ""), banned
    for required in ("aria-expanded", '"Escape"', "hashchange", "popstate", "noopener noreferrer", 'weekday: "long"',
                     'addEventListener("submit"', "preventDefault", "replaceState", "pushState", "aria-busy",
                     "aria-current", "aria-pressed", "aria-live"):
        assert required in app, required
    # Web storage only through one helper that catches every failure (private mode, blocked storage).
    store = re.search(r"\nfunction store\(kind, key, value\) \{(.*?)\n\}", app, re.S).group(1)
    assert "try {" in store and "catch (err)" in store
    for area in ("localStorage", "sessionStorage"):
        assert app.count(area) == 1 and area in store, area
    # CSS custom properties through the CSSOM only (allowed by the CSP).
    assert app.count(".style.setProperty(") == 1  # inside setVar
    assert set(re.findall(r'setVar\([\w.]+, "(--[a-z0-9-]+)"', app)) <= {"--v", "--w", "--bar-h"}
    # SVG only through createElementNS with a fixed attribute list (no href, no style, no event handlers).
    attrs = re.search(r"const SVG_ATTRS = new Set\(\[(.*?)\]\);", app, re.S).group(1)
    assert not re.search(r'"(href|xlink:href|style|on[a-z]+)"', attrs)
    assert app.count("createElementNS(") == 1
    assert "Vente du lundi" not in app
    # Jumps to a lot or a section are instant: no smooth scrolling from the script.
    assert '"smooth"' not in app
    # The page never hard-codes the fees: they come from the snapshot's fee model (BRIEF v2 §B3, §6).
    assert "0.144" not in app and "14,4" not in app and "14.4" not in app


def test_the_sheet_opens_with_the_listing_before_any_analysis():
    app = _read("app.js")
    body = re.search(r"function renderSheet\(car\) \{(.*?)\n\}", app, re.S).group(1)
    order = [body.index(f"{name}(car)") for name in
             ("sheetHead", "gateBanners", "sheetActions", "sheetBrief", "sheetCost", "sheetCt", "sheetChecks", "sheetSources")]
    assert order == sorted(order)
    # The listing's label names the auction house; a missing link says so and offers the sale's catalogue.
    actions = re.search(r"function sheetActions\(car\) \{(.*?)\n\}", app, re.S).group(1)
    assert 'listingTarget(car, ui.sale)' in actions and 't("sheet.listing"' in actions and 't("sheet.no_listing")' in actions


def test_app_js_touches_the_dom_only_after_load():
    app = _read("app.js")
    assert 'if (typeof module !== "undefined" && module.exports)' in app
    assert app.rstrip().endswith('if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", boot);')
    top_level = [line for line in app.splitlines() if line and not line[0].isspace()]
    assert not [line for line in top_level if line.startswith(("document.", "window.", "$(", "load("))]


def test_only_alcopa_links_and_the_page_api_are_used():
    app = _read("app.js")
    # The page's own dictionaries and its snapshot, nothing else.
    assert re.findall(r"fetch\(`([^`]*)`", app) == ["/i18n/${code}.json", "/api/snapshot?t=${Date.now()}"]
    assert len(re.findall(r"\bfetch\(", app)) == 2
    assert 'host === "alcopa-auction.fr" || host.endsWith(".alcopa-auction.fr")' in app
    assert 'target: "_blank", rel: "noopener noreferrer nofollow"' in app


# ------------------------------------------------------------------ style

def _hex_tokens(block):
    return {m.group(1): m.group(2) for m in re.finditer(r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})\s*;", block)}


def _luminance(hex_color):
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _palettes():
    css = _read("styles.css")
    light = _hex_tokens(re.search(r":root\s*\{(.*?)\n\}", css, re.S).group(1))
    dark_block = re.search(r"@media \(prefers-color-scheme: dark\)\s*\{\s*:root\s*\{(.*?)\}", css, re.S).group(1)
    return css, light, {**light, **_hex_tokens(dark_block)}


def _dark_tokens():
    css = _read("styles.css")
    return _hex_tokens(re.search(r"@media \(prefers-color-scheme: dark\)\s*\{\s*:root\s*\{(.*?)\}", css, re.S).group(1))


def _rules(css):
    """(selector, declarations) for every plain rule, at any nesting level."""
    flat = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}@;]+)\{([^{}]*)\}", flat)]


def test_css_rules():
    css, light, dark = _palettes()
    for required in ("prefers-reduced-motion", "prefers-color-scheme: dark", "color-scheme: light dark",
                     ":focus-visible", "forced-colors: active", "font-stretch: 75%", "tabular-nums",
                     '"Archivo"', '"JetBrains Mono"', "ui-sans-serif", "system-ui", "Arial", "font-display: swap"):
        assert required in css, required
    assert "@import" not in css and "http" not in css  # nothing from another host
    # BRIEF v2 §3: same-origin .woff2 fonts only.
    urls = re.findall(r"url\(([^)]*)\)", css)
    assert urls and all(re.fullmatch(r'"/fonts/[a-z0-9-]+\.woff2"', u) for u in urls), urls
    assert css.count("@font-face") == 2
    for u in set(urls):
        assert (WEB / u.strip('"').lstrip("/")).is_file(), u
    rules = _rules(css)
    # Capitals only on the small mono labels.
    upper = [sel for sel, body in rules if "text-transform: uppercase" in body]
    assert upper == [".lbl"]
    # Letter-spacing: +0.06em on .lbl, a slight tightening (−0.025em to −0.01em) on display headings.
    for sel, body in rules:
        for value in re.findall(r"letter-spacing:\s*([^;]+);", body):
            if sel == ".lbl":
                assert value.strip() == "0.06em"
            else:
                em = float(value.strip().removesuffix("em"))
                assert -0.025 <= em <= -0.01, (sel, value)
    # Shadows only through the two elevation tokens (sticky bar, desktop sheet).
    for sel, body in rules:
        for value in re.findall(r"(?<!-)box-shadow:\s*([^;]+);", body):
            assert value.strip() in ("var(--elev-bar)", "var(--elev-pane)"), (sel, value)
    assert "--elev-bar:" in css and "--elev-pane:" in css
    assert len(light) >= 30 and set(_dark_tokens()) == set(light)  # every colour has a dark value


def test_motion_is_short_and_on_compositor_properties():
    """BRIEF v2 §7: the phone sheet slides in (240 ms), the rest fades; nothing else moves."""
    css = _read("styles.css")
    frames = re.findall(r"@keyframes ([a-z-]+) \{(.*?)\}\n", css, re.S)
    assert {name for name, _ in frames} == {"sheet-in", "fade-in"}
    for name, body in frames:
        props = set(re.findall(r"([a-z-]+)\s*:", body))
        assert props <= {"opacity", "transform"}, (name, props)
    for value in re.findall(r"(\d+)ms", css):
        assert int(value) <= 240, value
    # Reduced motion: no transform, fades of 120 ms at most.
    reduced = re.search(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", css, re.S).group(1)
    assert "sheet-in" not in reduced and "translate" not in reduced and "transform: none" in reduced
    assert all(int(v) <= 120 for v in re.findall(r"(\d+)ms", reduced))
    assert "scroll-behavior: smooth" not in css
    # Nothing animates on numbers: no animation or transition on figures or rings.
    for sel, body in _rules(css):
        if re.search(r"\.fig\b|\.ring", sel):
            assert "animation" not in body and "transition" not in body, sel


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_token_contrast_ratios(mode):
    _, light, dark = _palettes()
    p = light if mode == "light" else dark
    # Text on the page's backgrounds (BRIEF v2 §4.1).
    for text in ("ink", "ink-2", "ink-3", "ok", "major", "crit", "watch",
                 "word-green", "word-yellow", "word-orange", "word-red"):
        for bg in ("paper", "surface", "inset"):
            assert _ratio(p[text], p[bg]) >= 4.5, (mode, text, bg, round(_ratio(p[text], p[bg]), 2))
    for text, wash in (("ok", "ok-wash"), ("major", "major-wash"), ("crit", "crit-wash"), ("ink", "hi-wash"),
                       ("ink", "inset"), ("on-hi", "hi"), ("on-btn", "btn"), ("ink", "crit-wash")):
        assert _ratio(p[text], p[wash]) >= 4.5, (mode, text, wash)
    # Rings, CT cells and control edges (non-text, 3:1).
    for ring in ("ring-green", "ring-yellow", "ring-orange", "ring-red"):
        assert _ratio(p[ring], p["surface"]) >= 3, (mode, ring)
        assert _ratio(p[ring], p["ring-track"]) >= 3 if mode == "dark" else _ratio(p[ring], p["ring-track"]) >= 2.9
    for mark in ("minor-edge", "major-fill", "crit-fill", "control-edge"):
        assert _ratio(p[mark], p["surface"]) >= 3, (mode, mark)
    # The carbon band: its texts and the sale card's.
    for text in ("band-text", "band-text-2", "band-text-3"):
        for bg in ("band", "band-card"):
            assert _ratio(p[text], p[bg]) >= 4.5, (mode, text, bg)
    for cell in ("waffle-fav", "waffle-crit", "waffle-none"):
        assert _ratio(p[cell], p["band-card"]) >= 3, (mode, cell)
    # The yellow is a highlighter, never a status: lot tags carry carbon text on it.
    assert _ratio(p["on-hi"], p["hi"]) >= 7


@pytest.mark.parametrize("page", PAGES)
def test_theme_colors_match_the_band(page):
    _, light, dark = _palettes()
    html = _read(page)
    assert f'content="{light["carbon"]}" media="(prefers-color-scheme: light)"' in html
    assert f'content="{dark["carbon"]}" media="(prefers-color-scheme: dark)"' in html


# ------------------------------------------------------------------ favicon, fonts and preview config

def test_favicon_is_safe():
    svg = _read("favicon.svg")
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    rest = svg.replace('xmlns="http://www.w3.org/2000/svg"', "")
    for banned in ("<script", "<style", "href=", "http", "<foreignObject", "on"):
        if banned == "on":
            assert not re.search(r"\son[a-z]+\s*=", rest)
        else:
            assert banned not in rest, banned
    png = (WEB / "favicon-32.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", png[16:24]) == (32, 32)


def test_fonts_ship_with_their_licences():
    for font, licence in (("archivo-latin-wdth-wght.woff2", "OFL-Archivo.txt"),
                          ("jetbrains-mono-latin-wght.woff2", "OFL-JetBrainsMono.txt")):
        assert (WEB / "fonts" / font).read_bytes()[:4] == b"wOF2"
        assert "SIL OPEN FONT LICENSE" in (WEB / "fonts" / licence).read_text(encoding="utf-8").upper()


def test_vercel_csp_allows_the_site_as_built():
    vercel = json.loads(_read("vercel.json"))
    csp = {h["key"]: h["value"] for h in vercel["headers"][0]["headers"]}["Content-Security-Policy"]
    assert "img-src 'self'" in csp and "style-src 'self'" in csp and "script-src 'self'" in csp
    assert "font-src 'self'" in csp and "connect-src 'self'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp


def test_launch_json_runs_the_preview():
    config = json.loads((PROJECT_ROOT / ".claude" / "launch.json").read_text(encoding="utf-8"))
    preview = next(c for c in config["configurations"] if c["name"] == "site-preview")
    assert preview["runtimeArgs"] == ["-m", "auction_agent", "preview"] and preview["port"] == 8765
    assert preview["runtimeExecutable"].replace("\\", "/") == "venv/Scripts/python.exe"


# ------------------------------------------------------------------ the page's helpers (Node)

# Run under Node with the page's own dictionaries (``m`` is the module, ``dict(lang)`` a dictionary).
# The same text runs in a browser with ``m = window`` (see docs/DESIGN.md, Verification).
NODE_BODY = r"""
const out = {};
const fr = dict("fr"), en = dict("en");
m.setStrings("fr", fr, fr);
const sale = {date: "2026-11-09", state: "upcoming", house: "alcopa", room: "Beauvais", category: "VP",
  url: "https://www.alcopa-auction.fr/salle/10209", fee_model: {rate: 0.144, min: 360, vat_rate: 0.2, venue: "room"}};
const at = (t) => new Date(t);
out.countdown = ["2026-11-07T12:00:00Z", "2026-11-08T12:00:00Z", "2026-11-09T05:00:00Z", "2026-11-09T10:00:00Z",
                 "2026-11-10T10:00:00Z"].map((t) => m.countdownText(sale, at(t)));
out.hash = [m.parseHash("#lot-751"), m.parseHash("#lot-"), m.parseHash("#x"), m.parseHash("")];
const snap = {top: [{lot: 751, title: "MINI HATCH", fuel: "Essence", effective_price: 10000, score: 79, tier: "yellow", ct_result: "FAVORABLE"}],
  cards: [{lot: 810, title: "RENAULT ZOE", fuel: "Électrique", effective_price: null, score: 70, tier: "yellow", ct_result: "DEFAVORABLE_MAJEURE"},
          {lot: 502, title: "PEUGEOT 308", fuel: "Diesel", effective_price: 8000, score: 79, tier: "yellow", engine: "stellantis_bluehdi_15_dv5"}],
  compact: [{lot: 990, title: "DACIA SPRING", fuel: "Électrique", effective_price: 15000, score: 40, tier: "red", ct_result: "DEFAVORABLE_CRITIQUE"}]};
const model = m.prepare(snap);
const refs = (r) => r.shown.map((c) => c.ref).join(",");
const b = m.filterCars(model.all, {budget: 12000});
out.budget = {shown: refs(b), hidden: b.hiddenNoPrice};
out.elec = refs(m.filterCars(model.all, {fuel: "elec"}));
out.lot = refs(m.filterCars(model.all, {q: "75"}));
out.words = refs(m.filterCars(model.all, {q: "peugeot 30"}));
out.ct = ["fav", "major", "crit", "none"].map((ct) => refs(m.filterCars(model.all, {ct})));
out.safe = refs(m.filterCars(model.all, {engine: "safe"}));
out.lotWords = ["Lot 75", "n° 751", "n°751", "No 502", "lot"].map((q) => refs(m.filterCars(model.all, {q})));
out.price = m.sortCars(model.all.filter((c) => c.section !== "rest"), "price").map((c) => c.ref).join(",");
out.score = m.sortCars(model.all.filter((c) => c.section !== "rest"), "score").map((c) => c.ref).join(",");
out.waffle = m.waffleOf(model.all).counts;
// The bid simulator (BRIEF v2 §6) and its check values.
const fm = sale.fee_model;
const p538 = {start: 5300, vat: 1060, fees: 763.2, repairs: 0, ct_redo: 0, total: 7123.2, basis: "start", vat_applies: true};
const p816 = {start: 4600, vat: 0, fees: 662.4, repairs: 0, ct_redo: 0, total: 5262.4, basis: "start", vat_applies: false};
const p751 = {start: 8900, vat: 0, fees: 1281.6, repairs: 0, ct_redo: 0, total: 10181.6, basis: "start"};
out.sim = [m.bidCost(fm, p538, 6000), m.bidMax(fm, p538, 8000), m.bidCost(fm, p816, 5000), m.bidMax(fm, p816, 6000),
           m.bidMax(fm, p751, 10000)];
out.simNote = [m.simulate(fm, p751, null, 10000, 8900).maxNote, m.simulate(fm, p816, null, 300, 4600).maxNote];
out.per100 = [m.per100(fm, p538, 6000), m.per100(fm, p816, 5000), m.per100(fm, p816, 2000)];
out.defaults = [m.simDefaults(p538), m.simDefaults(p816)];
out.online = [m.bidCost({rate: 0, min: 0, vat_rate: 0.2, venue: "online"}, p538, 6000),
              m.bidMax({rate: 0, min: 0, vat_rate: 0.2, venue: "online"}, p538, 7200)];
out.repairs = m.bidMax(fm, {start: 7000, vat: 0, fees: 1008, repairs: 890, ct_redo: 80, total: 8978}, 9000);
out.rows = m.priceRows(p538, null, fm);
// Without the sale's fee model: no simulator (fees are never guessed), the rate read from the lot.
out.feeNull = m.feeModelOf({}, {fee_rate: 0.144});
out.rowsNoFm = m.priceRows(Object.assign({}, p538, {fee_rate: 0.144}), null, null).rows.map((r) => r.label);
// A lot on one line opened in the sheet: its CT and its engine, said once.
out.compactBrief = m.briefOf({lot: 504, tier: "red", engine: "stellantis_bluehdi_15_dv5", why: "x", why_code: "engine_kb",
  why_params: {ref: "stellantis_bluehdi_15_dv5"}, ct_result: "DEFAVORABLE_MAJEURE", ct_counts: {minor: 0, major: 1, critical: 0}},
  {llm: {}}).lines.map((l) => l.text);
out.basis = [m.priceBasis({basis: "shown"}), m.priceBasis({basis: "bid", start: 9000}, 8700), m.priceBasis({basis: "bid", start: 9000}),
             m.priceBasis({basis: "start"}), m.priceBasis(null, 8000), m.priceBasis(null, null)];
out.basisLine = [m.basisLine({detail: {price: p538}}), m.basisLine({detail: {price: p751}}),
                 m.basisLine({detail: {price: Object.assign({}, p751, {basis: "shown"})}})];
// Rings and the CT grid.
out.band = [84, 79, 70, 69, 60, 57, null].map((s) => m.ringBand(s, {green_min: 80, yellow_min: 60}));
const g = m.ringGeom(78, 48);
out.ring = [Math.round(g.r * 100) / 100, Math.round((g.len / g.circ) * 100)];
out.grid = [m.gridOf({ct_grid: "....nn..."}), m.gridOf({ct_grid: "bad"}),
            m.gridOf({detail: {ct: {result: "DEFAVORABLE_MAJEURE", defects: [{code: "1.2.1.a.1", severity: "major"},
              {code: "12.1.1.a.1", severity: "critical"}, {code: "6.1.1.a.1", severity: "minor"}]}}}),
            m.gridOf({detail: {ct: {result: "UNKNOWN", defects: []}}})];
out.ctSummary = [{ct_result: "FAVORABLE", ct_counts: {minor: 2, major: 0, critical: 0}}, {ct_result: "FAVORABLE", ct_counts: {minor: 0, major: 0, critical: 0}},
                 {ct_result: "DEFAVORABLE_MAJEURE", ct_counts: {minor: 5, major: 6, critical: 0}}, {ct: "CT défavorable (défaillance critique)"},
                 {ct_line: "CT pas encore exigé (moins de 4 ans)"}, {ct_result: "UNKNOWN"}]
  .map((c) => { const s = m.ctSummary(c); return [s.state, s.word, s.detail]; });
// Formats, sale lines, signals (French).
out.fmt = [m.eur(10182), m.kmText(89569), m.saleTitle(sale), m.saleLine(sale, 573), m.saleShort(sale)];
const c816 = {lot: 816, engine: "stellantis_puretech_eb2_turbo", gates: ["Motorisation à risque connu : …"], gate_codes: ["engine_risk"],
  gate_params: [{ref: "stellantis_puretech_eb2_turbo"}], ct_result: "FAVORABLE", ct_counts: {minor: 1, major: 0, critical: 0},
  effective_price: 5262.4, mileage: 55209, fuel: "Essence",
  detail: {price: p816, ct: {result: "FAVORABLE", defects: []}, ct_doc: "parsed", damages: null,
    reasons: [{code: "engine_kb", text: "1.2 PureTech turbo : …", src: "base moteurs", pillar: "Fiabilité", pts: -8.8, params: {ref: "stellantis_puretech_eb2_turbo"}},
              {code: "ct_favorable", text: "CT favorable, 1 défaillance mineure", src: "contrôle technique", pillar: "État", pts: 7, params: {n: 1}},
              {code: "warranty", text: "Garantie mécanique incluse (formule non précisée)", src: "annonce", pillar: "État", pts: 1.8, params: {kind: "unspecified"}}]}};
const c538 = {effective_price: 7123.2, mileage: 36785, gates: [], detail: {price: p538, ct: {result: "FAVORABLE"}, ct_doc: "parsed", reasons: []}};
const engines = {stellantis_puretech_eb2_turbo: {label: "1.2 PureTech turbo : …", check: "Demandez une facture de remplacement de la courroie."}};
out.signal = [m.rowSignal(c816, {engines}), m.rowSignal(c538, {}),
              m.rowSignal({effective_price: null, gates: [], detail: {}}, {}),
              m.rowSignal({effective_price: 1, mileage: 1, gates: ["Motorisation à risque connu (ancien)"]}, {})]
  .map((s) => s && [s.tone, s.text]);
out.why = [m.whyText({why: "50 993 km/an, au-dessus de la normale", why_code: "km_per_year", why_params: {km_year: 50993}}, {}),
           m.whyText({why: "1.5 BlueHDi …", why_code: "engine_kb", why_params: {ref: "stellantis_bluehdi_15_dv5"}}, {}),
           m.whyText({why: "Ancien texte"}, {})];
const brief = m.briefOf(c816, {engines, llm: {enabled: false}});
out.brief = {lines: brief.lines.map((l) => [l.tone, l.text, l.src]), verify: brief.verify, ai: brief.ai};
out.checks = m.joinChecks(brief.verify);
out.defect = [m.defectLabel("RÉGLAGE (FEUX DE BROUILLARD AVANT) : Mauvaise orientation horizontale d'un feu de brouillard avant (G, AVD)"),
              m.defectLabel("ÉTAT DE LA CABINE ET DE LA CARROSSERIE : Panneau ou élément endommagé (AV)"), m.defectLabel("Frein")];
out.fnCounts = m.functionCounts({defects: [{code: "1.1.1", severity: "major"}, {code: "1.2.1", severity: "minor"}, {code: "12.1", severity: "major"}]})[1];
out.err = [m.errorFor("storage_error").retry, m.errorFor("no_snapshot").retry, m.errorFor(undefined, 401).reload,
           m.errorFor("teapot", 418).title, m.errorFor(null, 504).retry];
out.safeUrl = [m.safeUrl("https://www.alcopa-auction.fr/x"), m.safeUrl("https://evil.example/alcopa-auction.fr"),
               m.safeUrl("javascript:alert(1)"), m.safeUrl("https://alcopa-auction.fr.evil.example/")];
const ALC = "https://www.alcopa-auction.fr";
out.target = [m.listingTarget({url: `${ALC}/voiture-occasion/x`}, sale), m.listingTarget({url: null}, sale),
              m.listingTarget({url: "https://evil.example/"}, {url: "javascript:alert(1)"})].map((x) => [x.kind, x.house]);
out.prov = [m.provisionalParts({provisional: true, counts: {green: 0, yellow: 167, red: 406, partial_why: {value_unknown: 572}}}),
            m.provisionalParts({provisional: false})];
out.ai = [m.aiState({enabled: false}), m.aiState({enabled: true, coverage: {listed: 50, with_verdict: 50}}),
          m.aiState({enabled: true, coverage: {listed: 50, with_verdict: 3}}), m.aiState({enabled: true, error: "api"})];
out.sales = [m.salesOf({sale}).length, m.salesOf({sales: [sale, sale]}).length, m.salesOf({}).length,
             m.featuredSale({sales: [Object.assign({}, sale, {date: "2026-11-16"}), sale]}, at("2026-11-08T12:00:00Z")).date];
out.cased = ["MINI HATCH 5 PORTES F55 LCI", "1.2 PURETECH 130CH S&S BVM6 ALLURE", "", null].map(m.displayCase);
// English: the same helpers, words written from codes, never translated from the French.
m.setStrings("en", en, fr);
out.en = {countdown: m.countdownText(sale, at("2026-11-08T12:00:00Z")), fmt: [m.eur(10182), m.kmText(89569), m.saleTitle(sale), m.saleShort(sale)],
  signal: m.rowSignal(c816, {engines}).text, why: m.whyText({why: "50 993 km/an…", why_code: "km_per_year", why_params: {km_year: 50993}}, {}),
  reasons: c816.detail.reasons.map((r) => m.reasonText(r)), basis: m.basisLine({detail: {price: p538}}),
  sim: m.simulate(fm, p751, null, 10000, 8900).maxNote, ct: m.ctSummary({ct_result: "DEFAVORABLE_MAJEURE", ct_counts: {major: 2}}).detail,
  whyGate: [m.whyText({why: "Kilométrage non garanti", why_code: "mileage_not_guaranteed"}, {}),
            m.whyText({why: "CT défavorable pour défaillance critique", why_code: "ct_critique", why_params: null}, {}),
            m.whyText({why: "CT défavorable : 2 défaillances critiques", why_code: "ct_critique", why_params: {n: 2}}, {})]};
out.missing = [...m.MISSING].filter((k) => !(k in en));
"""

_NODE_SCRIPT = r"""
const path = require("path");
const m = require(process.argv[1]);
const dict = (lang) => require(path.join(path.dirname(process.argv[1]), "i18n", `${lang}.json`));
""" + NODE_BODY + "\nconsole.log(JSON.stringify(out));\n"

NB = " "


def check_page_helpers(out):
    """The expectations of the page's helpers (shared by the Node run and the browser run)."""
    assert out["countdown"] == ["dans 2 jours", "demain", "aujourd’hui", "en cours", "terminée"]
    assert out["hash"] == ["751", None, None, None]
    assert out["budget"] == {"shown": "751,502", "hidden": 1}  # the car without a price is counted, not shown
    assert out["elec"] == "810,990" and out["lot"] == "751" and out["words"] == "502"
    assert out["ct"] == ["751", "810", "990", "502"]
    assert out["safe"] == "751,810,990"  # « Sans moteur à risque connu »
    assert out["lotWords"] == ["751", "751", "751", "502", ""]
    assert out["price"] == "502,751,810" and out["score"] == "502,751,810"
    assert out["waffle"] == {"fav": 1, "major": 1, "crit": 1, "none": 1}
    # BRIEF v2 §6 check values: 538 (VAT) 8 064 / 5 952, 816 5 720 / 5 244, 751 8 741 under its 8 900 start.
    assert out["sim"] == [8064, 5952, 5720, 5244, 8741]
    assert out["simNote"] == [f"Sous la mise à prix (8{NB}900{NB}€){NB}: ce budget ne permet pas d’ouvrir les enchères.",
                              "Ce budget ne couvre pas les frais et réparations."]
    assert out["per100"] == [134, 114, None]  # none at the 360 € minimum fee
    assert out["defaults"] == [{"P": 6000, "T": 8000}, {"P": 5000, "T": 6000}]
    assert out["online"] == [7200, 6000]
    assert out["repairs"] == 7019  # floor(min((9000-970)/1.144, 9000-970-360)): repairs and CT redo first
    assert sum(r["value"] for r in out["rows"]["rows"]) == out["rows"]["total"] == 7123
    assert out["feeNull"] is None
    assert out["rowsNoFm"][2] == f"Frais acheteur, 14,4{NB}%"  # the lot's own rate, no assumed minimum
    assert out["compactBrief"] == [f"CT défavorable{NB}: 1{NB}défaillance majeure", "Moteur 1.5 BlueHDi (DV5) à risque connu"]
    assert [r["label"] for r in out["rows"]["rows"]] == [
        "Mise à prix HT", f"TVA non récupérable, 20{NB}%", f"Frais acheteur, 14,4{NB}% (min. 360{NB}€)",
        "Réparations demandées par le CT (forfait)"]
    assert out["basis"] == ["shown", "bid", "shown", "start", "start", None]
    assert out["basisLine"] == [f"5{NB}300{NB}€ HT + TVA", f"mise à prix 8{NB}900{NB}€", f"prix affiché 8{NB}900{NB}€"]
    assert out["band"] == ["green", "yellow", "yellow", "orange", "orange", "red", "none"]
    assert out["ring"] == [21.5, 78]  # the arc is the score's share of the circle
    assert out["grid"] == ["....nn...", None, ".M....n..", None]
    assert out["ctSummary"] == [["fav", "Favorable", f"2{NB}mineures"], ["fav", "Favorable", "aucune défaillance"],
                                ["major", "Défavorable · majeure", f"6{NB}majeures"], ["crit", "Défavorable · critique", ""],
                                ["none", "CT pas encore exigé", ""], ["none", "Pas de CT lisible", ""]]
    assert out["fmt"] == [f"10{NB}182{NB}€", f"89{NB}569{NB}km", "Lundi 9 novembre",
                          f"Alcopa · salle de Beauvais · 573{NB}lots · voitures particulières", "Alcopa Beauvais · lun. 9 nov."]
    # A missing price is said in the cost column, never a second time as the row's signal.
    assert out["signal"] == [["risk", f"Moteur à risque connu{NB}: 1.2 PureTech, courroie «{NB}humide{NB}»"],
                             ["watch", f"TVA non récupérable incluse{NB}: 1{NB}060{NB}€"],
                             None, ["risk", "Motorisation à risque connu (ancien)"]]
    assert out["why"] == [f"50{NB}993{NB}km par an, au-dessus de la normale",
                          f"Moteur à risque connu{NB}: 1.5 BlueHDi, chaîne d’arbres à cames", "Ancien texte"]
    brief = out["brief"]
    assert brief["lines"][0] == ["risk", "Moteur 1.2 PureTech turbo à risque connu", "kb"]
    assert ["good", "CT favorable, 1 défaillance mineure", "ct"] in brief["lines"]
    assert brief["verify"][0] == "Demandez une facture de remplacement de la courroie."  # the engine's own check first
    assert brief["ai"] == {"kind": "off"}
    assert out["checks"].startswith("demandez une facture") and out["checks"].endswith("photos de l’annonce.")
    assert out["defect"] == [
        {"text": "Réglage (feux de brouillard avant) : mauvaise orientation horizontale d'un feu de brouillard avant", "where": "G, AVD"},
        {"text": "État de la cabine et de la carrosserie : panneau ou élément endommagé", "where": "AV"},
        {"text": "Frein", "where": None}]
    assert out["fnCounts"] == {"minor": 1, "major": 1, "critical": 0}  # function 12 has no cell
    assert out["err"] == [True, False, True, "Analyse indisponible (code 418)", True]
    assert out["safeUrl"] == ["https://www.alcopa-auction.fr/x", None, None, None]
    assert out["target"] == [["lot", "Alcopa"], ["sale", "Alcopa"], ["none", None]]
    assert out["prov"] == [{"short": "Classement provisoire", "reason": "prix pas encore comparés"}, None]
    assert out["ai"] == ["off", "full", "partial", "error"]
    assert out["sales"] == [1, 2, 0, "2026-11-09"]
    assert out["cased"] == ["Mini Hatch 5 Portes F55 LCI", "1.2 Puretech 130CH S&S BVM6 Allure", "", None]
    en = out["en"]
    assert en["countdown"] == "tomorrow"
    assert en["fmt"] == ["€10,182", f"89,569{NB}km", "Monday 9 November", "Alcopa Beauvais · Mon 9 Nov"]
    assert en["signal"] == "Known engine risk: 1.2 PureTech, “wet” timing belt"
    assert en["why"] == f"50,993{NB}km a year, above normal"
    assert en["reasons"] == ["Known engine risk: 1.2 PureTech turbo", "CT passed, 1 minor defect",
                             "Mechanical warranty included (terms not specified)"]
    assert en["basis"] == "€5,300 excl. VAT + VAT"
    assert en["sim"] == "Below the starting price (€8,900): this budget cannot open the bidding."
    assert en["ct"] == "2 major"
    # A lot ranked low by a gate is worded from the gate's code; the critical-CT gate has no count.
    assert en["whyGate"] == ["Mileage not guaranteed", "CT failed for a critical defect", "CT failed: 2 critical defects"]
    assert out["missing"] == []  # no English key falls back to French


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_page_helpers():
    raw = subprocess.run(["node", "-e", _NODE_SCRIPT, str(WEB / "app.js")], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    check_page_helpers(json.loads(raw))
