// Renders /api/snapshot as the sale's catalogue (redesign v2 « Relevé »): a sale card with one
// square per lot, rows with the CT in a 3×3 grid and a score ring, a sheet per lot with the
// listing link first, the estimated cost and a bid simulator. French and English: all copy
// comes from /i18n/<lang>.json. Every value from the snapshot (including AI text) is inserted
// with textContent, never as HTML. Nothing touches the DOM before DOMContentLoaded, so the pure
// helpers can be loaded and tested with Node.
"use strict";

// ------------------------------------------------------------------ constants

const STALE_DAYS = 8;
// The sheet is a pane beside the list from 75rem (both fit: 600 + 24 + 460 px); below, a full-screen view.
const DESKTOP_MQ = "(min-width: 75rem)";
const PHONE_MQ = "(max-width: 47.99rem)";
// The auction houses whose listings the page links to (host -> name shown on the buttons).
const HOUSES = { "alcopa-auction.fr": "Alcopa" };
const HOUSE_NAMES = { alcopa: "Alcopa" };
const BUDGETS = [5000, 8000, 10000, 15000, 20000, 30000];
const KM_STEPS = [50000, 100000, 150000];
const CT_STATES = ["fav", "major", "crit", "none"];
const GRID_FUNCTIONS = [0, 1, 2, 3, 4, 5, 6, 7, 8];
// Row signals, most important first (BRIEF v2 §5.1): a gate, then softer signals.
const SIGNAL_GATES = ["engine_risk", "ct_major_safety", "odometer", "battery_soh", "comment_dealbreaker",
  "mileage_not_guaranteed"];
// Snapshot source labels (French) -> dictionary keys.
const SRC_KEYS = { "contrôle technique": "ct", annonce: "listing", commentaires: "comment", "base moteurs": "kb",
  comparables: "cohort", IA: "llm", "certificat batterie": "battery" };
const PILLARS = [["R", "Fiabilité"], ["C", "État"], ["V", "Prix"]];
const PILLAR_KEY = { Fiabilité: "R", État: "C", Prix: "V", IA: "AI" };
const GATE_SRC = { engine_risk: "kb", ct_critique: "ct", ct_major_safety: "ct", ct_non_lu: "ct", odometer: "ct",
  battery_soh: "battery", comment_dealbreaker: "comment", mileage_not_guaranteed: "comment" };
// Engine and gearbox acronyms longer than three letters that stay in capitals (older snapshots).
const KEEP_CAPS = new Set(["PHEV", "MHEV", "TFSI", "CRDI", "CDTI", "TDCI", "MIVEC", "VTEC"]);
const NBSP = "\u00a0";
const MINUS = "\u2212";

// ------------------------------------------------------------------ strings (i18n)

let LANG = "fr";
let DICT = {};
let BASE = {};  // French: the fallback for a key English lacks
const MISSING = new Set();
let NF = new Intl.NumberFormat("fr-FR");
let EUR = new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });

function locale() {
  return LANG === "en" ? "en-GB" : "fr-FR";
}

