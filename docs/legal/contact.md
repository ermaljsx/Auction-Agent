# Contact

> **Brouillon interne, non publié.** Ne pas mettre en ligne avant la relecture juridique et la décision de lancement public (phase 6 du MASTER_PLAN). « Auction France » est un nom de travail, à confirmer. Chaque `[À fournir : …]` renvoie à une ligne de [A_FOURNIR.md](A_FOURNIR.md).

Dernière mise à jour : [À fournir : date de publication — TERMS-12]

## 1. Écrire à l'éditeur

[À fournir : adresse électronique de contact — CONTACT-1]

Le site ne propose pas de formulaire : écrivez à l'adresse ci-dessus. [À fournir : choix d'un formulaire ou non — CONTACT-7]

## 2. Une question sur un lot ou une vente

Le site ne vend rien et ne gère pas les enchères. Pour un lot, la vente, les frais, la visite ou l'enlèvement d'un véhicule, adressez-vous à la maison de vente, depuis l'annonce d'origine : chaque fiche du site y renvoie.

## 3. Demander le retrait d'une fiche ou vous y opposer

Vous êtes titulaire d'un véhicule présenté, vendeur, maison de vente ou titulaire de droits sur ces données ? [À fournir : personnes pouvant faire une demande et justificatifs demandés — CONTACT-5]

- **Où écrire :** [À fournir : canal des demandes de retrait — CONTACT-2]
- **Ce qu'il faut indiquer :** le numéro du lot, la date de la vente, le lien vers l'annonce d'origine, le motif de la demande et un moyen de vous répondre.
- **Délai de réponse :** [À fournir : délai — CONTACT-3]
- **Ce qui se passe ensuite :** [À fournir : mécanisme de retrait, qui n'existe pas encore — CONTACT-4]

## 4. Vos données personnelles

Pour exercer vos droits (accès, rectification, effacement, opposition), écrivez à la même adresse. Voir la [politique de confidentialité](confidentialite.md). [À fournir : vérification d'identité et rédaction validée — PRIV-14]

## 5. Demander un accès au site

[À fournir : possibilité ou non de demander une invitation, et comment — CONTACT-8]

## 6. Le programme de collecte

[À fournir : identification du programme de collecte auprès des sites consultés, avec une adresse de contact, si l'éditeur le décide — CONTACT-6]

---

## Notes de rédaction (à retirer avant publication)

| Affirmation | Source vérifiée le 2026-11-08 |
|---|---|
| Pas de formulaire | `web/vercel.json` (CSP `form-action 'none'`) ; aucun `<form>` de contact dans `web/` |
| Le site ne vend rien et ne gère pas les enchères ; ventes et enchères sur le site de la maison de vente | pied de page de `web/index.html` ; site statique qui lit un seul fichier (`web/app.js`, `web/api/snapshot.py`) |
| Chaque fiche renvoie à l'annonce d'origine | `auction_agent/snapshot.py` (`_url`) ; README « Online MVP » |
| Retrait et opposition prévus, pas encore en place | MASTER_PLAN §5 (`/methode` : « takedown contact and opt-out ») ; aucune liste d'exclusion dans `auction_agent/` |
| Identification du programme de collecte (option) | README, tableau de configuration (`AUCTION_AGENT_USER_AGENT`) |
