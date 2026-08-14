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
`admin.html`, `flux.html`, `profile.html`, `privacy.html`, `ui.js` (partagé),
`flux-panel.js` (module Flux du terminal, extrait de `index.html` — cf.
section dédiée plus bas), `gex_levels.pine` (indicateur TradingView),
`quantower/` (indicateur C#), `widget/` (widget Flux embarquable, cf. section
dédiée plus bas).

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
3. `python test_html_ids.py`
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
de secours, intrajournaliers `?intraday=1` (jamais canoniques :
niveaux/gex_by_strike/open_grid/expected_move/pine gelés sur ceux déjà
publiés aujourd'hui, mais prix/basis/net_gex/régime/P·C OI/IV et le flux se
rafraîchissent à chaque tir — cf. garde de fraîcheur ci-dessous) toutes les
5 min de 12h30 (8h30 ET — pas
l'ouverture cash à 9h30 ET, une heure plus tôt pour couvrir les publications
macro US type CPI/NFP qui bougent déjà le future) à 13h30 (première heure)
et de 19h00 à 20h00 (dernière heure, clôture cash 16h ET inchangée), toutes
les 10 min entre les deux — cf. `vercel.json`. Bornes tenues IDENTIQUES entre
le cron serveur et `FLUX_SESSION_START_ET`/`FLUX_SESSION_END_ET` côté client
(`index.html`) : les faire dériver l'une de l'autre affiche une portion
"historique" que la matrice projetée ne couvre pas encore (ou plus).
Symétrique : les deux bornes de séance (ouverture, clôture) sont les moments
où l'historique du panneau Flux (cf. section ci-dessous) et le flux lui-même
bougent le plus vite, resserrer ailleurs coûterait un fetch CBOE en plus par
tir pour un gain de lisibilité marginal. Démarrer à l'ouverture réelle, pas
une heure après : un décalage ici laisse le panneau Flux figé toute la
première heure de séance sans que rien ne le signale (bug vu en prod le
2026-08-07, faux diagnostic possible si on suppose la séance déjà avancée
sans revérifier l'heure d'ouverture produit/ET). Côté client, la portion
"historique" (prix réalisé depuis le début de séance) doit être plafonnée à
la clôture, jamais à l'horloge murale — sinon le panneau semble "avancer"
tout seul après 16h ET et le week-end (bug vu en prod le 2026-08-08).
QStash : 00h11 daily, 15h25 publication. GitHub Actions : niveaux et macro.

**Garde de fraîcheur `/api/cron`.** Une cible dont le payload publié date
d'aujourd'hui et a été généré après `11:30:00` UTC ne redéclenche jamais un
recalcul CANONIQUE — niveaux/gex_by_strike/open_grid/expected_move/pine
restent ceux déjà publiés, `_freeze_levels` est inconditionnel sur ce
chemin, string Pine figée. `?force=1` republie tout (niveaux + Pine compris)
en ignorant cette garde.

Ce n'est PAS un no-op pour autant : un tir intrajournalier (`?intraday=1`)
qui tombe sur une cible déjà fraîche republie quand même prix/basis/
net_gex/régime/P·C OI/IV/flux (le fetch CBOE est de toute façon nécessaire
pour recalculer le flux — republier ne coûte qu'un `SET` Redis de plus par
cible et par tir, aucun appel réseau en plus). Sans ce republishing, le
bandeau et `_track_intraday` restent figés sur les valeurs de la
publication canonique jusqu'au lendemain (bug vu en prod le 2026-08-13 :
plus aucune métrique n'avait bougé depuis la publication de 13h25 UTC —
la garde de fraîcheur sautait la cible en entier au lieu de ne sauter que
le recalcul des niveaux). Ce chemin ne notifie JAMAIS Discord — ni l'embed
(`?notify=1` uniquement), ni le canal News (ces payloads n'alimentent
jamais `computed`, seule liste qui déclenche ce ping).