// The page's dictionary, and French under it (a missing English key falls back to French).
function setStrings(lang, dict, base) {
  LANG = lang === "en" ? "en" : "fr";
  DICT = dict && typeof dict === "object" ? dict : {};
  BASE = base && typeof base === "object" ? base : DICT;
  NF = new Intl.NumberFormat(locale());
  EUR = new Intl.NumberFormat(locale(), { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
}

function has(dict, key) {
  return Object.prototype.hasOwnProperty.call(dict, key) && typeof dict[key] === "string";
}

function fill(s, params) {
  if (!params) return s;
  return s.replace(/\{(\w+)\}/g, (m, k) => (params[k] === undefined || params[k] === null ? m : String(params[k])));
}

function t(key, params) {
  if (has(DICT, key)) return fill(DICT[key], params);
  MISSING.add(key);
  return has(BASE, key) ? fill(BASE[key], params) : key;
}

function tOr(key, fallback, params) {
  return has(DICT, key) || has(BASE, key) ? t(key, params) : fallback;
}

function isOne(n) {
  return LANG === "en" ? n === 1 : Math.abs(n) < 2;
}

// Plurals: "<key>_one" / "<key>_other"; {n} is the formatted number.
function tn(key, n, params) {
  return t(`${key}_${isOne(n) ? "one" : "other"}`, Object.assign({ n: num(n) }, params));
}

// ------------------------------------------------------------------ formats

function isNum(n) {
  return typeof n === "number" && isFinite(n);
}

// French groups digits with a narrow no-break space, which the condensed figures render
// almost without width: a plain no-break space reads « 10 182 € » as designed.
function wide(s) {
  return s.replace(/ /g, NBSP);
}

function num(n) {
  return isNum(n) ? wide(NF.format(n)) : "";
}

function eur(n) {
  return isNum(n) ? wide(EUR.format(Math.round(n))) : null;
}

function kmText(n) {
  return isNum(n) ? t("unit.km", { n: num(n) }) : null;
}

function capitalise(s) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

function parisDay(now) {
  return (now || new Date()).toLocaleDateString("en-CA", { timeZone: "Europe/Paris" });
}

function parisHour(now) {
  return Number((now || new Date()).toLocaleString("en-GB", { timeZone: "Europe/Paris", hour: "2-digit", hourCycle: "h23" }));
}

function isDay(iso) {
  return typeof iso === "string" && /^\d{4}-\d{2}-\d{2}$/.test(iso);
}

// A calendar day in the page's language: "lundi 9 novembre" / "Monday 9 November".
function dayText(iso, opts) {
  if (!isDay(iso)) return null;
  const o = Object.assign({ timeZone: "Europe/Paris", weekday: "long", day: "numeric", month: "long" }, opts || {});
  return new Date(`${iso}T12:00:00Z`).toLocaleDateString(locale(), o);
}

function shortDate(iso) {
  return dayText(iso, { weekday: undefined, month: "short", year: "numeric" });
}

function signed(n) {
  return `${n < 0 ? MINUS : "+"}${num(Math.abs(n))}`;
}

// ------------------------------------------------------------------ sale and time

// The sale's state right now (the snapshot's own state is frozen at publish time).
function saleStateNow(sale, now) {
  if (!sale || !sale.date) return sale ? sale.state : null;
  const at = now || new Date();
  const today = parisDay(at);
  if (today < sale.date) return "upcoming";
  if (today > sale.date) return "over";
  return parisHour(at) >= 8 ? "in_progress" : "today";
}

function daysUntil(dateIso, now) {
  const from = Date.parse(`${parisDay(now)}T12:00:00Z`);
  const to = Date.parse(`${dateIso}T12:00:00Z`);
  return Math.round((to - from) / 864e5);
}

// The chip on the sale card: « Demain », « Aujourd'hui », « Dans 3 jours »…
function countdownText(sale, now) {
  const state = saleStateNow(sale, now);
  if (state === "over") return t("when.over");
  if (state === "in_progress") return t("when.live");
  if (state === "today") return t("when.today");
  if (!sale || !sale.date) return "";
  const days = daysUntil(sale.date, now);
  if (days === 1) return t("when.tomorrow");
  return days > 1 ? tn("when.in_days", days) : "";
}

// Every covered sale (the snapshot's ``sales``, or its one ``sale`` before v2).
function salesOf(s) {
  const snap = s && typeof s === "object" ? s : {};
  const list = Array.isArray(snap.sales) ? snap.sales.filter((x) => x && typeof x === "object") : [];
  if (list.length) return list;
  return snap.sale && typeof snap.sale === "object" ? [snap.sale] : [];
}

// The sale the card shows: the soonest one still to come, else the latest.
function featuredSale(s, now) {
  const list = salesOf(s);
  const live = list.filter((x) => saleStateNow(x, now) !== "over").sort((a, b) => String(a.date).localeCompare(String(b.date)));
  if (live.length) return live[0];
  return list.slice().sort((a, b) => String(b.date).localeCompare(String(a.date)))[0] || null;
}

function houseOf(sale) {
  if (!sale) return null;
  if (sale.house && HOUSE_NAMES[sale.house]) return HOUSE_NAMES[sale.house];
  return sourceName(sale.url);
}

function roomOf(sale) {
  return sale && sale.room ? capitalise(String(sale.room)) : null;
}

// « Alcopa · salle de Beauvais · voitures particulières »
function saleLine(sale, lots) {
  if (!sale) return "";
  const parts = [];
  const house = houseOf(sale);
  if (house) parts.push(house);
  if (roomOf(sale)) parts.push(t("sale.room", { room: roomOf(sale) }));
  if (isNum(lots)) parts.push(tn("sale.lots_n", lots));
  if (sale.category) parts.push(tOr(`category.${sale.category}`, sale.category));
  return parts.join(" · ");
}

// « Alcopa Beauvais · lun. 9 nov. » (the catalogue bar)
function saleShort(sale) {
  if (!sale) return "";
  const who = [houseOf(sale), roomOf(sale)].filter(Boolean).join(" ");
  const day = dayText(sale.date, { weekday: "short", month: "short" });
  return [who, day].filter(Boolean).join(" · ");
}

function saleTitle(sale) {
  if (!sale) return t("sale.none");
  return capitalise(dayText(sale.date) || sale.title || t("sale.untitled"));
}

function ageHours(iso, now) {
  return ((now || new Date()).getTime() - new Date(iso).getTime()) / 3.6e6;
}

// « Analyse publiée dim. 8 nov. à 02:10 »
function publishedText(iso) {
  const when = new Date(iso);
  if (!isFinite(when.getTime())) return null;
  const day = when.toLocaleDateString(locale(), { timeZone: "Europe/Paris", weekday: "short", day: "numeric", month: "short" });
  const time = when.toLocaleTimeString(locale(), { timeZone: "Europe/Paris", hour: "2-digit", minute: "2-digit" });
  return t("status.published", { day, time });
}

function totalLots(s) {
  const c = (s && s.counts) || {};
  return (c.green || 0) + (c.yellow || 0) + (c.red || 0);
}

// Why the ranking is provisional, for the status line: {short, full} or null.
function provisionalParts(s) {
  const snap = s && typeof s === "object" ? s : {};
  if (!snap.provisional) return null;
  const why = (snap.counts && snap.counts.partial_why) || {};
  const total = totalLots(snap);
  if (total && why.value_unknown >= 0.9 * total) return { short: t("status.provisional"), reason: t("status.prices") };
  return { short: t("status.provisional"), reason: t("status.missing") };
}

// The AI review's state for this publication: "off" | "error" | "partial" | "full".
function aiState(llm) {
  const l = llm && typeof llm === "object" ? llm : {};
  if (!l.enabled) return "off";
  if (l.error) return "error";
  const cov = l.coverage || {};
  if (isNum(cov.listed) && isNum(cov.with_verdict) && cov.with_verdict < cov.listed) return "partial";
  return "full";
}

// The card's status line: each part only when it is true.
function statusParts(s) {
  const parts = [];
  const prov = provisionalParts(s);
  const ai = aiState(s && s.llm);
  return { prov, ai: ai === "full" ? null : t(`status.ai_${ai}`), parts };
}

// ------------------------------------------------------------------ cars

function fold(s) {
  return String(s === null || s === undefined ? "" : s).normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

// Readable case for an older snapshot without ``title_display`` ("MINI HATCH" -> "Mini Hatch").
function displayWord(w) {
  const m = /^([^\p{L}\p{N}]*)(.*?)([^\p{L}\p{N}]*)$/su.exec(w || "");
  const core = m ? m[2] : "";
  if (!core || core !== core.toUpperCase() || /\d/.test(core) || KEEP_CAPS.has(core) || /^[IVX]+$/.test(core)) return w;
  if ((core.match(/\p{L}/gu) || []).length <= 3) return w;
  return `${m[1]}${core.toLowerCase().replace(/\p{L}/u, (c) => c.toUpperCase())}${m[3]}`;
}

function displayCase(text) {
  if (text === null || text === undefined) return text;
  return String(text).split(/(\s+)/)
    .map((word) => word.split(/([-+/])/).map((part, i) => (i % 2 ? part : displayWord(part))).join("")).join("");
}

function titleOf(car) {
  return car.title_display || displayCase(car.title) || t("car.untitled");
}

function subtitleOf(car) {
  return car.subtitle_display || displayCase(car.subtitle) || null;
}

function fuelKind(fuel) {
  const f = fold(fuel);
  if (f === "electrique") return "electric";
  if (f.startsWith("hybride rechargeable")) return "plugin";
  if (f.startsWith("hybride")) return "hybrid";
  return "fuel";
}

// Fuel and gearbox in the page's language (the snapshot names them in French).
function fuelText(fuel) {
  if (!fuel) return null;
  return tOr(`fuel.${fold(fuel).replace(/[^a-z0-9]+/g, "_")}`, String(fuel));
}

function gearboxText(g) {
  if (!g) return null;
  return tOr(`gearbox.${fold(g).replace(/[^a-z0-9]+/g, "_")}`, String(g));
}

// « 2019 · 89 569 km · Essence · Manuelle »
function specsLine(car) {
  return [car.year, kmText(car.mileage), fuelText(car.fuel), gearboxText(car.gearbox)]
    .filter((v) => v !== null && v !== undefined && v !== "").join(" · ");
}

function carRef(car, i) {
  return car && car.lot !== null && car.lot !== undefined && /^[A-Za-z0-9]+$/.test(String(car.lot)) ? String(car.lot) : `x${i}`;
}

function parseHash(hash) {
  const m = /^#lot-([A-Za-z0-9]+)$/.exec(hash || "");
  return m ? m[1] : null;
}

function wordsOf(car) {
  // The lot number too, for lots numbered with letters ("A12", "12B").
  return fold([car.title, car.title_display, car.subtitle, car.trim, car.fuel, car.gearbox, car.lot].filter((x) => x !== null && x !== undefined && x !== "").join(" "))
    .split(/[^a-z0-9]+/).filter(Boolean);
}

// The CT in four states: "fav" | "major" | "crit" | "none" (absent or unreadable).
function ctState(car) {
  const result = car.ct_result || (car.detail && car.detail.ct && car.detail.ct.result) || null;
  if (result === "FAVORABLE") return "fav";
  if (result === "DEFAVORABLE_MAJEURE") return "major";
  if (result === "DEFAVORABLE_CRITIQUE") return "crit";
  if (!result && typeof car.ct === "string") {  // schema 1: only a sentence
    if (/^CT favorable/.test(car.ct)) return "fav";
    if (/critique/.test(car.ct)) return "crit";
    if (/^CT défavorable/.test(car.ct)) return "major";
  }
  return "none";
}

// The nine cells, from the snapshot's ct_grid, else from the sheet's defects, else null.
function gridOf(car) {
  if (typeof car.ct_grid === "string" && /^[.nMCu]{9}$/.test(car.ct_grid)) return car.ct_grid;
  const ct = car.detail && car.detail.ct;
  if (!ct || !Array.isArray(ct.defects) || !["FAVORABLE", "DEFAVORABLE_MAJEURE", "DEFAVORABLE_CRITIQUE"].includes(ct.result)) return null;
  const cells = ".........".split("");
  const rank = { ".": 0, n: 1, M: 2, C: 3 };
  ct.defects.forEach((d) => {
    const m = /^(\d{1,2})\./.exec(String((d && d.code) || ""));
    const mark = { minor: "n", major: "M", critical: "C" }[d && d.severity];
    if (m && mark && Number(m[1]) <= 8 && rank[mark] > rank[cells[Number(m[1])]]) cells[Number(m[1])] = mark;
  });
  return cells.join("");
}

function countsOf(car) {
  const c = car.ct_counts;
  if (c && typeof c === "object") return { minor: c.minor || 0, major: c.major || 0, critical: c.critical || 0 };
  const ct = car.detail && car.detail.ct;
  if (!ct || !Array.isArray(ct.defects)) return null;
  const out = { minor: 0, major: 0, critical: 0 };
  ct.defects.forEach((d) => { if (d && d.severity in out) out[d.severity] += 1; });
  return out;
}

// What the CT cell says: {state, word, detail} (« Favorable » / « 2 mineures »).
function ctSummary(car) {
  const state = ctState(car);
  const n = countsOf(car);
  if (state === "fav") {
    const minor = n ? n.minor : null;
    return { state, word: t("ct.word_fav"), detail: !minor ? (minor === 0 ? t("ct.no_defect") : "") : tn("ct.minor", minor) };
  }
  if (state === "major" || state === "crit") {
    const k = state === "crit" ? (n && n.critical) : (n && n.major);
    return { state, word: t(`ct.word_${state}`), detail: k ? tn(state === "crit" ? "ct.critical" : "ct.major", k) : "" };
  }
  const doc = car.detail && car.detail.ct_doc;
  if (doc === "unreadable" || car.ct_result === "UNKNOWN") return { state, word: t("ct.word_unread"), detail: "" };
  const young = (reasonsOf(car) || car.reasons || []).some((r) => r && r.code === "ct_not_required")
    || /pas encore exigé/.test(car.ct_line || "");
  return { state, word: t(young ? "ct.word_young" : "ct.word_none"), detail: "" };
}

// The ring's colour band (BRIEF v2 §4.1): a gradation of the three tiers.
function ringBand(score, scoring) {
  if (!isNum(score)) return "none";
  const sc = scoring && typeof scoring === "object" ? scoring : {};
  const green = isNum(sc.green_min) ? sc.green_min : 80;
  const yellow = isNum(sc.yellow_min) ? sc.yellow_min : 60;
  if (score >= green) return "green";
  if (score >= Math.max(70, yellow)) return "yellow";
  if (score >= yellow) return "orange";
  return "red";
}

// The ring's geometry for a size in px: the value arc is score % of the circle.
function ringGeom(score, size) {
  const sw = size >= 90 ? 9 : size >= 44 ? 5 : 4;
  const r = (size - sw) / 2;
  const circ = 2 * Math.PI * r;
  const v = isNum(score) ? Math.max(0, Math.min(100, score)) : 0;
  return { sw, r, c: size / 2, circ, len: (circ * v) / 100 };
}

// The sale's lots by CT state, in lot-number order (the waffle) and counted.
function waffleOf(cars) {
  const list = (cars || []).slice().sort((a, b) => cmpNullLast(lotNum(a), lotNum(b), 1));
  const counts = { fav: 0, major: 0, crit: 0, none: 0 };
  const cells = list.map((c) => { const s = ctState(c); counts[s] += 1; return s; });
  return { cells, counts, total: cells.length };
}

function prepare(s) {
  const snap = s && typeof s === "object" ? s : {};
  const all = [];
  const byRef = new Map();
  let i = 0;
  const add = (list, section) => {
    (Array.isArray(list) ? list : []).forEach((c) => {
      if (!c || typeof c !== "object") return;
      let ref = carRef(c, i);
      if (byRef.has(ref)) ref = `x${i}`;
      i += 1;
      const item = Object.assign({}, c, { ref, section, words: wordsOf(c) });
      byRef.set(ref, item);
      all.push(item);
    });
  };
  add(snap.top, "top");
  add(snap.cards, "cards");
  add(snap.compact, "rest");
  const uniq = (key) => [...new Set(all.map((c) => c[key]).filter((v) => v !== null && v !== undefined && v !== ""))];
  return {
    all,
    byRef,
    fuels: uniq("fuel").sort((a, b) => fold(a).localeCompare(fold(b))),
    gearboxes: uniq("gearbox").sort(),
    years: uniq("year").filter(isNum).sort((a, b) => b - a),
    hasCompact: Array.isArray(snap.compact),
    waffle: waffleOf(all),
  };
}

const LOT_WORDS = new Set(["lot", "lots", "n", "no", "num", "numero", "number"]);

// With a number in the query, "lot" and "n°" only introduce it: "Lot 751" and "n°751" find lot 751.
function queryTokens(q) {
  const tokens = fold(q || "").split(/[^a-z0-9]+/).filter(Boolean);
  return tokens.some((x) => /^\d+$/.test(x)) ? tokens.filter((x) => !LOT_WORDS.has(x)) : tokens;
}

function matchesQuery(car, tokens) {
  const words = car.words || wordsOf(car);
  const lot = car.lot === null || car.lot === undefined ? "" : String(car.lot);
  return tokens.every((x) => words.some((w) => w.startsWith(x)) || (/^\d+$/.test(x) && lot.startsWith(x)));
}

// An engine-risk match, from the v2 ``engine`` ref, a gate code, or an older gate's words.
function hasEngineRisk(car) {
  if (car.engine) return true;
  if (Array.isArray(car.gate_codes) && car.gate_codes.includes("engine_risk")) return true;
  if ((Array.isArray(car.gates) ? car.gates : []).some((g) => /^Motorisation à risque/.test(String(g)))) return true;
  // Older snapshots: an engine that is only penalised shows as a point from the engine list.
  const reasons = (car.detail && Array.isArray(car.detail.reasons) ? car.detail.reasons : []).concat(car.reasons || []);
  return reasons.some((r) => r && (r.code === "engine_kb" || r.src === "base moteurs"));
}

// true, false, or "noprice" (would match, but a budget is set and the price is unknown)
function matchesFilters(car, f, tokens) {
  if (f.exact) return String(car.lot) === String(f.q);  // a lot asked for by its number (#lot-75 is not 751)
  if (tokens.length && !matchesQuery(car, tokens)) return false;
  if (f.fuel === "elec") {
    if (fuelKind(car.fuel) === "fuel") return false;
  } else if (f.fuel && car.fuel !== f.fuel) return false;
  if (f.gearbox && car.gearbox !== f.gearbox) return false;
  if (f.km && !(isNum(car.mileage) && car.mileage < f.km)) return false;
  if (f.year && !(isNum(car.year) && car.year >= f.year)) return false;
  if (f.ct && ctState(car) !== f.ct) return false;
  if (f.engine === "safe" && hasEngineRisk(car)) return false;
  if (f.budget) {
    if (!isNum(car.effective_price)) return "noprice";
    if (car.effective_price > f.budget) return false;
  }
  return true;
}

function filterCars(list, f) {
  const filters = f || {};
  const tokens = queryTokens(filters.q);
  const shown = [];
  let hiddenNoPrice = 0;
  (list || []).forEach((car) => {
    const ok = matchesFilters(car, filters, tokens);
    if (ok === true) shown.push(car);
    else if (ok === "noprice") hiddenNoPrice += 1;
  });
  return { shown, hiddenNoPrice };
}

function cmpNullLast(a, b, dir) {
  const an = !isNum(a), bn = !isNum(b);
  if (an || bn) return an === bn ? 0 : an ? 1 : -1;
  return dir * (a - b);
}

function lotNum(car) {
  const n = Number(car.lot);
  return car.lot === null || car.lot === undefined || !isFinite(n) ? null : n;
}

function sortCars(list, key) {
  const byScore = (a, b) => cmpNullLast(a.score, b.score, -1) || cmpNullLast(a.effective_price, b.effective_price, 1)
    || cmpNullLast(lotNum(a), lotNum(b), 1);
  const orders = {
    score: byScore,
    price: (a, b) => cmpNullLast(a.effective_price, b.effective_price, 1) || byScore(a, b),
    km: (a, b) => cmpNullLast(a.mileage, b.mileage, 1) || byScore(a, b),
    year: (a, b) => cmpNullLast(a.year, b.year, -1) || byScore(a, b),
    lot: (a, b) => cmpNullLast(lotNum(a), lotNum(b), 1) || byScore(a, b),
  };
  return (list || []).slice().sort(orders[key] || byScore);
}

// ------------------------------------------------------------------ price and simulator

// What the price the estimate starts from is: "start" (opening price), "bid" (a running bid
// above it) or "shown" (a price shown without an opening price, maybe the opening price itself).
function priceBasis(price, startingPrice) {
  if (!price) return isNum(startingPrice) ? "start" : null;
  if (price.basis === "shown") return "shown";
  if (price.basis !== "bid") return "start";
  return isNum(startingPrice) && isNum(price.start) && price.start > startingPrice ? "bid" : "shown";
}

function vatApplies(price) {
  if (!price) return false;
  if (typeof price.vat_applies === "boolean") return price.vat_applies;
  return isNum(price.vat) && price.vat > 0;
}

// « mise à prix 8 900 € », « 5 300 € HT + TVA », « prix affiché 9 200 € » (the row's cost cell).
function basisLine(car) {
  const p = car.detail && car.detail.price;
  const basis = priceBasis(p, car.starting_price);
  if (!basis) return "";
  const amount = eur(p ? p.start : car.starting_price);
  if (vatApplies(p)) return t("cost.ht_plus_vat", { amount });
  return t(`cost.row_${basis}`, { amount });
}

// The fee model of the lot's sale, as published in the snapshot; null without one (then no
// simulator: the page never guesses fees, BRIEF v2 B3).
function feeModelOf(sale, price) {
  void price;
  const fm = sale && sale.fee_model;
  return fm && isNum(fm.rate) && isNum(fm.min) && isNum(fm.vat_rate) ? fm : null;
}

function simParts(fm, price) {
  return {
    v: vatApplies(price) ? fm.vat_rate : 0,
    R: isNum(price && price.repairs) ? price.repairs : 0,
    C: isNum(price && price.ct_redo) ? price.ct_redo : 0,
    online: fm.venue === "online",
  };
}

// What a bid of P costs in all (BRIEF v2 §6): (1+v)·P + max(rate·P, min) + R + C in a room sale.
function bidCost(fm, price, P) {
  if (!fm || !isNum(P) || P < 0) return null;
  const s = simParts(fm, price);
  const fee = s.online ? 0 : Math.max(fm.rate * P, fm.min);
  return (1 + s.v) * P + fee + s.R + s.C;
}

// The highest bid a total budget T allows, floored to the euro (never rounded up).
function bidMax(fm, price, T) {
  if (!fm || !isNum(T)) return null;
  const s = simParts(fm, price);
  const X = T - s.R - s.C;
  const B = s.online ? X / (1 + s.v) : Math.min(X / (1 + s.v + fm.rate), (X - fm.min) / (1 + s.v));
  return Math.floor(B + 1e-9);
}

// What 100 € more on the bid adds, on the rate branch only (null at the fixed minimum fee).
function per100(fm, price, P) {
  if (!fm) return null;
  const s = simParts(fm, price);
  if (!s.online && (!isNum(P) || fm.rate * P < fm.min)) return null;
  return Math.round(100 * (1 + s.v + (s.online ? 0 : fm.rate)));
}

function roundUp1000(n) {
  return isNum(n) ? Math.ceil(n / 1000) * 1000 : null;
}

function simDefaults(price) {
  return { P: roundUp1000(price && price.start), T: roundUp1000(price && price.total) };
}

// The bid simulator's answers, in words: {cost, max, maxNote, note}.
function simulate(fm, price, P, T, start) {
  const out = { cost: null, max: null, maxNote: null, per100: per100(fm, price, P) };
  if (isNum(P)) out.cost = bidCost(fm, price, P);
  if (isNum(T)) {
    const B = bidMax(fm, price, T);
    out.max = B;
    if (B <= 0) out.maxNote = t("sim.too_low");
    else if (isNum(start) && B < start) out.maxNote = t("sim.below_start", { amount: eur(start) });
  }
  return out;
}

// The cost table, rounded to the euro so the column adds up exactly to the total.
function priceRows(price, startingPrice, fm) {
  if (!price || !isNum(price.total) || !isNum(price.start)) return null;
  const r = (x) => (isNum(x) ? Math.round(x) : 0);
  const basis = priceBasis(price, startingPrice);
  const ht = vatApplies(price) ? t("cost.ht") : "";
  const rows = [{ key: "start", label: `${t(`cost.label_${basis}`)}${ht}`, value: r(price.start) }];
  // Rates from the sale's fee model; without one, read back from the lot itself (never assumed).
  const vatRate = fm ? fm.vat_rate : (price.start > 0 ? price.vat / price.start : null);
  if (r(price.vat)) rows.push({ key: "vat", label: t("cost.vat", { pct: num(Math.round(vatRate * 100)) }), value: r(price.vat) });
  const pct = (x) => new Intl.NumberFormat(locale(), { maximumFractionDigits: 1 }).format(x * 100);
  let feeLabel = t("cost.fees");
  if (fm && fm.rate > 0) feeLabel = t("cost.fees_rate", { pct: pct(fm.rate), min: eur(fm.min) });
  else if (!fm && isNum(price.fee_rate) && price.fee_rate > 0) feeLabel = t("cost.fees_pct", { pct: pct(price.fee_rate) });
  rows.push({ key: "fees", label: feeLabel, value: r(price.fees) });
  rows.push({ key: "repairs", label: t("cost.repairs"), value: r(price.repairs) });
  if (r(price.ct_redo)) rows.push({ key: "ct_redo", label: t("cost.ct_redo"), value: r(price.ct_redo) });
  const total = r(price.total);
  const diff = total - rows.reduce((sum, row) => sum + row.value, 0);
  if (diff) (rows.find((row) => row.key === "fees" && row.value) || rows[0]).value += diff;
  return { rows, total };
}

// ------------------------------------------------------------------ links

function safeUrl(url) {
  if (typeof url !== "string") return null;
  try {
    const u = new URL(url);
    const host = u.hostname.toLowerCase();
    return u.protocol === "https:" && (host === "alcopa-auction.fr" || host.endsWith(".alcopa-auction.fr")) ? u.href : null;
  } catch (err) {
    return null;
  }
}

// "Alcopa" for a listing on an auction house the page knows, else null.
function sourceName(url) {
  const safe = safeUrl(url);
  if (!safe) return null;
  const host = new URL(safe).hostname.toLowerCase();
  const key = Object.keys(HOUSES).find((d) => host === d || host.endsWith(`.${d}`));
  return key ? HOUSES[key] : null;
}

// Where "Voir l'annonce" goes: the lot's own listing, else the sale's catalogue, else nowhere.
// A link is never built from a lot number (lot numbers restart at every sale).
function listingTarget(car, sale) {
  const lot = safeUrl(car && car.url);
  if (lot) return { kind: "lot", url: lot, house: sourceName(lot) };
  const cat = safeUrl(sale && sale.url);
  if (cat) return { kind: "sale", url: cat, house: sourceName(cat) };
  return { kind: "none", url: null, house: null };
}

// ------------------------------------------------------------------ rule output

function engineName(ref, snap) {
  return tOr(`engine.${ref}.name`, ((snap && snap.engines && snap.engines[ref]) || {}).label || ref);
}

function engineShort(ref, snap) {
  return tOr(`engine.${ref}.short`, engineName(ref, snap));
}

function engineCheck(ref, snap) {
  const kb = (snap && snap.engines && snap.engines[ref]) || {};
  return LANG === "fr" ? (kb.check || tOr(`engine.${ref}.check`, null)) : tOr(`engine.${ref}.check`, kb.check || null);
}

function engineBanner(ref, snap) {
  const kb = (snap && snap.engines && snap.engines[ref]) || {};
  return tOr(`engine.${ref}.banner`, kb.label || "");
}

function systemName(key) {
  return tOr(`system.${key}`, String(key || ""));
}

// A reason in the page's language: French keeps the snapshot's own sentence; English is
// written from the code and its parameters (never translated from the French).
function reasonText(r) {
  if (!r) return "";
  if (LANG === "fr" || !r.code) return r.text || "";
  const p = r.params || {};
  const code = r.code;
  if (code === "llm") return r.text || "";
  if (code === "age") return t(isNum(p.years) && p.years > 15 ? "r.age_old" : "r.age", { years: num(p.years) });
  if (code === "mileage") return t("r.mileage", { km: kmText(p.km) });
  if (code === "km_per_year" || code === "low_mileage") return t(`r.${code}`, { km_year: num(p.km_year) });
  if (code === "engine_kb") return t("r.engine_kb", { engine: engineName(p.ref || "", ui.snapshot) });
  if (code === "ct_favorable") return p.n ? tn("r.ct_favorable", p.n) : t("r.ct_favorable_none");
  if (code === "ct_critique" && !isNum(p.n)) return t("gate.ct_critique");  // the gate, without a count
  if (code === "ct_major" || code === "ct_critique") return tn(`r.${code}`, p.n || 0);
  if (code === "ct_young_failed") return t(p.voluntary ? "r.ct_young_failed_vol" : "r.ct_young_failed");
  if (code === "battery_soh" || code === "battery_soh_mismatch") return t(`r.${code}`, { soh: num(p.soh) });
  if (code === "warranty") return p.kind === "named" ? t("r.warranty_named", { label: p.label || "" }) : t("r.warranty");
  if (code === "body_damage") return tn("r.body_damage", p.n || 0);
  if (code === "value") return t(p.below ? "r.value_below" : "r.value_above");
  return tOr(`r.${code}`, r.text || "");
}

// True when reasonText(r) can only give the snapshot's French sentence on this page (marked lang="fr").
function frOnly(r) {
  if (LANG === "fr") return false;
  if (!r || !r.code || r.code === "llm") return true;
  return !["", "_one", "_none", "_old", "_below", "_vol"].some((suffix) => has(DICT, `r.${r.code}${suffix}`));
}

function gateFrOnly(g) {
  return LANG !== "fr" && (!g.code || !has(DICT, `gate.${g.code}`));
}

// A text node, or a span marked lang="fr" when French data shows on the English page.
function textIn(text, french) {
  return french ? h("span", { text, attrs: { lang: "fr" } }) : document.createTextNode(text);
}

// A gate in the page's language (same rule as reasonText).
function gateText(code, params, text, snap) {
  const p = params || {};
  if (!code) return text || "";
  if (code === "engine_risk") return t("gate.engine_risk", { engine: engineShort(p.ref || "", snap) });
  if (LANG === "fr" && code !== "ct_major_safety") return text || tOr(`gate.${code}`, "");
  if (code === "ct_major_safety") return t("gate.ct_major_safety", { system: systemName(p.system).toLowerCase() });
  if (code === "battery_soh") return t("gate.battery_soh", { soh: num(p.soh) });
  if (code === "comment_dealbreaker") return t("gate.comment_dealbreaker", { word: p.word || "" });
  return tOr(`gate.${code}`, text || "");
}

function gatesOf(car) {
  const texts = Array.isArray(car.gates) ? car.gates : [];
  const codes = Array.isArray(car.gate_codes) ? car.gate_codes : [];
  const params = Array.isArray(car.gate_params) ? car.gate_params : [];
  return texts.map((text, i) => ({ text: String(text || ""), code: codes[i] || null, params: params[i] || null }));
}

function reasonsOf(car) {
  return car.detail && Array.isArray(car.detail.reasons) ? car.detail.reasons.filter((r) => r && r.text) : null;
}

function findReason(car, code) {
  return (reasonsOf(car) || car.reasons || []).find((r) => r && r.code === code) || null;
}

// A gap in this lot's own data (never the sale-wide "price not compared yet"), or null.
function dataGap(car) {
  if (!car) return null;
  const d = car.detail;
  if (!isNum(car.effective_price)) return t("gap.price");
  if (d && d.ct_doc === "unreadable") return t("gap.ct_unreadable");
  if (d && d.ct && d.ct.doubtful) return t("gap.ct_doubtful");
  if (!isNum(car.mileage)) return t("gap.mileage");
  return null;
}

// The one non-CT signal a detailed row shows: {tone: risk|watch|gap, text} or null.
function rowSignal(car, snap) {
  const gates = gatesOf(car);
  for (const code of SIGNAL_GATES) {
    const g = gates.find((x) => x.code === code);
    if (g) return { tone: "risk", text: gateText(g.code, g.params, g.text, snap) };
  }
  // Older snapshots: gates by text only (a CT defect is already in the CT column).
  const old = gates.find((g) => !g.code && !/^(Défaillance|CT )/.test(g.text));
  if (old) return { tone: "risk", text: old.text };
  const d = car.detail || {};
  if (d.ct && d.ct.odometer_inconsistent) return { tone: "risk", text: t("sig.odometer") };
  const b = d.battery;
  if (b && b.leased) return { tone: "watch", text: t("sig.leased") };
  if (b && isNum(b.soh)) return { tone: "watch", text: t(`sig.soh_${b.source === "listing" ? "listing" : "doc"}`, { soh: num(b.soh) }) };
  const km = findReason(car, "km_per_year");
  if (km && km.params && isNum(km.params.km_year)) return { tone: "watch", text: t("sig.km_year", { km_year: num(km.params.km_year) }) };
  const age = findReason(car, "age");
  if (age && age.params && isNum(age.params.years)) return { tone: "watch", text: t("sig.age", { years: num(age.params.years) }) };
  const p = d.price;
  if (vatApplies(p)) return { tone: "watch", text: t("sig.vat", { amount: eur(p.vat) }) };
  const gap = dataGap(car);
  // A missing price is already said in the cost column: no second time on the same row.
  return gap && isNum(car.effective_price) ? { tone: "gap", text: gap } : null;
}

// Codes that only ever come from a gate (its "why" has no point count of its own).
const GATE_ONLY = new Set(["engine_risk", "ct_major_safety", "ct_non_lu", "odometer", "comment_dealbreaker",
  "mileage_not_guaranteed"]);  // battery_soh is also a light penalty: worded as a reason

// The main reason a compact lot sits where it is: {text, fr} (fr: the snapshot's French on
// the English page, marked lang="fr").
function whyOf(car, snap) {
  const p = car.why_params || {};
  const code = car.why_code;
  if (code === "engine_kb" || code === "engine_risk") return { text: t("gate.engine_risk", { engine: engineShort(p.ref || car.engine || "", snap) }), fr: false };
  if (code === "km_per_year") return { text: t("sig.km_year", { km_year: num(p.km_year) }), fr: false };
  if (!code || LANG === "fr") return { text: car.why || "", fr: LANG !== "fr" };
  // The critical-CT gate and reason share a code; only the reason carries a count.
  if (GATE_ONLY.has(code) || (code === "ct_critique" && !isNum(p.n))) {
    return { text: gateText(code, p, car.why, snap), fr: !has(DICT, `gate.${code}`) };
  }
  const r = { code, params: p, text: car.why };
  return { text: reasonText(r) || car.why || "", fr: frOnly(r) };
}

function whyText(car, snap) {
  return whyOf(car, snap).text;
}

// The tier's word on a row and a sheet.
function tierWord(tier) {
  return tOr(`tier.${tier}`, "");
}

// The "En bref" lines: {lines: [{tone, text, src}], verify: [text], ai: {kind, text}, adjust}
function briefOf(car, snap) {
  const s = snap || {};
  const d = car.detail || null;
  const ct = d && d.ct;
  const state = ctState(car);
  const failed = state === "major" || state === "crit";
  const lines = [];
  const seen = new Set();
  const norm = (x) => String(x).replace(/\s+/g, " ").trim();
  const add = (tone, text, src, fr) => {
    // Older snapshots say a gate and its reason in two sentences, one inside the other; the
    // dictionary writes no-break spaces where the snapshot has plain ones.
    const n = text ? norm(text) : "";
    if (!n || seen.has(n) || lines.some((l) => norm(l.text).includes(n) || n.includes(norm(l.text)))) return;
    seen.add(n);
    lines.push({ tone, text, src: src || null, fr: Boolean(fr) });
  };
  // Risks: the CT, the gates, then the rules' negative points.
  if (failed) add("risk", ctLineText(car), "ct");
  gatesOf(car).forEach((g) => {
    if (g.code === "ct_critique" || g.code === "ct_non_lu") return;  // already in the CT line
    if (g.code === "engine_risk") add("risk", t("brief.engine", { engine: engineName((g.params || {}).ref || car.engine || "", s) }), "kb");
    else add("risk", gateText(g.code, g.params, g.text, s), GATE_SRC[g.code] || null, gateFrOnly(g));
  });
  if (ct && ct.odometer_inconsistent) add("risk", t("sig.odometer"), "ct");
  const b = d && d.battery;
  if (b && b.leased) add("risk", t("sig.leased"), "listing");
  // A lot listed on one line (no sheet published): its engine and the main reason for its score.
  if (!d && car.engine && !gatesOf(car).some((g) => g.code === "engine_risk")) {
    add("risk", t("brief.engine", { engine: engineName(car.engine, s) }), "kb");
  }
  const engineWhy = car.why_code === "engine_kb" || car.why_code === "engine_risk";
  if (!d && !(car.reasons || []).length && (car.why || car.why_code) && !(engineWhy && car.engine)) {
    const why = whyOf(car, s);
    add(car.tier === "red" ? "risk" : "note", why.text, null, why.fr);
  }
  const reasons = reasonsOf(car);
  const skip = new Set(["engine_kb", "ct_major", "ct_critique", "ct_young_failed", "llm"]);
  if (reasons) {
    reasons.filter((r) => isNum(r.pts) && r.pts < 0 && r.pillar !== "IA" && !skip.has(r.code))
      .sort((a, b2) => a.pts - b2.pts).slice(0, 2).forEach((r) => add("risk", reasonText(r), SRC_KEYS[r.src] || null, frOnly(r)));
    reasons.filter((r) => isNum(r.pts) && r.pts > 0 && r.pillar !== "IA" && !(failed && r.code === "ct_not_required"))
      .sort((a, b2) => b2.pts - a.pts).slice(0, 3).forEach((r) => add("good", reasonText(r), SRC_KEYS[r.src] || null, frOnly(r)));
  } else {
    // Schema 1: reasons without points cannot be sorted into good and bad; they are listed.
    (car.reasons || []).filter((r) => r && r.text).slice(0, 3)
      .forEach((r) => add("note", reasonText(r), SRC_KEYS[r.src] || null, frOnly(r)));
  }
  // What to check: the engine's own action first, then the rules.
  const verify = [];
  if (car.engine && engineCheck(car.engine, s)) verify.push(engineCheck(car.engine, s));
  if (ct && ct.doubtful) verify.push(t("verify.ct_doubtful"));
  else if (d && d.ct_doc === "unreadable") verify.push(t("verify.ct_unreadable"));
  else if (d && !ct && d.ct_doc === "missing" && !findReason(car, "ct_not_required")) verify.push(t("verify.ct_missing"));
  else if (ct && ct.result === "UNKNOWN") verify.push(t("verify.ct_unknown"));
  const price = d && d.price;
  if (failed && price && isNum(price.repairs) && price.repairs > 0) verify.push(t("verify.repairs", { amount: eur(price.repairs) }));
  if (!isNum(car.mileage)) verify.push(t("verify.mileage"));
  const electric = ["electric", "plugin"].includes(fuelKind(car.fuel));
  if (electric && !(b && isNum(b.soh))) verify.push(t("verify.battery"));
  const w = findReason(car, "warranty");
  if (w && ((w.params && w.params.kind === "unspecified") || /formule non précisée/.test(w.text || ""))) verify.push(t("verify.warranty"));
  if (d && d.damages === null) verify.push(t("verify.photos"));
  const llm = s.llm || {};
  let ai;
  if (car.verdict) ai = { kind: "verdict", text: car.verdict };
  else ai = { kind: !llm.enabled ? "off" : llm.error ? "error" : "missing" };
  return { lines: lines.slice(0, 6), verify, ai, adjust: d && d.score && isNum(d.score.llm) ? d.score.llm : 0 };
}

// The CT as one sentence (the row's line), in the page's language.
function ctLineText(car) {
  if (LANG === "fr" && car.ct_line) return car.ct_line;
  // Without the snapshot's sentence (English, a lot on one line): the same words as a reason.
  const n = countsOf(car);
  const state = ctState(car);
  if (n && state === "fav") return n.minor ? tn("r.ct_favorable", n.minor) : t("r.ct_favorable_none");
  if (n && state === "major" && n.major) return tn("r.ct_major", n.major);
  if (n && state === "crit" && n.critical) return tn("r.ct_critique", n.critical);
  const sum = ctSummary(car);
  return [sum.word, sum.detail].filter(Boolean).join(", ");
}

// A defect's label from the procès-verbal, sentence-cased (the PV writes headings in capitals),
// with its location in brackets kept apart: {text, where}.
function defectLabel(label) {
  const raw = String(label || "").trim();
  const m = /^(.*?)\s*(\(([A-Z]{1,4}(?:,\s*[A-Z]{1,4})*)\))\s*$/.exec(raw);
  const body = m ? m[1] : raw;
  const where = m ? m[3] : null;
  const parts = body.split(/\s:\s/);
  const fix = (s, first) => {
    const lower = s === s.toUpperCase() ? s.toLowerCase() : s.charAt(0).toLowerCase() + s.slice(1);
    return first ? capitalise(lower) : lower;
  };
  return { text: parts.map((p, i) => fix(p, i === 0)).join(" : "), where };
}

// The defects of each CT function (0 to 8), for the sheet's labelled grid.
function functionCounts(ct) {
  const out = GRID_FUNCTIONS.map(() => ({ minor: 0, major: 0, critical: 0 }));
  (ct && Array.isArray(ct.defects) ? ct.defects : []).forEach((d) => {
    const m = /^(\d{1,2})\./.exec(String((d && d.code) || ""));
    if (m && Number(m[1]) <= 8 && d.severity in out[0]) out[Number(m[1])][d.severity] += 1;
  });
  return out;
}

function errorFor(status, httpStatus) {
  if (httpStatus === 401 || httpStatus === 403) return { title: t("err.session.title"), text: t("err.session.text"), retry: false, reload: true };
  const known = ["no_snapshot", "not_configured", "storage_unavailable", "storage_error", "snapshot_too_large",
    "snapshot_unreadable", "session", "network"].includes(status);
  if (!known) return { title: t("err.other.title", { code: httpStatus || status || "?" }), text: t("err.other.text"), retry: true, reload: false };
  const retry = ["storage_unavailable", "storage_error", "network"].includes(status);
  return { title: t(`err.${status}.title`), text: t(`err.${status}.text`), retry, reload: status === "session" };
}

// ------------------------------------------------------------------ storage (per viewer)

// The only place the page touches web storage; every access can throw (private mode).
function store(kind, key, value) {
  try {
    const area = kind === "session" ? window.sessionStorage : window.localStorage;
    if (value === undefined) return area.getItem(key);
    if (value === null) area.removeItem(key);
    else area.setItem(key, value);
  } catch (err) {
    return null;
  }
  return null;
}

// ------------------------------------------------------------------ DOM helpers

const SVG_NS = "http://www.w3.org/2000/svg";
const ATTRS = new Set(["id", "class", "href", "rel", "target", "type", "role", "hidden", "tabindex", "for", "scope",
  "colspan", "lang", "hreflang", "inputmode", "min", "step", "value", "placeholder", "autocomplete", "readonly",
  "name", "title", "datetime"]);

function $(id) {
  return document.getElementById(id);
}

function h(tag, opts, children) {
  const node = document.createElement(tag);
  const o = opts || {};
  if (o.cls) node.className = o.cls;
  if (o.text !== undefined && o.text !== null) node.textContent = String(o.text);
  Object.entries(o.attrs || {}).forEach(([k, v]) => {
    if (v === null || v === undefined || v === false) return;
    if (ATTRS.has(k) || k.startsWith("aria-") || k.startsWith("data-")) node.setAttribute(k, v === true ? "" : String(v));
  });
  Object.entries(o.on || {}).forEach(([ev, fn]) => node.addEventListener(ev, fn));
  (children || []).forEach((c) => { if (c !== null && c !== undefined && c !== false) node.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
  return node;
}

const SVG_ATTRS = new Set(["viewBox", "d", "cx", "cy", "r", "x", "y", "x1", "y1", "x2", "y2", "width", "height", "rx",
  "points", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "class",
  "aria-hidden", "focusable"]);

function svg(tag, attrs, children) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs || {}).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== false && SVG_ATTRS.has(k)) node.setAttribute(k, String(v));
  });
  (children || []).forEach((c) => node.appendChild(c));
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

function setVar(node, name, value) {
  node.style.setProperty(name, String(value));  // CSSOM: allowed by the CSP, unlike style=""
}

const ICONS = {
  ext: "M7 17 17 7M9 7h8v8",
  check: "m5 12 5 5 9-10",
  warn: "M12 3 2 21h20zM12 10v5M12 18v.5",
  q: "M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6v.6M12 17.5v.5",
  x: "M6 6l12 12M18 6 6 18",
  back: "M19 12H5M11 6l-6 6 6 6",
  link: "M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1",
  diamond: "M12 5 19 12 12 19 5 12z",
  arrow: "M5 12h14M13 6l6 6-6 6",
};

function icon(name, cls) {
  const kids = [svg("path", { d: ICONS[name] })];
  if (name === "q") kids.unshift(svg("circle", { cx: 12, cy: 12, r: 9 }));
  return svg("svg", { viewBox: "0 0 24 24", width: 18, height: 18, class: `ico ico-${name}${cls ? ` ${cls}` : ""}`,
    "aria-hidden": "true", focusable: "false" }, kids);
}

function sr(text) {
  return h("span", { cls: "sr", text });
}

// A link to the auction site: new tab, no referrer, no opener, never followed by crawlers.
function extLink(url, text, cls) {
  const safe = safeUrl(url);
  if (!safe) return null;
  return h("a", { cls, attrs: { href: safe, target: "_blank", rel: "noopener noreferrer nofollow" } },
    [h("span", { text }), icon("ext"), sr(` ${t("a11y.new_tab")}`)]);
}

// The lot number on its tag; in a row, read as « Lot 751 » (the column header is aria-hidden).
function tagNode(car, size, named) {
  return h("span", { cls: `tag tag-${size || "md"}` }, [named ? sr(t("a11y.lot")) : null,
    car.lot === null || car.lot === undefined ? "–" : String(car.lot)]);
}

// The score ring: a track and the score's arc, the number inside (aria-hidden: the row says it).
function ringNode(car, size, scoring) {
  const g = ringGeom(car.score, size);
  const band = ringBand(car.score, scoring);
  const box = h("span", { cls: `ring ring-${size} band-${band}`, attrs: { "aria-hidden": "true" } });
  box.appendChild(svg("svg", { viewBox: `0 0 ${size} ${size}`, width: size, height: size, focusable: "false" }, [
    svg("circle", { class: "ring-track", cx: g.c, cy: g.c, r: g.r, fill: "none", "stroke-width": g.sw }),
    svg("circle", { class: "ring-value", cx: g.c, cy: g.c, r: g.r, fill: "none", "stroke-width": g.sw, "stroke-linecap": "round",
      "stroke-dasharray": `${g.len.toFixed(2)} ${g.circ.toFixed(2)}` }),
  ]));
  box.appendChild(h("span", { cls: "ring-n fig", text: isNum(car.score) ? String(car.score) : "–" }));
  return box;
}

function scoreA11y(car) {
  if (!isNum(car.score)) return "";
  return t("a11y.score", { score: car.score, tier: tOr(`tier_long.${car.tier}`, tierWord(car.tier)) });
}

// The CT in a 3×3 grid of nine cells (aria-hidden: the text beside it says the same).
function gridNode(car) {
  const grid = gridOf(car);
  if (!grid) return null;
  const box = h("span", { cls: "ctg", attrs: { "aria-hidden": "true" } });
  grid.split("").forEach((c) => box.appendChild(h("i", { cls: `cell cell-${{ ".": "ok", n: "n", M: "M", C: "C", u: "u" }[c]}` })));
  return box;
}

function ctCellNode(car) {
  const sum = ctSummary(car);
  const grid = gridNode(car);
  const text = h("span", { cls: "ct-text" }, [
    h("span", { cls: `ct-word ct-${sum.state}`, text: sum.word }),
    sum.detail ? h("span", { cls: "ct-detail", text: sum.detail }) : null,
  ]);
  if (grid) return h("span", { cls: "r-ct" }, [grid, text]);
  // No grid (older snapshots): a chip, a filled square in the state's colour.
  return h("span", { cls: "r-ct" }, [h("i", { cls: `ct-chip cell-${{ fav: "ok", major: "M", crit: "C", none: "u" }[sum.state]}`, attrs: { "aria-hidden": "true" } }), text]);
}

function signalNode(sig) {
  if (!sig) return null;
  const name = sig.tone === "risk" ? "warn" : sig.tone === "watch" ? "diamond" : "q";
  return h("span", { cls: `sig sig-${sig.tone}` }, [icon(name), h("span", { text: sig.text })]);
}

// ------------------------------------------------------------------ page state

const ui = {
  snapshot: null,
  model: null,
  sale: null,
  rows: new Map(),
  openRef: null,
  hiddenOpen: false,
  desktop: true,
  phone: false,
  expanded: { top: false, cards: false, rest: false },
  absentLot: null,
  pushed: false,
  scrollY: 0,
  carry: null,
  section: "sec-top",
  spy: null,
  autoOpened: false,
};

function readFilters() {
  const v = (id) => ($(id) ? $(id).value : "");
  const n = (id) => { const x = Number(v(id)); return v(id) && isFinite(x) && x > 0 ? x : null; };
  const q = v("f-q").trim();
  return { q, budget: n("f-budget"), fuel: v("f-fuel"), gearbox: v("f-gearbox"), km: n("f-km"),
    year: n("f-year"), ct: v("f-ct"), engine: v("f-engine"), sort: v("f-sort") || "score",
    exact: Boolean(ui.absentLot) && q === ui.absentLot };
}

const FILTER_IDS = ["f-budget", "f-fuel", "f-gearbox", "f-km", "f-year", "f-ct", "f-engine"];

function activeCount() {
  return FILTER_IDS.filter((id) => $(id) && $(id).value).length;
}

function filtersActive(f) {
  return Boolean(f.q || activeCount());
}

function autoGearbox() {
  return (ui.model ? ui.model.gearboxes : []).find((g) => fold(g).startsWith("auto")) || null;
}

// The hero's shortcuts are views onto the catalogue's filters.
const TOGGLES = {
  fav: { id: "f-ct", value: () => "fav" },
  noengine: { id: "f-engine", value: () => "safe" },
  auto: { id: "f-gearbox", value: autoGearbox },
  km100: { id: "f-km", value: () => "100000" },
  elec: { id: "f-fuel", value: () => "elec" },
};

// ------------------------------------------------------------------ rendering: band

function renderLangLinks() {
  // A lot the reader opened follows the switch; the one the pane shows by default does not.
  const hash = ui.openRef && !ui.autoOpened ? `#lot-${ui.openRef}` : "";
  if ($("lang-fr")) $("lang-fr").setAttribute("href", `/${hash}`);
  if ($("lang-en")) $("lang-en").setAttribute("href", `/en/${hash}`);
}

function renderSaleCard(s) {
  const sale = ui.sale;
  const total = totalLots(s) || (ui.model ? ui.model.all.length : 0);
  const state = saleStateNow(sale);
  const label = $("sale").querySelector(".sc-head .lbl");
  label.textContent = t(state === "over" ? "sale.last" : "sale.next");
  label.hidden = !sale;  // no sale: the title alone says so
  $("sale-title").textContent = saleTitle(sale);
  $("sale-line").textContent = saleLine(sale, ui.phone ? total : null);
  const cd = countdownText(sale);
  $("countdown").textContent = capitalise(cd);
  $("countdown").hidden = !cd;
  const w = ui.model.waffle;
  const has = Boolean(sale) && w.total > 0;
  // One square per lot: only when the snapshot lists every lot (not schema 1, nor a cut list).
  const whole = has && w.total >= total;
  $("sale-figs").hidden = !has;
  ["fig-read", "fig-fav"].forEach((id) => { $(id).parentNode.hidden = !whole; });
  ["waffle-cap", "legend", "legend-note"].forEach((id) => { $(id).hidden = !whole; });
  $("sale-all").hidden = !has;
  $("waffle").hidden = !whole || ui.phone;
  $("stack").hidden = !whole || !ui.phone;
  if (has) {
    $("fig-lots").textContent = num(total);
    $("fig-read").textContent = num(w.total - w.counts.none);
    $("fig-fav").textContent = num(w.counts.fav);
    renderWaffle(w);
    renderLegend(w);
    // « Voir les N lots »: the lots this page lists (fewer than the sale's on an older snapshot).
    clear($("sale-all")).appendChild(h("span", { text: tn("sale.see_all", whole ? total : ui.model.all.length) }));
    $("sale-all").appendChild(icon("arrow"));
  }
  renderCardStatus(s);
  // Other covered sales: a compact list under the featured one.
  const others = salesOf(s).filter((x) => x !== sale);
  const list = clear($("sale-others"));
  others.forEach((x) => list.appendChild(h("li", { text: [houseOf(x), roomOf(x), capitalise(dayText(x.date) || ""),
    isNum(x.lots) ? tn("sale.lots_n", x.lots) : null].filter(Boolean).join(" · ") })));
  list.hidden = !others.length;
}

function renderWaffle(w) {
  const box = clear($("waffle"));
  box.setAttribute("aria-label", t("waffle.label", { total: num(w.total), fav: num(w.counts.fav),
    major: num(w.counts.major), crit: num(w.counts.crit), none: num(w.counts.none) }));
  const frag = document.createDocumentFragment();
  w.cells.forEach((state) => frag.appendChild(h("i", { cls: `w w-${state}` })));
  box.appendChild(frag);
  const stack = clear($("stack"));
  CT_STATES.forEach((state) => {
    if (!w.counts[state]) return;
    const seg = h("i", { cls: `st st-${state}` });
    setVar(seg, "--w", w.counts[state]);
    stack.appendChild(seg);
  });
}

// The legend's buttons are built once per snapshot; a filter change only updates them in place,
// so the focused button stays where it is and its new state is announced.
function renderLegend(w) {
  const box = clear($("legend"));
  CT_STATES.forEach((state) => {
    box.appendChild(h("button", { cls: "leg", attrs: { type: "button", "aria-pressed": "false", "data-ct": state },
    on: { click: () => setCtFilter(readFilters().ct === state ? "" : state) } }, [
      h("i", { cls: `sw sw-${state}`, attrs: { "aria-hidden": "true" } }),
      h("span", { cls: "fig leg-n", text: num(w.counts[state]) }),
      h("span", { cls: "leg-t t-wide", text: t(`legend.${state}`) }),
      h("span", { cls: "leg-t t-narrow", text: t(`legend.${state}_short`) }),
    ]));
  });
  updateLegend();
}

function updateLegend() {
  const current = readFilters().ct;
  $("legend").querySelectorAll(".leg").forEach((btn) => {
    btn.setAttribute("aria-pressed", btn.getAttribute("data-ct") === current ? "true" : "false");
  });
  const only = CT_STATES.includes(current) ? ` only-${current}` : "";
  $("waffle").className = `waffle${only}`;
  $("stack").className = `stack${only}`;
}

function renderCardStatus(s) {
  const box = clear($("sc-status"));
  const pub = publishedText(s.generated_at);
  if (pub) {
    box.appendChild(h("span", { cls: "st-line" }, [pub, h("span", { cls: "st-more" }, [" · ",
      h("a", { text: t("status.details"), attrs: { href: "#status" }, on: { click: openStatus } })])]));
  }
  const st = statusParts(s);
  if (st.prov || st.ai) {
    const parts = [];
    if (st.prov) parts.push(h("span", { cls: "st-prov" }, [st.prov.short, h("span", { cls: "st-reason", text: ` : ${st.prov.reason}` })]));
    if (st.ai) parts.push(h("span", { text: st.ai }));
    const line = h("span", { cls: "st-line st-flag" }, [h("i", { cls: "st-mark", attrs: { "aria-hidden": "true" } })]);
    parts.forEach((p, i) => { if (i) line.appendChild(document.createTextNode(" · ")); line.appendChild(p); });
    box.appendChild(line);
  }
}

function openStatus(event) {
  event.preventDefault();
  $("status").open = true;
  $("status").scrollIntoView({ block: "start", behavior: "auto" });
  $("status").querySelector("summary").focus({ preventScroll: true });
}

function renderCoverage(s) {
  const list = clear($("cov-list"));
  const seen = new Set();
  salesOf(s).forEach((x) => {
    const label = [houseOf(x), roomOf(x)].filter(Boolean).join(" · ");
    if (!label || seen.has(label)) return;
    seen.add(label);
    list.appendChild(h("span", { cls: "cov-item" }, [h("i", { cls: "cov-dot", attrs: { "aria-hidden": "true" } }), label]));
  });
  $("coverage").hidden = !seen.size;
}

function renderToggles() {
  const w = ui.model.waffle;
  $("t-fav-n").textContent = num(w.counts.fav);
  document.querySelectorAll("[data-toggle]").forEach((btn) => {
    const def = TOGGLES[btn.getAttribute("data-toggle")];
    const value = def && def.value();
    btn.hidden = !value;
    btn.setAttribute("aria-pressed", value && $(def.id).value === String(value) ? "true" : "false");
  });
  $("toggles").hidden = !ui.model.all.length;
  updateLegend();
}

function renderBanners(s) {
  const box = clear($("banners"));
  const add = (text) => box.appendChild(h("p", { cls: "banner", attrs: { role: "note" } }, [icon("warn"), h("span", { text })]));
  const age = ageHours(s.generated_at);
  if (isFinite(age) && age > STALE_DAYS * 24) add(t("banner.stale", { days: num(Math.round(age / 24)) }));
  const crawl = s.crawl || {};
  if (["blocked", "failed", "incomplete"].includes(crawl.status)) add(t(`banner.crawl_${crawl.status}`));
  if (s.counts && s.counts.compact_dropped) add(tn("banner.dropped", s.counts.compact_dropped));
}

function renderStatus(s) {
  const dl = clear($("status-list"));
  const row = (k, v) => { if (v !== null && v !== undefined && v !== "") { dl.appendChild(h("dt", { text: k })); dl.appendChild(h("dd", { text: v })); } };
  row(t("status.k_published"), publishedText(s.generated_at));
  const crawl = s.crawl || {};
  row(t("status.k_crawl"), crawl.status ? tOr(`crawl.${crawl.status}`, crawl.status) : null);
  const l = s.llm || {};
  row(t("status.k_ai"), l.enabled ? t("status.ai_on", { model: l.model || "?", calls: num(l.calls || 0), cached: num(l.cached || 0) }) : t("status.ai_off_long"));
  const prov = provisionalParts(s);
  const why = (s.counts && s.counts.partial_why) || {};
  const gaps = Object.keys(why).filter((k) => why[k] > 0).map((k) => tn(`partial.${k}`, why[k]));
  row(t("status.k_data"), [prov ? `${prov.short} : ${prov.reason}.` : null, gaps.join(" ; ")].filter(Boolean).join(" "));
  row(t("status.k_lots"), tn("sale.lots_n", totalLots(s)));
}

// ------------------------------------------------------------------ rendering: bar and filters

// The sale the catalogue shows. A selector needs each lot to say which sale it belongs to, which
// the snapshot does not yet do (one sale is covered): until then the label stays plain text, and
// the other sales are listed on the sale card. Never a one-option dropdown, never a dead one.
function renderBar(s) {
  const sales = salesOf(s);
  const lotsKnowTheirSale = ui.model.all.some((c) => c.sale !== undefined);
  $("bar-sale").hidden = !ui.sale;
  $("bar-sale-text").textContent = saleShort(ui.sale);
  const pick = $("bar-sale").querySelector(".cb-sale-pick");
  const choose = sales.length >= 2 && lotsKnowTheirSale;
  pick.hidden = !choose;
  $("bar-sale-text").hidden = choose;
  if (choose) {
    const sel = clear($("bar-sale-select"));
    sales.forEach((x, i) => sel.appendChild(h("option", { text: saleShort(x), attrs: { value: String(i) } })));
    sel.value = String(sales.indexOf(ui.sale));
  }
}

// The section index, with the counts the filters leave; a section they empty is not a link
// (following it would silently clear the reader's filters).
function renderIndex(counts) {
  const list = clear($("index-list"));
  [["top", "sec-top"], ["cards", "sec-cards"], ["rest", "sec-rest"]].forEach(([key, target]) => {
    const c = counts[key];
    if (!c || !c.all) return;
    const n = c.active ? c.shown : c.all;
    const label = [h("span", { text: t(`index.${key}`) }), " ", h("span", { cls: "fig", text: num(n) })];
    if (!n) {
      list.appendChild(h("li", {}, [h("span", { cls: "ix ix-off" }, label)]));
      return;
    }
    list.appendChild(h("li", {}, [h("a", { cls: "ix", attrs: { href: `#${target}`, "data-key": key,
      "aria-current": ui.section === target ? "true" : null },
    on: { click: (e) => { e.preventDefault(); jumpTo(key); } } }, label)]));
  });
  $("index").hidden = !list.childNodes.length;
  watchSections();
}

// The index marks the section being read (the first one under the bar).
function watchSections() {
  if (typeof IntersectionObserver !== "function" || ui.spy) return;
  const visible = new Map();
  ui.spy = new IntersectionObserver((entries) => {
    entries.forEach((e) => visible.set(e.target.id, e.isIntersecting));
    const current = ["sec-top", "sec-cards", "sec-rest"].find((id) => visible.get(id) && !$(id).hidden);
    if (!current || current === ui.section) return;
    ui.section = current;
    document.querySelectorAll(".ix").forEach((a) => {
      if (a.getAttribute("href") === `#${current}`) a.setAttribute("aria-current", "true");
      else a.removeAttribute("aria-current");
    });
  }, { rootMargin: "-160px 0px -50% 0px" });
  ["sec-top", "sec-cards", "sec-rest"].forEach((id) => ui.spy.observe($(id)));
}

function renderFilterOptions(model) {
  const fill = (id, values, label) => {
    const sel = $(id);
    const keep = sel.value;
    Array.from(sel.querySelectorAll("option[data-gen]")).forEach((o) => o.remove());
    values.forEach((v) => sel.appendChild(h("option", { text: label(v), attrs: { value: String(v), "data-gen": "1" } })));
    if (Array.from(sel.options).some((o) => o.value === keep)) sel.value = keep;
  };
  fill("f-budget", BUDGETS, (v) => t("filters.budget_max", { amount: eur(v) }));
  fill("f-fuel", model.fuels, fuelText);
  fill("f-gearbox", model.gearboxes, gearboxText);
  fill("f-km", KM_STEPS, (v) => t("filters.km_max", { km: kmText(v) }));
  fill("f-year", model.years.filter((y, i, a) => i === 0 || y % 2 === 0 || i === a.length - 1).slice(0, 12),
    (v) => t("filters.year_min", { year: v }));
}

// A budget typed in the hero becomes an option of the bar's budget list.
function setBudget(value) {
  const sel = $("f-budget");
  if (!value) { sel.value = ""; return; }
  if (!Array.from(sel.options).some((o) => o.value === String(value))) {
    sel.appendChild(h("option", { text: t("filters.budget_max", { amount: eur(value) }), attrs: { value: String(value), "data-gen": "1" } }));
  }
  sel.value = String(value);
}

function setCtFilter(state) {
  $("f-ct").value = state || "";
  onFilterInput();
}

function updateToggleCount() {
  const n = activeCount();
  $("f-n").textContent = n ? num(n) : "";
  $("f-reset").hidden = !filtersActive(readFilters());
}

function setMore(open) {
  $("f-more").classList.toggle("is-open", open);
  $("f-toggle").setAttribute("aria-expanded", open ? "true" : "false");
  measureBar();
}

function measureBar() {
  const bar = $("catbar");
  if (bar) setVar(document.documentElement, "--bar-h", `${Math.ceil(bar.getBoundingClientRect().height)}px`);
}

// ------------------------------------------------------------------ rendering: catalogue

const LIMITS = { top: { phone: 4, wide: Infinity }, cards: { phone: 4, wide: 4 }, rest: { phone: 4, wide: 4 } };

function rowFor(car) {
  let li = ui.rows.get(car.ref);
  if (!li) {
    li = car.section === "rest" ? compactRow(car) : lotRow(car);
    ui.rows.set(car.ref, li);
  }
  return li;
}

function costNode(car, compact) {
  return h("span", { cls: "r-cost" }, [
    h("span", { cls: "fig cost", text: eur(car.effective_price) || "–" }),
    compact ? null : h("span", { cls: "cost-basis", text: isNum(car.effective_price) ? basisLine(car) : t("gap.price") }),
  ]);
}

function scoreNode(car, size) {
  const snap = ui.snapshot || {};
  return h("span", { cls: "r-sc" }, [
    ringNode(car, size, snap.scoring),
    h("span", { cls: `tier tier-${car.tier}`, text: tierWord(car.tier), attrs: { "aria-hidden": "true" } }),
  ]);
}

function lotRow(car) {
  const sig = rowSignal(car, ui.snapshot);
  const btn = h("button", { cls: "row", attrs: { type: "button", "aria-controls": "sheet", "data-ref": car.ref },
    on: { click: () => toggleLot(car.ref) } }, [
    tagNode(car, "md", true),
    h("span", { cls: "r-car" }, [
      h("span", { cls: "r-title", text: titleOf(car) }),
      subtitleOf(car) ? h("span", { cls: "r-sub", text: subtitleOf(car) }) : null,
      h("span", { cls: "r-specs", text: specsLine(car) }),
      signalNode(sig),
    ]),
    ctCellNode(car),
    costNode(car, false),
    scoreNode(car, 48),
    sr(`, ${scoreA11y(car)}`),
  ]);
  return h("li", { cls: "lot", attrs: { "data-ref": car.ref } }, [btn]);
}

function compactRow(car) {
  const whyInfo = whyOf(car, ui.snapshot);
  const why = whyInfo.text;
  const line = [titleOf(car), car.year, kmText(car.mileage)].filter((x) => x !== null && x !== undefined && x !== "").join(" · ");
  const btn = h("button", { cls: "row crow", attrs: { type: "button", "aria-controls": "sheet", "data-ref": car.ref },
    on: { click: () => toggleLot(car.ref) } }, [
    tagNode(car, "sm", true),
    h("span", { cls: "r-car" }, [h("span", { cls: "r-title", text: line }),
      why ? h("span", { cls: "r-why", text: why, attrs: { lang: whyInfo.fr && why ? "fr" : null } }) : null]),
    ctCellNode(car),
    costNode(car, true),
    scoreNode(car, 36),
    sr(`, ${scoreA11y(car)}`),
  ]);
  return h("li", { cls: "lot lot-compact", attrs: { "data-ref": car.ref } }, [btn]);
}

function sectionCars(key) {
  return ui.model.all.filter((c) => c.section === key);
}

function renderSections() {
  if (!ui.model) return;
  const f = readFilters();
  const active = filtersActive(f);
  const all = { top: sectionCars("top"), cards: sectionCars("cards"), rest: sectionCars("rest") };
  let shownTotal = 0;
  let noPrice = 0;
  const counts = {};
  const shownRefs = new Set();
  ["top", "cards", "rest"].forEach((key) => {
    const res = filterCars(all[key], f);
    noPrice += res.hiddenNoPrice;
    const order = key === "rest" && f.sort === "score" ? "lot" : f.sort;
    const list = sortCars(res.shown, order);
    list.forEach((c) => shownRefs.add(c.ref));
    counts[key] = { all: all[key].length, shown: list.length, active };
    shownTotal += list.length;
    const sec = $(`sec-${key}`);
    sec.hidden = !list.length;
    $(`n-${key}`).textContent = active ? `${num(list.length)} / ${num(all[key].length)}` : num(all[key].length);
    const limit = ui.expanded[key] ? Infinity : LIMITS[key][ui.phone ? "phone" : "wide"];
    const ul = clear($(`${key}-list`));
    list.slice(0, limit).forEach((car) => ul.appendChild(rowFor(car)));
    const more = $(`more-${key}`);
    const rest = list.length - Math.min(limit, list.length);
    more.hidden = !(rest > 0 || (ui.expanded[key] && list.length > LIMITS[key][ui.phone ? "phone" : "wide"]));
    more.textContent = ui.expanded[key] ? t("more.less") : key === "rest" ? tn("more.rest", list.length) : tn(`more.${key}`, rest);
    more.setAttribute("aria-expanded", ui.expanded[key] ? "true" : "false");
  });
  renderIndex(counts);
  $("note-top").textContent = t(ui.snapshot.provisional ? "sec.top_note_prov" : "sec.top_note");
  $("note-rest").textContent = t("sec.rest_note", { house: houseOf(ui.sale) || t("sale.house_generic") });
  // Empty result: say why, and offer the way out.
  const none = shownTotal === 0;
  $("no-match").hidden = !none || !ui.model.all.length;
  if (none) renderNoMatch(f, noPrice);
  // The open lot is kept, and said to be hidden only when the filters leave it out
  // (not when its list is collapsed).
  if (ui.openRef) setMasked(active && !shownRefs.has(ui.openRef));
  markOpenRow();
  announceCount(f, shownTotal, noPrice);
}

function renderNoMatch(f, noPrice) {
  const text = $("no-match-text");
  const ctBtn = $("no-match-ct");
  ctBtn.hidden = true;
  if (ui.absentLot && f.q === ui.absentLot) {
    text.textContent = t("empty.absent", { lot: ui.absentLot, day: dayText(ui.sale && ui.sale.date) || "" });
    return;
  }
  if (f.ct) {
    const without = filterCars(ui.model.all, Object.assign({}, f, { ct: "" })).shown;
    if (without.length) {
      const n = without.length;
      text.textContent = t("empty.ct_hint", { what: t(`legend.${f.ct}`).toLowerCase(), others: tn("empty.lots", n) });
      ctBtn.textContent = t("empty.remove_ct");
      ctBtn.hidden = false;
      return;
    }
  }
  text.textContent = noPrice ? tn("empty.noprice", noPrice) : t("empty.text");
}

function announceCount(f, shown, noPrice) {
  const total = ui.model.all.length;
  let text = filtersActive(f) ? t("count.some", { shown: num(shown), total: tn("count.lots", total) }) : tn("count.all", total);
  if (noPrice) text += ` ${tn("count.noprice", noPrice)}`;
  if (!shown && total) text += `. ${t("empty.title")}.`;  // the status region says it, not only the box below
  $("count").textContent = text;
  updateToggleCount();
}

function jumpTo(key) {
  const sec = $(`sec-${key}`);
  if (!sec || sec.hidden) {
    clearFilters();
    renderSections();
    renderToggles();
  }
  const target = $(`sec-${key}`);
  target.scrollIntoView({ block: "start", behavior: "auto" });
  target.querySelector("h2").focus({ preventScroll: true });
}

// ------------------------------------------------------------------ the sheet

function sheetSection(id, labelKey, kids, cls) {
  const sec = h("section", { cls: `sh-sec${cls ? ` ${cls}` : ""}`, attrs: { "aria-labelledby": id } });
  sec.appendChild(h("h3", { cls: "lbl sh-h", text: t(labelKey), attrs: { id } }));
  kids.filter(Boolean).forEach((k) => sec.appendChild(k));
  return sec;
}

function sheetLink() {
  return `${location.origin}${location.pathname}#lot-${ui.openRef}`;
}

function copyLinkButton(cls, iconOnly) {
  const btn = h("button", { cls, attrs: { type: "button", "aria-label": iconOnly ? t("sheet.copy_long") : null } },
    iconOnly ? [icon("link")] : [icon("link"), h("span", { text: t("sheet.copy") })]);
  btn.addEventListener("click", () => {
    const url = sheetLink();
    const done = () => {
      const was = btn.getAttribute("aria-label");
      if (!iconOnly) btn.lastChild.textContent = t("sheet.copied");
      else btn.setAttribute("aria-label", t("sheet.copied"));
      if ($("sheet-status")) $("sheet-status").textContent = t("sheet.copied");  // said, not only shown
      setTimeout(() => {
        if (!iconOnly) btn.lastChild.textContent = t("sheet.copy"); else btn.setAttribute("aria-label", was);
        if ($("sheet-status")) $("sheet-status").textContent = "";
      }, 2000);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(url).then(done, () => showLink(btn, url));
    else showLink(btn, url);
  });
  return btn;
}

// Without the clipboard: the link, selected, to copy by hand.
function showLink(btn, url) {
  const input = h("input", { cls: "copy-field", attrs: { type: "text", readonly: true, value: url, "aria-label": t("sheet.copy_long") } });
  btn.replaceWith(input);
  input.focus();
  input.select();
}

function sheetTopBar(car) {
  // The phone's view: « ← Lots », where the lot sits, and the link to this sheet.
  const total = ui.model.all.length;
  return h("div", { cls: "sh-topbar" }, [
    h("button", { cls: "back", attrs: { type: "button" }, on: { click: () => closeSheet({ restoreFocus: true }) } },
      [icon("back"), h("span", { text: t("sheet.back") })]),
    h("span", { cls: "mono sh-pos", text: t("sheet.position", { lot: car.lot === null || car.lot === undefined ? "–" : car.lot,
      total: tn("sale.lots_n", total) }) }),
    copyLinkButton("icon-btn", true),
  ]);
}

function sheetHead(car) {
  const sale = ui.sale;
  const where = [houseOf(sale), roomOf(sale)].filter(Boolean).join(" · ");
  const head = h("div", { cls: "sh-head" }, [
    h("div", { cls: "sh-idrow" }, [
      tagNode(car, "lg"),
      h("span", { cls: "mono sh-where" }, [where, h("br"), dayText(sale && sale.date) || ""]),
      h("button", { cls: "icon-btn sheet-x", attrs: { type: "button", "aria-label": t("sheet.close") },
        on: { click: () => closeSheet({ restoreFocus: true }) } }, [icon("x")]),
    ]),
    h("div", { cls: "sh-names" }, [
      h("h2", { cls: "sh-title", text: titleOf(car), attrs: { id: "sheet-title", tabindex: "-1" } }),
      subtitleOf(car) ? h("p", { cls: "sh-sub", text: subtitleOf(car) }) : null,
    ]),
  ]);
  const specs = h("dl", { cls: "specs" });
  [["spec.year", car.year, true], ["spec.km", isNum(car.mileage) ? num(car.mileage) : null, true],
    ["spec.fuel", fuelText(car.fuel), false], ["spec.gearbox", gearboxText(car.gearbox), false]].forEach(([k, v, fig]) => {
    specs.appendChild(h("div", {}, [h("dt", { cls: "lbl", text: t(k) }), h("dd", { cls: fig ? "fig" : "", text: v === null || v === undefined ? "–" : String(v) })]));
  });
  head.appendChild(specs);
  return head;
}

function gateBanners(car) {
  const out = [];
  gatesOf(car).forEach((g) => {
    if (g.code === "ct_critique" || g.code === "ct_non_lu") return;  // the CT section says it
    let title;
    let text;
    if (g.code === "engine_risk") {
      title = t("banner.engine_title");
      text = engineBanner((g.params || {}).ref || car.engine || "", ui.snapshot);
    } else {
      title = gateText(g.code, g.params, g.text, ui.snapshot);
      text = g.code ? tOr(`banner.${g.code}`, "") : "";
    }
    out.push(h("p", { cls: "gate-banner", attrs: { role: "note" } }, [icon("warn"),
      h("span", {}, [h("strong", { text: title, attrs: { lang: gateFrOnly(g) ? "fr" : null } }), text ? ` ${text}` : ""])]));
  });
  // A lot on one line has no gates in the snapshot, but its engine ref says the same.
  if (!out.length && car.engine && !car.detail) {
    out.push(h("p", { cls: "gate-banner", attrs: { role: "note" } }, [icon("warn"),
      h("span", {}, [h("strong", { text: t("banner.engine_title") }), ` ${engineBanner(car.engine, ui.snapshot)}`])]));
  }
  return out.slice(0, 2);
}

function sheetActions(car) {
  const target = listingTarget(car, ui.sale);
  const box = h("div", { cls: "sh-actions" });
  if (target.kind === "lot") {
    box.appendChild(extLink(target.url, t("sheet.listing", { house: target.house || t("sale.house_generic") }), "btn-primary listing"));
    box.appendChild(h("p", { cls: "sh-hint" }, [h("span", { text: t("sheet.listing_hint") }), copyLinkButton("btn-ghost btn-sm copy-link", false)]));
  } else {
    const p = h("p", { cls: "no-listing" }, [h("span", { text: t("sheet.no_listing") })]);
    if (target.kind === "sale") { p.appendChild(document.createTextNode(" ")); p.appendChild(extLink(target.url, t("sheet.sale_catalogue"), "")); }
    box.appendChild(p);
  }
  return box;
}

function briefLine(line) {
  const name = line.tone === "risk" ? "warn" : line.tone === "good" ? "check" : line.tone === "verify" ? "q" : "diamond";
  return h("li", { cls: `bl bl-${line.tone}` }, [icon(name), h("span", {}, [textIn(line.text, line.fr),
    line.src ? h("span", { cls: "mono src", text: ` ${t(`src.${line.src}`)}` }) : null])]);
}

function pillarBars(car) {
  const box = h("div", { cls: "pillars" });
  const pil = car.pillars || {};
  const known = !(car.detail && car.detail.value && car.detail.value.known === false);
  PILLARS.forEach(([key, fr]) => {
    const v = pil[fr];
    const hatched = key === "V" && !known;
    const bar = h("i", { cls: `pbar${hatched ? " hatch" : ""}${isNum(v) && v < 65 && !hatched ? " low" : ""}`, attrs: { "aria-hidden": "true" } });
    if (isNum(v) && !hatched) setVar(bar, "--w", `${Math.max(0, Math.min(100, v))}%`);
    box.appendChild(h("div", { cls: "pillar" }, [
      h("span", { cls: "p-name", text: t(`pillar.${key}`) }),
      h("span", { cls: hatched ? "p-val p-nc" : "fig p-val", text: hatched ? t("pillar.not_compared") : (isNum(v) ? String(v) : "–") }),
      bar,
    ]));
  });
  return box;
}

function scoreCard(car) {
  const size = ui.phone ? 100 : 104;
  return h("div", { cls: "score-card" }, [
    h("div", { cls: "sc-ring" }, [
      ringNode(car, size, (ui.snapshot || {}).scoring),
      h("span", { cls: "ring-of", text: "/100", attrs: { "aria-hidden": "true" } }),
      h("span", { cls: `tier-long band-${ringBand(car.score, (ui.snapshot || {}).scoring)}`, text: tOr(`tier_long.${car.tier}`, ""),
        attrs: { "aria-hidden": "true" } }),
      sr(scoreA11y(car)),  // « note 69 sur 100, Profil correct », once
    ]),
    car.pillars ? pillarBars(car) : null,
  ]);
}

function aiNode(brief) {
  const ai = brief.ai;
  if (ai.kind === "verdict") {
    const kids = [h("strong", { cls: "ai-lbl", text: t("ai.label") }), h("span", { cls: "ai-text", text: ai.text,
      attrs: { lang: "fr" } })];
    if (LANG !== "fr") kids.push(h("span", { cls: "mono src", text: ` ${t("ai.in_french")}` }));
    if (brief.adjust) kids.push(h("span", { cls: "ai-adj", text: tn("ai.adjust", Math.abs(brief.adjust), { sign: signed(brief.adjust) }) }));
    return h("div", { cls: "ai-line ai-verdict" }, kids);
  }
  return h("p", { cls: "ai-line" }, [h("i", { cls: "ai-dot", attrs: { "aria-hidden": "true" } }), h("span", { cls: "ai-off", text: t(`ai.${ai.kind}`) })]);
}

function sheetBrief(car) {
  const brief = briefOf(car, ui.snapshot);
  const list = h("ul", { cls: "brief" });
  brief.lines.forEach((l) => list.appendChild(briefLine(l)));
  if (brief.verify.length) {
    list.appendChild(h("li", { cls: "bl bl-verify" }, [icon("q"), h("span", {}, [h("strong", { text: t("brief.verify") }), ` ${joinChecks(brief.verify)}`])]));
  }
  const s = ui.snapshot || {};
  const weights = (s.scoring && s.scoring.weights) || {};
  const known = !(car.detail && car.detail.value && car.detail.value.known === false);
  const caption = [t("brief.weights", { r: weights["Fiabilité"] || 35, c: weights["État"] || 35, v: weights.Prix || 30 }),
    known ? null : t("brief.price_neutral"), t("brief.bands")].filter(Boolean).join(" ");
  const wrap = h("div", { cls: "brief-wrap" }, [list, scoreCard(car)]);
  return sheetSection("s-brief", "sheet.brief", [wrap, h("p", { cls: "caption", text: caption }), aiNode(brief)]);
}

// « À vérifier : x ; y. » (after a colon, French and English both go on in lower case).
function joinChecks(items) {
  const parts = items.map((x) => String(x).trim().replace(/[.\s]+$/, ""));
  return `${parts.map((p) => p.charAt(0).toLowerCase() + p.slice(1)).join(LANG === "fr" ? `${NBSP}; ` : "; ")}.`;
}

function sheetCost(car) {
  const d = car.detail || {};
  const p = d.price;
  const fm = feeModelOf(ui.sale, p);
  const kids = [];
  if (!p) {
    kids.push(h("p", { cls: "cost-total" }, [h("span", { cls: "fig big", text: eur(car.effective_price) || "–" })]));
    kids.push(h("p", { cls: "caption", text: isNum(car.effective_price) ? t("cost.no_detail") : t("gap.price") }));
    return sheetSection("s-cost", "sheet.cost", kids);
  }
  const basis = priceBasis(p, car.starting_price);
  kids.push(h("p", { cls: "cost-total" }, [h("span", { cls: "fig big", text: eur(p.total) }),
    h("span", { cls: "cost-at", text: t(`cost.at_${basis}`, { amount: eur(p.start), ht: vatApplies(p) ? t("cost.ht") : "" }) })]));
  const rows = priceRows(p, car.starting_price, fm);
  if (rows) {
    const bar = h("div", { cls: "cost-bar", attrs: { role: "img" } });
    const shares = [];
    rows.rows.forEach((r) => {
      if (r.value <= 0) return;
      const seg = h("i", { cls: `cb cb-${r.key}` });
      setVar(seg, "--w", r.value);
      bar.appendChild(seg);
      shares.push(`${r.label} ${num(Math.round((100 * r.value) / rows.total))}${NBSP}%`);
    });
    bar.setAttribute("aria-label", t("cost.composition", { parts: shares.join(", ") }));
    kids.push(bar);
    const dl = h("dl", { cls: "ledger" });
    rows.rows.forEach((r) => dl.appendChild(h("div", { cls: `lg-row lg-${r.key}` }, [
      h("dt", {}, [h("i", { cls: `sw cb-${r.key}`, attrs: { "aria-hidden": "true" } }), r.label]),
      h("dd", { cls: "fig", text: eur(r.value) })])));
    kids.push(dl);
  }
  kids.push(h("p", { cls: "caption", text: t("cost.excluded") }));
  if (fm) kids.push(simulator(car, fm, p));
  return sheetSection("s-cost", "sheet.cost", kids);
}

function simKey(car) {
  return `af-sim-${(ui.sale && ui.sale.external_id) || "sale"}-${car.ref}`;
}

function simulator(car, fm, price) {
  const saved = (() => { try { return JSON.parse(store("session", simKey(car)) || "null"); } catch (err) { return null; } })();
  const defaults = simDefaults(price);
  const start = priceBasis(price, car.starting_price) === "start" ? price.start : (isNum(car.starting_price) ? car.starting_price : null);
  const field = (id, label, value) => h("label", { cls: "sim-in" }, [h("span", { text: label }),
    h("span", { cls: "num-wrap" }, [h("input", { cls: "num-in fig", attrs: { id, type: "number", inputmode: "numeric", min: "0", step: "100",
      value: isNum(value) ? String(value) : "" } }), h("span", { cls: "num-unit fig", text: "€", attrs: { "aria-hidden": "true" } })])]);
  // Each output is named by its own label. Typing is not read out keystroke by keystroke: one
  // status line says the result once the reader pauses (see announce below).
  const out = (id, label) => h("div", { cls: "sim-out" }, [h("span", { text: label, attrs: { id: `${id}-l` } }),
    h("output", { cls: "fig", attrs: { id, for: id.replace("-o", "-i"), "aria-labelledby": `${id}-l` } })]);
  const pIn = field("sim-p-i", t("sim.if_bid"), saved && isNum(saved.P) ? saved.P : defaults.P);
  const tIn = field("sim-t-i", t("sim.if_budget"), saved && isNum(saved.T) ? saved.T : defaults.T);
  const note = h("p", { cls: "sim-note", attrs: { id: "sim-note" } });
  const said = h("p", { cls: "sr", attrs: { role: "status" } });
  const write = (node, text) => { if (node.textContent !== text) node.textContent = text; };
  const per = h("p", { cls: "caption sim-caveat" });
  const box = h("div", { cls: "sim" }, [
    h("div", { cls: "sim-head" }, [h("h4", { text: t("sim.title") }), h("span", { cls: "mono", text: t("sim.tag") })]),
    h("div", { cls: "sim-grid" }, [pIn, out("sim-p-o", t("sim.would_pay")), tIn, out("sim-t-o", t("sim.max_bid"))]),
    note, per, said,
  ]);
  const update = () => {
    const P = Number(box.querySelector("#sim-p-i").value);
    const T = Number(box.querySelector("#sim-t-i").value);
    const okP = box.querySelector("#sim-p-i").value !== "" && isFinite(P) && P >= 0;
    const okT = box.querySelector("#sim-t-i").value !== "" && isFinite(T) && T >= 0;
    const r = simulate(fm, price, okP ? P : null, okT ? T : null, start);
    write(box.querySelector("#sim-p-o"), okP && isNum(r.cost) ? t("sim.about", { amount: eur(r.cost) }) : "–");
    write(box.querySelector("#sim-t-o"), okT && isNum(r.max) && r.max > 0 ? eur(r.max) : "–");
    write(note, okT ? (r.maxNote || "") : "");
    write(per, [t("sim.excluded"), isNum(r.per100) ? t(vatApplies(price) ? "sim.per100_vat" : "sim.per100", { n: num(r.per100) }) : null,
      t("sim.step_unknown")].filter(Boolean).join(" "));
    return { P: okP ? P : null, T: okT ? T : null,
      said: [`${t("sim.would_pay")} ${box.querySelector("#sim-p-o").textContent}`,
        `${t("sim.max_bid")} ${box.querySelector("#sim-t-o").textContent}`, note.textContent].filter(Boolean).join(". ") };
  };
  // Only the fields the reader typed in are kept (the others follow each new analysis's defaults).
  const typed = { P: Boolean(saved && isNum(saved.P)), T: Boolean(saved && isNum(saved.T)) };
  let timer = null;
  box.addEventListener("input", (event) => {
    if (event.target.id === "sim-p-i") typed.P = true;
    if (event.target.id === "sim-t-i") typed.T = true;
    const r = update();
    store("session", simKey(car), JSON.stringify({ P: typed.P ? r.P : null, T: typed.T ? r.T : null }));
    clearTimeout(timer);
    timer = setTimeout(() => { said.textContent = r.said; }, 700);
  });
  update();
  return box;
}

function ctResultChip(car) {
  const sum = ctSummary(car);
  return h("span", { cls: `ct-result ct-${sum.state}` }, [sum.state === "fav" ? icon("check") : sum.state === "none" ? icon("q") : icon("warn"),
    h("span", { text: sum.state === "fav" ? sum.word : [sum.word, sum.detail].filter(Boolean).join(" · ") })]);
}

function ctMeta(ct) {
  const nature = ct.nature ? capitalise(tOr(`nature.${fold(ct.nature).replace(/[^a-z]+/g, "_")}`, ct.nature)) : null;
  const parts = [nature, shortDate(ct.date), isNum(ct.km) ? kmText(ct.km) : null,
    shortDate(ct.valid_until) ? t("ct.valid_until", { day: shortDate(ct.valid_until) }) : null];
  return parts.filter(Boolean).join(" · ");
}

function gridList(car) {
  const ct = car.detail && car.detail.ct;
  const counts = ct ? functionCounts(ct) : null;
  const grid = gridOf(car);
  const ul = h("ul", { cls: "fgrid", attrs: { "aria-label": t("ct.grid_label") } });
  GRID_FUNCTIONS.forEach((fn) => {
    const c = counts ? counts[fn] : null;
    const mark = grid ? grid[fn] : "u";
    let state;
    if (c && (c.critical || c.major || c.minor)) {
      state = [c.critical ? tn("ct.critical", c.critical) : null, c.major ? tn("ct.major", c.major) : null,
        c.minor ? tn("ct.minor", c.minor) : null].filter(Boolean).join(", ");
    } else {
      state = t({ ".": "ct.cell_ok", n: "ct.cell_n", M: "ct.cell_M", C: "ct.cell_C", u: "ct.cell_u" }[mark]);
    }
    ul.appendChild(h("li", { cls: `fcell fcell-${{ ".": "ok", n: "n", M: "M", C: "C", u: "u" }[mark]}` }, [
      h("span", { cls: "f-name" }, [h("span", { cls: "f-no", text: String(fn) }), ` ${t(`fn.${fn}`)}`]),
      h("span", { cls: "f-state", text: state }),
    ]));
  });
  return ul;
}

function defectsNode(ct) {
  const defects = Array.isArray(ct.defects) ? ct.defects : [];
  if (!defects.length) return null;
  const item = (d) => {
    const lbl = defectLabel(d.label);
    return h("li", { cls: `defect sev-${d.severity}` }, [
      h("span", { cls: "d-sev" }, [h("i", { cls: `cell cell-${{ minor: "n", major: "M", critical: "C" }[d.severity] || "u"}`, attrs: { "aria-hidden": "true" } }),
        t(`sev.${d.severity}`)]),
      h("span", { cls: "d-label" }, [h("span", { text: lbl.text, attrs: { lang: "fr" } }),
        h("span", { cls: "mono d-code", text: ` ${[lbl.where ? `(${lbl.where})` : null, d.code].filter(Boolean).join(" · ")}` })]),
    ]);
  };
  const serious = defects.filter((d) => d.severity !== "minor");
  const minor = defects.filter((d) => d.severity === "minor");
  const box = h("div", { cls: "defects" });
  const ul = h("ul", { cls: "dlist" });
  serious.forEach((d) => ul.appendChild(item(d)));
  minor.slice(0, 4).forEach((d) => ul.appendChild(item(d)));
  box.appendChild(ul);
  if (minor.length > 4) {
    const more = h("ul", { cls: "dlist" });
    minor.slice(4).forEach((d) => more.appendChild(item(d)));
    box.appendChild(h("details", { cls: "fold" }, [h("summary", { text: tn("ct.more_minor", minor.length - 4) }), more]));
  }
  if (ct.more) box.appendChild(h("p", { cls: "caption", text: tn("ct.more_hidden", ct.more) }));
  if (LANG !== "fr") box.appendChild(h("p", { cls: "caption", text: t("ct.french_labels") }));
  return box;
}

function sheetCt(car) {
  const d = car.detail || {};
  const ct = d.ct;
  const head = h("div", { cls: "sh-ct-head" }, [h("h3", { cls: "lbl sh-h", text: t("sheet.ct"), attrs: { id: "s-ct" } }), ctResultChip(car)]);
  const sec = h("section", { cls: "sh-sec", attrs: { "aria-labelledby": "s-ct" } }, [head]);
  if (ct) sec.appendChild(h("p", { cls: "mono ct-meta", text: ctMeta(ct) }));
  else if (d.ct_doc === "unreadable") sec.appendChild(h("p", { cls: "caption", text: t("ct.unreadable") }));
  else if (findReason(car, "ct_not_required")) sec.appendChild(h("p", { cls: "caption", text: t("ct.not_required") }));
  else if (car.detail) sec.appendChild(h("p", { cls: "caption", text: t("ct.absent") }));
  if (ct && ct.doubtful) sec.appendChild(h("p", { cls: "callout", text: t("ct.doubtful") }));
  const grid = gridOf(car);
  if (grid && grid !== "uuuuuuuuu") sec.appendChild(gridList(car));  // nine "not read" cells say nothing
  if (ct) {
    const defects = defectsNode(ct);
    if (defects) sec.appendChild(defects);
    sec.appendChild(h("p", { cls: "caption", text: t("ct.legend") }));
    sec.appendChild(h("p", { cls: "caption", text: t("ct.severities") }));
  }
  return sec;
}

function verifyRow(labelKey, value, extra) {
  return h("div", { cls: "vrow" }, [h("dt", { text: t(labelKey) }), h("dd", {}, [value, extra || null])]);
}

function scoreDetail(car) {
  const d = car.detail || {};
  const box = h("details", { cls: "fold score-detail" }, [h("summary", { text: t("sheet.score_detail") })]);
  const sc = d.score || {};
  const lines = [];
  if (isNum(sc.before_cap) && sc.before_cap !== car.score) lines.push(t("score.before_cap", { n: sc.before_cap, final: car.score }));
  if (sc.cap) lines.push(LANG === "fr" ? sc.cap : t("score.capped", { n: car.score }));
  if (isNum(sc.llm) && sc.llm) lines.push(tn("ai.adjust", Math.abs(sc.llm), { sign: signed(sc.llm) }));
  lines.forEach((x) => box.appendChild(h("p", { cls: "caption", text: x })));
  const reasons = reasonsOf(car);
  if (reasons && reasons.length) {
    const ul = h("ul", { cls: "points" });
    reasons.forEach((r) => ul.appendChild(h("li", {}, [h("span", { cls: "fig pts", text: isNum(r.pts) ? signed(r.pts) : "" }),
      " ", textIn(reasonText(r), frOnly(r)),
      r.pillar ? h("span", { cls: "mono src", text: ` ${PILLAR_KEY[r.pillar] ? t(`pillar.${PILLAR_KEY[r.pillar]}`) : r.pillar}` }) : null])));
    box.appendChild(ul);
  } else if (Array.isArray(car.reasons)) {
    const ul = h("ul", { cls: "points" });
    car.reasons.filter((r) => r && r.text).forEach((r) => ul.appendChild(h("li", {}, [textIn(reasonText(r), frOnly(r))])));
    box.appendChild(ul);
  }
  box.appendChild(h("p", { cls: "caption" }, [h("a", { text: t("sheet.how_score"), attrs: { href: "/about.html#note", hreflang: LANG === "fr" ? null : "fr" } })]));
  return box;
}

function sheetChecks(car) {
  const s = ui.snapshot || {};
  const d = car.detail || {};
  const dl = h("dl", { cls: "verify" });
  const kb = Object.keys(s.engines || {}).length;
  if (car.engine) {
    dl.appendChild(verifyRow("check.engine", engineBanner(car.engine, s) || engineName(car.engine, s), h("a", { cls: "why-engine",
      text: ` ${t("check.engine_why")}`, attrs: { href: "/about.html#moteurs", hreflang: LANG === "fr" ? null : "fr" } })));
  } else {
    dl.appendChild(verifyRow("check.engine", kb ? t("check.engine_none", { n: tOr(`word.${kb}`, num(kb)) }) : t("check.engine_unknown")));
  }
  if (car.detail) {
    let body;
    if (d.damages === null || d.damages === undefined) body = t("check.body_unread");
    else if (!d.damages.length) body = t("check.body_none");
    else body = h("span", {}, [`${tn("check.body_n", d.damages.length)} `,
      textIn(d.damages.slice(0, 4).map((x) => `${x.zone} (${x.kind})`).join(", "), LANG !== "fr")]);  // the auction site's words
    dl.appendChild(verifyRow("check.body", body));
  }
  const b = d.battery;
  const electric = ["electric", "plugin"].includes(fuelKind(car.fuel));
  if (b && (isNum(b.soh) || b.leased)) {
    const parts = [];
    if (isNum(b.soh)) parts.push(t("check.battery_soh", { soh: num(b.soh), source: t(`battery_src.${b.source || "listing"}`) }));
    if (b.leased) parts.push(t("sig.leased"));
    dl.appendChild(verifyRow("check.battery", parts.join(" ")));
  } else if (electric) {
    dl.appendChild(verifyRow("check.battery", t("verify.battery")));
  }
  const w = findReason(car, "warranty");
  if (w) {
    const named = w.params && w.params.kind === "named";
    dl.appendChild(verifyRow("check.warranty", named ? t("check.warranty_named", { label: w.params.label }) : t("check.warranty_unspecified")));
  } else if (car.detail) {
    dl.appendChild(verifyRow("check.warranty", t("check.warranty_none")));
  }
  const kids = [dl];
  if (car.detail || car.reasons) kids.push(scoreDetail(car));
  if (!car.detail) kids.push(h("p", { cls: "caption", text: t("sheet.no_detail") }));
  return sheetSection("s-check", "sheet.checks", kids);
}

function sheetSources(car) {
  const house = listingTarget(car, ui.sale).house || houseOf(ui.sale) || t("sale.house_generic");
  const day = dayText(String((ui.snapshot && ui.snapshot.generated_at) || "").slice(0, 10), { weekday: undefined });
  return h("p", { cls: "sh-sources", text: t("sheet.sources", { house, day: day || "" }) });
}

function stickyBar(car) {
  const target = listingTarget(car, ui.sale);
  const kids = [ringNode(car, 40, (ui.snapshot || {}).scoring),
    h("span", { cls: "sb-cost" }, [h("span", { cls: "fig", text: eur(car.effective_price) || "–" }), h("span", { text: t("sb.cost") })])];
  if (target.kind === "lot") kids.push(extLink(target.url, t("sheet.listing", { house: target.house || t("sale.house_generic") }), "btn-primary sb-listing"));
  return h("div", { cls: "sh-sticky" }, kids);
}

function renderSheet(car) {
  const sheet = clear(ui.sheet);
  if (!ui.desktop) sheet.appendChild(sheetTopBar(car));
  sheet.appendChild(sheetHead(car));
  gateBanners(car).forEach((b) => sheet.appendChild(b));
  sheet.appendChild(sheetActions(car));
  sheet.appendChild(h("p", { cls: "masked-note callout", text: t("sheet.masked"), attrs: { id: "sheet-masked", hidden: !ui.hiddenOpen } }));
  sheet.appendChild(h("p", { cls: "sr", attrs: { id: "sheet-status", role: "status" } }));  // « Lien copié », said
  [sheetBrief(car), sheetCost(car), sheetCt(car), sheetChecks(car), sheetSources(car)].forEach((p) => sheet.appendChild(p));
  if (!ui.desktop) sheet.appendChild(stickyBar(car));
}

function setMasked(masked) {
  ui.hiddenOpen = masked;
  const note = $("sheet-masked");
  if (note) note.hidden = !masked;
}

function markOpenRow() {
  ui.rows.forEach((li, ref) => {
    const open = ref === ui.openRef;
    li.classList.toggle("is-open", open);
    const btn = li.querySelector(".row");
    if (open) btn.setAttribute("aria-current", "true");
    else btn.removeAttribute("aria-current");
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  });
}

// Everything but the phone's sheet view is inert while it is open (focus stays in the sheet).
function setInert(on) {
  Array.from(document.body.children).forEach((node) => {
    if (node.id === "sheet-layer" || node.tagName === "SCRIPT") return;
    if (on) node.setAttribute("inert", "");
    else node.removeAttribute("inert");
  });
}

function placeSheet() {
  if (!ui.sheet) return;
  const empty = $("sheet-pane").querySelector(".pane-empty");
  const layer = $("sheet-layer");
  if (!ui.openRef) {
    if (ui.sheet.parentNode) ui.sheet.parentNode.removeChild(ui.sheet);
    empty.hidden = !(ui.desktop && ui.model && ui.model.all.length);
    if (!layer.hidden) {
      layer.hidden = true;
      layer.classList.remove("is-in");
      document.documentElement.classList.remove("has-sheet");
      setInert(false);
    }
    return;
  }
  empty.hidden = true;
  if (ui.desktop) {
    $("sheet-pane").appendChild(ui.sheet);
    if (!layer.hidden) {
      layer.hidden = true;
      document.documentElement.classList.remove("has-sheet");
      setInert(false);
    }
  } else {
    layer.appendChild(ui.sheet);
    if (layer.hidden) {
      layer.hidden = false;
      document.documentElement.classList.add("has-sheet");
      setInert(true);
      layer.classList.remove("is-in");
      void layer.offsetWidth;  // restart the slide-in
      layer.classList.add("is-in");
    }
  }
}

function openLot(ref, opts) {
  const o = opts || {};
  const car = ui.model && ui.model.byRef.get(ref);
  if (!car) return false;
  const swap = ui.desktop && ui.openRef && ui.openRef !== ref;
  if (!ui.openRef && !ui.desktop) ui.scrollY = window.scrollY;
  ui.openRef = ref;
  ui.hiddenOpen = false;
  renderSheet(car);
  placeSheet();
  markOpenRow();
  const li = ui.rows.get(ref);
  setMasked(filtersActive(readFilters()) && !filterCars([car], readFilters()).shown.length);
  if (swap) {
    ui.sheet.classList.remove("is-swap");
    void ui.sheet.offsetWidth;
    ui.sheet.classList.add("is-swap");
  }
  const hash = `#lot-${ref}`;
  if (o.hash !== false && location.hash !== hash) {
    if (!ui.desktop && o.push) {
      history.pushState({ afLot: ref }, "", hash);
      ui.pushed = true;
    } else {
      history.replaceState(null, "", hash);
    }
  }
  renderLangLinks();
  if (ui.desktop) {
    $("sheet-pane").scrollTop = 0;
    if (o.reveal) {
      $("catbar").scrollIntoView({ block: "start", behavior: "auto" });
      if (li && li.isConnected) li.scrollIntoView({ block: "nearest", behavior: "auto" });
    }
    if (o.focus) $("sheet-title").focus({ preventScroll: true });
  } else {
    ui.sheet.scrollTop = 0;
    $("sheet-layer").scrollTop = 0;
    $("sheet-title").focus({ preventScroll: true });
  }
  return true;
}

function closeSheet(opts) {
  const ref = ui.openRef;
  if (!ref) return;
  if (!ui.desktop && ui.pushed && history.state && history.state.afLot === ref) {
    ui.pushed = false;  // a second tap before popstate must not go back twice (and leave the site)
    history.back();  // popstate closes it; Back and « ← Lots » do the same
    return;
  }
  finishClose(ref, opts);
}

function finishClose(ref, opts) {
  ui.openRef = null;
  ui.hiddenOpen = false;
  ui.pushed = false;
  const phone = !$("sheet-layer").hidden;  // the full-screen view was open: the list scroll comes back
  placeSheet();
  markOpenRow();
  if (parseHash(location.hash)) history.replaceState(null, "", location.pathname + location.search);
  renderLangLinks();
  if (phone) window.scrollTo(0, ui.scrollY);
  if (!opts || opts.restoreFocus !== false) {
    const li = ui.rows.get(ref);
    if (li && li.isConnected) li.querySelector(".row").focus({ preventScroll: phone });
    else $("f-q").focus({ preventScroll: true });
  }
}

function toggleLot(ref) {
  ui.autoOpened = false;
  if (ui.openRef === ref && ui.desktop) closeSheet({ restoreFocus: true });
  else openLot(ref, { focus: ui.desktop, push: !ui.desktop });
}

// A hash in the address opens that lot (on load and later); an absent lot says so. Another
// anchor (the skip link, « Voir les lots », « détails ») leaves the open sheet alone.
function onHashChange() {
  const ref = parseHash(location.hash);
  if (!ui.model) return;
  if (!ref) {
    if (ui.openRef && !location.hash && !ui.desktop) finishClose(ui.openRef, { restoreFocus: true });
    return;
  }
  ui.autoOpened = false;
  const car = ui.model.byRef.get(ref);
  if (!car) {
    showAbsent(ref);
    return;
  }
  const li = rowFor(car);
  if (!li.isConnected) {
    ui.expanded[car.section] = true;
    renderSections();
    if (!li.isConnected) {
      clearFilters();
      renderSections();
      renderToggles();
    }
  }
  openLot(ref, { focus: true, reveal: true, hash: false });
}

function onPopState() {
  const ref = parseHash(location.hash);
  if (!ref && ui.openRef && !ui.desktop) finishClose(ui.openRef, { restoreFocus: true });
  else if (ref && ref !== ui.openRef) onHashChange();
}

// The filters back to "all", without the form's reset event (whose handler runs a tick later).
function clearFilters() {
  ["f-q"].concat(FILTER_IDS).forEach((id) => { $(id).value = ""; });
  $("h-q").value = "";
  $("h-budget").value = "";
}

function showAbsent(ref) {
  if (!ui.model.all.length) {  // no lot at all: the page already says so
    history.replaceState(null, "", location.pathname + location.search);
    return;
  }
  ui.absentLot = ref;
  clearFilters();
  $("f-q").value = ref;
  $("h-q").value = ref;
  renderSections();
  renderToggles();
  history.replaceState(null, "", location.pathname + location.search);
  $("catbar").scrollIntoView({ block: "start", behavior: "auto" });
  $("no-match").querySelector("h2").setAttribute("tabindex", "-1");
  $("no-match").querySelector("h2").focus({ preventScroll: true });
}

function onKeydown(e) {
  if (e.key === "Escape") {
    if ($("f-more").classList.contains("is-open") && $("filters").contains(document.activeElement)) {
      setMore(false);
      $("f-toggle").focus();
      e.preventDefault();
      return;
    }
    // Esc closes the sheet from the sheet, a row or the page itself; in a search field or a
    // filter it belongs to that control (the desktop pane is open by default).
    const at = document.activeElement;
    const inSheet = ui.sheet && ui.sheet.contains(at);
    const onRow = at && at.classList && at.classList.contains("row");
    if (ui.openRef && (!ui.desktop || inSheet || onRow || at === document.body || !at)) {
      closeSheet({ restoreFocus: true });
      e.preventDefault();
    }
    return;
  }
  const target = e.target;
  if (!target || !target.classList || !target.classList.contains("row")) return;
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
  const buttons = Array.from(document.querySelectorAll(".lots .row"));
  const i = buttons.indexOf(target);
  if (i < 0) return;
  const next = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: buttons.length - 1 }[e.key];
  if (next >= 0 && next < buttons.length) {
    buttons[next].focus();
    e.preventDefault();
  }
}

// ------------------------------------------------------------------ events

function onFilterInput() {
  ui.absentLot = ui.absentLot && $("f-q").value === ui.absentLot ? ui.absentLot : null;
  renderSections();
  renderToggles();
  measureBar();
}

function onReset() {
  // The form's own reset runs after this handler: render once it is done. The button pressed
  // (« Tout effacer », « Retirer tous les filtres ») disappears, so the focus goes to the search.
  setTimeout(() => {
    ui.absentLot = null;
    $("h-q").value = "";
    $("h-budget").value = "";
    renderSections();
    renderToggles();
    if (!document.activeElement || document.activeElement === document.body || document.activeElement.closest("[hidden]")) {
      $("f-q").focus({ preventScroll: true });
    }
  }, 0);
}

// Typing in the search re-renders at once but announces the count once typing pauses.
let countTimer = null;
function onSearchInput(event) {
  if (event.target !== $("f-q")) return;
  event.stopPropagation();
  ui.absentLot = ui.absentLot && $("f-q").value === ui.absentLot ? ui.absentLot : null;
  $("count").setAttribute("aria-live", "off");
  renderSections();
  renderToggles();
  clearTimeout(countTimer);
  countTimer = setTimeout(() => {
    $("count").setAttribute("aria-live", "polite");
    const text = $("count").textContent;
    $("count").textContent = "";
    $("count").textContent = text;
  }, 400);
}

// A budget as typed: "8 000 €", "8000", "8 000,00 €" -> 8000; empty -> null; anything else -> NaN.
function parseBudget(text) {
  const s = String(text || "").replace(/[\s  €]/g, "").replace(/[.,]\d{1,2}$/, "");
  if (!s) return null;
  if (!/^\d+([.,]\d{3})*$/.test(s)) return NaN;
  const n = Number(s.replace(/[.,]/g, ""));
  return n > 0 ? n : null;
}

function onHeroSubmit(event) {
  event.preventDefault();
  const budget = parseBudget($("h-budget").value);
  if (Number.isNaN(budget)) {  // not a number: say so, change nothing
    $("h-budget").setAttribute("aria-invalid", "true");
    $("h-budget-err").hidden = false;
    $("h-budget").focus();
    return;
  }
  $("h-budget").removeAttribute("aria-invalid");
  $("h-budget-err").hidden = true;
  $("f-q").value = $("h-q").value.trim();
  setBudget(budget);
  onFilterInput();
  $("catbar").scrollIntoView({ block: "start", behavior: "auto" });
  const first = ["top", "cards", "rest"].map((k) => $(`sec-${k}`)).find((s) => !s.hidden);
  (first ? first.querySelector("h2") : $("no-match").querySelector("h2")).focus({ preventScroll: true });
}

function onToggle(event) {
  const btn = event.currentTarget;
  const def = TOGGLES[btn.getAttribute("data-toggle")];
  if (!def) return;
  const value = String(def.value() || "");
  const sel = $(def.id);
  sel.value = sel.value === value ? "" : value;
  onFilterInput();
}

// The language switch keeps the open lot (in the link) and the filters (handed over once).
function onLangClick(event) {
  const lang = event.currentTarget.getAttribute("hreflang");
  store("local", "af-lang", lang);
  const f = readFilters();
  store("session", "af-carry", JSON.stringify(f));
}

// The filters handed over by the language switch: read once and erased at load.
function readCarry() {
  let f = null;
  try { f = JSON.parse(store("session", "af-carry") || "null"); } catch (err) { f = null; }
  store("session", "af-carry", null);
  return f && typeof f === "object" ? f : null;
}

function applyCarry(f) {
  if (!f) return;
  const set = (id, v) => { const el = $(id); if (el && v !== null && v !== undefined && Array.from(el.options || []).some((o) => o.value === String(v))) el.value = String(v); };
  if (typeof f.q === "string") $("f-q").value = f.q;
  if (isNum(f.budget)) setBudget(f.budget);
  set("f-fuel", f.fuel); set("f-gearbox", f.gearbox); set("f-km", f.km); set("f-year", f.year);
  set("f-ct", f.ct); set("f-engine", f.engine); set("f-sort", f.sort);
}

// On the French page, a reader whose browser prefers English is told once that it exists.
function langHint() {
  const hint = $("lang-hint");
  if (!hint || LANG !== "fr" || store("local", "af-lang")) return;
  const langs = (navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || ""]).map(fold);
  const firstFr = langs.findIndex((l) => l.startsWith("fr"));
  const firstEn = langs.findIndex((l) => l.startsWith("en"));
  if (firstEn === 0 || (firstEn >= 0 && firstFr < 0)) {
    hint.hidden = false;
    $("lang-hint-x").addEventListener("click", () => { store("local", "af-lang", "fr"); hint.hidden = true; });
  }
}

