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
« voici l'exposition aux strikes ». Le module dit « **si** le prix arrivait
ici, **à** telle heure, voici le flux que les dealers seront contraints
d'exécuter ». C'est prospectif, pas descriptif.

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

Pour chaque couple (prix, temps), recalculer l'agrégat sur toute la chaîne.
Sortie : matrice `[temps][prix] → valeur signée`.

L'axe temporel est **prospectif** : il montre l'évolution mécanique du flux
d'ici la clôture, à prix constant. C'est là que le charm devient lisible — il
explique les dérives de fin de séance sans catalyseur.

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
| Crons | **Aucun nouveau cron.** Les trois tirs intrajournaliers suffisent |
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

**Contrôles ciblés.** Le serveur démarre, `/api/flow` répond, `test_html_ids.py`
et `node --check` si le HTML est touché.

**Non-régression.** `_gex_core.py` ne doit rien changer aux niveaux ni au Pine :
vérifier par `git diff` que les fonctions existantes sont intactes, et que
`gex_levels.py --selftest` passe.

Pas de batterie exhaustive au-delà de ça.

---

## Points ouverts à trancher pendant l'implémentation

- **Granularité de la grille.** Nombre de pas et amplitude, en σ ou en
  pourcentage. Contrainte : lisible sur mobile.
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
