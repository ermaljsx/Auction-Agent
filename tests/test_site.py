"""The private site's static files (web/): markup, script and style rules, contrast, favicon.

No browser here: the rules that keep the page safe under its CSP (no inline code or styles,
no HTML from the data) and accessible (landmarks, focus, contrast) are checked on the files.
The page's pure helpers run under Node when it is installed (CI); locally they are skipped.
"""
import json
import re
import shutil
import subprocess

import pytest

from tests.conftest import PROJECT_ROOT

WEB = PROJECT_ROOT / "web"
PAGES = ("index.html", "about.html")


def _read(name):
    return (WEB / name).read_text(encoding="utf-8")


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
    assert '<meta name="color-scheme" content="light dark">' in html
    assert '<html lang="fr">' in html and '<meta name="robots" content="noindex, nofollow">' in html
    assert "http://" not in html and "https://" not in html  # no external resources or links
    skip_target = re.search(r'class="skip" href="#([a-z-]+)"', html).group(1)
    assert f'id="{skip_target}"' in html


def test_index_has_the_catalogue_landmarks_and_controls():
    html = _read("index.html")
    assert re.search(r'<p id="count"[^>]*role="status"', html)
    assert 'aria-controls="f-more"' in html and 'aria-expanded="false"' in html
    assert 'id="catalogue"' in html and 'aria-busy="true"' in html
    assert 'role="search"' in html and 'aria-label="Filtrer les lots"' in html
    for field in ("f-q", "f-budget", "f-fuel", "f-gearbox", "f-km", "f-year", "f-ct", "f-tier", "f-sort"):
        assert f'<label' in html and f'for="{field}"' in html and f'id="{field}"' in html  # every control is labelled
    assert "<form" in html and 'autocomplete="off"' in html


def test_every_element_the_script_looks_up_exists():
    html, app = _read("index.html"), _read("app.js")
    made_by_script = {"sheet-title", "sheet-masked"}
    ids = set(re.findall(r'\$\("([a-z0-9-]+)"\)', app))
    assert ids and not {i for i in ids if f'id="{i}"' not in html} - made_by_script
    for group in ("g-yellow", "g-red"):  # looked up through a template
        assert f'id="{group}"' in html and f'id="{group}-list"' in html and f'id="n-{group}"' in html


# ------------------------------------------------------------------ script

SVG_NS = 'const SVG_NS = "http://www.w3.org/2000/svg";'


def test_app_js_rules():
    app = _read("app.js")
    assert app.lstrip().splitlines()[0].startswith("//") and '"use strict";' in app
    # The SVG namespace is a name for createElementNS, not a URL: it is the only "http://" allowed.
    assert app.count(SVG_NS) == 1 and app.count("http://") == 1
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function", "document.write",
                   'setAttribute("style"', ".style.cssText", "localStorage", "http://"):
        assert banned not in app.replace(SVG_NS, ""), banned
    for required in ("aria-expanded", '"Escape"', "hashchange", "peut se tromper", "nouvel onglet",
                     "noopener noreferrer", 'weekday: "long"', 'addEventListener("submit"', "preventDefault",
                     "replaceState", "prefers-reduced-motion", "aria-busy"):
        assert required in app, required
    # CSS custom properties through the CSSOM only (allowed by the CSP).
    assert app.count(".style.setProperty(") == 1  # inside setVar
    assert set(re.findall(r'setVar\([\w.]+, "(--[a-z0-9-]+)"', app)) == {"--v", "--w", "--bar-h"}
    # SVG only through createElementNS with a fixed attribute list (no href, no style, no event handlers).
    attrs = re.search(r"const SVG_ATTRS = new Set\(\[(.*?)\]\);", app, re.S).group(1)
    assert not re.search(r'"(href|xlink:href|style|on[a-z]+)"', attrs)
    assert app.count("createElementNS(") == 1
    assert "Vente du lundi" not in app


def test_app_js_touches_the_dom_only_after_load():
    app = _read("app.js")
    assert 'if (typeof module !== "undefined" && module.exports)' in app
    assert app.rstrip().endswith('if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", boot);')
    top_level = [line for line in app.splitlines() if line and not line[0].isspace()]
    assert not [line for line in top_level if line.startswith(("document.", "window.", "$(", "load("))]


