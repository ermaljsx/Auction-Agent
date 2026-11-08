# Politique de confidentialité

> **Brouillon interne, non publié.** Ne pas mettre en ligne avant la relecture juridique et la décision de lancement public (phase 6 du MASTER_PLAN). « Auction France » est un nom de travail, à confirmer. Chaque `[À fournir : …]` renvoie à une ligne de [A_FOURNIR.md](A_FOURNIR.md).

Dernière mise à jour : [À fournir : date de publication — TERMS-12]

## 1. Responsable du traitement

[À fournir : identité du responsable du traitement — ID-1, PRIV-1]
Contact : [À fournir : adresse de contact — CONTACT-1]

## 2. En bref

- Le site ne fait pas de mesure d'audience, n'affiche pas de publicité et ne dépose aucun cookie lui-même. Il garde seulement dans votre navigateur la langue choisie (stockage local) et, le temps de l'onglet, les chiffres saisis dans le simulateur d'enchère et vos filtres lors d'un changement de langue (stockage de session). Rien de cela n'est envoyé au site.
- La page ne charge rien depuis d'autres sites. Elle fait une seule requête, vers le site lui-même, pour lire l'analyse de la semaine.
- Les données publiées ne contiennent ni numéro de série (VIN), ni plaque d'immatriculation, ni photo, ni commentaire du vendeur.
- L'hébergeur, Vercel, contrôle l'accès au site. À ce titre, il peut déposer des cookies et conserver des journaux de connexion (voir § 3). [À fournir : description vérifiée — PRIV-5, PRIV-6]

## 3. Quand vous consultez le site

**Ce que fait la page.** La page ne dépose aucun cookie. Elle utilise le stockage du navigateur pour trois préférences, qui ne quittent pas votre appareil : la langue (`af-lang`, stockage local, jusqu'à ce que vous l'effaciez), les chiffres du simulateur par lot (`af-sim-…`, stockage de session) et vos filtres lors d'un changement de langue (`af-carry`, stockage de session, lu puis effacé dès le chargement de la page suivante). [À vérifier par le juriste : ces stockages, strictement nécessaires à une fonction demandée par la personne, sont-ils exemptés de consentement ?] Elle charge ses polices, scripts et styles depuis le site lui-même uniquement. Ses requêtes de données vont vers le site lui-même uniquement, avec votre session d'accès : l'analyse (`/api/snapshot`) et les textes de la page (`/i18n/fr.json`, et `/i18n/en.json` pour la version anglaise). Les liens vers l'annonce d'origine s'ouvrent dans un nouvel onglet sans transmettre l'adresse du site.

**L'accès, géré par l'hébergeur.** L'accès est contrôlé par Vercel Authentication : vous vous connectez avec un compte Vercel, ou vous utilisez un lien de partage temporaire.
- Cookies déposés par Vercel pour cette authentification (nom, finalité, durée) : [À fournir : description et besoin ou non de consentement — PRIV-5]
- Données de compte visibles par l'éditeur (nom, adresse électronique des personnes invitées) : [À fournir : à vérifier — PRIV-5]
- Journaux de connexion tenus par Vercel (adresse IP, navigateur, pages demandées) et leur durée de conservation : [À fournir : description vérifiée et lien vers la politique de confidentialité de Vercel — PRIV-6]
- La fonction qui lit les données s'exécute dans la région Vercel de Paris (« cdg1 »). [À fournir : confirmation — HOST-4]

## 4. Les données sur les véhicules

**D'où elles viennent.** Un programme, sur l'ordinateur de l'éditeur, lit le catalogue public de la maison de vente Alcopa et les documents publiés avec chaque lot : procès-verbal de contrôle technique, certificat de batterie pour les voitures électriques et hybrides rechargeables.

