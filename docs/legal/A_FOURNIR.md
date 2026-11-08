# Legal drafts: inputs to provide and claims to review

> **Internal working document. Not legal advice.** Drafted on 2026-11-08 for the Phase 6 review (licence outreach, lawyer review, public-launch decision; see [docs/MASTER_PLAN.md](../MASTER_PLAN.md) §6).

This folder holds internal French drafts of the four footer pages that `web/index.html` lists as « en préparation »:

- [mentions-legales.md](mentions-legales.md)
- [conditions-utilisation.md](conditions-utilisation.md)
- [confidentialite.md](confidentialite.md)
- [contact.md](contact.md)

None of them is published, and publishing them is a separate launch decision (LAUNCH-7).

**How to use this list.** Each gap in the drafts reads `[À fournir : … — ID]`. The ID points to one row below. A clause that is an owner's choice rather than a fact reads « Proposition à valider (ID) ». Every factual sentence in the drafts comes from the repo or from the Vercel project settings; the table at the end gives the source of each one. "Who" says who has to settle the item: the **owner**, the **lawyer**, or **both**.

Nothing below has been checked on an outside website (Vercel, Anthropic, Alcopa, Légifrance, CNIL). Where a row says "check", nobody has done it yet.

---

## 1. Operator identity (LCEN art. 6 items)

The repo holds no operator details at all: no name, address, phone, e-mail, SIRET or company form. None was invented.

| ID | Missing or to review | Used in | Who | Notes |
|---|---|---|---|---|
| ID-1 | Editor's identity: first and last name (individual), or company name (business) | mentions §2, conditions §1, confidentialité §1 | owner | The editor is also the data controller (PRIV-1). |
| ID-2 | Editor's status: non-professional individual, sole trader, or company | mentions §2 | owner + lawyer | It decides which items below are required and whether ID-7 applies. about.html says « projet personnel et non commercial » (TERMS-5). |
| ID-3 | Postal address (home address or registered office) | mentions §2 | owner | Required unless the ID-7 option applies. |
| ID-4 | Editor's phone number and e-mail address | mentions §2 | owner | The e-mail can be the contact address (CONTACT-1). |
| ID-5 | RCS or RM number, SIRET, share capital, intra-EU VAT number | mentions §2 | owner + lawyer | Only if the editor is a business or the activity is commercial. |
| ID-6 | Directeur de la publication (and responsable de la rédaction, if any) | mentions §3 | owner | Usually the editor, for an individual. |
| ID-7 | The option for a **non-professional** editor to publish only the host's details, the editor's identity being given to the host | mentions §2 (note) | lawyer | Confirm whether it applies to this project and whether the owner wants it. |
| ID-8 | Exact legal reference | mentions §2 (note) | lawyer | The brief cites "LCEN art. 6". The 2024 SREN law reorganised that article: confirm the current numbering before citing it on the page. |
| ID-9 | Final public name (working name « Auction France ») | every draft | owner | Pending owner approval. Consider a trademark and domain check before launch. about.html still says « Auction Agent » (another session is redesigning it). |

## 2. Host details

| ID | Missing or to review | Used in | Who | Notes |
|---|---|---|---|---|
| HOST-1 | Vercel's exact company name and legal form | mentions §4 | owner | Copy it from Vercel's own legal pages. The repo names only "Vercel". |
| HOST-2 | Vercel's postal address | mentions §4 | owner | Same source. Do not write it from memory. |
| HOST-3 | Vercel's phone number | mentions §4 | owner | Same source. |
| HOST-4 | Function region **cdg1** (Paris): confirm it, and decide whether the page states it | mentions §4, confidentialité §3 | owner | **Not in the repo.** Read from the Vercel API on 2026-11-08 (deployment `dpl_3RngXPUQSJy1RPGp7h45djbJsWo8`, `regions: ["cdg1"]`). `web/vercel.json` does not pin a region, so a later deployment could run elsewhere: pin it (a `web/` change) or drop the claim. It covers the function only, not Vercel's edge network or logs. |
| HOST-5 | Where the private Vercel Blob store keeps the snapshot, and whether Vercel keeps earlier versions | mentions §4, confidentialité §7 | owner | Unknown. `blobstore.py` overwrites `snapshot/latest.json` on every publish. |
| HOST-6 | Vercel plan, and whether its terms allow the intended use | — (launch) | owner | Project notes say Hobby. Check Vercel's plan terms before any commercial or public use. |

## 3. Contact and takedown channel

