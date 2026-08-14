# Module « Flux » — brief d'implémentation

Repo : `Sassakes/gexdash` · Lire `CLAUDE.md` avant toute modification.

---

## Objectif

Afficher le **delta que les teneurs de marché devront couvrir** si le prix
bouge et si le temps passe — projeté sur une matrice prix × temps, rendue en
dégradé vert/rouge.

Vert = gamma dealer positif : la couverture va **contre** le mouvement, elle
amortit. Zones de compression, de pin, de range.
Rouge = gamma dealer négatif : la couverture va **avec** le mouvement, elle
amplifie. Zones d'accélération, de cassure.

Ce que ça apporte par rapport au GEX par strike existant : le GEX actuel dit
« voici l'exposition aux strikes, maintenant ». Le module dit « voici comment
**cette même** exposition, strike par strike, évolue mécaniquement d'ici la
clôture ». Les concentrations affichées sont donc les mêmes que celles de
`gex_by_strike` (cf. section « Matrice prix × temps » pour pourquoi un
recalcul plein-chaîne à un prix hypothétique les effaçait) ; ce que la
matrice ajoute, c'est leur trajectoire dans le temps qui reste.

---

## Périmètre — livraison en deux temps

**Étape 1 — gamma seul.** C'est la seule des trois grecques vérifiable : le
GEX par strike existe déjà dans le payload. Si la projection est incohérente
avec lui, ça se voit immédiatement. Le gamma sert donc de **contrôle de
justesse pour tout le moteur de grecques**.

**Étape 2 — vanna et charm**, une fois le gamma validé. Même moteur, donc
faible risque supplémentaire. Ne pas les implémenter avant validation de
l'étape 1 : ils ne sont comparables à rien, une erreur y serait invisible.

Ce brief couvre les deux étapes ; livrer l'étape 1 d'abord et attendre
validation.

---

## Emplacement dans l'interface

**Nouveau bouton `FLUX` dans la barre d'onglets marchés**, à côté de GOLD.
Il ne sélectionne pas un marché : il **bascule le contenu du panneau central**.

- Chart de prix ↔ graphique de flux, un clic pour revenir.
- Le bandeau de métriques, les niveaux et les contrôles restent en place.
- Le marché sélectionné (NQ / ES / SPX / GC / XAU) reste actif : le flux
  affiché est celui du marché courant.
- Choix mémorisé entre sessions.

Raison du panneau dédié plutôt qu'une couche sur le chart : axes, échelle et
lecture sont différents. Superposer créerait la même confusion que les deux
boutons EM.

---

## Principe de calcul

### Grecques Black-Scholes

Pour chaque option, à un prix candidat `S` et un temps restant `T` :

```
d1 = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
d2 = d1 - σ·√T
φ(x) = exp(-x²/2) / √(2π)
```

```
Gamma  Γ = e^(-qT) · φ(d1) / (S · σ · √T)
Vega   ν = S · e^(-qT) · φ(d1) · √T
Vanna  = -e^(-qT) · φ(d1) · d2 / σ
Charm  = -e^(-qT) · φ(d1) · [2(r-q)T - d2·σ·√T] / (2·T·σ·√T)
```

Python pur, module `math`. Pas de scipy.

### Exposition dealer

**Convention de signe : réutiliser celle de `gex_by_strike`.** Ne jamais en
introduire une seconde — une convention concurrente produit des signes inversés
invisibles à la relecture. Lire le code avant d'écrire.

```
GEX$(K, S, T)   = OI_K · mult · Γ · S² · 0.01 · sign_K
VANNA$(K, S, T) = OI_K · mult · Vanna · S · 0.01 · sign_K
CHARM$(K, S, T) = OI_K · mult · Charm · S · sign_K
```

`mult` = 100 pour les chaînes options actions/ETF.
`S² · 0.01` = dollars de delta par mouvement de 1 %.

**Chaînes ETF** : appliquer le facteur `sources[].scale` **avant** tout calcul,
comme le fait `max_pain`. Ne jamais le recalculer.

### Matrice prix × temps

```
grille_prix = [S₀ · (1 + i·pas) for i in range(-N, N+1)]
grille_temps = [maintenant, +1h, +2h, ... jusqu'à la clôture]
```