**Comment elles sont protégées.**
- Les documents PDF sont lus en mémoire, puis le texte retenu est expurgé. Exception : un PDF que le programme n'arrive pas à lire peut être gardé tel quel sur l'ordinateur de l'éditeur, pour diagnostic (50 fichiers au plus), puis effacé par une collecte suivante une fois vieux de plus de 7 jours. [À fournir : maintien ou suppression de cette exception — PRIV-15]
- Les numéros de série (VIN) et les plaques d'immatriculation ne sont jamais affichés ni envoyés à une IA, et ne sont pas conservés en clair dans la base : le texte conservé les remplace par des marqueurs. Pour reconnaître une voiture remise en vente, le programme garde une empreinte du VIN calculée avec une clé secrète. C'est une pseudonymisation, pas une anonymisation. L'empreinte est supprimée avec la fiche du véhicule.
- Les champs qui peuvent désigner le titulaire du véhicule dans un document (propriétaire, titulaire, nom, adresse) sont expurgés.
- Les commentaires du vendeur sont conservés sur l'ordinateur de l'éditeur, sans VIN ni plaque. Ils ne sont jamais publiés.

**Ce qui est publié sur le site.** Une liste fermée de champs : caractéristiques du véhicule, note et raisons de la note, détail du prix, lecture du contrôle technique (les libellés des défaillances sont expurgés une seconde fois), dommages de carrosserie (zone et nature seulement), santé de la batterie, avis IA et lien vers l'annonce d'origine. La base de données reste sur l'ordinateur de l'éditeur ; seule cette analyse en sort.

