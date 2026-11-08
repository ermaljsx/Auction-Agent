// Renders /api/snapshot as a sale catalogue: one row per lot, a sheet per car.
// Every value from the snapshot (including AI text) is inserted with textContent, never as
// HTML: the data comes from an auction site and a language model. Nothing touches the DOM
// before DOMContentLoaded, so the pure helpers can be loaded and tested with Node.
"use strict";

// ------------------------------------------------------------------ constants and copy

const STALE_DAYS = 8;
const DESKTOP_MQ = "(min-width: 64rem)";
const TIER_WORD = { green: "favorable", yellow: "correct", red: "écarté" };
const TIER_PLURAL = { green: ["favorable", "favorables"], yellow: ["correct", "corrects"], red: ["écarté", "écartés"] };
const CRAWL_FR = {
  ok: "réussie", incomplete: "incomplète", blocked: "refusée par le site source", failed: "échouée",
  no_sale: "sans vente trouvée", skipped: "non relancée", running: "en cours",
};
const SEVERITY_GROUPS = [["critical", "Critiques"], ["major", "Majeures"], ["minor", "Mineures"]];
const CT_RESULT_FR = {
  FAVORABLE: "Favorable",
  DEFAVORABLE_MAJEURE: "Défavorable, défaillances majeures",
  DEFAVORABLE_CRITIQUE: "Défavorable, défaillance critique",
  UNKNOWN: "Résultat illisible",
};
const PARTIAL_COUNT_FR = {
  price_unknown: ["mise à prix non publiée", "mises à prix non publiées"],
  value_unknown: ["prix pas encore comparé (trop peu de voitures comparables)",
    "prix pas encore comparés (trop peu de voitures comparables)"],
  ct_absent: ["voiture sans contrôle technique publié", "voitures sans contrôle technique publié"],
  ct_unreadable: ["contrôle technique illisible", "contrôles techniques illisibles"],
  mileage_unknown: ["kilométrage inconnu", "kilométrages inconnus"],
  mileage_not_guaranteed: ["kilométrage non garanti", "kilométrages non garantis"],
};
const BATTERY_SOURCE_FR = {
  certificate: "certificat batterie publié",
  listing: "annonce",
  lowest: "valeur la plus basse entre l'annonce et le certificat",
};
const ERRORS = {
  no_snapshot: { title: "Aucune analyse publiée pour l'instant",
    text: "La première arrivera après la prochaine collecte, jeudi soir ou samedi matin." },
  not_configured: { title: "Le site n'est pas relié au stockage",
    text: "Reliez le stockage Blob auction-agent-state au projet Vercel, puis redéployez." },
  storage_unavailable: { title: "Le stockage ne répond pas", text: "Réessayez dans quelques minutes.", retry: true },
  storage_error: { title: "Le stockage a refusé la lecture",
    text: "Vérifiez BLOB_READ_WRITE_TOKEN dans les réglages du projet Vercel.", retry: true },
  snapshot_too_large: { title: "L'analyse dépasse 2 Mo", text: "Publiez-la à nouveau depuis le PC." },
  snapshot_unreadable: { title: "L'analyse publiée est illisible", text: "Publiez-la à nouveau depuis le PC." },
  session: { title: "Session expirée", text: "Rechargez la page pour vous reconnecter.", reload: true },
  network: { title: "Pas de connexion", text: "Vérifiez la connexion, puis réessayez.", retry: true },
};
const MINUS = "−";
const NBSP = "\u00a0";  // no-break space between a number and its unit ("11 693 €", "12 %")
const nf = new Intl.NumberFormat("fr-FR");
const nf1 = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
// Engine and gearbox acronyms longer than three letters that stay in capitals ("TFSI", not "Tfsi").
const KEEP_CAPS = new Set(["PHEV", "MHEV", "TFSI", "CRDI", "CDTI", "TDCI", "MIVEC", "VTEC"]);
const TALLY = [
  ["top", "en sélection", "en sélection"],
  ["cards", "autre fiche", "autres fiches"],
  ["rest", "correct sans fiche", "corrects sans fiche"],
  ["red", "écarté", "écartés"],
];

// ------------------------------------------------------------------ pure helpers (Node-tested)

function parisDay(now) {
  return (now || new Date()).toLocaleDateString("en-CA", { timeZone: "Europe/Paris" });
}

function parisHour(now) {
  return Number((now || new Date()).toLocaleString("en-GB", { timeZone: "Europe/Paris", hour: "2-digit", hourCycle: "h23" }));
}

// The sale's state right now (the snapshot's own state is frozen at publish time, and
// publish runs before the sale): Paris calendar date, sale under way from 08:00.
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

function countdownText(sale, now) {
  const state = saleStateNow(sale, now);
  if (state === "over") return "terminée";
  if (state === "in_progress") return "en cours";
  if (state === "today") return "aujourd'hui";
  if (!sale || !sale.date) return "";
  const days = daysUntil(sale.date, now);
  if (days === 1) return "demain";
  return days > 1 ? `dans ${days} jours` : "";
}