| ID | Missing or to review | Used in | Who | Notes |
|---|---|---|---|---|
| CONTACT-1 | Contact e-mail address | mentions §6, confidentialité §1 and §10, contact §1 | owner | None exists. A dedicated mailbox keeps the owner's personal or work address off the page. |
| CONTACT-2 | Channel for takedown and opt-out requests (same mailbox or a separate one) | contact §3 | owner | Planned in MASTER_PLAN §5 (`/methode`: "takedown contact and opt-out"), not built. |
| CONTACT-3 | Committed response time | contact §3 | owner + lawyer | Do not promise a delay that cannot be kept. |
| CONTACT-4 | How a lot is actually removed | contact §3 | owner | **No mechanism exists in the code.** `publish` has no exclusion list. Decide what it would remove (the sheet, the one-line entry, the AI request), whether the current snapshot is re-published at once, and whether the car is also deleted from the PC database. |
| CONTACT-5 | Who may ask (vehicle holder, seller, auction house, rights holder) and what proof is required | contact §3 | owner + lawyer | Ask for no more personal data than the request needs. |
| CONTACT-6 | Optional: an honest crawler User-Agent with a contact address | contact §6 | owner | The README configuration table suggests it (`AUCTION_AGENT_USER_AGENT`); the default is a browser string. |
| CONTACT-7 | Contact form or not | contact §1 | owner | The site's CSP has `form-action 'none'` (`web/vercel.json`), so a form would need a CSP change. An e-mail link needs none. |
| CONTACT-8 | Whether and how people may ask for access to the site | contact §5 | owner | Today access is by invitation only (Vercel Authentication or a temporary share link). |

## 4. Privacy (RGPD)