**Les personnes concernées.** Ces données peuvent concerner des personnes qui ne consultent pas le site : titulaires des véhicules, vendeurs. [À fournir : base légale de la collecte, et information de ces personnes au titre de l'article 14 du RGPD — PRIV-2, PRIV-11, PRIV-13] Pour demander le retrait d'une fiche, voir la page [Contact](contact.md).

## 5. La relecture par une IA

Avant chaque publication, le programme envoie à Anthropic (modèle « claude-haiku-5-5 ») les données des voitures présentées sur le site, des autres voitures au profil favorable et de celles dont la note est proche d'un seuil (80 voitures au plus par publication, réglage par défaut), pour qu'une IA rédige un court avis ou propose un ajustement limité de la note.

- **Ce qui est envoyé :** marque, modèle, finition, énergie, boîte, année, kilométrage et cylindrée ; les risques moteur connus ; le résultat, la date et le kilométrage du contrôle technique, avec chacune de ses défaillances ; les dommages de carrosserie (zone, nature, nombre) ; la santé de la batterie ; la présence d'une garantie ; les commentaires du vendeur, expurgés des VIN, plaques, montants, numéros de téléphone, adresses électroniques et liens (1 000 caractères au plus).
- **Ce qui n'est jamais envoyé :** le prix, l'adresse de l'annonce, le VIN, la plaque, le texte brut du contrôle technique.
- **Données personnelles pouvant subsister dans les commentaires** (un nom, par exemple) : [À fournir : évaluation — PRIV-9]
- **Conditions d'Anthropic** (usage des données, durée de conservation, entraînement, lieu de traitement) : [À fournir : non vérifiées à ce jour — PRIV-8]
- Le programme peut aussi utiliser un autre fournisseur (OpenAI). Ce n'est pas le réglage actuel ; s'il change, cette section sera mise à jour. [À fournir : suivi — PRIV-10]

## 6. Destinataires et sous-traitants

- **Vercel :** hébergement du site, authentification des personnes invitées, stockage privé de l'analyse publiée. [À fournir : contrat de sous-traitance et lieu de traitement — PRIV-7]
- **Anthropic :** relecture par IA (§ 5). [À fournir : contrat de sous-traitance et lieu de traitement — PRIV-8]
- **Autre prestataire éventuel**, pour le stockage ou la synchronisation des fichiers de l'ordinateur de l'éditeur : [À fournir : à vérifier — PRIV-12]

## 7. Durées de conservation

- **Analyse publiée :** un seul fichier, remplacé à chaque publication. [À fournir : conservation éventuelle des versions précédentes par Vercel — PRIV-4, HOST-5]
- **Base de données de l'éditeur :** aucune suppression automatique pour l'instant. Une mise en sommeil, puis une suppression des véhicules sortis des ventes (avec une archive réduite de données dérivées) est prévue. [À fournir : durées retenues, y compris pour les journaux du programme et les avis IA conservés — PRIV-3]
- **Journaux de connexion de l'hébergeur :** [À fournir : durée — PRIV-6]

## 8. Base légale

[À fournir : base légale de chaque traitement (accès au site, collecte des données des ventes, relecture par IA, journaux) — PRIV-2]

## 9. Transferts hors de l'Union européenne

[À fournir : existence de transferts, pays et garanties, pour Vercel et Anthropic — PRIV-7, PRIV-8]

## 10. Vos droits

Rédaction type, à faire valider [À fournir : relecture du juriste — PRIV-14] : vous pouvez demander l'accès à vos données, leur rectification ou leur effacement, vous opposer à leur traitement ou en demander la limitation, et, lorsque c'est applicable, leur portabilité. Écrivez à [À fournir : adresse de contact — CONTACT-1]. Vous pouvez aussi adresser une réclamation à la CNIL.

---

## Notes de rédaction (à retirer avant publication)

| Affirmation | Source vérifiée le 2026-11-08 |
|---|---|
| Pas de mesure d'audience, de publicité ni de cookie propre ; stockage navigateur limité à la langue, au simulateur et aux filtres | `web/app.js` (fonction `store`, seule à toucher au stockage ; requêtes vers `/i18n/` et `/api/snapshot` seulement) ; `tests/test_site.py` (`test_app_js_rules`) ; aucun `document.cookie` dans `web/` |
| Ressources du site seulement ; une requête vers `/api/snapshot` avec la session | `web/vercel.json` (CSP `default-src 'self'`, `connect-src 'self'`) ; `web/app.js` (`credentials: "same-origin"`) ; README (polices système) |
| Liens externes sans transmission de l'adresse du site, nouvel onglet | `web/vercel.json` (`Referrer-Policy: no-referrer`) ; `web/app.js` (`noopener noreferrer`) |
| Vercel Authentication ; lien de partage temporaire | README « Access » ; API Vercel `get_project` (`ssoProtection: all`) |
| Cookies et journaux Vercel | **non documentés dans le dépôt** : PRIV-5, PRIV-6 |
| Région `cdg1` | API Vercel `get_deployment`, **absent du dépôt** |
| Collecte sur l'ordinateur de l'éditeur ; catalogue public Alcopa ; PV de CT, certificat batterie | README « What it does today », « Why your PC » |
| PDF lus en mémoire ; exception de la quarantaine (50 fichiers, 7 jours, effacement à la collecte) | `auction_agent/documents.py` (`quarantine`, `sweep_quarantine`) ; `auction_agent/config.py` ; `auction_agent/ingest.py` |
| VIN et plaques : jamais affichés ni envoyés à une IA, remplacés par des marqueurs ; empreinte HMAC = pseudonymisation, supprimée avec le véhicule | `auction_agent/privacy.py` (docstring, `redact`) ; README « Legal and ethical notes » |
| Champs du titulaire expurgés | `auction_agent/privacy.py` (`_OWNER_FIELD_RE`, `_redact_cv_holder`) |
| Commentaires conservés expurgés, jamais publiés | MASTER_PLAN §2 (`comments /*VIN/plate-redacted*/`) ; `auction_agent/snapshot.py` (docstring) |
| Liste fermée des champs publiés ; libellés expurgés à nouveau ; base restée sur le PC | `auction_agent/snapshot.py` ; README « How it updates » §4 |
| Voitures relues par l'IA (présentées, autres favorables, proches d'un seuil ; `LLM_LIMIT` 80 par défaut) ; ajustement borné | README « Optional: AI review » et tableau de configuration ; MASTER_PLAN §4 « LLM role » |
| IA : Anthropic `claude-haiku-5-5`, données envoyées et jamais envoyées ; conditions non vérifiées ; OpenAI possible | README « Optional: AI review » (« What is sent », « Data », « Switching back to OpenAI ») ; `auction_agent/privacy.py` (`scrub_for_llm`) |
| Analyse publiée remplacée à chaque publication | `auction_agent/blobstore.py` (`allow_overwrite=True`) |
| Pas de suppression automatique ; cycle prévu | `auction_agent/ingest.py` (docstring) ; MASTER_PLAN §2 et annexe C |