Réponse JSON de `/api/cron` sur ce chemin : `results[target]` porte
`flow_check`/`flow_skip_reason` (l'écart de contrôle de justesse vs
`net_gex_bn`, cf. `_refresh_flow`) — seul moyen de le lire depuis un appel
manuel sans fouiller `FLOW_CRON_LOG_KEY`. Ne pas les retirer de la réponse.

`flowforce` (ancien paramètre `?intraday=1&flowforce=1`) est **retiré
partout** (code, `vercel.json`, appels manuels) : il ne gate plus rien
depuis que le republishing de métriques ci-dessus tourne pour tout tir
`?intraday=1`, `flowforce=1` seul n'aurait plus déclenché que le même
recalcul de flux — un paramètre qui ne fait rien de plus que sans lui
laisse croire à tort qu'il contrôle quelque chose. Choix tranché plutôt
que de le garder inerte : `?intraday=1&key=...` suffit pour un test
manuel hors cron.

---

## Widget Flux embarquable

**Deux copies distinctes du moteur Flux existent, volontairement.**
`flux-panel.js` (module complet du terminal : historique, niveaux CW/PW/
Flip, strikes, synthèse, contexte volume — simple extraction de ce qui
vivait avant inline dans `index.html`, mêmes globals `FLUX_*`, mêmes
fonctions) et `widget/flux-widget.js` (composant embarquable public,
périmètre volontairement réduit à la projection seule, état par instance
`this.state`/`this.gfx`/`this.dom`). Les deux ne sont **pas** encore
unifiées — un bug corrigé dans l'un ne l'est pas automatiquement dans
l'autre, à garder en tête tant qu'une fusion n'a pas été faite.
`flux-panel.js` est chargé par `<script src>` dans le `<head>` de
`index.html`, juste après `ui.js` et avant le script inline principal qui
définit `$`/`T`/`CHART`/`TARGET`/`C`/`fmt` — ça ne pose pas de problème
d'ordre car toutes les références de `flux-panel.js` à ces globals sont à
l'intérieur de corps de fonction (résolues à l'appel, jamais au
chargement).

`widget/flux-widget.js` — panneau Flux (projection seule, v1 : pas
d'historique/niveaux/strikes) extrait en composant autonome, embarquable sur
un site tiers via `<script>` + `new TheHubFluxWidget(el, {key, ...})`. Chaque
instance porte son propre état (`this.state`/`this.gfx`/`this.dom`), pas de
globals `FLUX_*` partagés — plusieurs widgets peuvent coexister sur une même
page hôte.

**`/api/embed/flow` lit le même cache `FLOW_KEY` que `/api/flow`, en lecture
seule.** Aucun calcul n'est jamais déclenché sur ce chemin — même règle que
`/api/flow` lui-même. Ne pas y ajouter de logique qui recalculerait quoi que
ce soit : le module Flux (`docs/BRIEF-flux.md`) reste calculé uniquement par
le cron intrajournalier.

**La clé est en query param (`?key=...`), jamais en header `x-gex-key`.**
Ce fichier n'a aucun handler `do_OPTIONS`. Un header custom cross-origin
déclenche un preflight CORS que le serveur ne sait pas répondre aujourd'hui
(tombe sur le 501 par défaut de `BaseHTTPRequestHandler`) ; un `GET
?key=...` reste une requête CORS "simple", sans préflight. Ne pas migrer
vers un header sans ajouter `do_OPTIONS` d'abord.

**La clé n'est pas un secret à protéger** — `/api/flow` n'a lui-même aucune
authentification, la matrice gamma/vanna/charm n'est pas confidentielle.
C'est un contrôle de distribution/attribution/coût (qui a le droit
d'embarquer le widget en direct, et peut-on le lui retirer), d'où un blob
JSON en clair (`EMBED_KEYS_KEY`, même forme que `WEBHOOKS_KEY`) plutôt qu'un
schéma cryptographique — ne pas complexifier ce stockage sans une vraie
raison de protéger une donnée qui ne l'est pas.

**Délai de révocation ~60s, pas instantané.** `Cache-Control: s-maxage=60`
sur `/api/embed/flow` veut dire que l'edge Vercel peut resservir un 200 déjà
en cache sans repasser par la fonction Python — une clé tout juste révoquée
(`POST /api/embed-keys {"action":"revoke",...}`) peut donc continuer à
répondre jusqu'à une minute après. Attendu, pas un bug à corriger.

Pas de panel de gestion pour l'instant : `/api/embed-keys` (POST
create/revoke, GET liste masquée) est le seul point d'entrée, gardé par
`_auth_key()` comme `/api/webhooks`. Un futur panel n'aura qu'à l'habiller
d'une UI, aucune migration de données à prévoir.

