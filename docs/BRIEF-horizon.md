# Module « Horizon » — brief d'implémentation

Page NON RÉPERTORIÉE : route existante, liée nulle part, réservée aux membres
connectés. Lire CLAUDE.md avant toute modification.

## Objectif

Répondre à une question que personne ne traite : où le prix peut aller d'ici
30 min, 1 h, 2 h, la clôture — et avec quelle probabilité.

Les outils GEX existants montrent un état. Ce module montre une trajectoire
probable, asymétrique, qui se déforme selon le positionnement dealer et se
resserre à mesure que la séance avance. Et il mesure s'il a eu raison.

## Principe

### 1. Amplitude — le socle
L'EM restant donne l'amplitude atteignable à un horizon donné :
  portée(h) = EM_jour × (IV_courante / IV_référence) × √(variance restante à h)
Déjà calculé par /api/emlive. C'est SYMÉTRIQUE — tout le travail consiste à
la déformer.

### 2. Direction — le charm rend le cône asymétrique
Le charm produit un flux de couverture indépendant du prix : les dealers
devront acheter ou vendre d'ici la clôture simplement parce que le temps passe.
  biais(h) = CHARM$(prix, h) × poids_charm
Le cône se décale. Le poids doit être calibré empiriquement, pas choisi
arbitrairement.

### 3. Amplitude modulée — le gamma resserre ou élargit
Gamma positif : la couverture amortit, le cône se resserre.
Gamma négatif : la couverture amplifie, le cône s'élargit.
  portée_ajustée(h) = portée(h) × f(gamma_local)
f monotone décroissante, bornée entre 0.6 et 1.6 — sinon un régime extrême
produit une projection absurde.

### 4. Barrières — les murs ne sont pas du terrain neutre
La probabilité d'atteindre un niveau au-delà d'un mur est plus faible que de
parcourir la même distance en terrain dégagé. Modéliser les murs comme des
barrières partielles : densité atténuée au-delà, d'un facteur dépendant de la
taille du mur relative au gamma total.

### 5. Sortie
Pour chaque horizon (30 min, 1 h, 2 h, clôture) : zone haute, zone basse,
médiane, biais directionnel, confiance, et les barrières traversées avec leur
probabilité de franchissement.
Deux enveloppes : 70 % et 90 %, pour distinguer le probable du possible.

## Confiance — ne jamais afficher un chiffre sans elle

La confiance chute quand :
- gamma et charm pointent en sens opposés
- l'IV bouge fortement (le modèle suppose une IV stable)
- le prix est très proche d'un mur (issue binaire)
- moins d'une heure avant la clôture

Une confiance basse s'affiche, elle ne se masque pas.

## Calibration — ce qui rend l'outil sérieux

C'est la partie qui différencie ce module de tous les autres outils GEX.

ENREGISTREMENT : à chaque tir intrajournalier, persister les projections
émises (horizon, zone, confiance, régime, biais).

VÉRIFICATION : au tir suivant l'échéance, comparer au prix effectif. Le prix
est-il resté dans la zone ? A-t-il touché quelle borne ?

STATISTIQUES EXPOSÉES, par régime et par horizon :
- taux de couverture réel de la zone 70 % (doit tendre vers 70 %)
- taux de réalisation du biais directionnel
- taille de l'échantillon

Affichées DANS l'interface. « En gamma négatif, la zone haute est atteinte
68 % du temps sur 40 séances » est vérifiable — c'est ce qui donne le droit
d'afficher une projection.

Sous 20 séances : afficher « calibration en cours ».

## Rendu

Moteur 2D maison (docs/REF-moteur-chart.md), pas lightweight-charts.

Axe vertical : prix. Axe horizontal : passé à gauche, projeté à droite, avec
une séparation nette au présent.
À gauche la trajectoire réalisée. À droite les enveloppes qui s'évasent —
70 % en plein, 90 % en atténué.
Les murs traversent tout le graphique. Marquer l'atténuation là où une
enveloppe rencontre un mur.
Chronologie des événements en bas : bascule de régime, franchissement du flip,
pic d'activité sur un strike.
Au survol : bornes chiffrées, probabilité, biais, confiance.

## Vulgarisation — exigence de conception

Aucun jargon dans l'interface principale. Le mot « gamma » ne doit pas être
nécessaire pour comprendre.

  gamma positif  → zone d'absorption, les mouvements sont amortis
  gamma négatif  → zone d'accélération, les mouvements s'amplifient
  charm positif  → le temps pousse à la hausse d'ici la clôture
  flip franchi   → changement de régime

