# Moteur graphique 2D gexdash — spécification

Remplace lightweight-charts. Canvas 2D, JavaScript vanilla, aucune dépendance.
Ce document est la référence d'architecture : le lire avant toute modification
du rendu.

---

## 1. Principe : séparer état, transformations et dessin

Trois couches strictement distinctes. Mélanger les deux premières avec le
dessin est la source de tous les rendus instables.

**L'état** — ce qui est vrai, indépendamment de l'affichage.

```js
const state = {
  bars: [],            // {t, o, h, l, c} triés par temps croissant
  levels: [],          // {price, label, kind}
  matrix: null,        // {times[], prices[], values[][]} pour le dégradé
};
```

**Le viewport** — ce qu'on regarde.

```js
const view = {
  firstIndex: 0,       // index de la première bougie visible (flottant)
  barSpacing: 8,       // px CSS entre deux bougies
  rightOffset: 12,     // bougies vides à droite, pour respirer
  priceMin: null,      // null = échelle automatique
  priceMax: null,
  autoScale: true,
};
```

**Le dessin** — pur, sans effet de bord. Il lit l'état et le viewport, il
n'écrit nulle part.

---

## 2. Transformations de coordonnées

Le cœur du moteur. Tout le reste en découle.

```js
const PAD = {top: 8, right: 62, bottom: 22, left: 0};   // right = échelle prix

function plotW(){ return cssW - PAD.left - PAD.right; }
function plotH(){ return cssH - PAD.top - PAD.bottom; }

// index de bougie <-> x
function indexToX(i){
  return PAD.left + (i - view.firstIndex) * view.barSpacing + view.barSpacing / 2;
}
function xToIndex(x){
  return view.firstIndex + (x - PAD.left - view.barSpacing / 2) / view.barSpacing;
}

// prix <-> y
function priceToY(p){
  const {lo, hi} = priceRange();
  return PAD.top + (hi - p) / (hi - lo) * plotH();
}
function yToPrice(y){
  const {lo, hi} = priceRange();
  return hi - (y - PAD.top) / plotH() * (hi - lo);
}
```

**Règles impératives.**

`priceRange()` doit être calculé **une seule fois par frame** et mis en cache.
L'appeler dans une boucle de dessin sur 500 bougies recalcule 500 fois le même
minimum et maximum.

Toujours renvoyer des valeurs finies. Si `hi === lo` — cas d'une série plate ou
d'une seule bougie — élargir artificiellement, sinon division par zéro et tout
le rendu devient `NaN`.

Ne jamais dessiner hors de la zone de tracé : `ctx.save()`, `ctx.rect(...)`,
`ctx.clip()` avant, `ctx.restore()` après. Sans cela les bougies débordent sur
l'échelle de prix.

---

## 3. Échelle de prix automatique

```js
function priceRange(){
  if (!view.autoScale && view.priceMin != null)
    return {lo: view.priceMin, hi: view.priceMax};

  let lo = Infinity, hi = -Infinity;
  const [a, b] = visibleIndexRange();
  for (let i = a; i <= b; i++){
    const bar = state.bars[i];
    if (!bar) continue;
    if (bar.l < lo) lo = bar.l;
    if (bar.h > hi) hi = bar.h;
  }
  // les niveaux proches doivent rester visibles
  for (const lv of state.levels){
    if (lv.price > lo - (hi - lo) * 0.5 && lv.price < hi + (hi - lo) * 0.5){
      if (lv.price < lo) lo = lv.price;
      if (lv.price > hi) hi = lv.price;
    }
  }
  if (!isFinite(lo) || !isFinite(hi)) return {lo: 0, hi: 1};
  if (hi === lo){ hi = lo + 1; lo -= 1; }
  const m = (hi - lo) * 0.08;              // marge haute et basse
  return {lo: lo - m, hi: hi + m};
}
```

**Graduations lisibles** — algorithme du « nombre rond ». Ne jamais diviser la
plage en N parts égales : on obtient 29 847,3 au lieu de 29 850.

```js
function niceStep(range, targetTicks){
  const raw = range / targetTicks;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
  return step * mag;
}
```

Puis parcourir de `Math.ceil(lo / step) * step` à `hi`.

---

## 4. Canvas en couches — la clé de la fluidité

**Trois canvas superposés**, pas un seul. C'est ce qui sépare un moteur fluide
d'un moteur qui rame.