// ------------------------------------------------------------------ states and boot

function showSkeleton(on) {
  document.querySelectorAll(".skel").forEach((node) => { if (!on) node.remove(); });
  $("catalogue").setAttribute("aria-busy", on ? "true" : "false");
}

// After "Réessayer" (focus true) the focus moves to the message, so a repeated failure is announced.
function renderState(kind, httpStatus, focus) {
  const err = errorFor(kind, httpStatus);
  showSkeleton(false);
  ["sec-top", "sec-cards", "sec-rest", "no-match"].forEach((id) => { $(id).hidden = true; });
  $("filters").hidden = true;
  $("index").hidden = true;
  $("sheet-pane").querySelector(".pane-empty").hidden = true;
  $("sale-title").textContent = t("state.error_title");
  $("sale-line").textContent = "";
  const box = clear($("state"));
  const title = h("h2", { text: err.title, attrs: { tabindex: "-1" } });
  box.appendChild(title);
  if (err.text) box.appendChild(h("p", { text: err.text }));
  if (err.retry) box.appendChild(h("button", { cls: "btn-ghost", text: t("state.retry"), attrs: { type: "button" }, on: { click: () => load({ retry: true }) } }));
  if (err.reload) box.appendChild(h("button", { cls: "btn-ghost", text: t("state.reload"), attrs: { type: "button" }, on: { click: () => location.reload() } }));
  box.hidden = false;
  if (focus) title.focus();
}