function fold(s) {
  return String(s === null || s === undefined ? "" : s).normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

function isNum(n) {
  return typeof n === "number" && isFinite(n);
}

function eur(n) {
  return isNum(n) ? `${nf.format(Math.round(n))}${NBSP}€` : null;
}

function kmText(n) {
  return isNum(n) ? `${nf.format(n)}${NBSP}km` : null;
}

function plural(n, one, many) {
  return `${nf.format(n)} ${Math.abs(n) < 2 ? one : many}`;
}

function capitalise(s) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

function frDate(iso, withYear) {
  if (!iso || !/^\d{4}-\d{2}-\d{2}$/.test(iso)) return null;
  const options = { timeZone: "Europe/Paris", day: "numeric", month: "long" };
  if (withYear !== false) options.year = "numeric";
  return new Date(`${iso}T12:00:00Z`).toLocaleDateString("fr-FR", options);
}

function saleHeading(sale, now) {
  if (!sale) return "Pas de vente cette semaine";
  let text = sale.title || "Vente de la semaine";
  if (sale.date) {
    const options = { timeZone: "Europe/Paris", weekday: "long", day: "numeric", month: "long" };
    if (sale.date.slice(0, 4) !== parisDay(now).slice(0, 4)) options.year = "numeric";
    text = capitalise(new Date(`${sale.date}T12:00:00Z`).toLocaleDateString("fr-FR", options));
    if (sale.room) text += `, ${capitalise(String(sale.room))}`;
  }
  return text;
}

function signed(n) {
  const text = Number.isInteger(n) ? nf.format(Math.abs(n)) : nf1.format(Math.abs(n));
  return `${n < 0 ? MINUS : "+"}${text}`;
}

// "Diesel" -> "diesel", but acronyms stay ("GPL", "Éthanol E85" -> "éthanol E85").
function lowerWords(s) {
  return String(s).split(" ").map((w) => (w.length <= 4 && w === w.toUpperCase() ? w : w.toLowerCase())).join(" ");
}

// The data arrives in capitals ("MINI HATCH 5 PORTES F55 LCI"); shown in readable case. A word with
// a digit, of three letters or fewer, a Roman numeral or a known acronym stays as is ("F55", "LCI",
// "S&S", "T-GDI", "VIII", "(PHEV)"); any other all-capitals word gets a capital and lower case
// ("Hatch"). Brackets and punctuation around a word are kept aside for the tests. Search still uses
// the raw text.
function displayWord(w) {
  const m = /^([^\p{L}\p{N}]*)(.*?)([^\p{L}\p{N}]*)$/su.exec(w || "");
  const core = m ? m[2] : "";
  if (!core || core !== core.toUpperCase() || /\d/.test(core) || KEEP_CAPS.has(core) || /^[IVX]+$/.test(core)) return w;
  if ((core.match(/\p{L}/gu) || []).length <= 3) return w;
  return `${m[1]}${core.toLowerCase().replace(/\p{L}/u, (c) => c.toUpperCase())}${m[3]}`;
}

// Each side of "-", "+" or "/" is a word of its own ("E-Tech", "Qashqai+2", "Auto/BVA").
function displayCase(text) {
  if (text === null || text === undefined) return text;
  return String(text).split(/(\s+)/)
    .map((word) => word.split(/([-+/])/).map((part, i) => (i % 2 ? part : displayWord(part))).join("")).join("");
}

function fuelKind(fuel) {
  const f = fold(fuel);
  if (f === "electrique") return "electric";
  if (f.startsWith("hybride rechargeable")) return "plugin";
  if (f.startsWith("hybride")) return "hybrid";
  return "fuel";
}

// The row's facts, each with the name of its icon.
function factsParts(car) {
  return [
    ["year", car.year],
    ["km", kmText(car.mileage)],
    [fuelKind(car.fuel), car.fuel ? lowerWords(car.fuel) : null],
    ["gearbox", car.gearbox ? lowerWords(car.gearbox) : null],
  ].filter(([, v]) => v !== null && v !== undefined && v !== "").map(([icon, v]) => ({ icon, text: String(v) }));
}

function factsLine(car) {
  return factsParts(car).map((p) => p.text).join(", ");
}

function carRef(car, i) {
  return car && car.lot !== null && car.lot !== undefined && /^[A-Za-z0-9]+$/.test(String(car.lot)) ? String(car.lot) : `x${i}`;
}

function parseHash(hash) {
  const m = /^#lot-([A-Za-z0-9]+)$/.exec(hash || "");
  return m ? m[1] : null;
}

function wordsOf(car) {
  return fold([car.title, car.subtitle, car.trim, car.fuel, car.gearbox].filter(Boolean).join(" "))
    .split(/[^a-z0-9]+/).filter(Boolean);
}

function ctKind(car) {
  const result = car.ct_result || (car.detail && car.detail.ct && car.detail.ct.result) || null;
  if (result === "FAVORABLE") return "fav";
  if (result === "DEFAVORABLE_MAJEURE" || result === "DEFAVORABLE_CRITIQUE") return "defav";
  if (!result && typeof car.ct === "string") {  // schema 1: only a sentence
    if (/^CT favorable/.test(car.ct)) return "fav";
    if (/^CT défavorable/.test(car.ct)) return "defav";
  }
  return "none";
}

// The colour of a CT line: "good" (favorable, no defect), "ok" (favorable, minor defects),
// "risk" (unfavourable, even a voluntary CT on a car too young to need one), "watch" (absent or
// unreadable), "neutral" (not required yet, and no unfavourable result).
function ctTone(car) {
  const line = car.ct_line || "";
  const kind = ctKind(car);
  if (kind === "defav") return "risk";
  if (/pas encore exigé/.test(line)) return "neutral";
  if (kind !== "fav") return "watch";
  const c = car.ct_counts;
  const n = c && typeof c === "object" ? (c.minor || 0) + (c.major || 0) + (c.critical || 0)
    : (/sans défaillance/.test(line || car.ct || "") ? 0 : 1);
  return n ? "ok" : "good";
}

function ctResultTone(ct) {
  if (!ct) return "watch";
  if (ct.result === "FAVORABLE") return Array.isArray(ct.defects) && ct.defects.length ? "ok" : "good";
  if (ct.result === "DEFAVORABLE_MAJEURE" || ct.result === "DEFAVORABLE_CRITIQUE") return "risk";
  return "watch";
}

// The sale's lots by where they appear on the page, for the masthead tally (zero parts left out).
// The totals by tier come first: they count every lot, even when the one-line list was cut or
// dropped from a snapshot that grew too large (detailed cars are never red).
function tallyParts(s) {
  const snap = s && typeof s === "object" ? s : {};
  const c = snap.counts && typeof snap.counts === "object" ? snap.counts : {};
  const len = (x) => (Array.isArray(x) ? x.filter((v) => v && typeof v === "object").length : 0);
  const top = len(snap.top);
  const cards = len(snap.cards);
  let rest;
  let red;
  if (isNum(c.green) && isNum(c.yellow) && isNum(c.red)) {
    rest = Math.max(0, c.green + c.yellow - top - cards);
    red = c.red;
  } else if (c.compact && typeof c.compact === "object") {
    rest = (c.compact.green || 0) + (c.compact.yellow || 0);
    red = c.compact.red || 0;
  } else {
    const compact = Array.isArray(snap.compact) ? snap.compact : [];
    red = compact.filter((x) => x && x.tier === "red").length;
    rest = len(compact) - red;
  }
  const n = { top, cards, rest, red };
  return TALLY.filter(([key]) => isNum(n[key]) && n[key] > 0)
    .map(([key, one, many]) => ({ key, n: n[key], label: n[key] < 2 ? one : many }));
}

function tierOf(v, t1, t2) {
  if (!isNum(v)) return null;
  return v >= t2 ? "green" : v >= t1 ? "yellow" : "red";
}

// A point on the gauge's half circle (0 at the left, 100 at the right), in the 64 x 38 viewBox.
function gaugePoint(v, r) {
  const a = Math.PI * (1 - Math.max(0, Math.min(100, v)) / 100);
  return [Math.round((32 + r * Math.cos(a)) * 100) / 100, Math.round((34 - r * Math.sin(a)) * 100) / 100];
}

function prepare(s) {
  const snap = s && typeof s === "object" ? s : {};
  const cars = [];
  const compact = [];
  const byRef = new Map();
  let i = 0;
  const add = (list, section, into) => {
    (Array.isArray(list) ? list : []).forEach((c) => {
      if (!c || typeof c !== "object") return;
      let ref = carRef(c, i);
      if (byRef.has(ref)) ref = `x${i}`;
      i += 1;
      const item = Object.assign({}, c, { ref, section: section || (c.tier === "red" ? "red" : "yellow"), words: wordsOf(c) });
      byRef.set(ref, item);
      into.push(item);
    });
  };
  add(snap.top, "top", cars);
  add(snap.cards, "cards", cars);
  add(snap.compact, null, compact);
  const all = cars.concat(compact);
  const uniq = (key) => [...new Set(all.map((c) => c[key]).filter((v) => v !== null && v !== undefined && v !== ""))];
  return {
    cars,
    compact,
    byRef,
    fuels: uniq("fuel").sort((a, b) => fold(a).localeCompare(fold(b))),
    gearboxes: uniq("gearbox").sort(),
    years: uniq("year").filter(isNum).sort((a, b) => b - a),
    hasDetail: cars.some((c) => c.detail && typeof c.detail === "object"),
    hasCompact: Array.isArray(snap.compact),
    someComplete: cars.some((c) => c.confidence === "full"),
  };
}

const LOT_WORDS = new Set(["lot", "lots", "n", "no", "num", "numero"]);

// With a number in the query, "lot" and "n°" only introduce it: "Lot 751" and "n°751" find lot 751.
function queryTokens(q) {
  const tokens = fold(q || "").split(/[^a-z0-9]+/).filter(Boolean);
  return tokens.some((t) => /^\d+$/.test(t)) ? tokens.filter((t) => !LOT_WORDS.has(t)) : tokens;
}

function matchesQuery(car, tokens) {
  const words = car.words || wordsOf(car);
  const lot = car.lot === null || car.lot === undefined ? "" : String(car.lot);
  return tokens.every((t) => words.some((w) => w.startsWith(t)) || (/^\d+$/.test(t) && lot.startsWith(t)));
}

// true, false, or "noprice" (would match, but a budget is set and the price is unknown)
function matchesFilters(car, f, tokens) {
  if (tokens.length && !matchesQuery(car, tokens)) return false;
  if (f.fuel && car.fuel !== f.fuel) return false;
  if (f.gearbox && car.gearbox !== f.gearbox) return false;
  if (f.km && !(isNum(car.mileage) && car.mileage < f.km)) return false;
  if (f.year && !(isNum(car.year) && car.year >= f.year)) return false;
  if (f.ct && ctKind(car) !== f.ct) return false;
  if (f.tier && car.tier !== f.tier) return false;
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

// The price table, rounded to the euro so the column adds up exactly to the total.
function priceRows(price) {
  if (!price || !isNum(price.total) || !isNum(price.start)) return null;
  const r = (x) => (isNum(x) ? Math.round(x) : 0);
  const rows = [{ key: "start", label: price.basis === "bid" ? "Enchère courante" : "Mise à prix", value: r(price.start) }];
  if (r(price.vat)) rows.push({ key: "vat", label: `TVA non récupérable (20${NBSP}%)`, value: r(price.vat) });
  rows.push({ key: "fees", label: "Frais acheteur", value: r(price.fees) });
  rows.push({ key: "repairs", label: "Réparations estimées d'après le CT", value: r(price.repairs) });
  if (r(price.ct_redo)) rows.push({ key: "ct_redo", label: "CT à refaire avant revente", value: r(price.ct_redo) });
  const total = r(price.total);
  const diff = total - rows.reduce((sum, row) => sum + row.value, 0);
  if (diff) (rows.find((row) => row.key === "fees" && row.value) || rows[0]).value += diff;
  return { rows, total };
}

function errorFor(status, httpStatus) {
  if (httpStatus === 401 || httpStatus === 403) return Object.assign({ retry: false, reload: true }, ERRORS.session);
  const known = Object.prototype.hasOwnProperty.call(ERRORS, status) ? ERRORS[status] : null;
  const base = known || { title: `Analyse indisponible (code ${httpStatus || status || "inconnu"})`,
    text: "Réessayez dans quelques minutes.", retry: true };
  return Object.assign({ retry: false, reload: false }, base);
}

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

function ageText(iso, now) {
  const hours = ((now || new Date()).getTime() - new Date(iso).getTime()) / 3.6e6;
  if (!isFinite(hours)) return "";
  if (hours < 1) return "il y a moins d'une heure";
  if (hours < 48) return `il y a ${Math.round(hours)} h`;
  return `il y a ${Math.round(hours / 24)} jours`;
}

// What is missing, most frequent first ("572 prix pas encore comparés (…)", …).
function partialParts(s, limit) {
  const why = (s.counts && s.counts.partial_why) || {};
  return Object.keys(PARTIAL_COUNT_FR).filter((k) => why[k] > 0)
    .sort((a, b) => why[b] - why[a]).slice(0, limit || undefined)
    .map((k) => plural(why[k], PARTIAL_COUNT_FR[k][0], PARTIAL_COUNT_FR[k][1]));
}

function partialLine(s) {
  const parts = partialParts(s, 3);
  const partial = (s.counts && s.counts.partial) || 0;
  if (s.provisional) {
    return `Classement provisoire : ${parts.length ? parts.join(", ") : "aucune voiture n'a encore toutes ses données"}.`;
  }
  if (partial > 0) return `Données incomplètes pour ${plural(partial, "lot", "lots")}${parts.length ? ` : ${parts.join(", ")}` : ""}.`;
  return "";
}

// The same note in one line, for a phone (the details are in the analysis status).
function partialShort(s) {
  const partial = (s.counts && s.counts.partial) || 0;
  if (s.provisional) return "Classement provisoire : données manquantes.";
  return partial > 0 ? `Données incomplètes pour ${plural(partial, "lot", "lots")}.` : "";
}

// ------------------------------------------------------------------ DOM helpers

const ATTRS = new Set(["id", "class", "href", "rel", "target", "type", "role", "hidden", "tabindex", "for", "scope", "colspan"]);

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
  (children || []).forEach((c) => {
    if (c === null || c === undefined || c === false || c === "") return;
    node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  });
  return node;
}

// SVG drawn by the page itself (icons, gauge): fixed shapes, presentation attributes only.
const SVG_NS = "http://www.w3.org/2000/svg";
const SVG_ATTRS = new Set(["viewBox", "d", "cx", "cy", "r", "x", "y", "x1", "y1", "x2", "y2", "width", "height", "rx",
  "points", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "class", "aria-hidden", "focusable"]);

function svg(tag, attrs, children) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs || {}).forEach(([k, v]) => {
    if (v !== null && v !== undefined && v !== false && SVG_ATTRS.has(k)) node.setAttribute(k, String(v));
  });
  (children || []).forEach((c) => { if (c) node.appendChild(c); });
  return node;
}