| Couche | Contenu | Redessinée quand |
|---|---|---|
| **fond** | dégradé de flux | les données changent, ou le viewport |
| **principal** | grille, bougies, niveaux, étiquettes | le viewport change |
| **curseur** | réticule, infobulle, étiquettes d'axe | à chaque mouvement de souris |

Sans cette séparation, chaque déplacement de souris redessinerait le dégradé —
l'opération la plus coûteuse — soixante fois par seconde.

```html
<div class="chartwrap" style="position:relative">
  <canvas id="cvBg"></canvas>
  <canvas id="cvMain"></canvas>
  <canvas id="cvCursor"></canvas>
</div>
```

Tous en `position:absolute; inset:0`. Seul celui du curseur reçoit les
événements ; les deux autres en `pointer-events:none`.

---

## 5. Netteté sur écran haute densité

Non négociable, sinon tout paraît flou.

```js
function sizeCanvas(cv, cssW, cssH){
  const dpr = window.devicePixelRatio || 1;
  cv.width  = Math.round(cssW * dpr);
  cv.height = Math.round(cssH * dpr);
  cv.style.width  = cssW + 'px';
  cv.style.height = cssH + 'px';
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return ctx;
}
```

Après `setTransform`, tout le code raisonne en pixels CSS.

**Traits d'un pixel** : décaler d'un demi-pixel, sinon le trait s'étale sur
deux pixels et paraît gris.

```js
ctx.moveTo(x0, Math.round(y) + 0.5);
```

---

## 6. Boucle de rendu

Une seule frame par cycle, avec des drapeaux indiquant ce qui doit être
redessiné.

```js
const dirty = {bg: true, main: true, cursor: true};
let queued = false;

function invalidate(...layers){
  for (const l of layers) dirty[l] = true;
  if (queued) return;
  queued = true;
  requestAnimationFrame(frame);
}

function frame(){
  queued = false;
  const pr = priceRange();                 // calculé UNE fois
  if (dirty.bg)     { drawBackground(pr); dirty.bg = false; }
  if (dirty.main)   { drawMain(pr);       dirty.main = false; }
  if (dirty.cursor) { drawCursor(pr);     dirty.cursor = false; }
}
```

**Aucun dessin direct depuis un gestionnaire d'événement.** Un `wheel` ou un
`mousemove` appelle `invalidate()`, jamais `draw()`.

---

## 7. Bougies

```js
function drawCandles(ctx, pr){
  const [a, b] = visibleIndexRange();
  const w = Math.max(1, Math.floor(view.barSpacing * 0.7));
  const half = Math.floor(w / 2);

  // grouper par couleur : deux passes valent mieux que N changements d'état
  for (const up of [true, false]){
    ctx.fillStyle = up ? C.teal : C.red;
    ctx.strokeStyle = up ? C.teal : C.red;
    ctx.beginPath();
    for (let i = a; i <= b; i++){
      const bar = state.bars[i];
      if (!bar || (bar.c >= bar.o) !== up) continue;
      const x = Math.round(indexToX(i));
      const yO = priceToY(bar.o), yC = priceToY(bar.c);
      const yH = priceToY(bar.h), yL = priceToY(bar.l);
      ctx.rect(x - half, Math.min(yO, yC),
               w, Math.max(1, Math.abs(yC - yO)));   // corps
      ctx.rect(x - 0.5, yH, 1, yL - yH);             // mèche
    }
    ctx.fill();
  }
}
```

**Deux `fill()` au total**, pas un par bougie. Changer `fillStyle` invalide
l'état du contexte et coûte cher — c'est la première optimisation à faire.

Quand `barSpacing < 3`, ne dessiner que les mèches : les corps deviennent
illisibles et le rendu s'allège.

---

## 8. Dégradé de flux

La matrice est une grille `[temps][prix]` grossière. Pour un rendu continu :

```js
// canvas hors écran à la taille de la GRILLE, pas du canvas
const off = document.createElement('canvas');
off.width = matrix.times.length;
off.height = matrix.prices.length;
const octx = off.getContext('2d');
const img = octx.createImageData(off.width, off.height);
// remplir img.data en RGBA depuis les valeurs normalisées
octx.putImageData(img, 0, 0);

// puis étirer : le navigateur interpole
ctx.imageSmoothingEnabled = true;
ctx.imageSmoothingQuality = 'high';
ctx.drawImage(off, x0, y0, x1 - x0, y1 - y0);
```