**Révisé après livraison de l'étape 1.** La première version recalculait
l'agrégat sur **toute la chaîne** à chaque couple (prix, temps) — l'exposition
totale du book *si* le prix arrivait là, le même objet que `zero_gamma_flip`.
Vérifié sur chaîne réelle (NQ, grille fine à 10 points d'indice, T par option
y compris les 0DTE) : cette somme est lisse par construction, y compris aux
strikes à fort OI qui font pourtant de vraies bosses dans `gex_by_strike` —
des milliers de cloches Black-Scholes qui se chevauchent effacent toute trace
de concentration, quels que soient la finesse de la grille ou le choix de
normalisation en aval. Rendu : deux blocs de couleur uniformes, jamais les
concentrations autour des gros strikes que ce module doit montrer.

**Densité localisée par strike, retenue à la place.** Chaque option est
classée **une fois** dans la colonne de grille la plus proche de son propre
strike (échelle indice `K / scale`, même convention que `per_strike_gex`) —
elle partitionne la chaîne au lieu de la resommer à chaque prix candidat. Le
gamma/vanna/charm de chaque colonne est ensuite recalculé au prix de **cette**
colonne et au temps restant de la ligne courante. Une option dont le strike
tombe hors de la grille affichée est exclue (pas compressée dans la colonne de
bord, qui deviendrait une valeur extrême isolée écrasant tout le dégradé) —
sauf pour une grille à une seule colonne (contrôle de justesse ci-dessous),
qui ne fenêtre jamais. Implémentation : `flow_gamma_matrix` dans
`api/_gex_core.py`, commentaire détaillé au-dessus de la fonction.

Sortie inchangée : matrice `[temps][prix] → valeur signée`. L'axe temporel
reste **prospectif** — chaque colonne montre comment SA propre concentration
évolue mécaniquement d'ici la clôture, à prix constant pour cette colonne.
C'est toujours là que le charm devient lisible — il explique les dérives de
fin de séance sans catalyseur.

### Volatilité et paramètres

- `σ` : IV par option issue de la chaîne. Traitement du skew en **sticky
  strike** pour commencer — chaque strike conserve son IV observée. Exposer le
  choix en paramètre pour pouvoir tester sticky delta plus tard.
- `T` : **s'aligner sur la base temporelle déjà utilisée par le module EM**
  (`/api/emlive`, fonctions `_session_progress` / `_variance_remaining`).
  Vérifier laquelle est en place et la reprendre. Ne pas en introduire une
  seconde.
- `r` : valeur fixe en configuration, raisonnable et documentée.
- `q` : nul pour les indices en futures.
- Échéances : les mêmes que `build_payload`. Le charm n'a de sens marqué que
  sur les échéances proches ; agréger du lointain dilue le signal.

---

## Décisions déjà prises

| Sujet | Décision |
|---|---|
| Moteur | `api/_gex_core.py`, à côté de `_strike_profile` |
| Endpoint | `GET /api/flow?target=NQ` — validation `target not in TARGETS` |
| Calcul | Dans le **cron intrajournalier existant** (`?intraday=1`), résultat en cache Redis. Endpoint en **lecture seule** |
| Cache | Clé `gex:flow:{target}:{date}`, TTL 2 jours |
| Crons | Pas de nouvel endpoint : `?intraday=1` existant, cadencé plus finement (10 min en séance, 5 min sur la dernière heure — cf. `vercel.json`) pour que le dégradé évolue par petits pas plutôt que par sauts |
| Verrou | Ne **jamais** écrire dans `levels`, `gex_by_strike`, `open_grid`, `expected_move`, `pine`. `_freeze_levels` doit rester valide |
| Discord | Aucun webhook pour ce module |
| Front | JS vanilla, canvas. Pas de framework, pas de build |
| Bilingue | Dictionnaire `T` existant, FR et EN |
| Mobile | Le panneau doit rester lisible en portrait |

---

## Rendu