const PUMP = "M2.5 13.5V3.5a1 1 0 0 1 1-1h5a1 1 0 0 1 1 1v10M1.5 13.5h9M2.5 6.5h7";
const SHIELD = "M8 1.5l5.5 2v4.25c0 3.2-2.3 5.6-5.5 6.75-3.2-1.15-5.5-3.55-5.5-6.75V3.5z";
const ICONS = {
  year: [["rect", { x: 2.5, y: 3.5, width: 11, height: 10, rx: 1.5 }], ["path", { d: "M2.5 6.5h11M5.5 2v3M10.5 2v3" }]],
  km: [["path", { d: "M2.5 12.5a5.5 5.5 0 1 1 11 0" }], ["path", { d: "M8 12.5l2.75-3.5" }], ["circle", { cx: 8, cy: 12.5, r: 1 }]],
  fuel: [["path", { d: PUMP }], ["path", { d: "M9.5 5.5h1.5a1 1 0 0 1 1 1v4a1 1 0 0 0 2 0V5.25L12.5 3.5" }]],
  hybrid: [["path", { d: PUMP }], ["path", { d: "M10.5 9c0-3.6 1.6-5.5 4-5.5 0 3.6-1.6 5.5-4 5.5zM10.5 9l2.4-2.6" }]],
  plugin: [["path", { d: PUMP }], ["path", { d: "M11.25 3v1.75M13.75 3v1.75M10.5 4.75h4v1a2 2 0 0 1-4 0zM12.5 7.75v2.75" }]],
  electric: [["path", { d: "M5.75 1.75v3M10.25 1.75v3M3.75 4.75h8.5v2.5a4.25 4.25 0 0 1-8.5 0zM8 11.5v2.75" }]],
  gearbox: [["path", { d: "M3.5 4.5v8M8 4.5v8M12.5 4.5v4M3.5 8.5h9" }], ["circle", { cx: 3.5, cy: 3, r: 1.5 }],
    ["circle", { cx: 8, cy: 3, r: 1.5 }], ["circle", { cx: 12.5, cy: 3, r: 1.5 }]],
  chevron: [["path", { d: "M6 3.5 10.5 8 6 12.5" }]],
  shield_good: [["path", { d: SHIELD }], ["path", { d: "M5.5 8.25l1.75 1.75 3.25-3.5" }]],
  shield_risk: [["path", { d: SHIELD }], ["path", { d: "M6 6l4 4M10 6l-4 4" }]],
  shield_watch: [["path", { d: SHIELD }], ["path", { d: "M8 5v3.25M8 10.75v.25" }]],
  shield_neutral: [["path", { d: SHIELD }], ["path", { d: "M5.75 8.25h4.5" }]],
};

function icon(name, cls) {
  return svg("svg", { viewBox: "0 0 16 16", width: 16, height: 16, fill: "none", stroke: "currentColor", "stroke-width": 1.5,
    "stroke-linecap": "round", "stroke-linejoin": "round", class: cls ? `ico ${cls}` : "ico", "aria-hidden": "true", focusable: "false" },
  (ICONS[name] || []).map(([tag, a]) => svg(tag, a)));
}

