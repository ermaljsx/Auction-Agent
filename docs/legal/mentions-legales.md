# Mentions légales

> **Brouillon interne, non publié.** Ne pas mettre en ligne avant la relecture juridique et la décision de lancement public (phase 6 du MASTER_PLAN). « Auction France » est un nom de travail, à confirmer. Chaque `[À fournir : …]` renvoie à une ligne de [A_FOURNIR.md](A_FOURNIR.md).

Dernière mise à jour : [À fournir : date de publication — TERMS-12]

## 1. Le site

- **Nom :** Auction France [À fournir : nom public définitif — ID-9]
- **Adresse :** https://auction-agent-mvp.vercel.app [À fournir : nom de domaine définitif — LAUNCH-6]
- **Objet :** chaque semaine, l'analyse du catalogue public de la vente du lundi de la salle Alcopa de Beauvais (véhicules particuliers) : une note de 0 à 100 pour chaque lot, la lecture du contrôle technique, un prix tout compris estimé et, pour les voitures présentées en détail, un avis rédigé par une IA.
- **Accès :** réservé aux personnes invitées. Le site est protégé par le service d'authentification de son hébergeur (Vercel Authentication) ; il n'est pas ouvert au public.
- **Référencement :** le site demande aux moteurs de recherche de ne pas l'indexer (fichier robots.txt, en-tête HTTP et balise de chaque page). [À fournir : à revoir au lancement public — LAUNCH-4]

## 2. Éditeur

- **Nom et prénom, ou dénomination sociale :** [À fournir : identité de l'éditeur — ID-1]
- **Statut :** [À fournir : particulier non professionnel, entrepreneur individuel ou société — ID-2]
- **Adresse (domicile ou siège social) :** [À fournir : adresse postale de l'éditeur — ID-3]
- **Téléphone :** [À fournir : numéro de téléphone de l'éditeur — ID-4]
- **Adresse électronique :** [À fournir : adresse électronique de l'éditeur — ID-4]
- **Le cas échéant, immatriculation :** [À fournir : numéro RCS ou RM, SIRET, capital social, numéro de TVA intracommunautaire — ID-5]

> Note pour la relecture (à retirer avant publication) : selon le statut retenu (ID-2), un éditeur non professionnel peut, sous conditions, ne publier que les coordonnées de son hébergeur, après lui avoir communiqué son identité. Possibilité et choix à confirmer [À fournir : avis du juriste — ID-7]. Référence légale à citer [À fournir : article de la LCEN dans sa numérotation actuelle — ID-8].

## 3. Directeur de la publication

[À fournir : nom du directeur de la publication — ID-6]

## 4. Hébergeur

Le site et la fonction qui lit les données publiées sont hébergés par **Vercel**.

- **Dénomination sociale :** [À fournir : dénomination sociale exacte et forme juridique de l'hébergeur — HOST-1]
- **Adresse :** [À fournir : adresse postale de l'hébergeur — HOST-2]
- **Téléphone :** [À fournir : numéro de téléphone de l'hébergeur — HOST-3]
- **Région d'exécution des fonctions :** Paris (région Vercel « cdg1 ») [À fournir : confirmation, et décision de la mentionner ou non — HOST-4]
- **Stockage des données publiées :** un espace de stockage privé Vercel Blob [À fournir : localisation du stockage — HOST-5]

## 5. Sources des données et propriété intellectuelle

- Les informations sur les véhicules proviennent du catalogue public de la maison de vente Alcopa et des documents publiés avec chaque lot (procès-verbal de contrôle technique, certificat de batterie). Chaque fiche renvoie vers l'annonce d'origine.
- Le site ne reproduit ni les photos, ni les documents PDF, ni les commentaires du vendeur publiés par la maison de vente.
- Projet indépendant, sans lien avec Alcopa ni avec les maisons de vente. [À fournir : formulation validée après lecture des CGV et CGU d'Alcopa — TERMS-1]
- Droits sur les données de la maison de vente : [À fournir : position après la démarche de licence auprès d'Alcopa — LAUNCH-1]
- Proposition à valider (TERMS-8) : les noms et marques cités appartiennent à leurs titulaires. Les notes, fiches et textes propres au site ne peuvent pas être reproduits sans autorisation de l'éditeur.

## 6. Contact

Voir la page [Contact](contact.md). [À fournir : adresse de contact — CONTACT-1]

---

## Notes de rédaction (à retirer avant publication)

| Affirmation | Source vérifiée le 2026-11-08 |
|---|---|
| Adresse `auction-agent-mvp.vercel.app` | README « Online MVP » ; alias du projet Vercel |
| Vente du lundi, salle Alcopa de Beauvais, véhicules particuliers ; note de 0 à 100 ; CT ; prix tout compris ; avis IA | README « Online MVP » ; `web/about.html` |
| Accès réservé, Vercel Authentication | README « Access » ; `web/api/snapshot.py` ; API Vercel `get_project` (`ssoProtection: all`) |
| Non indexé (robots.txt, en-tête, balise) | `web/robots.txt` ; `web/vercel.json` (`X-Robots-Tag`) ; `web/index.html` et `web/about.html` (`<meta name="robots">`) |
| Hébergé par Vercel ; région `cdg1` | API Vercel `get_deployment` (`regions: ["cdg1"]`), **absent du dépôt** |
| Stockage privé Vercel Blob | `auction_agent/blobstore.py` (`access="private"`) |
| Sources : catalogue public Alcopa, PV de CT, certificat de batterie ; lien vers l'annonce d'origine | README « What it does today » et « How it updates » ; `auction_agent/snapshot.py` (`_url`, `is_alcopa_url`) |
| Ni photos, ni PDF, ni commentaires du vendeur | `auction_agent/snapshot.py` (docstring) ; README « Legal and ethical notes » |
| Projet indépendant, sans lien avec Alcopa | pied de page de `web/index.html` |
