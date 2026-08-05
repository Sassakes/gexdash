# CLAUDE.md — contexte du dépôt gexdash

Terminal GEX pour futures NQ / ES / SPX / GOLD. Ce fichier est le contexte
permanent : le lire avant toute modification.

---

## Stack réelle

**Ce n'est PAS un projet Next.js / React.** Erreur d'hypothèse fréquente.

| Couche | Réalité |
|---|---|
| Backend | Python pur sur Vercel, `BaseHTTPRequestHandler`. Aucun framework |
| Routage | Tout passe par `api/gex.py` — un seul handler, branches `if path == ...` |
| Frontend | HTML/CSS/JS **vanilla** dans des fichiers monolithiques. Pas de build, pas de bundler, pas de composants |
| Persistance | Redis Upstash (payant) + fichiers JSON committés |
| Graphiques | `lightweight-charts` chargé par CDN |

Fichiers : `api/gex.py` (routes, crons), `api/_gex_core.py` (moteur de calcul),
`index.html` (terminal), `dash.html`, `heatmap.html`, `news.html`, `doc.html`,
`admin.html`, `ui.js` (partagé), `gex_levels.pine` (indicateur TradingView),
`quantower/` (indicateur C#).

---

## Règles non négociables

### Verrou des niveaux
`_freeze_levels` préserve `levels`, `gex_by_strike`, `open_grid`,
`expected_move` et `pine` du payload précédent. **Aucun nouveau module ne doit
écrire dans ces clés.** Si la string Pine change en séance, les utilisateurs
doivent tout recoller sur TradingView — c'est la régression la plus grave
possible.

Un cron intrajournalier n'est **jamais canonique** : il porte `?intraday=1`,
qui l'exclut du chemin de republication.

### Convention de signe dealer
Définie une seule fois dans `gex_by_strike` (`api/_gex_core.py`). **La
réutiliser, ne jamais en introduire une seconde.** Une convention concurrente
produit des signes inversés invisibles à la relecture.

### Base temporelle : 252 vs 365, ne pas les confondre
Deux conventions coexistent volontairement dans `api/_gex_core.py`, chacune
répond à une question différente :

- **`math.sqrt(252)`** (jours de bourse) dimensionne le module EM — convertit
  une IV annuelle en écart-type d'**un jour** (`open_grid`, `expected_move`).
  N'a de sens que pour ce dimensionnement-là.
- **`dte / 365.0`** (jours calendaires) est le `T` d'une grecque Black-Scholes
  recalculée à partir d'une option CBOE brute (`zero_gamma_flip`,
  `flow_gamma_matrix`). C'est la **seule** convention de `T` déjà validée
  contre `gex_by_strike` en production.

Tout nouveau calcul de grecque (vanna, charm, …) doit reprendre **365**,
jamais 252 — c'est ce qui rend un contrôle de justesse contre `gex_by_strike`
possible. Confondre les deux ne casse rien bruyamment : ça décale
silencieusement toutes les grecques recalculées.

### Échelle des chaînes ETF
NQ utilise la chaîne NDX, GOLD utilise GLD, etc. Le facteur de conversion est
dans `sources[].scale`. **L'appliquer, ne jamais le recalculer.** Bugs déjà
rencontrés : IV mesurée sur les mauvais strikes, Max Pain à l'échelle de l'ETF.

### Erreurs
Donnée manquante ou source muette → **503 avec motif**, jamais une exception
5xx. Les 5xx déclenchent des alertes d'anomalie Vercel.

### Redis
Payant. Pas de lecture par requête utilisateur : cache mémoire en amont,
regroupement des clés. Les sessions sont des jetons **signés**, pas stockés.

### Aucun appel réseau lourd sur le chemin utilisateur
Un endpoint qui télécharge un mois de bougies dépasse le délai d'exécution et
échoue silencieusement. Calibrations et agrégats lourds → **dans le cron**,
résultat en cache, endpoint en lecture seule.

---

## Frontend : pièges vérifiés

**`[hidden]` doit rester prioritaire.** Chaque page porte
`[hidden]{display:none !important}`. Une règle `display:` sur un id ou une
classe écrase l'attribut `hidden` — c'est ce qui avait rendu une fenêtre modale
impossible à fermer.

**Widgets tiers en iframe.** Ils se figent à la taille demandée à leur
construction. Mesurer **après** que le conteneur ait sa taille réelle (retenter
sur plusieurs frames), mémoriser la largeur utilisée, et n'installer
l'observateur de redimensionnement qu'ensuite — sinon deux constructions
concurrentes donnent un rendu instable d'un rechargement à l'autre.

**Bilinguisme FR/EN.** Dictionnaire `T = {fr:{...}, en:{...}}`, accès via
`t("clé")`. Toute chaîne visible passe par là, **y compris les libellés écrits
en dur dans le HTML** — ils doivent être réécrits au changement de langue.
Une clé absente affiche `undefined` à l'écran.

**Arrière-plan.** Rien ne se rafraîchit quand `document.hidden` est vrai.
Rythme piloté par `marketPollMs()` : rapide en séance US, lent hors séance,
nul le week-end.

**Mobile.** Cibles tactiles ≥ 40 px, champs à `font-size:16px` pour éviter le
zoom iOS, une seule section visible à la fois sur la page news.

---

## Spécificités marchés

**GOLD, deux échelles distinctes.** `GC` (future) et `XAU` (comptant) sont deux
marchés séparés, calculés à la source depuis la même chaîne GLD. Le future cote
au-dessus du comptant (portage), l'écart atteint plusieurs dizaines de dollars.

Yahoo **ne publie aucune série pour l'or comptant** (`XAUUSD=X` → 404). Le prix
vient de `gold-api.com` (gratuit, sans clé), la chart est dérivée du future
décalé de l'écart. Cet écart est **figé pour la séance** — une ouverture est un
point fixe, la recalculer la ferait dériver.

**Fenêtre de séance.** Globex 18h ET → 16h ET le lendemain. Elle traverse
minuit : tout calcul de progression doit gérer ce passage, sinon il retombe à
zéro dès la clôture cash.

---

## Avant de livrer

1. Test A/B NQ et ES : ancien moteur vs nouveau, niveaux et string Pine
   **identiques au caractère près**.
2. `python gex_levels.py --selftest`
3. Batteries existantes : `test_ab`, `test_auth`, `test_security`, `test_prod`,
   `test_feed`.
4. `node --check` sur le JS extrait de chaque page.
5. Vérifier qu'aucune référence DOM n'est orpheline.
6. Vérifier la symétrie FR/EN des dictionnaires.

Ne jamais livrer sans avoir exécuté le chemin de code réellement modifié. Un
bug de production récent — `name 'spot' is not defined` — venait d'une fonction
branchée à deux endroits dont un seul avait été testé.

---

## Environnement

Variables Vercel : `GEX_REFRESH_KEY` (admin), `GEX_AUTH_SECRET` (sessions),
`FINNHUB_API_KEY`, `KV_*` / `UPSTASH_*`.

Crons Vercel (UTC) : `30 14` publication de secours, `30 23` recalcul nocturne
de secours, `0 15` / `0 17` / `0 19` intrajournaliers (`?intraday=1`).
QStash : 00h11 daily, 15h25 publication. GitHub Actions : niveaux et macro.

Utilisateur sous Git Bash / Windows : fournir des chemins Unix et des commandes
git **une par bloc**.