Phrase de synthèse en haut, lisible par un non-initié :
« D'ici 18h, le marché a 70 % de chances de rester entre 29 850 et 29 980.
Le temps pousse légèrement à la hausse. Le mur à 30 020 freine. »

Le jargon reste en infobulle pour qui veut comprendre le mécanisme.

## Décisions prises

Page non répertoriée, membres connectés uniquement.
Rendu : moteur 2D maison, trois canvas.
Calcul dans le cron intrajournalier, cache Redis, endpoint lecture seule.
Aucun nouveau cron.
Ne jamais écrire dans levels, gex_by_strike, open_grid, expected_move, pine.
Sources : /api/emlive, /api/flow, payload publié.
Cache mémoire en amont de Redis.
Bilingue FR/EN.

## Ordre de construction

1. Projection symétrique depuis l'EM restant, sans déformation. Vérifier la
   cohérence avec le cône déjà affiché.
2. Asymétrie par le charm. Vérifier que le sens correspond aux dérives
   observées en fin de séance.
3. Modulation par le gamma. Vérifier que le cône se resserre en zone verte.
4. Barrières aux murs.
5. Enregistrement et vérification — commencer TÔT, l'échantillon met des
   semaines à se constituer.
6. Rendu et vulgarisation.

Valider chaque étape avant la suivante.

## Ce que ce module ne fait pas

- Pas de prix à une heure précise. La volatilité est stochastique : une zone
  avec sa probabilité est honnête, un point ne l'est pas.
- Pas d'affirmation sur le sens des flux clients. Le volume ne porte pas de
  sens, le positionnement dealer reste une inférence.
- Pas de signal d'entrée ou de sortie. Contexte probabiliste, la décision
  reste au trader.

Ces limites sont visibles dans l'interface, pas enfouies en mention légale.

---

## Note d'implémentation (2026-08-14)

`/api/emlive` référencé ci-dessus n'existe pas dans le dépôt : l'EM restant
est lu directement depuis `open_grid.unit` et `expected_move` du payload déjà
publié (cf. `api/_gex_core.py`), pas via un endpoint dédié. Aucun cône EM
n'était déjà affiché à l'écran non plus (le panneau Flux affiche une grille
prix×temps symétrique, pas une enveloppe tracée) — la validation de l'étape 1
s'est donc faite par cohérence numérique avec `expected_move`/`open_grid`,
pas par comparaison visuelle.

Les 6 étapes de l'ordre de construction sont implémentées (`horizon_envelope`
et fonctions associées dans `api/_gex_core.py`, orchestration dans
`api/gex.py`, rendu dans `horizon.html`) mais **pas encore validées en
séance réelle** — le brief demande de valider chaque étape avant la
suivante ; ça n'a pas été possible marché fermé (implémentation faite un
samedi). Décision explicite : construire les 6 étapes maintenant, valider et
peaufiner ensemble lundi 2026-08-17 en séance live, avant la séance
suivante. Points à surveiller en priorité lundi :

- **Étape 2 (charm)** : `HORIZON_CHARM_WEIGHT = 0.35` — poids PROVISOIRE,
  le brief exige explicitement une calibration empirique qu'aucun
  échantillon ne permet encore. Le biais est exprimé comme fraction
  adimensionnée de `portee_sigma(h)` (`charm$/(|charm$|+|gamma$|)`) plutôt
  qu'une conversion $→points inventée, pour borner le risque même avant
  calibration. Vérifier lundi que le SENS du biais correspond aux dérives
  de fin de séance observées.
- **Étape 3 (gamma)** : `HORIZON_GAMMA_SCALE_BN = 3.0` — normalisateur
  PROVISOIRE avant saturation tanh, borné [0.6,1.6] quelle que soit sa
  valeur (la borne ne dépend pas du normalisateur, seule la vitesse de
  saturation en dépend). Vérifier que le cône se resserre visiblement en
  régime gamma positif.
- **Étape 4 (murs)** : atténuation bornée [15%,65%] selon le poids du mur
  (`gex_by_strike`) relatif au gamma brut total de la chaîne.
- **Étape 5 (calibration)** : collecte démarrée dès le premier tir
  intrajournalier de lundi (`gex:horizonlog:*`, `gex:horizonstats:*`,
  `HORIZON_MIN_SAMPLE_DAYS = 20` séances avant d'afficher un vrai taux —
  "calibration en cours" avant ça). Ne rien attendre de significatif avant
  plusieurs semaines, conformément au brief.
- **Étape 6 (rendu)** : moteur 2D maison 3-canvas (pas lightweight-charts),
  jamais testé au clic dans un vrai navigateur en séance — seulement
  `node --check` sur la syntaxe. Vérifier lundi le rendu réel (cône,
  murs, hover) avec des données live.