---

## Chargement unifié (`loader.js`)

Overlay de marque partagé (anneau doré + `thehub-mark.png`, même motif que
`#fluxLoader`) qui masque les micro-temps de chargement plutôt que de
laisser voir une zone vide ou un texte brut — switch de marché NQ/ES/SPX/
GOLD, changement d'intervalle/fuseau du chart, ouverture de `news.html` /
`heatmap.html` / `dash.html`. Route statique `/loader.js` dans le `STATIC`
de `api/gex.py`, chargé par ces quatre pages.

API : `PageLoader.begin(container)` → jeton ; `PageLoader.end(container,
jeton)` → masque, avec un plancher `MIN_MS` (140ms) pour éviter qu'une
réponse déjà en cache ne fasse un flash au lieu d'une transition ;
`PageLoader.run(container, fn)` enchaîne les deux autour d'un `fn` async.
`container` doit être `position:relative` (ou plus) — l'overlay s'y ancre en
`position:absolute; inset:0`.

**Piège vérifié : un container dont l'`innerHTML` est réécrit après
`begin()` fait réapparaître le rond juste après l'affichage du contenu.**
`heatmap.html` reconstruit sa table via `host.innerHTML = html` dans
`render()`, ce qui détruit l'overlay monté ; `end()` en recrée alors un
nouveau — qui doit démarrer `hidden` par défaut dans `mount()`, sinon il
clignote visible une fraction de seconde après le tableau déjà affiché.
Ne pas retirer ce `hidden` par défaut sans revérifier chaque appelant.

**Par page :**
- `index.html` : `#stage` (englobe `#status`+`#app`) pour le switch de
  marché complet ; `#chartBox` seul pour un changement d'intervalle ou de
  fuseau (ne recouvre pas le reste de l'UI, qui reste valide).
- `heatmap.html` : un overlay par panneau, **seulement au tout premier
  chargement** (`LOAD_SHOWN`, un `Set` par cible) — un refresh périodique ne
  doit jamais recouvrir une table déjà peuplée de données valides.
- `dash.html` : un overlay par carte, seulement le tout premier fetch de
  `mountCard()`.
- `news.html` : overlay plein-page (`document.body`) tant que les fetches
  initiaux parallèles (macro/feed/news/cal/mag) n'ont pas tous résolu.

**Switch de marché parallélisé.** `levels.json`, `/api/chart` et
`/api/quote` étaient enchaînés en série (3 allers-retours réseau par clic
d'onglet) ; `prefetchBars()` (index.html) les lance désormais tous en
parallèle dès `loadTarget()`, ramenant la latence perçue à un seul
aller-retour. Le plancher `MIN_MS` a délibérément été baissé de 320 à 140ms
pour cette raison : un plancher haut au-dessus d'un fetch déjà rapide se
sent comme du lag ajouté, pas comme une transition.

**Garde anti-course (`TARGET_SEQ`).** Cliquer NQ puis ES très vite peut
faire revenir la réponse de NQ *après* celle d'ES si le réseau la retarde.
Chaque `loadTarget()` capture un numéro de séquence et vérifie, avant
chaque écriture DOM (application des données, fin de l'overlay), qu'aucun
switch plus récent n'a démarré entre-temps — sinon il abandonne sans
toucher à l'affichage. Sans cette garde, un switch rapide peut laisser
l'écran figé sur le mauvais marché après coup.

---

Utilisateur sous Git Bash / Windows : fournir des chemins Unix et des commandes
git **une par bloc**.