function render(s) {
  ui.snapshot = s;
  ui.model = prepare(s);
  ui.sale = featuredSale(s);
  ui.rows.clear();
  ui.openRef = null;
  $("state").hidden = true;
  showSkeleton(false);
  renderSaleCard(s);
  renderCoverage(s);
  renderBanners(s);
  renderStatus(s);
  renderBar(s);
  renderFilterOptions(ui.model);
  if (ui.carry) { applyCarry(ui.carry); ui.carry = null; }
  $("fees").textContent = s.sale ? (feeText(ui.sale) || "") : "";
  $("filters").hidden = !ui.model.all.length;
  $("sheet-pane").hidden = !ui.model.all.length;  // no lot: no empty pane beside the message
  if (!ui.model.all.length) {
    const box = clear($("state"));
    box.appendChild(h("h2", { text: t(ui.sale ? "state.no_lots" : "state.no_sale") }));
    box.hidden = false;
  }
  renderSections();
  renderToggles();
  measureBar();
  placeSheet();
}

// The footer's fees line, from the sale's fee model (never a fixed rate).
function feeText(sale) {
  const fm = sale && sale.fee_model;
  if (!fm || !isNum(fm.rate)) return t("foot.fees_generic");
  if (fm.venue === "online") return t("foot.fees_online");
  return t("foot.fees", { pct: new Intl.NumberFormat(locale(), { maximumFractionDigits: 1 }).format(fm.rate * 100), min: eur(fm.min) });
}