function shield(tone, cls) {
  return icon(`shield_${tone === "ok" ? "good" : tone}`, cls);
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

function $(id) {
  return document.getElementById(id);
}

function extLink(href, label, cls) {
  const url = safeUrl(href);
  if (!url) return null;
  return h("a", { cls, attrs: { href: url, target: "_blank", rel: "noopener noreferrer nofollow" } },
    [label, h("span", { cls: "ext", text: " (nouvel onglet)" })]);
}

function num(text, cls) {
  return h("span", { cls: cls ? `num ${cls}` : "num", text });
}

function setVar(node, name, value) {
  node.style.setProperty(name, String(value));  // CSSOM: allowed by the CSP, unlike style=""
}

// The lot number as a small French number plate (the blue band is decoration).
function plate(car, size) {
  const lot = car.lot === null || car.lot === undefined ? null : String(car.lot);
  return h("span", { cls: size ? `plate plate-${size}` : "plate" }, [
    h("span", { cls: "plate-band", attrs: { "aria-hidden": "true" } }),
    h("span", { cls: "plate-no" }, [h("span", { cls: "sr", text: lot ? "Lot " : "Lot non numéroté" }), lot || ""]),
  ]);
}

function thresholds() {
  const sc = (ui.snapshot && ui.snapshot.scoring) || {};
  return [isNum(sc.yellow_min) ? sc.yellow_min : 60, isNum(sc.green_min) ? sc.green_min : 80];
}

// The score as a speedometer arc: track, fill to the score, ticks at the two thresholds.
// The arc is decoration; the number and the tier word next to it carry the meaning.
function gaugeArc(score, size) {
  const lg = size === "lg";
  const sw = lg ? 4.5 : 5;
  const r = 26;
  const arc = (v) => { const [x, y] = gaugePoint(v, r); return `M6 34A${r} ${r} 0 0 1 ${x} ${y}`; };
  const parts = [svg("path", { d: arc(100), class: "g-track", "stroke-width": sw })];
  if (isNum(score) && score > 0) parts.push(svg("path", { d: arc(score), class: "g-fill", "stroke-width": sw }));
  thresholds().forEach((t) => {
    const [x1, y1] = gaugePoint(t, r + sw / 2 + 1.25);
    const [x2, y2] = gaugePoint(t, r + sw / 2 + 4.25);
    parts.push(svg("line", { x1, y1, x2, y2, class: "g-tick" }));
  });
  return svg("svg", { viewBox: "0 0 64 38", width: lg ? 132 : 80, height: lg ? 78 : 48, fill: "none",
    "stroke-linecap": "round", class: "gauge-arc", "aria-hidden": "true", focusable: "false" }, parts);
}

function gauge(car, size) {
  const tier = TIER_WORD[car.tier] ? car.tier : "yellow";
  const score = isNum(car.score) ? car.score : null;
  return h("div", { cls: `gauge gauge-${size || "sm"} tier-${tier}` }, [
    gaugeArc(score, size),
    h("span", { cls: "gauge-val" }, [num(score === null ? "–" : String(score)),
      h("span", { cls: "gauge-of", text: "/100", attrs: { "aria-hidden": "true" } })]),
    h("span", { cls: "sr", text: " sur 100, profil " }),
    h("span", { cls: "tier", text: TIER_WORD[car.tier] || car.tier || "" }),
  ]);
}

// A horizontal bar for a value out of 100, coloured by the thresholds (decoration: the number is text).
function meter(v, cls) {
  const [t1, t2] = thresholds();
  const known = isNum(v);
  const bar = h("span", { cls: `meter${known ? ` tier-${tierOf(v, t1, t2)}` : " is-unknown"}${cls ? ` ${cls}` : ""}` },
    known ? [h("span", { cls: "meter-fill" })] : []);
  if (known) setVar(bar, "--v", Math.max(0, Math.min(100, v)));
  return bar;
}

// ------------------------------------------------------------------ state

const ui = {
  snapshot: null,
  model: null,
  openRef: null,
  desktop: false,
  rows: new Map(),
  crows: new Map(),
  sheet: null,
  timers: {},
  countText: "",
  hiddenOpen: false,
  lastQ: null,  // the search text of the last render (groups open by themselves when it changes)
};

function reducedMotion() {
  return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// ------------------------------------------------------------------ masthead, notices, status

function renderMasthead(s) {
  const now = new Date();
  $("sale-title").textContent = saleHeading(s.sale, now);
  const countdown = $("countdown");
  const left = s.sale ? countdownText(s.sale, now) : "";
  countdown.textContent = left;
  countdown.hidden = !left;
  const line = $("sale-line");
  const c = s.counts || {};
  if (!s.sale) {
    line.textContent = "Aucune vente du lundi n'a été trouvée pour l'instant. La prochaine analyse est prévue jeudi soir ou samedi matin.";
  } else {
    const parts = [];
    if (c.green > 0) parts.push(plural(c.green, ...TIER_PLURAL.green));
    parts.push(plural(c.yellow || 0, ...TIER_PLURAL.yellow));
    parts.push(plural(c.red || 0, ...TIER_PLURAL.red));
    line.textContent = `${parts.join(", ")}.`;
  }
  // The tally replaces the sentence (its legend is the same numbers, by section of the page).
  line.hidden = renderTally(s);
  // The full note on a wide screen, one line on a phone (CSS shows one of the two).
  const provisional = clear($("provisional"));
  const full = s.sale ? partialLine(s) : "";
  if (full) {
    provisional.appendChild(h("span", { cls: "prov-full", text: full }));
    provisional.appendChild(h("span", { cls: "prov-short", text: partialShort(s) || full }));
  }
  provisional.hidden = !full;
}

// One stacked bar for every lot of the sale; the legend under it is the accessible text.
function renderTally(s) {
  const parts = s.sale ? tallyParts(s) : [];
  const bar = clear($("tally-bar"));
  const legend = clear($("tally-legend"));
  parts.forEach((p) => {
    const seg = h("span", { cls: `tally-seg seg-${p.key}` });
    setVar(seg, "--w", p.n);
    bar.appendChild(seg);
    legend.appendChild(h("li", { cls: `seg-${p.key}` }, [h("span", { cls: "sw", attrs: { "aria-hidden": "true" } }),
      num(nf.format(p.n)), ` ${p.label}`]));
  });
  $("tally").hidden = !parts.length;
  return parts.length > 0;
}

function notice(kind, lead, text) {
  return h("p", { cls: `notice notice-${kind}` }, [h("strong", { text: lead }), ` ${text}`]);
}

function renderBanners(s) {
  const box = clear($("banners"));
  const now = new Date();
  const crawl = s.crawl || {};
  const counts = crawl.counts || {};
  const generated = new Date(s.generated_at).getTime();
  if (isFinite(generated) && now.getTime() - generated > STALE_DAYS * 864e5) {
    box.appendChild(notice("risk", "Données anciennes.", `Dernière publication ${ageText(s.generated_at, now)} : l'ordinateur qui fait l'analyse était peut-être éteint.`));
  }
  if (crawl.status === "blocked") box.appendChild(notice("risk", "Collecte refusée par le site source.", "Les voitures affichées viennent de la dernière collecte réussie."));
  if (crawl.status === "failed") box.appendChild(notice("risk", "La dernière collecte a échoué.", "Les voitures affichées viennent de la dernière collecte réussie."));
  if (crawl.status === "incomplete") {
    const read = isNum(counts.lots_seen) && isNum(counts.expected_lots) ? ` : ${nf.format(counts.lots_seen)} lots lus sur ${nf.format(counts.expected_lots)}` : "";
    box.appendChild(notice("watch", "Collecte incomplète.", `Certains lots manquent peut-être${read}.`));
  }
  if (crawl.status === "no_sale" && s.sale) box.appendChild(notice("watch", "Pas de vente trouvée.", "Aucune vente du lundi n'a été trouvée cette semaine pour l'instant."));
  const state = saleStateNow(s.sale, now);
  if (state === "over") box.appendChild(notice("watch", "Vente terminée.", "La prochaine analyse est prévue jeudi soir."));
  if (state === "in_progress") box.appendChild(notice("watch", "Vente en cours.", "Certains lots sont peut-être déjà adjugés."));
  const llm = s.llm || {};
  if (llm.enabled && llm.error) {
    box.appendChild(notice("watch", "Avis IA interrompus.", llm.error === "config"
      ? "Un réglage de l'IA est à corriger sur le PC ; les notes viennent des règles seules."
      : "Le service d'IA était indisponible ; certains lots n'ont pas d'avis."));
  }
}

function llmSummary(llm) {
  if (!llm.enabled) return "IA désactivée";
  if (llm.error) return "IA interrompue";
  const cov = llm.coverage || {};
  return isNum(cov.listed) && cov.listed ? `avis IA sur ${nf.format(cov.with_verdict || 0)} des ${plural(cov.listed, "lot", "lots")} détaillés` : "IA activée";
}

function renderStatus(s) {
  const now = new Date();
  const crawl = s.crawl || {};
  const counts = crawl.counts || {};
  const llm = s.llm || {};
  const when = new Date(s.generated_at);
  const whenText = isFinite(when.getTime())
    ? `${when.toLocaleDateString("fr-FR", { timeZone: "Europe/Paris", weekday: "long", day: "numeric", month: "long" })} à ${when.toLocaleTimeString("fr-FR", { timeZone: "Europe/Paris", hour: "2-digit", minute: "2-digit" })} (${ageText(s.generated_at, now)})`
    : "à une date inconnue";
  $("status-summary").textContent = `Analyse publiée ${whenText}, collecte ${CRAWL_FR[crawl.status] || "inconnue"}, ${llmSummary(llm)}.`;
  const dl = clear($("status-list"));
  const row = (label, value) => {
    if (value === null || value === undefined || value === "") return;
    dl.appendChild(h("dt", { text: label }));
    dl.appendChild(typeof value === "string" ? h("dd", { text: value }) : h("dd", {}, [value]));
  };
  row("Collecte", CRAWL_FR[crawl.status] || crawl.status || null);
  if (isNum(counts.lots_seen)) {
    const pct = isNum(counts.expected_lots) && counts.expected_lots ? ` (${Math.round((100 * counts.lots_seen) / counts.expected_lots)}${NBSP}%)` : "";
    row("Lots lus", `${nf.format(counts.lots_seen)} sur ${isNum(counts.expected_lots) ? nf.format(counts.expected_lots) : "?"}${pct}`);
  }
  if (isNum(counts.detailed)) row("Fiches et CT", `${plural(counts.detailed, "fiche lue", "fiches lues")}, ${plural(counts.documents || 0, "document lu", "documents lus")}`);
  const c = s.counts || {};
  const scored = (c.green || 0) + (c.yellow || 0) + (c.red || 0);
  row("Voitures notées", `${nf.format(scored)}, dont ${nf.format(c.partial || 0)} aux données incomplètes`);
  if (c.partial > 0) row("Données manquantes", partialParts(s).join(", "));
  let ai = "désactivée pour cette publication";
  if (llm.enabled) {
    const models = Array.isArray(llm.models_answered) && llm.models_answered.length ? llm.models_answered.join(", ") : llm.model;
    const bits = [models, `${plural(llm.calls || 0, "appel", "appels")}`, `${plural(llm.cached || 0, "avis réutilisé", "avis réutilisés")}`];
    const cov = llm.coverage || {};
    if (isNum(cov.listed)) bits.push(`avis sur ${nf.format(cov.with_verdict || 0)} des ${nf.format(cov.listed)} lots détaillés`);
    if (isNum(llm.cost_usd) && llm.calls) bits.push(`environ ${new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 3 }).format(llm.cost_usd)}${NBSP}$`);
    if (llm.error) bits.push(`interrompue (${llm.error === "config" ? "configuration" : "erreur temporaire"})`);
    ai = bits.filter(Boolean).join(", ");
  }
  row("Avis IA", ai);
  const sc = s.scoring || {};
  if (isNum(sc.green_min) && isNum(sc.yellow_min)) row("Seuils", `favorable à partir de ${sc.green_min}, correct à partir de ${sc.yellow_min}`);
  if (s.sale) row("Catalogue", extLink(s.sale.url, "Catalogue de la vente sur Alcopa"));
  $("status").hidden = false;
}

function fillSelect(select, values) {
  while (select.options.length > 1) select.remove(1);
  values.forEach((v) => {
    const option = document.createElement("option");
    option.value = String(v);
    option.textContent = String(v);
    select.appendChild(option);
  });
}

function renderFilterOptions(model) {
  fillSelect($("f-fuel"), model.fuels);
  fillSelect($("f-gearbox"), model.gearboxes);
  fillSelect($("f-year"), model.years);
}

// ------------------------------------------------------------------ catalogue rows

function ctLineOf(car) {
  const counts = car.ct_counts || {};
  const n = (counts.major || 0) + (counts.critical || 0);
  // A car too young to need a CT, whose voluntary CT still failed: the line says so, like its colour.
  if (car.ct_line && /pas encore exigé/.test(car.ct_line) && ctKind(car) === "defav") {
    return `CT volontaire défavorable${n ? `, ${plural(n, "défaillance", "défaillances")}` : ""} (pas encore exigé)`;
  }
  if (car.ct_line) return car.ct_line;
  if (!car.ct) return "Pas de CT publié";
  return n ? `${car.ct}, ${plural(n, "défaillance", "défaillances")}` : car.ct;
}

function subtitleOf(car) {
  if (car.subtitle !== undefined) return car.subtitle;
  return car.trim && car.trim !== car.title ? car.trim : null;
}

function tierClass(car) {
  return `tier-${TIER_WORD[car.tier] ? car.tier : "yellow"}`;
}

// Year, km, fuel and gearbox, each after its icon (commas for screen readers only).
function factsNode(car) {
  const parts = factsParts(car);
  if (!parts.length) return h("p", { cls: "lot-facts", text: "Caractéristiques non publiées" });
  const kids = [];
  parts.forEach((p, i) => {
    if (i) kids.push(h("span", { cls: "sr", text: ", " }));
    kids.push(h("span", { cls: "fact" }, [icon(p.icon), p.text]));
  });
  return h("p", { cls: "lot-facts" }, kids);
}

function lotRow(car) {
  const sub = subtitleOf(car);
  const btn = h("button", {
    cls: "lot-btn",
    attrs: { type: "button", "aria-expanded": "false", "aria-controls": "sheet", "data-ref": car.ref },
    on: { click: () => toggleLot(car.ref) },
  }, [plate(car), h("span", { cls: "lot-name", text: displayCase(car.title) || "Véhicule" }),
    sub ? h("span", { cls: "lot-sub", text: displayCase(sub) }) : null]);
  const tone = ctTone(car);
  const price = eur(car.effective_price);
  const row = h("div", { cls: "row" }, [
    h("h3", {}, [btn]),
    factsNode(car),
    h("p", { cls: `lot-ct ct-${tone}` }, [shield(tone), ctLineOf(car)]),
    ui.model && ui.model.someComplete && car.confidence === "partial" ? h("p", { cls: "lot-flag", text: "Données incomplètes" }) : null,
    h("p", { cls: "lot-price" }, price ? [num(price), " ", h("span", { cls: "nw", text: "tout compris" })] : ["Mise à prix pas encore publiée"]),
    h("div", { cls: "lot-score" }, [gauge(car, "sm")]),
    h("span", { cls: "lot-open", attrs: { "aria-hidden": "true" } }, [h("span", { cls: "lot-open-label", text: "Voir la fiche" }), icon("chevron")]),
  ]);
  return h("li", { cls: `lot ${tierClass(car)}`, attrs: { id: `lot-${car.ref}` } }, [row]);
}

function compactRow(car) {
  const facts = [car.year, kmText(car.mileage), car.fuel ? lowerWords(car.fuel) : null].filter(Boolean).join(", ");
  const price = eur(car.effective_price);
  const link = extLink(car.url, "Annonce");
  return h("li", { cls: `clot ${tierClass(car)}`, attrs: { id: `lot-${car.ref}`, tabindex: link ? null : "-1" } }, [
    plate(car, "sm"),
    h("p", { cls: "clot-main" }, [h("span", { cls: "clot-title", text: displayCase(car.title) || "Véhicule" }), facts ? `, ${facts}` : "",
      price ? ", " : "", price ? h("span", { cls: "nw", text: `${price} tout compris` }) : null]),
    h("p", { cls: "clot-score" }, [num(isNum(car.score) ? String(car.score) : "–"), h("span", { cls: "sr", text: ` sur 100, ${TIER_WORD[car.tier] || ""}` })]),
    car.why ? h("p", { cls: "clot-why", text: car.why }) : null,
    link ? h("p", { cls: "clot-link" }, [link]) : null,
  ]);
}

function rowFor(car) {
  if (!ui.rows.has(car.ref)) ui.rows.set(car.ref, lotRow(car));
  return ui.rows.get(car.ref);
}

function compactFor(car) {
  if (!ui.crows.has(car.ref)) ui.crows.set(car.ref, compactRow(car));
  return ui.crows.get(car.ref);
}

// Only nodes out of place are moved: moving a node takes the focus away from it.
function fillList(list, nodes) {
  const want = new Set(nodes);
  Array.from(list.children).forEach((child) => { if (!want.has(child)) list.removeChild(child); });
  nodes.forEach((node, i) => {
    const at = list.children[i] || null;
    if (at !== node) list.insertBefore(node, at);
  });
}

// ------------------------------------------------------------------ filters

function readFilters() {
  const n = (id) => { const v = parseFloat($(id).value); return isFinite(v) && v > 0 ? v : null; };
  return {
    q: $("f-q").value.trim(),
    budget: n("f-budget"),
    fuel: $("f-fuel").value,
    gearbox: $("f-gearbox").value,
    km: n("f-km"),
    year: n("f-year"),
    ct: $("f-ct").value,
    tier: $("f-tier").value,
    sort: $("f-sort").value || "score",
  };
}

function activeExtra(f) {
  return ["budget", "fuel", "gearbox", "km", "year", "ct", "tier"].filter((k) => f[k]).length;
}

function renderSections() {
  const m = ui.model;
  if (!m) return;
  const s = ui.snapshot;
  const f = readFilters();
  const top = filterCars(m.cars.filter((c) => c.section === "top"), f);
  const cards = filterCars(m.cars.filter((c) => c.section === "cards"), f);
  const rest = filterCars(m.compact, f);
  fillList($("top-list"), sortCars(top.shown, f.sort).map(rowFor));
  fillList($("cards-list"), sortCars(cards.shown, f.sort).map(rowFor));
  $("n-top").textContent = top.shown.length ? nf.format(top.shown.length) : "";
  $("n-cards").textContent = cards.shown.length ? nf.format(cards.shown.length) : "";
  const anyTop = m.cars.some((c) => c.section === "top");
  const anyCards = m.cars.some((c) => c.section === "cards");
  const yMin = (s.scoring && s.scoring.yellow_min) || 60;
  // Filters that match nothing leave a single message (and its reset button), no empty sections.
  const filtered = Boolean(f.q) || activeExtra(f) > 0;
  const shownCards = top.shown.length + cards.shown.length;
  const nothing = filtered && shownCards + rest.shown.length === 0 && m.cars.length + m.compact.length > 0;
  // Groups with matches open by themselves when the search changes, not at every render
  // (closing one fires "toggle", which renders again).
  const newQuery = f.q !== ui.lastQ;
  ui.lastQ = f.q;
  const noteTop = $("note-top");
  if (!anyTop && !anyCards) {
    noteTop.textContent = s.sale ? `Aucune voiture correcte dans cette vente : ${plural(m.compact.length, "lot est écarté", "lots sont écartés")}.` : "";
  } else if (!anyTop) {
    noteTop.textContent = "Aucune voiture à mettre en avant pour l'instant.";
  } else if (!top.shown.length) {
    noteTop.textContent = "Aucune voiture de la sélection ne correspond à ces filtres.";
  } else {
    noteTop.textContent = s.provisional ? "Sélection provisoire : les mieux notées, en attendant les données manquantes." : "Les mieux notées de la vente, aux données complètes.";
  }
  $("sec-cards").hidden = !cards.shown.length;
  $("note-cards").textContent = `Notées ${yMin} ou plus, sans défaut bloquant.`;

  // The other lots: one line each, rendered only when their group is open (or searched).
  const other = $("sec-other");
  const yellow = rest.shown.filter((c) => c.tier !== "red");
  const red = rest.shown.filter((c) => c.tier === "red");
  if (m.hasCompact) {
    // Lines dropped from a snapshot that grew too large are still said (the masthead counts them).
    const dropped = (s.counts && isNum(s.counts.compact_dropped) && s.counts.compact_dropped) || 0;
    other.hidden = !(m.compact.length || dropped) || nothing;
    const droppedText = `${plural(dropped, "lot n'est pas affiché", "lots ne sont pas affichés")} : l'analyse était trop volumineuse.`;
    $("note-other").textContent = !dropped ? "Une ligne par lot, avec le motif principal de sa note."
      : m.compact.length ? `Une ligne par lot. ${droppedText}` : droppedText;
    [["g-yellow", yellow, m.compact.some((c) => c.tier !== "red")], ["g-red", red, m.compact.some((c) => c.tier === "red")]]
      .forEach(([id, items, exists]) => {
        const group = $(id);
        group.hidden = !exists;
        $(`n-${id}`).textContent = `(${nf.format(items.length)})`;
        if (newQuery && f.q && items.length && !group.open) group.open = true;
        fillList($(`${id}-list`), group.open ? sortCars(items, "lot").map(compactFor) : []);
      });
    if (!anyTop && !anyCards && !f.q && !ui.redOpened) {
      ui.redOpened = true;
      $("g-red").open = true;
      fillList($("g-red-list"), sortCars(red, "lot").map(compactFor));
    }
  } else {
    const n = (s.counts && s.counts.red) || 0;
    other.hidden = !n;
    $("note-other").textContent = n ? `${plural(n, "véhicule à risque élevé n'est", "véhicules à risque élevé ne sont")} pas listés.` : "";
    $("g-yellow").hidden = true;
    $("g-red").hidden = true;
  }

  $("no-match").hidden = !nothing;
  $("sec-top").hidden = nothing || !s.sale;
  const noPrice = top.hiddenNoPrice + cards.hiddenNoPrice + rest.hiddenNoPrice;
  let count = `${plural(shownCards, "lot avec fiche", "lots avec fiche")}`;
  if (m.hasCompact) count += `, ${nf.format(rest.shown.length)} sans fiche`;
  if (noPrice) count += ` (${plural(noPrice, "sans prix masqué", "sans prix masqués")})`;
  ui.countText = `${count}.`;
  const extra = activeExtra(f);
  $("f-toggle").textContent = extra ? `Filtres (${extra})` : "Filtres";
  $("f-reset").hidden = !(filtered || f.sort !== "score");

  // A filter hid the open lot: close it on phone; on desktop keep it, and say so.
  if (ui.openRef) {
    const visible = [...top.shown, ...cards.shown].some((c) => c.ref === ui.openRef);
    if (!visible && !ui.desktop) closeSheet({ restoreFocus: false });
    else setMasked(!visible);
  }
}

function announceCount(now) {
  clearTimeout(ui.timers.count);
  const apply = () => { $("count").textContent = ui.countText; };
  if (now) apply(); else ui.timers.count = setTimeout(apply, 400);
}

function onFilterInput() {
  clearTimeout(ui.timers.render);
  ui.timers.render = setTimeout(() => { renderSections(); announceCount(false); }, 120);
}

function onReset() {
  $("filters").reset();
  renderSections();
  announceCount(true);
  $("f-q").focus();
}

function measureBar() {
  setVar(document.documentElement, "--bar-h", `${$("filters").offsetHeight}px`);
}

function setMore(open) {
  $("f-more").classList.toggle("is-open", open);
  $("f-toggle").setAttribute("aria-expanded", open ? "true" : "false");
  measureBar();
}

function moreOpen() {
  return $("f-more").classList.contains("is-open") && getComputedStyle($("f-toggle")).display !== "none";
}

// ------------------------------------------------------------------ the sheet

function sheetSection(title, children, cls) {
  const items = (children || []).filter(Boolean);
  if (!items.length) return null;
  return h("div", { cls: cls ? `sheet-part ${cls}` : "sheet-part" }, [h("h4", { text: title }), ...items]);
}

function sheetHead(car) {
  const sub = subtitleOf(car);
  return h("header", { cls: "sheet-head" }, [
    plate(car, "lg"),
    h("div", { cls: "sheet-titles" }, [
      h("h3", { text: displayCase(car.title) || "Véhicule", attrs: { id: "sheet-title", tabindex: "-1" } }),
      sub ? h("p", { cls: "sheet-sub", text: displayCase(sub) }) : null,
    ]),
    h("button", { cls: "btn sheet-x", text: "Fermer", attrs: { type: "button", "aria-label": "Fermer la fiche" },
      on: { click: () => closeSheet({ restoreFocus: true }) } }),
  ]);
}

// Caps, gates and missing data, in one callout under the gauge.
function scoreNotes(car) {
  const d = car.detail || {};
  const score = d.score || {};
  const out = [];
  if (score.cap) {
    const before = isNum(score.before_cap) && score.before_cap > car.score ? ` au lieu de ${score.before_cap}` : "";
    const end = (t) => (/[.!?]$/.test(t) ? t : `${t}.`);
    let text = end(score.cap);
    if (score.cap_gate) text = `Plafonnée à ${car.score}${before}. ${end(score.cap_gate)}`;
    else if (car.confidence === "partial") text = `Plafonnée à ${car.score}${before} tant que les données sont incomplètes.`;
    out.push(h("p", { cls: "cap", text }));
  }
  (car.gates || []).filter((g) => g && g !== score.cap_gate).forEach((g) => out.push(h("p", { cls: "gate", text: `Plafond : ${g}` })));
  if (Array.isArray(d.incomplete) && d.incomplete.length) {
    out.push(h("p", { text: `Données incomplètes : ${d.incomplete.join(", ")}.` }));
  } else if (!car.detail && car.confidence === "partial") {
    out.push(h("p", { text: "Données incomplètes : la note est plafonnée sous le niveau favorable." }));
  }
  return out.length ? h("div", { cls: `callout callout-${car.tier === "red" ? "risk" : "watch"}` }, out) : null;
}

function pillarList(car) {
  const d = car.detail || {};
  const pillars = car.pillars || {};
  const value = d.value || null;
  const dl = h("dl", { cls: "pillars" });
  [["Fiabilité", pillars["Fiabilité"]], ["État", pillars["État"]], ["Prix", pillars.Prix]].forEach(([label, v]) => {
    const unknown = label === "Prix" && value && !value.known;
    const shown = unknown ? null : v;
    dl.appendChild(h("dt", { text: label }));
    dl.appendChild(h("dd", { cls: "num", text: isNum(shown) ? String(shown) : "–" }));
    dl.appendChild(h("dd", { cls: "bar", attrs: { "aria-hidden": "true" } }, [meter(shown)]));
    if (label === "Prix" && value && value.text) {
      if (value.known) dl.appendChild(h("dt", { cls: "sr", text: "Comparaison" }));
      dl.appendChild(h("dd", { cls: "wide", text: capitalise(value.text) }));
    }
  });
  return dl;
}

function sheetScore(car) {
  const d = car.detail || {};
  const sc = (ui.snapshot && ui.snapshot.scoring) || {};
  const w = sc.weights || { "Fiabilité": 35, "État": 35, Prix: 30 };
  const notes = [h("p", { cls: "small muted", text: `La note combine fiabilité ${w["Fiabilité"]}${NBSP}%, état ${w["État"]}${NBSP}% et prix ${w.Prix}${NBSP}%.` })];
  if (d.score && d.score.llm) notes.unshift(h("p", { cls: "small", text: `Ajustement de l'avis IA : ${signed(d.score.llm)} point${Math.abs(d.score.llm) > 1 ? "s" : ""}.` }));
  return sheetSection("La note", [
    h("div", { cls: "score-grid" }, [gauge(car, "lg"), pillarList(car)]),
    scoreNotes(car),
    h("div", { cls: "score-foot" }, notes),
  ]);
}

const PRICE_SHADES = ["start", "vat", "fees", "repairs", "ct_redo"];

function sheetPrice(car) {
  const d = car.detail || {};
  const table = priceRows(d.price);
  if (table) {
    // A thin stacked bar of the parts; the table rows carry the matching swatches and the exact numbers.
    const bar = h("div", { cls: "pricebar", attrs: { "aria-hidden": "true" } }, table.rows.filter((row) => row.value > 0).map((row) => {
      const seg = h("span", { cls: `pb-seg pb-${row.key}` });
      setVar(seg, "--w", row.value);
      return seg;
    }));
    const swatch = (key) => (PRICE_SHADES.includes(key) ? h("span", { cls: `sw pb-${key}`, attrs: { "aria-hidden": "true" } }) : null);
    const tbody = h("tbody", {}, table.rows.map((row) => h("tr", {}, [
      h("th", { attrs: { scope: "row" } }, [swatch(row.key), row.label]), h("td", { text: eur(row.value) })])));
    const tfoot = h("tfoot", {}, [h("tr", {}, [h("th", { text: "Total", attrs: { scope: "row" } }), h("td", { text: eur(table.total) })])]);
    return sheetSection("Prix tout compris", [
      h("p", { cls: "price-total" }, [num(eur(table.total)), h("span", { cls: "muted", text: " tout compris" })]),
      bar,
      h("table", { cls: "ledger" }, [h("caption", { cls: "sr", text: "Détail du prix tout compris" }), tbody, tfoot]),
      h("p", { cls: "small muted", text: `Estimation ${d.price.basis === "bid" ? "à l'enchère courante" : "à la mise à prix"}. Frais fixes par lot non inclus.` }),
    ]);
  }
  const items = [];
  if (isNum(car.effective_price)) items.push(h("p", { cls: "price-total" }, [num(eur(car.effective_price)), h("span", { cls: "muted", text: " tout compris, estimation" })]));
  if (isNum(car.starting_price)) items.push(h("p", { cls: "small muted", text: `Mise à prix : ${eur(car.starting_price)}.` }));
  if (!items.length) items.push(h("p", { text: "Mise à prix pas encore publiée." }));
  return sheetSection("Prix tout compris", items);
}

function defectItem(d) {
  const label = [d.system, d.label].filter(Boolean).join(" : ");
  return h("li", {}, [h("span", { cls: `sev sev-${d.severity}`, attrs: { "aria-hidden": "true" } }),
    h("span", { text: `${label || "Défaillance"}${d.code ? ` (${d.code})` : ""}` })]);
}

function ctStatus(tone, text) {
  return h("p", { cls: `ct-status ct-${tone}` }, [shield(tone), text]);
}

function sheetCt(car) {
  const d = car.detail || {};
  const ct = d.ct;
  if (!car.detail) {
    return sheetSection("Contrôle technique", [ctStatus(ctTone(car), ctLineOf(car))]);
  }
  if (!ct) {
    const text = d.ct_doc === "unreadable" ? "CT illisible (document sans texte lisible)."
      : car.ct_line && car.ct_line !== "Pas de CT publié" ? `${ctLineOf(car)}.` : "Pas de contrôle technique publié.";
    return sheetSection("Contrôle technique", [ctStatus(d.ct_doc === "unreadable" ? "watch" : ctTone(car), text)]);
  }
  const items = [ctStatus(ctResultTone(ct), `${CT_RESULT_FR[ct.result] || "Résultat illisible"}${ct.nature ? ` (${ct.nature})` : ""}`)];
  const facts = [["Date", frDate(ct.date)], ["Kilométrage", kmText(ct.km)], ["Valable jusqu'au", frDate(ct.valid_until)]]
    .filter(([, v]) => v);
  if (facts.length) {
    items.push(h("dl", { cls: "ct-facts" }, facts.map(([k, v]) => h("div", {}, [h("dt", { text: k }), h("dd", { text: v })]))));
  }
  if (ct.doubtful) items.push(h("p", { cls: "watch", text: "Lecture incertaine : la note ne s'y fie pas, vérifiez le procès-verbal sur l'annonce." }));
  if (ct.odometer_inconsistent) items.push(h("p", { cls: "risk", text: "Kilométrage incohérent dans l'historique du contrôle technique." }));
  if (ct.redo) items.push(h("p", { cls: "watch", text: `À refaire avant une revente à un particulier (+80${NBSP}€ compté dans le prix).` }));
  const defects = Array.isArray(ct.defects) ? ct.defects : [];
  if (!defects.length) items.push(h("p", { cls: "muted", text: "Aucune défaillance relevée." }));
  SEVERITY_GROUPS.forEach(([sev, label]) => {
    const list = defects.filter((x) => x && x.severity === sev);
    if (!list.length) return;
    const ul = h("ul", {}, list.map(defectItem));
    const heading = `${label} (${list.length})`;
    if (sev === "minor" && list.length > 4) {
      items.push(h("details", { cls: "defects more" }, [h("summary", { text: heading }), ul]));
    } else {
      items.push(h("div", { cls: "defects" }, [h("h5", { text: heading }), ul]));
    }
  });
  if (ct.more > 0) items.push(h("p", { cls: "small muted", text: `Et ${plural(ct.more, "autre défaillance", "autres défaillances")}, à voir sur le procès-verbal.` }));
  return sheetSection("Contrôle technique", items);
}

function sheetDamages(car) {
  const d = car.detail;
  if (!d || !("damages" in d)) return null;
  const title = "Carrosserie, relevé de la maison de vente";
  if (d.damages === null) return sheetSection(title, [h("p", { cls: "muted", text: "Dommages de carrosserie pas encore relevés." })]);
  if (!d.damages.length) return sheetSection(title, [h("p", { text: "Aucun dommage signalé par la maison de vente." })]);
  const zones = new Map();
  d.damages.forEach((x) => {
    if (!x) return;
    const zone = x.zone || "Zone inconnue";
    const kind = `${String(x.kind || "défaut").toLowerCase()}${x.n > 1 ? ` ×${x.n}` : ""}`;
    if (!zones.has(zone)) zones.set(zone, []);
    zones.get(zone).push(kind);
  });
  const ul = h("ul", { cls: "damages" }, [...zones.entries()].map(([zone, kinds]) => h("li", { text: `${zone} : ${kinds.join(", ")}` })));
  return sheetSection(title, [ul]);
}

function sheetBattery(car) {
  const b = car.detail && car.detail.battery;
  const fuel = fold(car.fuel || "");
  const electric = fuel === "electrique" || fuel === "hybride rechargeable";
  if (!b && !electric) return null;
  const items = [];
  if (b && isNum(b.soh)) {
    const src = BATTERY_SOURCE_FR[b.source];
    // Under 75 % the car is set aside (see the about page), so the outline turns red there.
    const cell = h("span", { cls: `battery tier-${b.soh >= 80 ? "green" : b.soh >= 75 ? "yellow" : "red"}`, attrs: { "aria-hidden": "true" } },
      [h("span", { cls: "battery-fill" })]);
    setVar(cell, "--v", Math.max(0, Math.min(100, b.soh)));
    items.push(h("p", { cls: "battery-line" }, [cell, h("span", { text: `Santé de la batterie : ${b.soh}${NBSP}%${src ? ` (${src})` : ""}.` })]));
  } else if (electric) {
    items.push(h("p", { cls: "muted", text: "Santé de la batterie non communiquée." }));
  }
  if (b && b.leased) items.push(h("p", { cls: "watch", text: "Batterie en location : un loyer mensuel est à prévoir." }));
  return sheetSection("Batterie", items);
}

function sheetAi(car) {
  const llm = (ui.snapshot && ui.snapshot.llm) || {};
  const text = car.verdict
    ? h("p", { text: car.verdict })
    : h("p", { cls: "muted", text: llm.enabled ? "Avis IA pas encore disponible pour ce lot." : "Avis IA désactivé pour cette publication." });
  return sheetSection("Avis IA, peut se tromper", [text], "sheet-ai");
}

function sheetReasons(car) {
  const d = car.detail;
  const list = d && Array.isArray(d.reasons) ? d.reasons : (car.reasons || []).map((r) => ({ text: r.text, src: r.src, pts: null }));
  if (!list.length) return null;
  // Schema 1 has no points: then the text takes the whole line (no empty chip column).
  const withPts = list.some((r) => isNum(r.pts) && r.pts !== 0);
  const ul = h("ul", { cls: withPts ? "points" : "points no-pts" }, list.map((r) => {
    const pts = isNum(r.pts) && r.pts !== 0 ? r.pts : null;
    const cell = h("span", { cls: pts === null ? "pts" : pts > 0 ? "pts pos" : "pts neg" }, pts === null ? [] : [
      signed(pts), h("span", { cls: "sr", text: `${Math.abs(pts) < 2 ? " point" : " points"}, ${pts > 0 ? "favorable" : "défavorable"}` })]);
    return h("li", {}, [cell, h("span", {}, [r.text || "", r.src ? h("span", { cls: "src", text: r.src }) : null])]);
  }));
  return sheetSection("Ce qui fait la note", [ul]);
}

function sheetLinks(car) {
  return h("div", { cls: "sheet-links" }, [
    extLink(car.url, "Voir l'annonce sur Alcopa", "btn btn-primary"),
    h("button", { cls: "btn", text: "Fermer la fiche", attrs: { type: "button" }, on: { click: () => closeSheet({ restoreFocus: true }) } }),
  ]);
}

function renderSheet(car) {
  const sheet = clear(ui.sheet);
  sheet.appendChild(sheetHead(car));
  const parts = [h("p", { cls: "masked-note callout callout-watch", text: "Ce lot est masqué par les filtres.",
    attrs: { id: "sheet-masked", hidden: !ui.hiddenOpen } })];
  if (!car.detail) parts.push(h("p", { cls: "small muted", text: "Fiche complète à la prochaine publication." }));
  parts.push(sheetScore(car), sheetPrice(car), sheetCt(car), sheetDamages(car), sheetBattery(car), sheetAi(car), sheetReasons(car), sheetLinks(car));
  parts.filter(Boolean).forEach((p) => sheet.appendChild(p));
}

function setMasked(masked) {
  ui.hiddenOpen = masked;
  const note = $("sheet-masked");
  if (note) note.hidden = !masked;
}

function markRow(ref, open) {
  const li = ui.rows.get(ref);
  if (!li) return;
  li.classList.toggle("is-open", open);
  const btn = li.querySelector(".lot-btn");
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  li.querySelector(".lot-open-label").textContent = open ? "Fermer" : "Voir la fiche";
}

function placeSheet() {
  if (!ui.sheet) return;
  const empty = $("sheet-pane").querySelector(".pane-empty");
  if (!ui.openRef) {
    if (ui.sheet.parentNode) ui.sheet.parentNode.removeChild(ui.sheet);
    empty.hidden = !(ui.model && ui.model.cars.length);  // only when there is a lot to choose
    return;
  }
  empty.hidden = true;
  if (ui.desktop) $("sheet-pane").appendChild(ui.sheet);
  else rowFor(ui.model.byRef.get(ui.openRef)).appendChild(ui.sheet);
}

function openLot(ref, opts) {
  const o = opts || {};
  const car = ui.model && ui.model.byRef.get(ref);
  if (!car || (car.section !== "top" && car.section !== "cards")) return false;
  if (ui.openRef && ui.openRef !== ref) markRow(ui.openRef, false);
  ui.openRef = ref;
  ui.hiddenOpen = false;
  markRow(ref, true);
  renderSheet(car);
  placeSheet();
  if (o.hash !== false) history.replaceState(null, "", `#lot-${ref}`);
  const behavior = reducedMotion() || o.instant ? "auto" : "smooth";
  if (ui.desktop) {
    $("sheet-pane").scrollTop = 0;
    if (o.focus) $("sheet-title").focus({ preventScroll: true });
    if (o.reveal) rowFor(car).scrollIntoView({ block: "nearest", behavior });
  } else if (o.scroll) {
    rowFor(car).scrollIntoView({ block: "start", behavior });
  }
  return true;
}

function closeSheet(opts) {
  const ref = ui.openRef;
  if (!ref) return;
  markRow(ref, false);
  ui.openRef = null;
  ui.hiddenOpen = false;
  placeSheet();
  if (parseHash(location.hash)) history.replaceState(null, "", location.pathname + location.search);
  if (opts && opts.restoreFocus) {
    const li = ui.rows.get(ref);
    if (li && li.isConnected) li.querySelector(".lot-btn").focus();
  }
}

function toggleLot(ref) {
  if (ui.openRef === ref) closeSheet({ restoreFocus: true });
  else openLot(ref, { focus: ui.desktop, scroll: !ui.desktop });
}

function revealCompact(car, instant) {
  const id = car.tier === "red" ? "g-red" : "g-yellow";
  const group = $(id);
  group.open = true;
  renderSections();
  let li = ui.crows.get(car.ref);
  if (!li || !li.isConnected) {  // hidden by a filter: clear them so the lot can be shown
    $("filters").reset();
    renderSections();
    announceCount(true);
    li = ui.crows.get(car.ref);
  }
  if (!li) return;
  ui.crows.forEach((node) => node.classList.remove("is-target"));
  li.classList.add("is-target");
  li.scrollIntoView({ block: "center", behavior: reducedMotion() || instant ? "auto" : "smooth" });
  const link = li.querySelector("a");
  (link || li).focus({ preventScroll: true });
}

// A hash in the address opens that lot. On page load (event-less call) the jump is instant.
function onHashChange(event) {
  const instant = !event;
  const ref = parseHash(location.hash);
  if (!ref || !ui.model) return;
  const car = ui.model.byRef.get(ref);
  if (!car) return;
  if (car.section === "top" || car.section === "cards") {
    if (!rowFor(car).isConnected) {
      $("filters").reset();
      renderSections();
      announceCount(true);
    }
    openLot(ref, { focus: true, scroll: true, reveal: true, hash: false, instant });
  } else {
    revealCompact(car, instant);
  }
}

function onKeydown(e) {
  if (e.key === "Escape") {
    if (moreOpen() && $("filters").contains(document.activeElement)) {
      setMore(false);
      $("f-toggle").focus();
      e.preventDefault();
      return;
    }
    if (ui.openRef && document.activeElement !== $("f-q")) {
      closeSheet({ restoreFocus: true });
      e.preventDefault();
    }
    return;
  }
  const target = e.target;
  if (!target || !target.classList || !target.classList.contains("lot-btn")) return;
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
  const buttons = Array.from(document.querySelectorAll(".lots .lot-btn"));
  const i = buttons.indexOf(target);
  if (i < 0) return;
  const next = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: buttons.length - 1 }[e.key];
  if (next >= 0 && next < buttons.length) {
    buttons[next].focus();
    e.preventDefault();
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
  ["sec-top", "sec-cards", "sec-other", "no-match"].forEach((id) => { $(id).hidden = true; });
  $("filters").hidden = true;
  $("sheet-pane").querySelector(".pane-empty").hidden = true;  // no lot to choose
  $("sale-line").textContent = "L'analyse de la semaine n'a pas pu être affichée.";
  $("sale-line").hidden = false;
  $("tally").hidden = true;
  const box = clear($("state"));
  const title = h("h2", { text: err.title, attrs: { tabindex: "-1" } });
  box.appendChild(title);
  if (err.text) box.appendChild(h("p", { text: err.text }));
  if (err.retry) box.appendChild(h("button", { cls: "btn", text: "Réessayer", attrs: { type: "button" }, on: { click: () => load({ retry: true }) } }));
  if (err.reload) box.appendChild(h("button", { cls: "btn", text: "Recharger", attrs: { type: "button" }, on: { click: () => location.reload() } }));
  box.hidden = false;
  if (focus) title.focus();
}

function render(s) {
  ui.snapshot = s;
  ui.model = prepare(s);
  ui.rows.clear();
  ui.crows.clear();
  ui.openRef = null;
  ui.redOpened = false;
  ui.lastQ = null;
  $("state").hidden = true;
  showSkeleton(false);
  renderMasthead(s);
  renderBanners(s);
  renderStatus(s);
  renderFilterOptions(ui.model);
  $("fees").textContent = s.fees_note || "";
  $("filters").hidden = !s.sale;
  $("sec-top").hidden = false;
  renderSections();
  announceCount(true);
  // Before any scroll to a lot (the observer only reports at the next frame), and after the
  // count is written: on a phone the count is a line of the bar of its own.
  measureBar();
  placeSheet();
}

function applyHash() {
  if (parseHash(location.hash)) {
    onHashChange();
  } else if (ui.desktop && ui.model) {
    const first = sortCars(ui.model.cars.filter((c) => c.section === "top"), readFilters().sort)[0];
    if (first) openLot(first.ref, { focus: false, hash: false });
  }
}

async function load(opts) {
  const retry = Boolean(opts && opts.retry);
  $("catalogue").setAttribute("aria-busy", "true");
  $("sale-line").textContent = "Chargement de l'analyse…";
  $("sale-line").hidden = false;
  if (retry) {  // keep the message box (and the focus in it) while the new request runs
    const box = clear($("state"));
    const title = h("h2", { text: "Chargement de l'analyse…", attrs: { tabindex: "-1" } });
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

function boot() {
  ui.sheet = h("section", { cls: "sheet", attrs: { id: "sheet", "aria-labelledby": "sheet-title", tabindex: "-1" } });
  const form = $("filters");
  form.addEventListener("submit", (event) => event.preventDefault());
  form.addEventListener("input", onFilterInput);
  form.addEventListener("change", onFilterInput);
  $("f-reset").addEventListener("click", onReset);
  $("no-match-reset").addEventListener("click", onReset);
  $("f-toggle").addEventListener("click", () => setMore(!$("f-more").classList.contains("is-open")));
  ["g-yellow", "g-red"].forEach((id) => $(id).addEventListener("toggle", () => renderSections()));
  document.addEventListener("keydown", onKeydown);
  window.addEventListener("hashchange", onHashChange);
  const mq = window.matchMedia(DESKTOP_MQ);
  ui.desktop = mq.matches;
  mq.addEventListener("change", () => {
    ui.desktop = mq.matches;
    placeSheet();
    if (ui.desktop && !ui.openRef) applyHash();
  });
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(measureBar).observe(form);
  }
  load();
}

if (typeof module !== "undefined" && module.exports) {  // for tests (Node)
  module.exports = { saleStateNow, daysUntil, countdownText, fold, parseHash, prepare, filterCars, sortCars,
    priceRows, errorFor, factsLine, factsParts, saleHeading, partialLine, partialShort, safeUrl, displayCase, ctTone,
    ctResultTone, ctLineOf, tallyParts, gaugePoint };
}

if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", boot);