Canvas 2D. Axe vertical = prix (aligné sur l'échelle du chart de prix pour que
l'œil fasse le lien), axe horizontal = temps jusqu'à la clôture.

Chaque cellule colorée selon la valeur signée normalisée : vert pour le gamma
dealer positif, rouge pour le négatif, intensité proportionnelle à la
magnitude. Interpolation ou lissage léger pour l'effet de dégradé continu.

**Transition entre deux tirs de cron.** Le cron tournant toutes les 5-10 min
en séance, remplacer la matrice affichée d'un bloc au prochain sondage
`/api/flow` (poll client, 2 min) donnerait un saut visible plutôt qu'une
évolution. Côté client (`fluxDisplayData`, `index.html`) : quand une nouvelle
matrice a exactement la même forme que la précédente (mêmes colonnes de prix,
même nombre d'heures), le dégradé affiché est interpolé cellule à cellule sur
~1s ; sinon (changement de marché, franchissement d'une heure pleine qui
raccourcit `hours`) le nouveau dégradé s'affiche directement. Purement
cosmétique : les tooltips/synthèse/repères passent à la nouvelle valeur dès
qu'elle arrive, seul le fond dégradé transitionne.

Superposer les repères existants pour la lecture : prix courant, et les
niveaux clés déjà calculés (Call Wall, Put Wall, Gamma Flip).

**Normalisation** : par percentile plutôt que par min/max — une valeur extrême
isolée écraserait tout le reste du dégradé.

Palette : reprendre les couleurs du terminal (teal `#26A69A`, rouge `#EF5350`,
or `#F0B90B`). Aucune nouvelle identité visuelle.

Étape 2 : sélecteur gamma / vanna / charm, affichés **séparément**, gamma par
défaut. Ne pas fondre les trois en un agrégat unique — leurs unités et leurs
interprétations diffèrent.

---

## Validation

**Contrôle de justesse (étape 1, obligatoire).** À `T` courant et sur la
colonne correspondant au spot actuel, la projection gamma doit être cohérente
avec `gex_by_strike` du payload publié. Écart significatif = erreur de
convention, d'échelle ou de formule. C'est le test qui compte le plus.
Implémentation : `flow_gamma_matrix` appelée avec une grille à **une seule**
colonne (`[spot]`) — le fenêtrage par colonne (cf. « Matrice prix × temps »)
se désactive alors et toute la chaîne y retombe, donnant directement le total
attendu. Toujours vrai après le passage à la densité localisée par strike :
mesuré à 3,3 % d'écart sur chaîne NQ réelle, sous le seuil `FLOW_CHECK_TOL_PCT`
(20 %) déjà en place.

**Contrôles ciblés.** Le serveur démarre, `/api/flow` répond, `test_html_ids.py`
et `node --check` si le HTML est touché.

**Non-régression.** `_gex_core.py` ne doit rien changer aux niveaux ni au Pine :
vérifier par `git diff` que les fonctions existantes sont intactes, et que
`gex_levels.py --selftest` passe.

Pas de batterie exhaustive au-delà de ça.

---

## Points ouverts à trancher pendant l'implémentation

- ~~**Granularité de la grille.**~~ Tranché : 25 colonnes, pas 0.25σ, amplitude
  totale inchangée (+/-3σ, cf. `_flow_grids` dans `api/gex.py`) — deux fois
  plus fin que la grille d'origine (13 colonnes, pas 0.5σ). Nécessaire pour
  que la densité localisée par strike (cf. « Matrice prix × temps ») ait assez
  de colonnes pour séparer les gros strikes plutôt que de les mélanger dans un
  même bac. Le rendu restant un bitmap suréchantillonné côté client (jamais
  une cellule = un pixel), la finesse supplémentaire ne coûte rien en
  lisibilité mobile.
- **Pas temporel.** Une heure suffit-elle, ou faut-il resserrer sur la dernière
  heure de séance où le charm s'emballe ?
- **Normalisation entre marchés.** Valeurs brutes en dollars, ou rapportées à
  l'open interest total pour comparer NQ et ES ?
- **Δσ par défaut pour la vanna** (étape 2) : valeur fixe, ou dérivée de la
  variation d'IV observée en séance — `iv_ratio` existe déjà dans
  `/api/emlive` ?

Proposer un choix motivé pour chacun **avant** d'implémenter : ces paramètres
changent tous les chiffres en aval, les découvrir après coup impose de tout
reprendre.
