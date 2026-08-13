# TheHub — GEX Levels Autofill

Extension Chrome (Manifest V3) qui remplit automatiquement l'indicateur
TradingView **GEX Daily Levels** avec tes niveaux TheHub — plus besoin
d'ouvrir gexdash pour copier-coller la string Pine à chaque séance.

## Installation (mode développeur)

1. Ouvrir `chrome://extensions`.
2. Activer **Mode développeur** (interrupteur en haut à droite).
3. Cliquer **Charger l'extension non empaquetée** et sélectionner ce dossier
   (`extension/`).
4. Épingler l'icône de l'extension dans la barre d'outils (facultatif, mais
   pratique pour accéder rapidement à la popup).

## Prérequis

- Un compte gexdash avec une **clé API personnelle** — disponible sur
  [gexdash.wealthbuilders.group/profile](https://gexdash.wealthbuilders.group/profile),
  section « Clé API personnelle » (bouton *Copier*, ou *Régénérer* si besoin
  d'une nouvelle clé).
- L'indicateur **GEX Daily Levels** doit déjà être posé sur le graphique
  TradingView, avec le champ **Market** sur `Auto` (ou le marché voulu) — la
  détection NQ/ES/SPX/GOLD se fait par symbole dans l'indicateur lui-même,
  l'extension ne fait que remplir les 5 champs, jamais de choix de marché.

## Utilisation

1. Cliquer l'icône de l'extension, coller la clé API, **Enregistrer**.
2. Le bouton **Tester la connexion** confirme que la clé est valide avant
   d'enregistrer quoi que ce soit.
3. Laisser un onglet TradingView ouvert avec le graphique et l'indicateur
   posés : le service worker interroge `/api/mylevels` toutes les 5 minutes
   et ne fait rien si rien n'a changé (comparaison par hash). Il n'interroge
   pas du tout si aucun onglet TradingView n'est ouvert.
4. **Synchroniser maintenant** force un cycle immédiat (utile juste après
   avoir posé l'indicateur, ou pour vérifier que tout fonctionne).
5. L'interrupteur **Synchronisation active** coupe le polling périodique
   sans supprimer la clé enregistrée.

### Fraîcheur : démarrage + alarmes calées sur les publications

Les niveaux/pine ne changent réellement qu'aux deux publications QStash
canoniques (00h11 et 15h25 UTC — cf. `CLAUDE.md` à la racine du dépôt), pas
en continu. Deux mécanismes s'ajoutent au sondage générique de 5 minutes
(`background.js`) :

- **Démarrage** : au lancement du navigateur (ou install/mise à jour de
  l'extension), une vérification de fraîcheur se déclenche immédiatement au
  lieu d'attendre le premier tick de l'alarme périodique — sans ça,
  `chrome.alarms.create` avec seulement `periodInMinutes` ne tire pas avant
  la première période écoulée, donc jusqu'à 5 minutes de retard possible
  juste après un démarrage.
- **Alarmes alignées** (`ALIGN_ALARMS`) : deux alarmes dédiées, recalées
  chaque jour sur 00h13 et 15h27 UTC (horaire de publication + 2 min de
  marge de propagation), pour un remplissage quasi immédiat après chaque
  publication réelle plutôt que de dépendre du tick générique — qui n'est
  pas calé sur l'horloge serveur et peut donc traîner jusqu'à ~5 min pile
  après une publication.

Les deux respectent les mêmes garde-fous que le sondage générique
(`enabled`, `backoff`, aucun onglet TradingView ouvert = aucun appel
réseau) — ce sont des déclencheurs supplémentaires, pas un chemin qui
contourne quoi que ce soit.

## Comment le remplissage fonctionne réellement (`content.js`)

TradingView n'expose aucune API publique pour piloter ses dialogues, et son
DOM a deux pièges déjà rencontrés en production — les connaître évite de
recasser un correctif déjà validé :

- **Les champs de niveaux sont des `<input>` sans attribut `type="text"`
  dans le markup** (le type "text" est la valeur implicite par défaut du
  navigateur). Un sélecteur CSS `input[type="text"]` ne matche donc **rien**
  — il faut filtrer sur la propriété IDL `el.type` (`textInputs()`), jamais
  sur `getAttribute`/le sélecteur CSS. Sur la fenêtre de paramètres, les 5
  champs de niveaux sont les 5 **premiers** champs texte trouvés, dans
  l'ordre de déclaration du script Pine (NQ, ES, SPX, GOLD GC, GOLD
  XAUUSD) — vérifié en prod, ce ne sont pas des `<textarea>`.
- **TradingView n'ouvre sa fenêtre de paramètres ni sur un simple
  `dispatchEvent(dblclick)` ni sur un `.click()` isolé.** Piste initialement
  suivie (13/08/2026) : `isTrusted:false`, écarté par A/B testing point par
  point — les événements synthétiques restent `isTrusted:false` du début à
  la fin et fonctionnent quand même. La vraie cause : le système interne de
  gestion de pointeur de TradingView attend la **chorégraphie complète d'un
  vrai geste** (`pointerdown` → `mousedown` → `pointerup` → `mouseup` →
  `click`, avec de vrais objets `PointerEvent` porteurs de
  `pointerId`/`pointerType`/`isPrimary`), et seulement sur le **bon élément
  cible** (le libellé de titre `[class*="title-"]` à l'intérieur de la
  ligne de légende, pas la ligne entière — dont la largeur peut déborder
  sur le canvas du chart). Ni la cible seule ni la séquence seule ne
  suffisent ; vérifié en isolant chaque variable séparément (4 tests
  A/B/C/D croisant cible × complétude de la séquence, un seul combo marche).
- **Le panneau de légende peut être replié** (repli manuel de l'utilisateur,
  ou état par défaut avec beaucoup d'indicateurs sur le graphique — icône
  « ⌄ N » en haut à gauche). L'indicateur reste trouvable dans le DOM par
  `findIndicatorLegendItem()` (le texte n'a pas besoin d'être visible), mais
  son `getBoundingClientRect()` est vide et `offsetParent === null` —
  vérifié en prod. `verifiedPoint()` rejette alors tout clic (aucun point
  valide), ce qui est le comportement correct plutôt que de cliquer à
  l'aveugle, mais faisait partir inutilement sur le repli presse-papiers.
  `ensureLegendsExpanded()` déplie systématiquement, avant la recherche de
  l'indicateur, tout panneau `[class*="sourcesWrapper-"]` marqué
  `closed-*` en cliquant son bouton `[class*="toggler-"]` (plusieurs panes
  = plusieurs wrappers indépendants, tous traités) — sans effet si tout est
  déjà déplié.

La conséquence directe : tout clic qui doit réellement déclencher une
action TradingView (double-clic sur la légende, bouton réglages) passe par
**`syntheticClick()`** (`fireSequence()` pour la chorégraphie). Le
changement d'onglet et la validation finale, eux, répondent très bien à un
simple `.click()` — vérifié séparément, pas besoin d'alourdir ces deux-là.

Avant chaque `syntheticClick()`, une garde de sécurité (`verifiedPoint()`)
vérifie par `document.elementFromPoint()` que le point de clic calculé
retombe bien sur l'élément visé (ou un de ses descendants) — jamais de clic
à l'aveugle sur une coordonnée. Cette garde n'est pas cosmétique : les
boutons d'exécution **BUY/SELL** sont physiquement juste à côté de la
légende dans l'UI TradingView, et une ligne de légende peut avoir une boîte
qui déborde largement sur le canvas du chart (son centre géométrique tombe
alors sur une bougie, pas sur le texte). Si le point ne vérifie pas, le clic
est annulé plutôt que tenté à l'aveugle.

Aucune permission `"debugger"`/Chrome DevTools Protocol n'est nécessaire —
un temps envisagée puis retirée une fois la vraie cause identifiée. C'est
une permission sensible côté Chrome Web Store (revue manuelle, délai
parfois long) : ne pas la réintroduire sans avoir d'abord re-testé qu'un
`dispatchEvent` complet (cible + chorégraphie) ne suffit vraiment plus.

## Si le remplissage automatique échoue

L'extension ne reste jamais silencieuse en cas d'échec (indicateur absent du
graphique, fenêtre de paramètres introuvable, champ de configuration
détecté à la place d'un champ de niveaux, sélecteur cassé par une mise à
jour TradingView) : les 5 strings Pine sont copiées dans le presse-papiers
et une notification Chrome explique quoi faire (coller manuellement dans
l'onglet **Inputs** de l'indicateur). Une extension qui échoue en silence
est pire qu'un collage manuel.

Chaque étape logge sous le préfixe `[GEX]` dans la console de la page
TradingView (F12) — c'est la première chose à regarder en cas d'échec :
elle indique exactement quel maillon a cassé (indicateur introuvable,
fenêtre non détectée, clic annulé faute de point valide, champ jugé
dangereux à écraser, valeur non retenue après écriture...).

Erreurs réseau spécifiques, affichées distinctement dans la popup et en
notification :

- **401** — clé invalide ou manquante.
- **403** — clé bloquée ou révoquée (regénérer une clé sur `/profile`).
- **429** — trop de requêtes, l'extension recule automatiquement
  (`backoff` exponentiel jusqu'à 60 min) sans jamais boucler dessus.

## Sécurité

La clé API est stockée uniquement dans `chrome.storage.local` (jamais dans
le code source, jamais en clair dans les logs). Elle est envoyée en paramètre
d'URL (`?key=...`) à `/api/mylevels`, exactement comme le fait le reste de
l'écosystème gexdash (widget Flux embarquable) — voir `CLAUDE.md` à la racine
du dépôt pour le raisonnement (pas de header custom, pour éviter un
préflight CORS que le serveur ne sait pas répondre).

## Règle générale : sélecteurs TradingView

Tous les sélecteurs CSS ciblant le DOM de TradingView (`content.js`) doivent
utiliser des **préfixes de classe** (`[class*="study-"]`, `[class*="item-"]`,
…), **jamais une classe complète**. Exemple relevé en prod sur la légende
d'un indicateur :

```html
<div class="withCustomTextColor-quatTGAC item-quatTGAC study-quatTGAC has5Buttons-quatTGAC">
  GEX Daily Levels
</div>
```

Le suffixe `-quatTGAC` est un hash de build TradingView : il **change à
chaque déploiement**, sans préavis ni changelog public. Un sélecteur sur la
classe complète (`.study-quatTGAC`) casse au prochain déploiement ; un
sélecteur par préfixe (`[class*="study-"]`) survit tant que la convention de
nommage elle-même ne change pas. `findIndicatorLegendItem()` illustre le
patron à suivre : cascade de stratégies du plus précis au plus large, jamais
une seule classe exacte comme unique piste.

## Tester après une modification

Ouvrir un graphique avec l'indicateur posé, cliquer **Synchroniser
maintenant** dans la popup, observer si les 5 champs se remplissent. En cas
d'échec, ouvrir les devtools sur la page TradingView et lire les logs
`[GEX]` dans l'ordre — c'est presque toujours le premier maillon cassé qui
déclenche le repli presse-papiers.