function applyHash() {
  if (parseHash(location.hash)) {
    onHashChange();
  } else if (ui.desktop && ui.model) {
    const first = sortCars(ui.model.all.filter((c) => c.section === "top"), readFilters().sort)[0]
      || sortCars(ui.model.all.filter((c) => c.section === "cards"), readFilters().sort)[0];
    if (first) {
      openLot(first.ref, { focus: false, hash: false });
      ui.autoOpened = true;  // shown by default, not chosen: it does not follow to a phone's full screen
      renderLangLinks();
    }
  }
}

async function loadDict(code) {
  try {
    const response = await fetch(`/i18n/${code}.json`, { credentials: "same-origin" });
    return response.ok ? await response.json() : null;
  } catch (err) {
    return null;
  }
}

async function loadStrings() {
  const lang = document.documentElement.lang === "en" ? "en" : "fr";
  const [fr, own] = await Promise.all([loadDict("fr"), lang === "fr" ? null : loadDict(lang)]);
  setStrings(lang, own || fr || {}, fr || {});
}

async function load(opts) {
  const retry = Boolean(opts && opts.retry);
  $("catalogue").setAttribute("aria-busy", "true");
  if (retry) {  // keep the message box (and the focus in it) while the new request runs
    const box = clear($("state"));
    const title = h("h2", { text: t("state.loading_title"), attrs: { tabindex: "-1" } });
    box.appendChild(title);
    box.hidden = false;
    title.focus();
  } else {
    $("state").hidden = true;
  }
  let response;
  try {
    response = await fetch(`/api/snapshot?t=${Date.now()}`, { cache: "no-store", credentials: "same-origin" });
  } catch (err) {
    renderState("network", undefined, retry);
    return;
  }
  if (response.status === 401 || response.status === 403) {
    renderState("session", response.status, retry);
    return;
  }
  let data;
  try {
    data = await response.json();
  } catch (err) {
    // A page that is not JSON: with a success status it is the Vercel login page (the
    // session has expired); otherwise it is a platform error page (timeout, crash).
    renderState(response.ok ? "session" : null, response.status, retry);
    return;
  }
  if (!response.ok || !data || typeof data !== "object") {
    renderState(data && data.status, response.status, retry);
    return;
  }
  render(data);
  if (retry) $("catalogue").focus({ preventScroll: true });
  applyHash();
}