def test_only_alcopa_links_and_the_page_api_are_used():
    app = _read("app.js")
    assert re.findall(r"fetch\(`([^`]*)`", app) == ["/api/snapshot?t=${Date.now()}"]
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


def test_css_rules():
    css, light, dark = _palettes()
    for required in ("prefers-reduced-motion", "prefers-color-scheme: dark", "color-scheme: light dark",
                     ":focus-visible", "forced-colors: active", "font-stretch: 75%", "tabular-nums",
                     '"Bahnschrift"', "system-ui"):
        assert required in css, required
    assert "@import" not in css and "url(" not in css and "@font-face" not in css  # nothing downloaded
    assert "box-shadow" not in css and "text-transform: uppercase" not in css  # no shadows, no all-caps labels
    assert "letter-spacing" not in css
    assert len(light) >= 13 and set(light) == set(dark)  # every colour has a dark value


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_token_contrast_ratios(mode):
    _, light, dark = _palettes()
    p = light if mode == "light" else dark
    for text in ("ink", "ink-2", "accent", "good", "watch", "risk"):
        for bg in ("paper", "surface", "surface-2", "accent-wash"):
            assert _ratio(p[text], p[bg]) >= 4.5, (mode, text, bg, round(_ratio(p[text], p[bg]), 2))
    for bg in ("paper", "surface", "surface-2"):
        assert _ratio(p["ink-3"], p[bg]) >= 3  # control borders, icons, ticks (non-text)
        assert _ratio(p["watch-mark"], p[bg]) >= 3  # gauge fill and edge of a "correct" lot (non-text)
    assert _ratio(p["ink"], p["watch-wash"]) >= 4.5 and _ratio(p["ink"], p["risk-wash"]) >= 4.5
    assert _ratio(p["risk"], p["risk-wash"]) >= 4.5 and _ratio(p["watch"], p["watch-wash"]) >= 4.5
    assert _ratio(p["good"], p["good-wash"]) >= 4.5 and _ratio(p["ink"], p["good-wash"]) >= 4.5
    assert _ratio(p["accent"], p["paper"]) >= 4.5  # focus ring on the page
    assert _ratio(p["on-accent"], p["accent"]) >= 4.5  # the filled "Voir l'annonce" button
    # The masthead band: white and pale-blue text, the provisional callout, the yellow countdown stamp.
    for text in ("band-ink", "band-ink-2"):
        for bg in ("band", "band-panel"):
            assert _ratio(p[text], p[bg]) >= 4.5, (mode, text, bg)
    assert _ratio(p["band"], p["plate-yellow"]) >= 4.5 and _ratio(p["plate-yellow"], p["band"]) >= 3
    assert _ratio(p["plate-ink"], p["plate-bg"]) >= 4.5  # the number on the plate
    # Tally segments: filled, so none reads as the empty track of a progress bar.
    assert _ratio(p["tally-rest"], p["band"]) >= 3 and _ratio(p["tally-red"], p["band"]) >= 3
    assert p["plate-bg"] == light["plate-bg"] and p["plate-ink"] == light["plate-ink"]  # a white plate in both modes


def test_theme_colors_match_the_paper():
    _, light, dark = _palettes()
    html = _read("index.html")
    assert f'content="{light["paper"]}" media="(prefers-color-scheme: light)"' in html
    assert f'content="{dark["paper"]}" media="(prefers-color-scheme: dark)"' in html


# ------------------------------------------------------------------ favicon and preview config

def test_favicon_is_safe():
    svg = _read("favicon.svg")
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    rest = svg.replace('xmlns="http://www.w3.org/2000/svg"', "")
    for banned in ("<script", "<style", "href=", "http", "<foreignObject", "on"):
        if banned == "on":
            assert not re.search(r"\son[a-z]+\s*=", rest)
        else:
            assert banned not in rest, banned


def test_vercel_csp_allows_the_site_as_built():
    vercel = json.loads(_read("vercel.json"))
    csp = {h["key"]: h["value"] for h in vercel["headers"][0]["headers"]}["Content-Security-Policy"]
    assert "img-src 'self'" in csp and "style-src 'self'" in csp and "script-src 'self'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp


def test_launch_json_runs_the_preview():
    config = json.loads((PROJECT_ROOT / ".claude" / "launch.json").read_text(encoding="utf-8"))
    preview = next(c for c in config["configurations"] if c["name"] == "site-preview")
    assert preview["runtimeArgs"] == ["-m", "auction_agent", "preview"] and preview["port"] == 8765
    assert preview["runtimeExecutable"].replace("\\", "/") == "venv/Scripts/python.exe"


# ------------------------------------------------------------------ the page's helpers (Node)

_NODE_SCRIPT = r"""
const m = require(process.argv[1]);
const sale = {date: "2026-11-09", state: "upcoming"};
const at = (t) => new Date(t);
const out = {};
out.countdown = ["2026-11-07T12:00:00Z", "2026-11-08T12:00:00Z", "2026-11-09T05:00:00Z", "2026-11-09T10:00:00Z",
                 "2026-11-10T10:00:00Z"].map((t) => m.countdownText(sale, at(t)));
out.hash = [m.parseHash("#lot-751"), m.parseHash("#lot-"), m.parseHash("#x"), m.parseHash("")];
const snap = {top: [{lot: 751, title: "MINI HATCH", fuel: "Essence", effective_price: 10000, score: 79, tier: "yellow"}],
  cards: [{lot: 810, title: "RENAULT ZOE", fuel: "Électrique", effective_price: null, score: 70, tier: "yellow"},
          {lot: 502, title: "PEUGEOT 308", fuel: "Diesel", effective_price: 8000, score: 79, tier: "yellow"}],
  compact: [{lot: 990, title: "DACIA SPRING", fuel: "Électrique", effective_price: 15000, score: 40, tier: "red"}]};
const model = m.prepare(snap);
const cars = model.cars.concat(model.compact);
const refs = (r) => r.shown.map((c) => c.ref).join(",");
out.budget = m.filterCars(cars, {budget: 12000});
out.budget = {shown: refs(out.budget), hidden: out.budget.hiddenNoPrice};
out.elec = refs(m.filterCars(cars, {q: "elec"}));
out.lot = refs(m.filterCars(cars, {q: "75"}));
out.words = refs(m.filterCars(cars, {q: "peugeot 30"}));
out.tier = refs(m.filterCars(cars, {tier: "yellow", fuel: "Diesel"}));
out.price = m.sortCars(model.cars, "price").map((c) => c.ref).join(",");
out.score = m.sortCars(model.cars, "score").map((c) => c.ref).join(",");
out.rows = m.priceRows({start: 8900, vat: 0, fees: 1281.6, repairs: 0, ct_redo: 0, total: 10181.6});
out.rows2 = m.priceRows({start: 9999, vat: 1999.8, fees: 1439.86, repairs: 250, ct_redo: 80, total: 13768.66});
out.noRows = m.priceRows(null);
out.bid = [m.priceRows({start: 8700, vat: 0, fees: 1252.8, repairs: 0, ct_redo: 0, total: 9952.8, basis: "bid"}),
           m.priceRows({start: 8700, vat: 0, fees: 1252.8, repairs: 0, ct_redo: 0, total: 9952.8, basis: "start"}),
           m.priceRows({start: 8700, vat: 0, fees: 1252.8, repairs: 0, ct_redo: 0, total: 9952.8})]
  .map((t) => t.rows[0].label);
out.lotWords = ["Lot 75", "n° 751", "n°751", "No 502", "lot"].map((q) => refs(m.filterCars(cars, {q})));
out.err = [m.errorFor("storage_error").retry, m.errorFor("no_snapshot").retry, m.errorFor(undefined, 401).reload,
           m.errorFor("teapot", 418).title, m.errorFor(null, 504).title, m.errorFor(null, 504).retry];
out.fold = m.fold("Électrique ÉTAT");
out.safe = [m.safeUrl("https://www.alcopa-auction.fr/x"), m.safeUrl("https://evil.example/alcopa-auction.fr"),
            m.safeUrl("javascript:alert(1)"), m.safeUrl("https://alcopa-auction.fr.evil.example/")];
out.facts = m.factsLine({year: 2019, mileage: 89569, fuel: "Éthanol E85", gearbox: "Manuelle"});
out.heading = m.saleHeading({date: "2026-11-09", room: "beauvais"}, at("2026-11-07T12:00:00Z"));
out.cased = ["MINI HATCH 5 PORTES F55 LCI", "LAND ROVER RANGE ROVER EVOQUE", "1.2 PURETECH 130CH S&S BVM6 ALLURE",
             "1.0 T-GDI 100 HYBRID 48V INITIA", "DS DS7 CROSSBACK", "PURETECH 83 S&S BVM5 YOU!", "BMW SERIE 3 G20",
             "1.6 CRDI 136 HYBRID 48V DCT-7 N LINE EXECUTIVE", "E-TECH 140 - 21N BUSINESS", "MAZDA CX-5",
             "Renault Clio", "", null, "RANGE ROVER SPORT MARK VIII", "NISSAN QASHQAI+2", "OUTLANDER (PHEV)",
             "1.5 DCI 115 AUTO/BVA"].map(m.displayCase);
out.icons = m.factsParts({year: 2021, mileage: 9000, fuel: "Hybride rechargeable", gearbox: "Automatique"}).map((p) => p.icon)
  .concat([m.factsParts({fuel: "Électrique"})[0].icon, m.factsParts({fuel: "Hybride"})[0].icon, m.factsParts({fuel: "GPL"})[0].icon]);
out.tone = [{ct_result: "FAVORABLE", ct_counts: {minor: 0, major: 0, critical: 0}, ct_line: "CT favorable, sans défaillance"},
            {ct_result: "FAVORABLE", ct_counts: {minor: 2, major: 0, critical: 0}},
            {ct_result: "DEFAVORABLE_MAJEURE", ct_line: "CT défavorable, 2 défaillances"},
            {ct_result: "DEFAVORABLE_MAJEURE", ct_line: "CT pas encore exigé (moins de 4 ans)"},
            {ct_result: "UNKNOWN", ct_line: "CT illisible"}, {ct: "CT favorable"}, {ct: "CT défavorable (défaillances majeures)"}, {},
            {ct_line: "CT pas encore exigé (moins de 4 ans)"}, {ct_result: "UNKNOWN", ct_line: "CT pas encore exigé (moins de 4 ans)"}]
  .map(m.ctTone);
out.ctLine = [{ct_result: "DEFAVORABLE_MAJEURE", ct_counts: {minor: 5, major: 6, critical: 0}, ct_line: "CT pas encore exigé (moins de 4 ans)"},
              {ct_result: "DEFAVORABLE_CRITIQUE", ct_line: "CT pas encore exigé (moins de 4 ans)"},
              {ct_result: "UNKNOWN", ct_line: "CT pas encore exigé (moins de 4 ans)"},
              {ct_result: "FAVORABLE", ct_line: "CT favorable, sans défaillance"}].map(m.ctLineOf);
out.ctResult = [m.ctResultTone({result: "FAVORABLE", defects: []}), m.ctResultTone({result: "FAVORABLE", defects: [{}]}),
                m.ctResultTone({result: "DEFAVORABLE_CRITIQUE"}), m.ctResultTone({result: "UNKNOWN"}), m.ctResultTone(null)];
out.tally = m.tallyParts({top: [{}, {}], cards: [{}], compact: [], counts: {compact: {green: 1, yellow: 138, red: 384}}});
out.tallyV1 = m.tallyParts({top: [{}], cards: [], counts: {green: 0, yellow: 10, red: 1}});
out.tallyNone = m.tallyParts({});
// The one-line list dropped from a snapshot that grew too large: the totals by tier still count every lot.
out.tallyDropped = m.tallyParts({top: [{}], cards: [], compact: [],
  counts: {green: 0, yellow: 10, red: 5, compact: {green: 0, yellow: 0, red: 0}, compact_dropped: 14}});
out.short = [m.partialShort({provisional: true, counts: {partial: 573}}), m.partialShort({counts: {partial: 3}}),
             m.partialShort({counts: {partial: 0}})];
out.gauge = [m.gaugePoint(0, 26), m.gaugePoint(50, 26), m.gaugePoint(100, 26), m.gaugePoint(140, 26)];
out.headingYear = m.saleHeading({date: "2027-01-04", room: "Beauvais"}, at("2026-12-30T12:00:00Z"));
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_page_helpers():
    raw = subprocess.run(["node", "-e", _NODE_SCRIPT, str(WEB / "app.js")], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    out = json.loads(raw)
    assert out["countdown"] == ["dans 2 jours", "demain", "aujourd'hui", "en cours", "terminée"]
    assert out["hash"] == ["751", None, None, None]
    assert out["budget"] == {"shown": "751,502", "hidden": 1}  # the car without a price is counted, not shown
    assert out["elec"] == "810,990" and out["lot"] == "751" and out["words"] == "502"
    assert out["tier"] == "502"
    assert out["price"] == "502,751,810"  # unknown price last
    assert out["score"] == "502,751,810"  # equal scores: the cheaper first
    for rows in (out["rows"], out["rows2"]):
        assert sum(r["value"] for r in rows["rows"]) == rows["total"]
    assert [r["key"] for r in out["rows"]["rows"]] == ["start", "fees", "repairs"]
    assert [r["key"] for r in out["rows2"]["rows"]] == ["start", "vat", "fees", "repairs", "ct_redo"]
    assert out["rows"]["total"] == 10182 and out["noRows"] is None
    # A platform error page (not JSON, status 504) is an outage to retry, not an expired session.
    assert out["err"] == [True, False, True, "Analyse indisponible (code 418)", "Analyse indisponible (code 504)", True]
    assert out["bid"] == ["Enchère courante", "Mise à prix", "Mise à prix"]
    assert out["lotWords"] == ["751", "751", "751", "502", ""]  # "Lot 751", "n°751": the lot number
    assert out["fold"] == "electrique etat"
    assert out["safe"] == ["https://www.alcopa-auction.fr/x", None, None, None]
    # A number never breaks from its unit (no-break space before "km").
    assert out["facts"] == "2019, 89\u202f569\u00a0km, éthanol E85, manuelle"
    assert out["heading"] == "Lundi 9 novembre, Beauvais"
    assert out["headingYear"] == "Lundi 4 janvier 2027, Beauvais"
    # Data titles in readable case; words with a digit, of three letters or fewer, or acronyms stay.
    assert out["cased"] == ["Mini Hatch 5 Portes F55 LCI", "Land Rover Range Rover Evoque",
                            "1.2 Puretech 130CH S&S BVM6 Allure", "1.0 T-GDI 100 Hybrid 48V Initia", "DS DS7 Crossback",
                            "Puretech 83 S&S BVM5 YOU!", "BMW Serie 3 G20", "1.6 CRDI 136 Hybrid 48V DCT-7 N Line Executive",
                            "E-Tech 140 - 21N Business", "Mazda CX-5", "Renault Clio", "", None,
                            "Range Rover Sport Mark VIII", "Nissan Qashqai+2", "Outlander (PHEV)", "1.5 DCI 115 Auto/BVA"]
    assert out["icons"] == ["year", "km", "plugin", "gearbox", "electric", "hybrid", "fuel"]
    # A failed voluntary CT on a car too young to need one is still a risk; without a result it is neutral.
    assert out["tone"] == ["good", "ok", "risk", "risk", "watch", "ok", "risk", "watch", "neutral", "neutral"]
    assert out["ctLine"] == ["CT volontaire défavorable, 6 défaillances (pas encore exigé)",
                             "CT volontaire défavorable (pas encore exigé)", "CT pas encore exigé (moins de 4 ans)",
                             "CT favorable, sans défaillance"]
    assert out["ctResult"] == ["good", "ok", "risk", "watch", "watch"]
    assert out["tally"] == [{"key": "top", "n": 2, "label": "en sélection"}, {"key": "cards", "n": 1, "label": "autre fiche"},
                            {"key": "rest", "n": 139, "label": "corrects sans fiche"},
                            {"key": "red", "n": 384, "label": "écartés"}]
    assert out["tallyV1"] == [{"key": "top", "n": 1, "label": "en sélection"},
                              {"key": "rest", "n": 9, "label": "corrects sans fiche"}, {"key": "red", "n": 1, "label": "écarté"}]
    assert out["tallyNone"] == []
    assert out["tallyDropped"] == [{"key": "top", "n": 1, "label": "en sélection"},
                                   {"key": "rest", "n": 9, "label": "corrects sans fiche"},
                                   {"key": "red", "n": 5, "label": "écartés"}]
    assert out["short"] == ["Classement provisoire : données manquantes.", "Données incomplètes pour 3 lots.", ""]
    assert out["gauge"] == [[6, 34], [32, 8], [58, 34], [58, 34]]  # half circle, clamped at 100