Le canvas hors écran n'est reconstruit que quand **les données** changent. Un
déplacement ou un zoom ne fait qu'un `drawImage` — quelques dixièmes de
milliseconde.

**Normalisation par percentile**, jamais min/max : une valeur extrême isolée
écraserait tout le dégradé. Prendre le 5ᵉ et le 95ᵉ percentile des valeurs
absolues comme bornes, borner au-delà.

Échelle divergente centrée sur zéro : teal pour le positif, rouge pour le
négatif, transparent au voisinage de zéro. L'opacité porte l'intensité, la
teinte porte le signe.

---

## 9. Interaction

**Molette — zoom ancré sur le curseur.** Le point sous la souris ne doit pas
bouger, sinon la navigation est désagréable.

```js
function onWheel(e){
  e.preventDefault();
  const idx = xToIndex(e.offsetX);                 // avant zoom
  const f = e.deltaY < 0 ? 1.1 : 1 / 1.1;
  view.barSpacing = clamp(view.barSpacing * f, 0.5, 60);
  view.firstIndex = idx - (e.offsetX - PAD.left - view.barSpacing / 2)
                        / view.barSpacing;          // après zoom
  clampView();
  invalidate('bg', 'main', 'cursor');
}
```

**Glisser — déplacement.** Mémoriser `firstIndex` au `mousedown`, appliquer le
delta en divisant par `barSpacing`.

**Tactile.** Un doigt déplace, deux doigts zooment sur la distance entre eux.
Ne pas oublier `touch-action: none` en CSS, sinon le navigateur intercepte.

**Réticule.** Seule la couche curseur est redessinée : ligne horizontale au
prix, verticale à la bougie, étiquettes sur les deux axes, infobulle OHLC.

---

## 10. Étiquettes de niveaux — anti-collision

1. Calculer les `y`, écarter ce qui est hors zone.
2. Trier par `y` croissant.
3. Pousser vers le bas ce qui chevauche de moins que la hauteur de ligne.
4. Si le bas est atteint, repasser en sens inverse depuis le haut.

Fixer `ctx.font` **une seule fois** hors de la boucle : chaque changement
invalide le cache de mesure du navigateur. Mettre `measureText` en cache par
chaîne. Fond semi-opaque derrière chaque texte, sinon illisible sur le
dégradé.

---

## 11. Redimensionnement

```js
let last = {w: 0, h: 0};
new ResizeObserver(entries => {
  const {width, height} = entries[0].contentRect;
  if (width < 1 || height < 1) return;                  // conteneur masqué
  if (Math.abs(width - last.w) < 1 && Math.abs(height - last.h) < 1) return;
  last = {w: width, h: height};
  cssW = width; cssH = height;
  for (const cv of [cvBg, cvMain, cvCursor]) sizeCanvas(cv, width, height);
  invalidate('bg', 'main', 'cursor');
}).observe(wrap);
```

Le test sur la taille nulle est indispensable : un graphique dans un panneau
masqué a des dimensions nulles, et tout le rendu produirait des `NaN`.

---

## 12. Objectifs de performance

- Moins de 8 ms par frame en déplacement, sur 1200 × 600.
- Aucune allocation dans la boucle de dessin : réutiliser les tableaux,
  précalculer les couleurs.
- Le dégradé n'est reconstruit qu'au changement de données.
- Le réticule ne touche jamais aux couches fond et principale.
- Une seule frame en vol : le drapeau `queued` l'assure.

---

## 13. Conventions gexdash

Fond `#0A0A0C`, surface `#111114`, teal `#26A69A`, rouge `#EF5350`,
or `#F0B90B`, texte `#ECEAE4`, atténué `#8A8A94`, lignes `#212127`.

Monospace pour toute valeur numérique. Traits secondaires en 1 px avec forte
transparence. Aucune couleur en dur : variables CSS.

---

## 14. Ordre de construction recommandé

1. Transformations et échelle de prix automatique — tout en dépend.
2. Grille et axes, avec graduations rondes.
3. Bougies.
4. Zoom, déplacement, tactile.
5. Réticule sur sa propre couche.
6. Niveaux et étiquettes anti-collision.
7. Dégradé de flux en dernier.

Valider chaque étape avant la suivante. Un bug de transformation découvert
après le dégradé oblige à tout reprendre.