async function boot() {
  ui.sheet = h("section", { cls: "sheet", attrs: { id: "sheet", "aria-labelledby": "sheet-title", tabindex: "-1" } });
  const desktop = window.matchMedia(DESKTOP_MQ);
  const phone = window.matchMedia(PHONE_MQ);
  ui.desktop = desktop.matches;
  ui.phone = phone.matches;
  await loadStrings();
  const form = $("filters");
  form.addEventListener("submit", (event) => event.preventDefault());
  $("f-q").addEventListener("input", onSearchInput);
  form.addEventListener("input", onFilterInput);
  form.addEventListener("change", onFilterInput);
  form.addEventListener("reset", onReset);
  $("no-match-reset").addEventListener("click", () => $("filters").reset());  // its reset event runs onReset
  $("no-match-ct").addEventListener("click", () => {
    setCtFilter("");
    const first = ["top", "cards", "rest"].map((k) => $(`sec-${k}`)).find((s) => !s.hidden);
    if (first) first.querySelector("h2").focus({ preventScroll: false });
  });
  $("f-toggle").addEventListener("click", () => setMore(!$("f-more").classList.contains("is-open")));
  $("hero-search").addEventListener("submit", onHeroSubmit);
  document.querySelectorAll("[data-toggle]").forEach((btn) => btn.addEventListener("click", onToggle));
  ["top", "cards", "rest"].forEach((key) => $(`more-${key}`).addEventListener("click", () => {
    ui.expanded[key] = !ui.expanded[key];
    renderSections();
  }));
  ["lang-fr", "lang-en"].forEach((id) => $(id).addEventListener("click", onLangClick));
  document.addEventListener("keydown", onKeydown);
  window.addEventListener("hashchange", onHashChange);
  window.addEventListener("popstate", onPopState);
  const onMedia = () => {
    const was = ui.desktop;
    ui.desktop = desktop.matches;
    ui.phone = phone.matches;
    $("h-budget").setAttribute("placeholder", t(ui.phone ? "hero.budget_ph_short" : "hero.budget_ph"));
    if (!ui.model) return;
    const focused = document.activeElement;  // the redraw below would drop a focused legend button or row
    renderSaleCard(ui.snapshot);
    renderSections();
    if (focused && focused.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
    else if (focused && focused.getAttribute && focused.getAttribute("data-ct")) {
      const again = $("legend").querySelector(`[data-ct="${focused.getAttribute("data-ct")}"]`);
      if (again) again.focus({ preventScroll: true });
    }
    if (ui.openRef) {
      // The sheet is drawn again for its new place (pane or full-screen view). A lot the page
      // opened by itself in the pane is closed rather than turned into a full-screen view.
      if (was !== ui.desktop) {
        const ref = ui.openRef;
        if (!ui.desktop && ui.autoOpened) {
          finishClose(ref, { restoreFocus: false });
        } else {
          ui.openRef = null;
          placeSheet();
          openLot(ref, { hash: false });
        }
      }
    } else if (ui.desktop) {
      applyHash();
    }
  };
  // Older Safari (≤ 13) has addListener only.
  [desktop, phone].forEach((mq) => (mq.addEventListener ? mq.addEventListener("change", onMedia) : mq.addListener(onMedia)));
  $("h-budget").setAttribute("placeholder", t(ui.phone ? "hero.budget_ph_short" : "hero.budget_ph"));
  if (typeof ResizeObserver === "function") new ResizeObserver(measureBar).observe($("catbar"));
  ui.carry = readCarry();
  langHint();
  renderLangLinks();
  load();
}

if (typeof module !== "undefined" && module.exports) {  // for tests (Node)
  module.exports = { setStrings, t, tn, MISSING, saleStateNow, daysUntil, countdownText, fold, parseHash, prepare,
    filterCars, sortCars, priceRows, priceBasis, basisLine, errorFor, specsLine, safeUrl, displayCase, ctState,
    ctSummary, gridOf, ringBand, ringGeom, waffleOf, bidCost, bidMax, per100, simDefaults, simulate, feeModelOf,
    sourceName, listingTarget, salesOf, featuredSale, saleLine, saleShort, saleTitle, provisionalParts, aiState,
    dataGap, rowSignal, whyText, reasonText, gateText, briefOf, defectLabel, functionCounts, joinChecks, eur, kmText };
}

if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", boot);
