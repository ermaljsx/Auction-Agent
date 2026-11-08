# Conditions d'utilisation

> **Brouillon interne, non publié.** Ne pas mettre en ligne avant la relecture juridique et la décision de lancement public (phase 6 du MASTER_PLAN). « Auction France » est un nom de travail, à confirmer. Chaque `[À fournir : …]` renvoie à une ligne de [A_FOURNIR.md](A_FOURNIR.md) ; chaque « Proposition à valider » est un choix de l'éditeur, pas un fait.

Dernière mise à jour : [À fournir : date de publication — TERMS-12]

## 1. Objet

Ces conditions encadrent l'utilisation du site Auction France [À fournir : nom public définitif — ID-9], édité par [À fournir : identité de l'éditeur — ID-1].

Le site analyse chaque semaine le catalogue public de la vente du lundi de la salle Alcopa de Beauvais (véhicules particuliers). Pour chaque lot, il présente une note de 0 à 100, la lecture du contrôle technique, un prix tout compris estimé et, pour les voitures détaillées, un avis rédigé par une IA.

Le site se présente aujourd'hui comme un projet personnel et non commercial. [À fournir : confirmation de ce statut pour la suite — TERMS-5]

## 2. Accès

- Le site est réservé aux personnes invitées. On y accède par le service d'authentification de l'hébergeur (Vercel Authentication) ou par un lien de partage temporaire. Le site lui-même ne crée pas de compte et ne demande aucune inscription.
- Conditions d'invitation, durée des liens de partage et retrait de l'accès : [À fournir : règles d'accès — TERMS-7]
- Proposition à valider (TERMS-7) : l'accès est personnel. Ne transmettez pas vos liens d'accès à d'autres personnes.

## 3. Nature des informations

**Une note selon nos critères, pas un conseil d'achat.** Les notes sont calculées par des règles fixes, à partir des données publiées par la maison de vente : fiabilité 35 %, état 35 %, prix 30 %. Les niveaux « Profil favorable selon nos critères » (80 et plus), « Profil correct selon nos critères » (60 à 79) et « Risque élevé selon nos critères » (moins de 60) sont des indications, expliquées sur la page Méthode. [À fournir : libellés uniques sur tout le site — TERMS-3] Ils ne constituent pas un conseil d'achat : vérifiez toujours l'annonce d'origine et le véhicule avant d'enchérir. [À fournir : formulation validée — TERMS-2]

**L'avis IA peut se tromper.** Un modèle d'IA relit les données de chaque voiture détaillée et rédige un court avis, signalé « Avis IA ». C'est un second regard, pas une expertise. [À fournir : suffisance de cette mention — TERMS-11]

**Des données parfois incomplètes ou anciennes.**
- Les informations viennent de la maison de vente. Son catalogue se remplit au fil des jours et les prix peuvent changer après la première publication. Tant qu'une donnée manque (mise à prix pas encore publiée, prix pas encore comparé, contrôle technique absent ou illisible), la note reste sous le niveau favorable et la fiche le signale.
- L'analyse est publiée en principe le jeudi soir et le samedi matin (heure de Paris). [À fournir : engagement de calendrier ou non — TERMS-6] Un avertissement s'affiche quand la dernière publication date de plus de 8 jours.

**Un prix tout compris estimé.** Il additionne la mise à prix (ou l'enchère courante quand une enchère est déjà portée), les frais acheteur (14,4 % TTC, au moins 360 € en salle), la TVA quand un particulier ne peut pas la récupérer, une estimation des réparations exigées par le contrôle technique et le prix d'un nouveau contrôle quand il faudra le refaire. Les frais fixes par lot ne sont pas inclus. Le prix final dépend des enchères. [À fournir : vérification des taux de frais dans les CGV de la maison de vente — TERMS-4]

## 4. Indépendance

- Projet indépendant, sans lien avec Alcopa ni avec les maisons de vente. [À fournir : formulation validée après lecture des CGV et CGU d'Alcopa — TERMS-1]
- Le site ne permet ni d'enchérir ni d'acheter. Les ventes, les enchères et leurs conditions se passent sur le site de la maison de vente.
- Chaque fiche renvoie vers l'annonce d'origine, qui s'ouvre dans un nouvel onglet.

## 5. Utilisation autorisée

Proposition à valider (TERMS-8) :
- Le site est destiné à un usage personnel, pour préparer ses propres enchères.
- Il est interdit d'extraire systématiquement, de rediffuser ou de revendre les notes, fiches et textes du site, et de contourner son authentification.

[À fournir : décision de l'éditeur et rédaction du juriste — TERMS-8]

## 6. Responsabilité

[À fournir : clause de responsabilité rédigée par le juriste — TERMS-9]

## 7. Données personnelles

Voir la [politique de confidentialité](confidentialite.md).

## 8. Signalement et demande de retrait

Voir la page [Contact](contact.md).

## 9. Modification des conditions et droit applicable

[À fournir : modalités de modification, droit applicable et juridiction compétente — TERMS-10]

---

## Notes de rédaction (à retirer avant publication)

| Affirmation | Source vérifiée le 2026-11-08 |
|---|---|
| Vente du lundi, salle Alcopa de Beauvais, véhicules particuliers ; note 0–100 ; CT ; prix tout compris ; avis IA sur les voitures détaillées | README « Online MVP » ; `web/about.html` |
| « Projet personnel et non commercial » | `web/about.html` |
| Accès réservé ; Vercel Authentication ; lien de partage temporaire ; pas de compte créé par le site | README « Access » ; `web/app.js` (seule requête : `/api/snapshot`) ; CSP `form-action 'none'` dans `web/vercel.json` |
| Pondération 35 / 35 / 30 ; seuils 80 et 60 | `auction_agent/scoring/heuristics.py` (`WEIGHTS`) ; `web/about.html` ; README (tableau des niveaux) |
| Libellés « … selon nos critères » | `auction_agent/report.py` (`TIER_LABELS`) ; MASTER_PLAN §4 « Labels » |
| Pas un conseil d'achat ; vérifier l'annonce et le véhicule | pied de page de `web/index.html` ; `web/about.html` |
| « Avis IA », « peut se tromper », second regard | README ; `web/about.html` ; `tests/test_site.py` |
| Catalogue rempli progressivement ; prix modifiés après la première publication | MASTER_PLAN §1 « Sparring », points 1 et 3 |
| Données manquantes : note plafonnée sous le niveau favorable | `web/about.html` ; MASTER_PLAN §4 « Confidence gate » |
| Publication jeudi soir et samedi matin | pied de page de `web/index.html` ; README « How it updates » |
| Avertissement au-delà de 8 jours | `web/app.js` (`STALE_DAYS = 8`) |
| Formule du prix tout compris ; 14,4 % TTC, minimum 360 € ; frais fixes non inclus | `auction_agent/snapshot.py` (`fees_note`) ; `web/about.html` ; MASTER_PLAN §3 (« to be checked against the CGV ») |
| Indépendance ; ventes et enchères sur le site de la maison de vente | pied de page de `web/index.html` |
| Lien vers l'annonce d'origine dans un nouvel onglet | `web/app.js` (`target: "_blank"`, `rel: "noopener noreferrer nofollow"`) |
