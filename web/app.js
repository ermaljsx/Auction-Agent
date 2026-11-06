// Renders /api/snapshot. Every value from the snapshot (including AI text) is inserted with
// textContent, never as HTML: the data comes from an auction site and a language model.
"use strict";

const STALE_DAYS = 8;
const TIER = {
  green: { label: "Profil favorable", cls: "tier-green" },
  yellow: { label: "Profil correct", cls: "tier-yellow" },
  red: { label: "Risque élevé", cls: "tier-red" },
};
const CRAWL_FR = {
  ok: "réussie", incomplete: "incomplète", blocked: "refusée par le site source", failed: "échouée",
  no_sale: "aucune vente trouvée cette semaine", skipped: "non relancée", running: "en cours",
};
const nf = new Intl.NumberFormat("fr-FR");
let snapshot = null;

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function eur(value) {
  return value === null || value === undefined ? "prix inconnu" : `${nf.format(Math.round(value))} €`;
}

function parisDate(iso, withTime) {
  if (!iso) return "—";
  const options = { timeZone: "Europe/Paris", day: "numeric", month: "long", year: "numeric" };
  if (withTime) Object.assign(options, { hour: "2-digit", minute: "2-digit" });
  return new Date(iso).toLocaleString("fr-FR", options);
}

function ageText(iso) {
  const hours = (Date.now() - new Date(iso).getTime()) / 3.6e6;
  if (!isFinite(hours)) return "";
  if (hours < 1) return "il y a moins d'une heure";
  if (hours < 48) return `il y a ${Math.round(hours)} h`;
  return `il y a ${Math.round(hours / 24)} jours`;
}

function banner(kind, text) {
  document.getElementById("banners").appendChild(el("p", `banner banner-${kind}`, text));
}

function statusRow(label, value) {
  const dl = document.getElementById("status");
  dl.appendChild(el("dt", null, label));
  dl.appendChild(el("dd", null, value));
}

function renderHeader(s) {
  const line = document.getElementById("sale-line");
  line.textContent = "";
  if (!s.sale) {
    line.textContent = "Aucune vente analysée pour le moment.";
    return;
  }
  const date = s.sale.date ? parisDate(`${s.sale.date}T12:00:00Z`) : "date inconnue";
  line.appendChild(el("span", null, `Vente du lundi ${date} – ${s.sale.room || "Beauvais"} `));
  if (s.sale.url) {
    const a = el("a", null, "voir la vente sur le site d'Alcopa");
    a.href = s.sale.url;
    a.rel = "noopener noreferrer nofollow";
    a.target = "_blank";
    line.appendChild(a);
  }
}

function renderStatus(s) {
  const crawl = s.crawl || {};
  const counts = crawl.counts || {};
  const llm = s.llm || {};
  statusRow("Dernière publication", `${parisDate(s.generated_at, true)} (${ageText(s.generated_at)})`);
  statusRow("Dernière collecte", CRAWL_FR[crawl.status] || crawl.status || "—");
  if (counts.expected_lots || counts.lots_seen) {
    const pct = counts.expected_lots ? Math.round((100 * counts.lots_seen) / counts.expected_lots) : null;
    statusRow("Lots lus", `${counts.lots_seen} sur ${counts.expected_lots || "?"}${pct !== null ? ` (${pct} %)` : ""}`);
  }
  if (counts.detailed !== undefined) statusRow("Fiches et contrôles techniques", `${counts.detailed} fiches, ${counts.documents} CT lus`);
  const c = s.counts || {};
  statusRow("Véhicules notés", `${(c.green || 0) + (c.yellow || 0) + (c.red || 0)} (dont ${c.partial || 0} aux données incomplètes)`);
  let ai = "désactivée";
  if (llm.enabled) {
    ai = `${(llm.models_answered && llm.models_answered.length ? llm.models_answered.join(", ") : llm.model)} – `
       + `${llm.calls} appel(s), ${llm.cached} réutilisé(s)`;
    if (llm.error) ai += `, interrompue (${llm.error === "config" ? "configuration" : "erreur temporaire"})`;
  }
  statusRow("Analyse IA", ai);
}

function renderBanners(s) {
  const generated = new Date(s.generated_at).getTime();
  if (Date.now() - generated > STALE_DAYS * 864e5) {
    banner("red", `Données anciennes : dernière publication ${ageText(s.generated_at)}. L'ordinateur qui fait l'analyse était peut-être éteint.`);
  }
  const status = (s.crawl || {}).status;
  if (status === "blocked") banner("red", "La dernière collecte a été refusée par le site source. Les voitures affichées viennent de la dernière collecte réussie.");
  if (status === "failed") banner("red", "La dernière collecte a échoué. Les voitures affichées viennent de la dernière collecte réussie.");
  if (status === "no_sale") banner("yellow", "Aucune vente du lundi n'a été trouvée cette semaine pour l'instant.");
  const state = s.sale && s.sale.state;
  if (state === "over") banner("yellow", "Cette vente est terminée. La prochaine analyse est prévue jeudi soir.");
  if (state === "in_progress") banner("yellow", "Vente en cours : certains lots sont peut-être déjà adjugés.");
}