| ID | Missing or to review | Used in | Who | Notes |
|---|---|---|---|---|
| PRIV-1 | Data controller (presumably the editor, ID-1); is a DPO or an EU representative needed? | confidentialité §1 | lawyer | — |
| PRIV-2 | Legal basis for each processing: (a) site access and authentication; (b) collection of Alcopa catalogue data and CT documents (VINs and plates read in memory, VIN kept as a keyed HMAC); (c) the AI review; (d) host logs | confidentialité §8 | lawyer | MASTER_PLAN App. A cites CNIL guidance on legitimate interest and scraping (PRIV-13). |
| PRIV-3 | Retention periods on the owner's PC | confidentialité §7 | owner + lawyer | **No purge exists yet** (`ingest.py`: Phase 1 runs never purge). The Phase 2 lifecycle (MASTER_PLAN §2: dormancy, deletion 36 h after a sale or after a relist window of 28 days by default, a 45-day TTL, a compact derived archive) is planned, not built. Also decide for the logs in `data\logs\` and the AI cache (`llm_assessments`). |
| PRIV-4 | Retention of the published snapshot | confidentialité §7 | owner | It is overwritten on each publish (`blobstore.py`). Whether Vercel keeps versions or backups is unknown (HOST-5). |
| PRIV-5 | The cookies Vercel sets for **Vercel Authentication** and for share links: names, purpose, lifetime; whether they are exempt from consent | confidentialité §2 and §3 | owner + lawyer | **Not documented in the repo.** The site itself sets none (only request: `fetch('/api/snapshot…')` in `web/app.js`; `localStorage` banned by `tests/test_site.py`). Also check which Vercel account data (name, e-mail) the editor can see about invited people. |
| PRIV-6 | Vercel request logs: what is recorded (IP address, user agent, URL…), for how long, who can read them; a link to Vercel's privacy policy | confidentialité §3 and §7 | owner + lawyer | **Not documented in the repo.** |
| PRIV-7 | Vercel as a processor: data processing agreement, where data is processed, transfer mechanism if it leaves the EU | confidentialité §6 and §9 | owner + lawyer | Only the function region (HOST-4) is known. |
| PRIV-8 | Anthropic (AI review, `claude-haiku-5-5`): data use, retention, training, processing location, transfer mechanism, data processing agreement | confidentialité §5, §6 and §9 | owner + lawyer | **Anthropic's terms have not been read** (README "Data"; MASTER_PLAN App. A and C). The README points to the commercial terms and privacy pages for the owner to read. |
| PRIV-9 | Personal data that can survive in the seller comments sent to the AI | confidentialité §5 | owner + lawyer | `scrub_for_llm` removes VINs, plates, labelled owner fields (« propriétaire », « titulaire », « nom », « adresse »), amounts, phones, e-mails and links. An unlabelled personal name would still go through. Decide whether that is acceptable or whether comments should stop being sent. |
| PRIV-10 | The OpenAI switch | confidentialité §5 | owner | The code can use OpenAI (`LLM_PROVIDER=openai`). If it ever does, rewrite §5 and §6 (the README's OpenAI note says requests may be kept up to 30 days and processed in the US; not verified). |
| PRIV-11 | Informing people whose data is collected without their visiting the site (vehicle holders: VINs and plates in CT documents; sellers: comments), RGPD art. 14 | confidentialité §4 | lawyer | Is a public page enough, or does an exemption apply? |
| PRIV-12 | Where the database, `.env` and logs actually live | confidentialité §6 | owner | They default to `data/` in the project folder. That folder's path is under « OneDrive - SAVOYE », so it may sync to a OneDrive account. Confirm the location (`AUCTION_AGENT_DATA_DIR`, `AUCTION_AGENT_ENV_FILE`) and whether that sync is acceptable. If kept, it is another recipient to list. |
| PRIV-13 | CNIL position on scraping (« moissonnage ») and legitimate interest | confidentialité §4 | lawyer | Source listed in MASTER_PLAN App. A, read only as a search summary. |
| PRIV-14 | The rights section (access, rectification, erasure, objection, restriction, portability where it applies, complaint to the CNIL) and the identity check for requests | confidentialité §10, contact §4 | lawyer | Standard wording in the draft, not tailored. |
| PRIV-15 | **The quarantine exception: a CT or battery PDF that cannot be parsed is saved to disk unredacted** | confidentialité §4 | owner + lawyer | `documents.py` writes it to `data/quarantine/<sha256>.pdf` for debugging (at most 50 files), and each crawl deletes files older than 7 days (`sweep_quarantine`, called from `ingest.py`). Such a PDF can show the VIN and the plate. The README says PDFs are "never written to disk" without this exception. Decide whether to keep the quarantine (and say so on the page, as the draft does) or switch it off. |

## 5. Terms

| ID | Missing or to review | Used in | Who | Notes |
|---|---|---|---|---|
| TERMS-1 | Independence from Alcopa: wording, use of the name « Alcopa » and links to its listings; **Alcopa's CGV, CGU and mentions légales have not been read** | mentions §5, conditions §4 | owner + lawyer | Footer: « Projet indépendant, sans lien avec Alcopa ni avec les maisons de vente ». README legal notes: Alcopa's terms of sale reserve its descriptions and photos. MASTER_PLAN App. B §2: read and save a dated copy during recon. |
| TERMS-2 | "Not purchase advice": wording and reach | conditions §3 | lawyer | Footer: « Indications selon nos critères, pas un conseil d'achat ». |
| TERMS-3 | One set of tier labels, all « selon nos critères » | conditions §3 | owner | Three versions exist: the report (`report.py`) says « Profil favorable / Profil correct selon nos critères » and « Risque élevé selon nos critères »; `app.js` says favorable / correct / risque élevé; `about.html` says « favorable », « correcte », « écartée ». MASTER_PLAN §4 chose the « selon nos critères » labels to limit disparagement risk. |
| TERMS-4 | Fee figures | conditions §3 | owner | 14,4 % TTC, minimum 360 € for room sales: config values "to be checked against the CGV" (MASTER_PLAN §3). Fixed per-lot fees are not included (`snapshot.fees_note`). |
| TERMS-5 | The « projet personnel et non commercial » statement in about.html | conditions §1 | owner | Keep it only if it stays true after launch (ID-2, HOST-6). |
| TERMS-6 | Publication schedule | conditions §3 | owner | The footer says « jeudi soir et samedi matin »; the README schedules Thursday 19:17 and Saturday 09:43. Project notes say the scheduled task was not yet installed on 2026-11-08. The draft says « en principe » and promises nothing. |
| TERMS-7 | Access rules: who is invited, share-link lifetime, revoking access, no sharing of access | conditions §2 | owner | Proposal in the draft. |
| TERMS-8 | Permitted use and the reuse ban | mentions §5, conditions §5 | owner + lawyer | Proposal in the draft. The program's code is MIT-licensed in a public GitHub repository (README "License"), so any clause should cover the site's content (scores, sheets, texts), not the code. |
| TERMS-9 | Liability clause | conditions §6 | lawyer | Placeholder only. |
| TERMS-10 | Changes to the terms, governing law, competent courts | conditions §9 | lawyer | Placeholder only. |
| TERMS-11 | Disclosure of the AI-written verdict: is « Avis IA … peut se tromper » enough? | conditions §3 | lawyer | The site labels it « Avis IA » and « peut se tromper » (README; `tests/test_site.py`). |
| TERMS-12 | Effective date or version of each page | every draft | owner | Set at publication. |

## 6. Launch prerequisites

| ID | Missing or to review | Used in | Who | Notes |
|---|---|---|---|---|
| LAUNCH-1 | Licence or partnership outreach to Alcopa before any public use | mentions §5 | owner | Database-right risk: CJEU *Innoweb v Wegener* (C-202/12), Leboncoin v Entreparticuliers, Jinka v SeLoger (MASTER_PLAN §1 and App. A; README legal notes). |
| LAUNCH-2 | Review by a French IP and data-protection lawyer, of the project and of these drafts | banner of every draft | owner | README legal notes; MASTER_PLAN §6, Phase 6 exit criterion "Lawyer sign-off". |
| LAUNCH-3 | Public mode limited to a top-N subset | — | owner | MASTER_PLAN §5: public mode shows a top-N subset per filter, with no full-catalogue search and no claim of exhaustiveness. **The current site lists every lot of the sale** (README: « Les autres lots »), which is fine for private use only. |
| LAUNCH-4 | Indexing | mentions §1 | owner | robots.txt `Disallow: /`, the `X-Robots-Tag: noindex, nofollow` header and `<meta name="robots" content="noindex, nofollow">` keep the site out of search engines. Change them on purpose at launch, and update mentions §1. |
| LAUNCH-5 | Legal pages reachable without signing in | — | owner | Vercel Authentication protects all deployments today (`ssoProtection: all`), so a visitor who is not signed in cannot read the mentions légales either. |
| LAUNCH-6 | Final domain name | mentions §1 | owner | Today: `auction-agent-mvp.vercel.app`. |
| LAUNCH-7 | Publishing the pages | — | owner | Turn the drafts into pages under the site's CSP (no inline script or style) and link them from the footer in `web/index.html`, where they now read « en préparation ». |

---

## Verified facts used in the drafts

Each was checked on 2026-11-08 in the file named, or in the Vercel project settings where stated.

| Fact | Source |
|---|---|
| Site URL `auction-agent-mvp.vercel.app` | README "Online MVP"; Vercel project aliases |
| Protected by Vercel Authentication, on all deployments; no password or IP protection | README "Access"; `web/api/snapshot.py` docstring; Vercel API `get_project` (`ssoProtection.deploymentType: "all"`) |
| Function region `cdg1` | Vercel API `get_deployment` (`regions: ["cdg1"]`); **not in the repo** |
| noindex at every layer | `web/robots.txt`; `web/vercel.json` (`X-Robots-Tag`); `web/index.html` and `web/about.html` (`<meta name="robots">`) |
| Only network request: same-origin `GET /api/snapshot`; `localStorage` banned; no third-party resource; system fonts | `web/app.js` (`fetch`); `tests/test_site.py` (banned list); `web/vercel.json` (CSP `default-src 'self'`, `connect-src 'self'`, `form-action 'none'`); README |
| `Referrer-Policy: no-referrer`; outbound links `noopener noreferrer`, new tab | `web/vercel.json`; `web/app.js` |
| Snapshot built from a whitelist: no VIN or VIN hash, plate, seller comment, photo or internal id; links to the Alcopa listing only | `auction_agent/snapshot.py` docstring and `_url`/`is_alcopa_url`; README "How it updates" |
| Snapshot stored privately in Vercel Blob, overwritten on each publish | `auction_agent/blobstore.py` (`access="private"`, `allow_overwrite=True`) |
| VINs and plates never displayed or sent to an AI, and not stored in clear in the database; keyed HMAC kept as pseudonymisation, deleted with the vehicle; owner fields redacted | `auction_agent/privacy.py`; README legal notes |
| PDFs read in memory; **exception:** an unparseable PDF is kept unredacted in `data/quarantine/` (at most 50 files), deleted after 7 days by the next crawl | `auction_agent/documents.py` (`quarantine`, `sweep_quarantine`); `auction_agent/config.py`; `auction_agent/ingest.py` |
| Database stays on the owner's PC; only the snapshot leaves | README "How it updates" §4 |
| No purge yet (Phase 1) | `auction_agent/ingest.py` docstring; MASTER_PLAN App. C |
| AI review by Anthropic `claude-haiku-5-5` before upload; what is sent and never sent; Anthropic's terms not checked; OpenAI selectable | README "Optional: AI review" ("What is sent", "Data", "Switching back to OpenAI"); MASTER_PLAN §4 |
| Weights 35 / 35 / 30, tiers from 80 and 60, partial data capped below favourable | `auction_agent/scoring/heuristics.py` (`WEIGHTS`); `web/about.html`; MASTER_PLAN §4 |
| AI verdict labelled « Avis IA », « peut se tromper » | README; `web/about.html`; `tests/test_site.py` |
| All-in price formula and fee figures (to check against the CGV) | `auction_agent/snapshot.py` (`fees_note`); `web/about.html`; MASTER_PLAN §3 |
| Notice when the data is more than 8 days old | `web/app.js` (`STALE_DAYS = 8`) |
| Independence and "not purchase advice" statements | `web/index.html` footer; `web/about.html` |
| Planned `/methode` with takedown contact and opt-out; public mode top-N only | MASTER_PLAN §5 |
| Database-right risk; Alcopa CGV reserve descriptions and photos | README legal notes; MASTER_PLAN §1 (item 7) and App. A |
| No operator identity anywhere in the repo | repo search, 2026-11-08 |
