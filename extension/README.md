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

## Si le remplissage automatique échoue

L'extension ne reste jamais silencieuse en cas d'échec (indicateur absent du
graphique, fenêtre de paramètres introuvable, sélecteur cassé par une mise à
jour TradingView) : les 5 strings Pine sont copiées dans le presse-papiers et
une notification Chrome explique quoi faire (coller manuellement dans
l'onglet **Inputs** de l'indicateur). Une extension qui échoue en silence est
pire qu'un collage manuel.

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

## Limites connues (non vérifiables sans navigateur réel)

Cette extension a été écrite sans accès à un TradingView réel. Le squelette
(manifest, polling, popup, presse-papiers, notifications, gestion 401/403/
429) est testable tel quel, mais tout ce qui touche au DOM effectif de
TradingView repose sur des heuristiques best-effort à revérifier :

- **Ouverture du dialogue de paramètres** (`content.js`,
  `openSettingsDialog`) — double-clic sur la légende en premier (geste
  utilisateur habituel), avec repli sur un bouton d'options révélé au
  survol si le double-clic ne produit aucune fenêtre dans le délai
  d'attente actif (`waitForDialog`). À confirmer que l'un des deux
  fonctionne sur la version actuelle de TradingView.
- **Détection de l'onglet Inputs** (`ensureInputsTabWithTextareas`) — clique
  chaque onglet du dialogue jusqu'à trouver 5 `<textarea>`, pour rester
  indépendant de la langue de l'UI TradingView (FR/EN/etc.). À confirmer que
  le dialogue s'ouvre bien avec `role="dialog"` et des `[role="tab"]`.
- **Association champ ↔ libellé** (`nearbyLabelText` / `mapFieldsToTextareas`)
  — remonte jusqu'à 6 niveaux de parents pour retrouver le texte du libellé
  à côté de chaque `<textarea>`. Un repli par ordre de déclaration (NQ, ES,
  SPX, GOLD GC, GOLD XAUUSD — l'ordre exact du script Pine) s'active si
  exactement 5 champs sont trouvés mais que le matching par libellé échoue.
- **Bouton de validation** (`findSubmitButton`) — cherche
  `[data-name="submit-button"]`, sinon prend le dernier bouton visible du
  dialogue. À confirmer que ce n'est pas "Annuler" sur la version actuelle.
- **`document.execCommand('insertText', …)`** pour simuler une saisie réelle
  (contourne les composants contrôlés React de TradingView) — fonctionne sur
  Chrome au moment de l'écriture mais est une API dépréciée ; un repli via le
  setter natif du prototype + événements `input`/`change` est en place si
  `execCommand` échoue ou si la valeur n'est pas retenue.

Tester dans l'ordre : ouvrir un graphique avec l'indicateur posé, cliquer
**Synchroniser maintenant** dans la popup, observer si les 5 champs se
remplissent. En cas d'échec, ouvrir les devtools sur la page TradingView et
vérifier lequel des sélecteurs ci-dessus ne trouve rien — c'est presque
toujours le premier maillon cassé qui déclenche le repli presse-papiers.