function card(c) {
  const tier = TIER[c.tier] || { label: c.tier, cls: "" };
  const box = el("article", `card ${tier.cls}`);
  const head = el("div", "card-head");
  head.appendChild(el("span", `chip ${tier.cls}`, tier.label));
  head.appendChild(el("span", "score", `${c.score}/100`));
  box.appendChild(head);
  box.appendChild(el("h3", null, [c.title, c.trim && c.trim !== c.title ? c.trim : null].filter(Boolean).join(" – ")));
  const facts = [c.year, c.mileage !== null && c.mileage !== undefined ? `${nf.format(c.mileage)} km` : null, c.fuel, c.gearbox]
    .filter(Boolean).join(" · ");
  box.appendChild(el("p", "facts", facts));
  const price = el("p", "price");
  price.appendChild(el("strong", null, eur(c.effective_price)));
  price.appendChild(el("span", "small", " estimation frais inclus"));
  if (c.lot) price.appendChild(el("span", "small", ` · lot n° ${c.lot}`));
  box.appendChild(price);
  if (c.confidence === "partial") box.appendChild(el("p", "pill", "Données incomplètes"));
  if (c.ct) box.appendChild(el("p", "ct", c.ct));
  if (c.reasons && c.reasons.length) {
    const ul = el("ul", "reasons");
    c.reasons.forEach((r) => ul.appendChild(el("li", null, `${r.text} (${r.src})`)));
    box.appendChild(ul);
  }
  (c.gates || []).forEach((g) => box.appendChild(el("p", "gate", `Plafonné : ${g}`)));
  if (c.verdict) box.appendChild(el("p", "verdict", `Avis IA (peut se tromper) : ${c.verdict}`));
  if (c.url) {
    const a = el("a", "link", "Voir l'annonce");
    a.href = c.url;
    a.rel = "noopener noreferrer nofollow";
    a.target = "_blank";
    box.appendChild(a);
  }
  return box;
}

function applyFilters(list) {
  const budget = parseFloat(document.getElementById("f-budget").value);
  const fuel = document.getElementById("f-fuel").value;
  const tier = document.getElementById("f-tier").value;
  const sort = document.getElementById("f-sort").value;
  const out = list.filter((c) => (!budget || (c.effective_price !== null && c.effective_price <= budget))
    && (!fuel || c.fuel === fuel) && (!tier || c.tier === tier));
  if (sort === "price") {
    out.sort((a, b) => (a.effective_price ?? Infinity) - (b.effective_price ?? Infinity));
  }
  return out;
}

function renderLists() {
  if (!snapshot) return;
  for (const [id, list] of [["top", snapshot.top || []], ["rest", snapshot.cards || []]]) {
    const box = document.getElementById(id);
    box.textContent = "";
    const shown = applyFilters(list);
    shown.forEach((c) => box.appendChild(card(c)));
    if (!shown.length) box.appendChild(el("p", "empty", "Aucun véhicule ne correspond."));
  }
  const red = (snapshot.counts || {}).red || 0;
  document.getElementById("rest-note").textContent = red
    ? `${red} véhicule(s) à risque élevé ne sont pas listés.` : "";
}

function render(s) {
  snapshot = s;
  renderHeader(s);
  renderBanners(s);
  renderStatus(s);
  const topNote = document.getElementById("top-note");
  if (s.provisional) {
    topNote.textContent = "Classement provisoire : les données de cette vente sont encore incomplètes (prix ou contrôles techniques pas encore publiés).";
    topNote.hidden = false;
  }
  const fuels = [...new Set([...(s.top || []), ...(s.cards || [])].map((c) => c.fuel).filter(Boolean))].sort();
  const select = document.getElementById("f-fuel");
  fuels.forEach((f) => { const o = el("option", null, f); o.value = f; select.appendChild(o); });
  document.getElementById("fees").textContent = s.fees_note || "";
  renderLists();
}

async function load() {
  try {
    const response = await fetch(`/api/snapshot?t=${Date.now()}`, { cache: "no-store", credentials: "same-origin" });
    const data = await response.json();
    if (!response.ok) {
      const messages = {
        no_snapshot: "Aucune analyse n'a encore été publiée. La première arrivera après la prochaine collecte.",
        not_configured: "Le stockage des analyses n'est pas encore relié au site.",
      };
      document.getElementById("sale-line").textContent = messages[data.status] || "Analyse momentanément indisponible.";
      return;
    }
    render(data);
  } catch (err) {
    document.getElementById("sale-line").textContent = "Analyse momentanément indisponible.";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("filters").addEventListener("input", renderLists);
  load();
});
