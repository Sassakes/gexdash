# Module « Flux Projection » — brief d'implémentation

Repo : `Sassakes/gexdash` · Cible : `api/_gex_core.py` + endpoint dans `api/gex.py`

---

## Objectif

Projeter, sur une grille de prix autour du spot, les flux de couverture que les
teneurs de marché seront mécaniquement contraints d'exécuter — décomposés en
trois sources : **gamma** (mouvement du sous-jacent), **vanna** (variation de
l'IV) et **charm** (écoulement du temps).

Le produit final répond à une question opérationnelle : *si le prix atteint X,
les dealers deviennent-ils acheteurs ou vendeurs, et de quelle taille ?* Cela
complète les niveaux statiques existants (walls, flip) par une lecture de
l'intensité et du sens du flux entre ces niveaux.

---

## Principe de calcul

### 1. Grecques unitaires (Black-Scholes)

Pour chaque option de la chaîne, à un prix candidat `S` :

```
d1 = [ln(S/K) + (r - q + σ²/2)·T] / (σ·√T)
d2 = d1 - σ·√T
φ(x) = exp(-x²/2) / √(2π)
```

```
Gamma  Γ = e^(-qT) · φ(d1) / (S · σ · √T)
Vega   ν = S · e^(-qT) · φ(d1) · √T
Vanna  ∂ν/∂S = -e^(-qT) · φ(d1) · d2 / σ
Charm  ∂Δ/∂t = -e^(-qT) · φ(d1) · [2(r-q)T - d2·σ·√T] / (2·T·σ·√T)
```

> `T` en années (base 365 pour le calendaire, voir Points ouverts).
> `r` : taux sans risque. `q` : dividende/portage — nul pour les indices en
> futures, non nul pour les chaînes ETF.

### 2. Exposition dealer par strike

Convention du repo : **dealers short calls / long puts** (voir `_gex_core.py`,
signe déjà appliqué dans `gex_by_strike`). Réutiliser cette convention, ne pas
en introduire une seconde.

```
GEX$(K, S)   = Σ  OI_K · mult · Γ(K,S) · S² · 0.01 · sign_K
VANNA$(K, S) = Σ  OI_K · mult · Vanna(K,S) · S · 0.01 · sign_K
CHARM$(K, S) = Σ  OI_K · mult · Charm(K,S) · S · sign_K
```

- `mult` : multiplicateur du contrat (100 pour les chaînes options actions/ETF)
- `S² · 0.01` : conversion en dollars de delta par mouvement de 1 %
- `VANNA$` : dollars de delta par point d'IV (1 vol point → diviser par 100)
- `CHARM$` : dollars de delta par jour écoulé

**Chaînes ETF** : les strikes sont à l'échelle de l'ETF. Appliquer le facteur
`scale` déjà présent dans `sources[].scale` **avant** tout calcul, comme le
fait `max_pain`. Ne pas recalculer ce facteur.

### 3. Projection sur grille

```
grid = [S₀ · (1 + i·step) for i in range(-N, N+1)]
```

Pour chaque `S` de la grille, recalculer les trois agrégats sur toute la chaîne.
Sortie : trois séries alignées sur la grille.

**Flux de rehedge entre deux niveaux adjacents** (le résultat exploitable) :

```
Flow(S_i → S_i+1) ≈ GEX$(S_i) · (S_i+1 - S_i) / S_i · 100
```

Signe positif = dealers acheteurs sur la hausse (amplification, gamma négatif).
Signe négatif = dealers vendeurs sur la hausse (amortissement, gamma positif).

### 4. Vanna et charm : flux sans mouvement de prix

Ces deux composantes produisent des rehedges **à prix constant** :

```
Flow_vanna = VANNA$(S) · Δσ        (Δσ : variation d'IV attendue, en points)
Flow_charm = CHARM$(S) · Δt        (Δt : fraction de journée écoulée)
```

Pertinent surtout en 0DTE, où le charm explose en fin de séance. À croiser avec
le régime : en gamma négatif, ces flux vont **dans le sens** du mouvement.

---

## Décisions déjà prises

| Sujet | Décision |
|---|---|
| Langage | Python pur, `math` uniquement. Pas de scipy. numpy toléré s'il est déjà dans `requirements.txt` |
| Emplacement moteur | `api/_gex_core.py`, à côté de `_strike_profile` |
| Endpoint | `GET /api/flow?target=NQ` — même validation `target not in TARGETS` que les autres |
| Signe dealer | Réutiliser la convention existante de `gex_by_strike`. Aucune nouvelle convention |
| Échelle ETF | Facteur `sources[].scale` appliqué avant calcul, jamais recalculé |
| Grille | Centrée sur le spot, pas sur l'ancre Open. Pas de valeur en dur : dériver de `TARGETS[t]["bucket"]` |
| Cache | Redis, clé `gex:flow:{target}:{date}`, TTL 2 jours. Recalcul via le cron intrajournalier existant (`?intraday=1`), pas un nouveau cron |
| Verrou | Ce module **ne touche jamais** à `levels`, `open_grid`, `expected_move` ni `pine`. `_freeze_levels` doit rester valide |
| Publication | Aucun webhook Discord pour ce module |
| Front | Nouvelle carte dans `index.html`, masquée par défaut (`hidden`), bilingue FR/EN via le dictionnaire `T` existant |
| CSS | Garde `[hidden]{display:none !important}` déjà présente — ne pas ajouter de `display:` sur l'id de la carte |
| Tests | Ajouter au harnais existant : non-régression NQ/ES vs version actuelle, et vérification que le payload publié est inchangé |

---

## Points ouverts

**Traitement du skew lors de la projection.** Quand `S` bouge sur la grille,
quelle IV utiliser pour chaque strike ?
- *Sticky strike* : chaque strike garde son IV observée. Simple, sous-estime le
  flux sur les grands mouvements.
- *Sticky delta* : l'IV suit le moneyness. Plus réaliste, nécessite
  d'interpoler la surface.
Recommandation : démarrer en sticky strike, exposer le choix en paramètre.

**Base temporelle de `T`.** Calendaire 365 vs jours de bourse 252. Le module EM
existant utilise déjà une convention — **vérifier laquelle et s'aligner**, ne
pas en introduire une seconde.

**Taux `r` et portage `q`.** Valeur fixe en configuration, ou dérivée du basis
future/spot déjà calculé par `future_basis()` ? La seconde option est plus
juste mais couple les deux modules.

**Échéances retenues.** Toute la chaîne, ou seulement les N premières comme le
fait `build_payload` ? Le charm n'a de sens marqué que sur le 0DTE ; agréger
des échéances lointaines dilue le signal.

**Granularité et amplitude de la grille.** Nombre de pas et amplitude en σ ou
en pourcentage. Doit rester lisible sur mobile (largeur contrainte).

**Normalisation de l'affichage.** Valeurs brutes en dollars, ou normalisées par
l'ADV / l'open interest total pour comparer les marchés entre eux ?

**Δσ et Δt par défaut** pour les flux vanna/charm : valeurs fixes, ou dérivées
de l'IV réalisée du jour et de l'horloge de volatilité calibrée du module EM ?

---

## Contraintes non négociables

- Vercel serverless Python, `BaseHTTPRequestHandler`. Pas de framework.
- Aucune régression sur NQ / ES / SPX : test A/B obligatoire avant livraison.
- Une donnée manquante renvoie **503 avec motif**, jamais une exception 5xx.
- Redis Upstash payant : pas de lecture par requête utilisateur, cache mémoire
  en amont.
