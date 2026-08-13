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
- **TradingView ignore les événements souris purement synthétiques**
  (`dispatchEvent`/`.click()` depuis un content script ont toujours
  `isTrusted:false`) pour ouvrir la fenêtre de paramètres — vérifié
  empiriquement : un double-clic dispatché sur la légende ne fait
  strictement rien, alors qu'un vrai double-clic sur le même élément
  fonctionne à l'identique. Aucun sélecteur ne peut contourner ce filtre de
  confiance du navigateur.

La conséquence directe du deuxième point : tout clic qui doit réellement
déclencher une action TradingView (double-clic sur la légende, bouton
réglages, changement d'onglet, validation finale) passe par
**`nativeClick()`**, qui délègue à `background.js` un clic généré au niveau
navigateur via **Chrome DevTools Protocol** (`Input.dispatchMouseEvent`,
permission `"debugger"`). C'est le seul mécanisme capable de produire un
événement `isTrusted:true` depuis une extension.

Avant chaque `nativeClick()`, une garde de sécurité (`verifiedPoint()`)
vérifie par `document.elementFromPoint()` que le point de clic calculé
retombe bien sur l'élément visé (ou un de ses descendants) — jamais de clic
à l'aveugle sur une coordonnée. Cette garde n'est pas cosmétique : les
boutons d'exécution **BUY/SELL** sont physiquement juste à côté de la
légende dans l'UI TradingView, et une ligne de légende peut avoir une boîte
qui déborde largement sur le canvas du chart (son centre géométrique tombe
alors sur une bougie, pas sur le texte). Si le point ne vérifie pas, le clic
natif est annulé et le code retombe sur l'ancien `dispatchEvent`/`.click()`
synthétique en dernier repli (best-effort, ne fonctionnera vraisemblablement
pas contre le filtre `isTrusted`, mais ne coûte rien à tenter si le clic
natif est indisponible — ex. devtools déjà ouverts sur l'onglet, ce qui fait
échouer `chrome.debugger.attach`).

`chrome.debugger.attach`/`detach` encadrent chaque clic le plus brièvement
possible (une poignée de commandes CDP) pour limiter le temps d'affichage du
bandeau Chrome *« ce navigateur est en cours de débogage »*, qui apparaît
et disparaît à chaque remplissage — attendu, pas un bug.

## Si le remplissage automatique échoue

L'extension ne reste jamais silencieuse en cas d'échec (indicateur absent du
graphique, fenêtre de paramètres introuvable, clic natif indisponible,
champ de configuration détecté à la place d'un champ de niveaux, sélecteur
cassé par une mise à jour TradingView) : les 5 strings Pine sont copiées
dans le presse-papiers et une notification Chrome explique quoi faire
(coller manuellement dans l'onglet **Inputs** de l'indicateur). Une
extension qui échoue en silence est pire qu'un collage manuel.

Chaque étape logge sous le préfixe `[GEX]` dans la console de la page
TradingView (F12) — c'est la première chose à regarder en cas d'échec :
elle indique exactement quel maillon a cassé (indicateur introuvable,
fenêtre non détectée, clic natif refusé, champ jugé dangereux à écraser,
valeur non retenue après écriture...).

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

La permission `"debugger"` (Chrome DevTools Protocol) n'est utilisée que
pour simuler un clic réellement fiable sur les éléments de l'indicateur GEX
Daily Levels — jamais pour lire ou modifier le contenu d'autres onglets, ni
pour intercepter du trafic réseau. `chrome.debugger.attach` est appelé juste
avant un clic et `detach` juste après (cf. section précédente).

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
maintenant** dans la popup, observer si les 5 champs se remplissent (le
bandeau jaune "débogage" doit apparaître brièvement). En cas d'échec, ouvrir
les devtools sur la page TradingView et lire les logs `[GEX]` dans l'ordre —
c'est presque toujours le premier maillon cassé qui déclenche le repli
presse-papiers.
