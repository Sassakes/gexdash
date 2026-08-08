/* GEX Terminal — module Flux : gamma dealer projete, prix x temps
   (docs/BRIEF-flux.md). Extrait de index.html (etait inline) pour ne plus
   dupliquer sa propre maintenance -- ce fichier est la seule copie du
   moteur de rendu Flux du terminal, distincte de widget/flux-widget.js
   (widget embarquable public, perimetre reduit : projection seule, pas
   d'historique/niveaux/strikes/synthese). Charge par <script src> juste
   apres ui.js, AVANT le script inline principal qui definit $/T/CHART/
   TARGET/C/fmt/etc. -- toutes les references a ces globals dans ce fichier
   sont a l'interieur de corps de fonction (resolues a l'appel, jamais au
   chargement), donc l'ordre relatif ne pose pas de probleme tant que ce
   fichier reste charge avant que quoi que ce soit n'appelle une fonction
   flux -- ce qui n'arrive qu'a la fin du script inline, dans load(). */
"use strict";

/* ═══ FLUX — gamma dealer projeté, prix × temps (ÉTAPE 1, cf.
   docs/BRIEF-flux.md) ═══════════════════════════════════════════════════
   Panneau dédié plutôt qu'une couche sur le chart de prix : axes et lecture
   sont différents (même raison que les deux boutons EM). Le calcul vit
   entièrement côté serveur (cron intrajournalier, 10 min en séance / 5 min
   sur la dernière heure, cf. vercel.json) ; ce module ne fait que lire
   /api/flow et dessiner sur un canvas 2D — aucune dépendance à
   lightweight-charts. */
let FLUX_ON = false;
try{ FLUX_ON = localStorage.getItem("gexFluxOn") === "1"; }catch(_){}
let FLUX_DATA = null;      // dernière matrice reçue (ou null si pas encore prête)
// Transition douce (~1s) entre deux tirs de cron consécutifs : le cron
// tournant désormais toutes les 5-10 min en séance, un remplacement brut de
// la matrice deviendrait un saut visible plutôt qu'une évolution. Purement
// cosmétique côté dégradé -- FLUX_DATA (tooltips, synthèse, repères) passe à
// la nouvelle valeur immédiatement, cf. fluxDisplayData().
const FLUX_ANIM_MS = 1000;
let FLUX_ANIM = null;   // {from:{gamma,vanna,charm}, to:{...}, t0} ou null hors transition
let FLUX_POLL = null;
let FLUX_RESIZE_WIRED = false;
// Bascule des repères de strikes actifs sur le dégradé -- séparée du dégradé
// lui-même (cf. renderFluxVolCtx) : certains veulent le dégradé seul.
let FLUX_STRIKES_ON = true;
try{ FLUX_STRIKES_ON = localStorage.getItem("gexFluxStrikesOn") !== "0"; }catch(_){}

function bindFluxStrikesToggle(){
  const b = $("fluxStrikesBtn");
  if (!b) return;
  b.classList.toggle("on", FLUX_STRIKES_ON);
  b.onclick = () => {
    FLUX_STRIKES_ON = !FLUX_STRIKES_ON;
    try{ localStorage.setItem("gexFluxStrikesOn", FLUX_STRIKES_ON ? "1" : "0"); }catch(_){}
    b.classList.toggle("on", FLUX_STRIKES_ON);
    drawFlux();
  };
}

// Historique de séance : reconstitue l'exposition dealer RÉELLE (prix réel,
// temps réel) sur la portion déjà écoulée, à partir de la colonne "maintenant"
// que chaque tir de cron a déjà calculée (cf. _refresh_flow côté serveur,
// endpoint /api/flow?hist=1). Récupéré une fois à l'activation puis à chaque
// poll normal (loadFlux) tant que la bascule est active -- jamais tant
// qu'elle est désactivée, pour ne pas faire l'appel réseau pour rien.
let FLUX_HIST_ON = false;
try{ FLUX_HIST_ON = localStorage.getItem("gexFluxHistOn") === "1"; }catch(_){}
let FLUX_HIST_DATA = null;   // {target, entries:[...]} ou null

function bindFluxHistToggle(){
  const b = $("fluxHistBtn");
  if (!b) return;
  b.classList.toggle("on", FLUX_HIST_ON);
  b.onclick = () => {
    FLUX_HIST_ON = !FLUX_HIST_ON;
    try{ localStorage.setItem("gexFluxHistOn", FLUX_HIST_ON ? "1" : "0"); }catch(_){}
    b.classList.toggle("on", FLUX_HIST_ON);
    if (FLUX_HIST_ON) loadFlux(); else { FLUX_HIST_DATA = null; drawFlux(); }
  };
}

// Étape 2 : gamma / vanna / charm, affichés SÉPARÉMENT (jamais fondus en un
// agrégat unique — unités et interprétations diffèrent, cf. BRIEF-flux.md).
// Gamma par défaut à chaque chargement de page, pas persisté.
let FLUX_MODE = "gamma";
const FLUX_MODE_LABEL = {gamma:"Gamma", vanna:"Vanna", charm:"Charm"};

function bindFluxMode(){
  document.querySelectorAll("#fluxModeChips button[data-m]").forEach(b => b.onclick = () => {
    if (b.dataset.m === FLUX_MODE) return;
    FLUX_MODE = b.dataset.m;
    document.querySelectorAll("#fluxModeChips button[data-m]").forEach(x =>
      x.classList.toggle("on", x.dataset.m === FLUX_MODE));
    renderFluxLegend();
    drawFlux();
  });
}

// Animation de bascule du bouton FLUX/CHART au clic : rejouée à chaque
// pression en réappliquant la classe (le navigateur ignore un ré-ajout sans
// reflow entre-temps, d'où le forced reflow).
function pulseFluxBtn(){
  const btn = $("fluxBtn");
  if (!btn) return;
  btn.classList.remove("swap");
  void btn.offsetWidth;
  btn.classList.add("swap");
}

// Légende +/− : le mot (Gamma/Vanna/Charm) suit le sélecteur, le signe est
// bilingue via t() comme le reste des libellés.
function renderFluxLegend(){
  const lbl = FLUX_MODE_LABEL[FLUX_MODE] || "Gamma";
  const nl = $("fluxNegLbl"); if (nl) nl.textContent = lbl + " " + t("fluxNeg");
  const pl = $("fluxPosLbl"); if (pl) pl.textContent = lbl + " " + t("fluxPos");
}

function fluxPercentile(sortedAsc, p){
  if (!sortedAsc.length) return 0;
  const idx = Math.min(sortedAsc.length - 1, Math.max(0, Math.round(p * (sortedAsc.length - 1))));
  return sortedAsc[idx];
}

// ---- palette du dégradé : MÊME traitement que la heatmap du dashboard
// (hmCellStyle, dash.html) -- interpolation de teinte base->saturée, pas un
// simple alpha sur une couleur plate. Un halo doré signale les cellules
// extrêmes (percentile 99), même langage visuel que le reste du produit
// plutôt qu'une convention isolée à ce seul panneau.
// easing pow(1.35) et non 0.7 comme dash.html : un exposant < 1 pousse les
// valeurs FAIBLES vers la saturation, donc l'essentiel du champ finit
// "chaud" et le dégradé paraît plat. > 1 réserve la pleine saturation aux
// valeurs réellement extrêmes -- la structure interne (où c'est VRAIMENT
// fort) redevient lisible plutôt qu'un aplat à deux tons. ----
const FLUX_TEAL_BASE = [15, 26, 24], FLUX_TEAL_HOT = [38, 166, 154];
const FLUX_RED_BASE = [28, 18, 18], FLUX_RED_HOT = [239, 83, 80];
const FLUX_GOLD = [240, 185, 11];
function fluxShade(v, ref, ref99){
  const av = Math.abs(v);
  const x = Math.pow(Math.min(1, av / ref), 1.35);
  const base = v >= 0 ? FLUX_TEAL_BASE : FLUX_RED_BASE, hot = v >= 0 ? FLUX_TEAL_HOT : FLUX_RED_HOT;
  let r = base[0] + (hot[0] - base[0]) * x, g = base[1] + (hot[1] - base[1]) * x, b = base[2] + (hot[2] - base[2]) * x;
  if (ref99 && av >= ref99){
    const g2 = Math.min(1, (av - ref99) / (ref * 0.5 || 1));   // au-delà de p99 : vire au doré
    r += (FLUX_GOLD[0] - r) * g2; g += (FLUX_GOLD[1] - g) * g2; b += (FLUX_GOLD[2] - b) * g2;
  }
  return [Math.round(r), Math.round(g), Math.round(b), 0.20 + 0.80 * x];
}

// $ bruts, jamais moyennes entre grecques (unites differentes, cf. BRIEF-flux.md) :
// simple mise a l'echelle M/Md pour la lisibilite, le signe reprend la convention
// dealer de gex_by_strike (deja appliquee dans flow_gamma_matrix cote serveur).
function fmtFluxUsd(v){
  if (v == null) return "—";
  const av = Math.abs(v), sign = v < 0 ? "−" : "+";
  if (av >= 1e9) return sign + fmt(av / 1e9, 2) + " $Bn";
  if (av >= 1e6) return sign + fmt(av / 1e6, 1) + " $M";
  if (av >= 1e3) return sign + fmt(av / 1e3, 1) + " $K";
  return sign + fmt(av, 0) + " $";
}

function fluxNearestIdx(grid, val){
  if (!grid || !grid.length || val == null) return -1;
  let bi = 0, bd = Infinity;
  for (let i = 0; i < grid.length; i++){
    const dd = Math.abs(grid[i] - val);
    if (dd < bd){ bd = dd; bi = i; }
  }
  return bi;
}

function fluxRef95(mat){
  const vals = [];
  for (const row of mat) for (const v of row) vals.push(Math.abs(v));
  vals.sort((a, b) => a - b);
  return fluxPercentile(vals, 0.95) || 0;
}

// En-dessous de 15% du 95e percentile de sa propre matrice : lu comme plat,
// pas comme un signal directionnel (evite qu'un bruit residuel bascule le
// verdict).
const FLUX_NEUTRAL_FRAC = 0.15;

// Synthese au point (maintenant, prix courant) : verdict unique + une lecture
// par grecque, jamais fondues (unites differentes). Gamma est l'axe primaire
// -- seule grecque validee contre gex_by_strike (cf. BRIEF-flux.md) -- vanna
// et charm ne font que confirmer ou contredire ensuite. Une contradiction
// nette avec le gamma affiche DIVERGENCE plutot que d'etre lissee.
function computeFluxSynthesis(d){
  if (!d || !d.gamma || !d.vanna || !d.charm || !d.price_grid || !d.price_grid.length) return null;
  // Prix live (même source que le cône EM, cf. updateEmCone) : suit pollQuote()
  // tick par tick, pas seulement le dernier payload /api/gex publié.
  const spot = CHART.lastBar ? CHART.lastBar.close
             : ((LAST_D && LAST_D.nq_price != null) ? LAST_D.nq_price : d.spot);
  const pi = fluxNearestIdx(d.price_grid, spot);
  if (pi < 0 || !d.gamma[0] || !d.vanna[0] || !d.charm[0]) return null;
  const g = d.gamma[0][pi], v = d.vanna[0][pi], c = d.charm[0][pi];
  const refG = fluxRef95(d.gamma), refV = fluxRef95(d.vanna), refC = fluxRef95(d.charm);
  const dirOf = (val, ref) => ref <= 0 ? 0 : (Math.abs(val) < FLUX_NEUTRAL_FRAC * ref ? 0 : Math.sign(val));
  const gDir = dirOf(g, refG), vDir = dirOf(v, refV), cDir = dirOf(c, refC);

  let verdict;
  if (gDir > 0){
    verdict = ((cDir < 0 && Math.abs(c) > 0.5 * refC) || (vDir < 0 && Math.abs(v) > 0.5 * refV))
      ? "divergence" : "compression";
  } else if (gDir < 0){
    verdict = ((cDir > 0 && Math.abs(c) > 0.5 * refC) || (vDir > 0 && Math.abs(v) > 0.5 * refV))
      ? "divergence" : "acceleration";
  } else {
    verdict = (cDir !== 0 || vDir !== 0) ? "derive" : "compression";
  }

  return {
    verdict,
    gamma: {val: g, dir: gDir},
    vanna: {val: v, dir: vDir},
    charm: {val: c, dir: cDir},
  };
}

const FLUX_VERDICT_CLASS = {compression:"cmp", acceleration:"acc", derive:"drf", divergence:"dvg"};

function renderFluxSynth(){
  const box = $("fluxSynth");
  if (!box) return;
  const s = FLUX_DATA ? computeFluxSynthesis(FLUX_DATA) : null;
  if (!s){ box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  const row = (key, item, unit) => {
    const tone = item.dir > 0 ? "pos" : item.dir < 0 ? "neg" : "flat";
    const sensKey = "fs_" + key + "_" + (item.dir > 0 ? "pos" : item.dir < 0 ? "neg" : "flat");
    return `<div class="fsrow ${tone}">
      <span class="fsk">${t("fs_" + key)}</span>
      <span class="fsval">${fmtFluxUsd(item.val)} ${t(unit)}</span>
      <span class="fssens">${t(sensKey)}</span>
    </div>`;
  };
  box.innerHTML =
    `<div class="verdict ${FLUX_VERDICT_CLASS[s.verdict]}">
       <div class="vlab">${t("fsVerdict")}</div>
       <div class="vval">${t("fsV_" + s.verdict)}</div>
     </div>
     <div class="fsrows">` +
     row("gamma", s.gamma, "fs_unitGamma") +
     row("vanna", s.vanna, "fs_unitVanna") +
     row("charm", s.charm, "fs_unitCharm") +
    `</div>`;
}

// -- comptage brut (pas de $) : volume/OI en nombre de contrats, jamais
// mêlé aux unités $ de fmtFluxUsd ci-dessus.
function fmtFluxCount(v){
  if (v == null) return "—";
  if (v >= 1e6) return fmt(v / 1e6, 1) + "M";
  if (v >= 1e3) return fmt(v / 1e3, 1) + "k";
  return fmt(v, 0);
}

// Contexte live volume/OI (cf. docs/BRIEF-flux.md non concerné -- module
// séparé, calculé serveur dans _refresh_flow/flow_volume_context à partir
// des MÊMES options déjà agrégées pour gex_by_strike). OI figé depuis hier
// soir, volume propre au jour : les comparer montre où la position se
// construit AUJOURD'HUI. Ne dit jamais le sens (cf. fvCaveat, en infobulle) —
// zone d'attention, pas signal directionnel.
function renderFluxVolCtx(){
  const box = $("fluxVolCtx");
  if (!box) return;
  const vc = FLUX_DATA && FLUX_DATA.volume_context;
  if (!vc){ box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  const row = (e) => {
    const hi = e.vol_oi_ratio != null && e.vol_oi_ratio > 1;
    const dist = e.distance == null ? "" :
      (e.distance >= 0 ? "+" : "") + fmt(e.distance, 0) + " " + t("pts");
    return `<div class="fvrow ${hi ? "hi" : ""}" ${hi ? `title="${t("fvRatioHi")}"` : ""}>
      <span class="fvk">${fmt(e.strike)}</span>
      · ${fmtFluxCount(e.volume)} ${t("fvVolUnit")}
      · <span class="fvr">×${e.vol_oi_ratio != null ? e.vol_oi_ratio.toFixed(2) : "—"}</span>
      · <span class="fvd">${dist}</span>
    </div>`;
  };
  const col = (cls, head, rows) => `<div class="fvcol ${cls}">
      <div class="fvhead">${head}</div>
      ${rows.length ? rows.map(row).join("") : `<div class="fvempty">${t("fvEmpty")}</div>`}
    </div>`;
  const ratio = vc.call_put_ratio;
  const dom = ratio == null ? null : (ratio >= 1 ? "call" : "put");
  const biasTxt = ratio == null ? "—"
    : "×" + (dom === "call" ? ratio : (1 / ratio)).toFixed(2);
  box.innerHTML =
    `<div class="fvbias">
       <span class="fvlab">${t("fvBiasLbl")}</span>
       <span class="fvval ${dom || ""}">${dom ? t(dom === "call" ? "fvCalls" : "fvPuts") : "—"}</span>
       <span class="fvd">(${biasTxt} · ${fmtFluxCount(vc.call_vol)} / ${fmtFluxCount(vc.put_vol)})</span>
       <span class="fvd" title="${t("fvCaveat")}" style="cursor:help">ⓘ</span>
     </div>
     <div class="fvcols">` +
     col("call", t("fvCalls"), vc.calls || []) +
     col("put", t("fvPuts"), vc.puts || []) +
    `</div>`;
}

// Call Wall / Put Wall / Gamma Flip déjà calculés côté serveur — mêmes
// niveaux que ceux tracés sur le chart de prix, mêmes couleurs (KIND_COLOR).
function fluxLevelMarkers(){
  const d = LAST_D;
  if (!d || !d.levels) return [];
  const kinds = {res:"CW", sup:"PW", flip:"Flip"};
  return Object.keys(kinds).map(k => d.levels.find(L => L.kind === k))
    .filter(Boolean)
    .map(L => ({price: L.price_nq, label: kinds[L.kind], full: L.label, color: KIND_COLOR[L.kind]}));
}

// ═══ trois canevas empilés (cvBg dégradé / cvMain grille+niveaux / cvCursor
// réticule) + drapeaux dirty vidés par un seul rAF en vol. Le bitmap du
// dégradé (pixels + montée en résolution, la partie coûteuse) est mis en
// cache et ne se reconstruit QUE quand les DONNÉES changent (nouvelle
// matrice, mode, bascule strikes ou taille de canevas) -- jamais pendant un
// pan/zoom, qui ne fait plus que recadrer ce bitmap déjà prêt avec un
// drawImage. FLUX_LAYOUT porte les coordonnées dont le croisillon (cvCursor,
// repeint indépendamment, jamais sur le dégradé) a besoin pour retrouver
// prix/heure/valeur sous le pointeur. ═══════════════════════════════════
let FLUX_LAYOUT = null, FLUX_HOVER_WIRED = false;
// Fenêtre de prix visible (zoom/pan) : null = plage complète, sinon
// {min, max} en prix produit. Remise à null uniquement quand elle ne
// recoupe plus la grille reçue (cf. fluxCurrentView) -- persiste d'un
// rafraîchissement ou d'un changement de mode à l'autre.
let FLUX_VIEW = null;

let FLUX_DIRTY = {bg: false, main: false, cursor: false};
let FLUX_RAF = null;
let FLUX_SIZE = {w: 0, h: 0, dpr: 0};
let FLUX_CURSOR = null;   // {mx, my} en coordonnées CSS du canevas, ou null si masqué

// bitmap du dégradé, alloué une seule fois et redimensionné seulement si
// nécessaire (jamais recréé dans la boucle de dessin, req. zéro allocation)
let FLUX_OFF = null, FLUX_STAGES = [], FLUX_GRAD_SRC = null, FLUX_GRAD_META = null, FLUX_GRAD_KEY = null;

function fluxDataKey(d){
  // Les strikes (FLUX_STRIKES_ON) n'entrent PAS ici : ce sont des repères
  // vectoriels dessinés à part sur cvMain (cf. drawFluxMain), jamais une
  // composante du bitmap couleur -- les y inclure ne faisait que forcer une
  // reconstruction inutile du dégradé à chaque clic sur le bouton Strikes.
  return (d.generated_utc || "") + "|" + FLUX_MODE;
}

// Même grille (mêmes colonnes de prix, même nombre d'heures) requise pour
// interpoler cellule à cellule -- change une fois par heure entière écoulée
// (hours_grid raccourcit), auquel cas la transition suivante saute plutôt
// que d'interpoler des tableaux de tailles différentes.
function fluxSameShape(a, b){
  return !!(a && b && a.price_grid && b.price_grid && a.hours && b.hours
    && a.price_grid.length === b.price_grid.length
    && a.hours.length === b.hours.length);
}

function fluxLerpMat(from, to, t){
  return to.map((row, hi) => row.map((v, pi) => {
    const v0 = from[hi] ? from[hi][pi] : v;
    return v0 + (v - v0) * t;
  }));
}

// Retourne l'objet à afficher CE FRAME : FLUX_DATA tel quel hors transition,
// ou une copie avec gamma/vanna/charm interpolés pendant les ~FLUX_ANIM_MS
// qui suivent un nouveau tir de cron. La clé generated_utc porte le progrès
// (t) pour que fluxDataKey/fluxGradient invalident le bitmap en cache à
// chaque frame de la transition -- seulement pendant elle, jamais après.
function fluxDisplayData(){
  if (!FLUX_ANIM || !FLUX_DATA) return FLUX_DATA;
  const t = Math.min(1, (performance.now() - FLUX_ANIM.t0) / FLUX_ANIM_MS);
  if (t >= 1){ FLUX_ANIM = null; return FLUX_DATA; }
  invalidateFlux("bg", "main");   // enchaîne la frame suivante tant que ça anime
  return Object.assign({}, FLUX_DATA, {
    generated_utc: FLUX_DATA.generated_utc + "|anim" + t.toFixed(3),
    gamma: fluxLerpMat(FLUX_ANIM.from.gamma, FLUX_DATA.gamma, t),
    vanna: fluxLerpMat(FLUX_ANIM.from.vanna, FLUX_DATA.vanna, t),
    charm: fluxLerpMat(FLUX_ANIM.from.charm, FLUX_DATA.charm, t),
  });
}

function invalidateFlux(...layers){
  for (const l of layers) FLUX_DIRTY[l] = true;
  if (FLUX_RAF != null) return;
  FLUX_RAF = requestAnimationFrame(flushFluxDirty);
}

function fluxEnsureSize(cvBg, cvMain, cvCur){
  const dpr = window.devicePixelRatio || 1;
  const w = cvMain.clientWidth, h = cvMain.clientHeight;
  if (!w || !h) return false;
  if (w === FLUX_SIZE.w && h === FLUX_SIZE.h && dpr === FLUX_SIZE.dpr) return true;
  FLUX_SIZE = {w, h, dpr};
  [cvBg, cvMain, cvCur].forEach(cv => {
    cv.width = Math.max(1, Math.round(w * dpr));
    cv.height = Math.max(1, Math.round(h * dpr));
  });
  FLUX_GRAD_KEY = null;   // le bitmap en cache ne correspond plus à la nouvelle taille
  return true;
}

// Lisse la réserve de droite (padR) au lieu de sauter instantanément à sa
// nouvelle cible -- activer/désactiver Strikes change le texte le plus long
// à réserver (donc gw), et un saut sec décalait toute la chart d'un coup,
// vilain et donnait l'impression d'un bug de mise en page ("ça décale la
// chart"). Rattrape ~30% de l'écart restant par frame rendue tant que ce
// n'est pas négligeable ; sans état précédent (premier rendu) ou pour un
// écart trop large (marché/mode changé, pas un simple toggle Strikes),
// affiche direct la cible -- inutile d'animer une transition qui n'a rien
// de continu pour l'œil.
let FLUX_PADR_CUR = null;
function fluxEasePadR(lay, w){
  const target = lay.padR;
  if (FLUX_PADR_CUR == null || Math.abs(target - FLUX_PADR_CUR) > 150){
    FLUX_PADR_CUR = target;
    return lay;
  }
  let padR = FLUX_PADR_CUR + (target - FLUX_PADR_CUR) * 0.3;
  if (Math.abs(target - padR) < 0.5) padR = target;
  FLUX_PADR_CUR = padR;
  if (padR === target) return lay;
  invalidateFlux("bg", "main");   // pas encore arrivé : programme la frame suivante
  return Object.assign({}, lay, {padR, gw: Math.max(1, w - lay.padL - padR)});
}

// Nombre de frames consécutives où le conteneur restait à taille 0 -- borne
// le réessai automatique ci-dessous (cf. FLUX_SIZE_RETRIES) pour ne jamais
// boucler indéfiniment si la taille reste réellement nulle (bug CSS externe
// à ce module), sur le même principe que _stabilizeSize pour le chart de
// prix.
let FLUX_SIZE_TRIES = 0;
const FLUX_SIZE_RETRIES = 20;

function flushFluxDirty(){
  FLUX_RAF = null;
  const cvBg = $("fluxCvBg"), cvMain = $("fluxCvMain"), cvCur = $("fluxCvCursor");
  if (!cvBg || !cvMain || !cvCur || cvMain.hidden || cvMain.offsetParent === null){
    FLUX_DIRTY.bg = FLUX_DIRTY.main = FLUX_DIRTY.cursor = false;
    FLUX_SIZE_TRIES = 0;
    return;
  }
  // Conteneur pas encore mesurable (vient d'être rendu visible, layout pas
  // encore posé) : sans ce réessai, le rendu restait bloqué (dirty=true,
  // rien de programmé) jusqu'à ce qu'un événement extérieur (survol, clic)
  // relance invalidateFlux -- symptôme observé : chart figée/vide tant que
  // la souris ne bouge pas dessus à l'arrivée sur la vue Flux.
  if (!fluxEnsureSize(cvBg, cvMain, cvCur) || !FLUX_SIZE.w || !FLUX_SIZE.h){
    if (FLUX_SIZE_TRIES++ < FLUX_SIZE_RETRIES) FLUX_RAF = requestAnimationFrame(flushFluxDirty);
    return;
  }
  FLUX_SIZE_TRIES = 0;

  const d = fluxDisplayData(), mat = d && d[FLUX_MODE];
  let lay = (d && mat && mat.length && d.price_grid && d.price_grid.length)
    ? fluxLayout(d, mat, FLUX_SIZE.w, FLUX_SIZE.h) : null;
  if (lay) lay = fluxEasePadR(lay, FLUX_SIZE.w);

  if (FLUX_DIRTY.bg){ FLUX_DIRTY.bg = false; drawFluxBg(cvBg, d, mat, lay); }
  if (FLUX_DIRTY.main){ FLUX_DIRTY.main = false; drawFluxMain(cvMain, d, mat, lay); }
  if (FLUX_DIRTY.cursor){ FLUX_DIRTY.cursor = false; drawFluxCursorLayer(cvCur); }
}

function drawFlux(){
  invalidateFlux("bg", "main", "cursor");
}

// Bornes de la séance Flux, en ET. 8h30 (pas 9h30, l'open cash) pour couvrir
// l'heure des publications macro US (CPI, NFP...) qui bougent déjà le future
// avant l'ouverture cash ; 16h00, clôture cash, inchangée. Mêmes repères que
// le cron serveur (cf. vercel.json) -- ne pas faire dériver l'un sans l'autre,
// sinon le panneau affiche une portion "historique" que la matrice projetée
// ne couvre pas encore (ou plus).
const FLUX_SESSION_START_ET = [8, 30];
const FLUX_SESSION_END_ET = [16, 0];

// Epoch UTC réel d'une heure ET donnée, pour le jour courant. Re-mesure
// l'offset ET à chaque appel (Intl.DateTimeFormat, cf. marketPollMs dans
// ui.js) plutôt que de supposer -4h/-5h en dur -- correct des deux côtés
// d'un changement d'heure d'été sans cas particulier à coder.
function etEpochToday(hh, mm){
  const P = tSec => new Intl.DateTimeFormat("en-US", {timeZone: "America/New_York",
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false})
    .formatToParts(new Date(tSec * 1000)).reduce((a, x) => (a[x.type] = x.value, a), {});
  const now = Math.floor(Date.now() / 1000);
  const p = P(now);
  const guess = Date.UTC(+p.year, +p.month - 1, +p.day, hh, mm, 0) / 1000;
  const pg = P(guess);
  const diffMin = (hh * 60 + mm) - ((+pg.hour % 24) * 60 + (+pg.minute));
  return guess + diffMin * 60;
}

// Vrai uniquement pendant la fenêtre où le cron serveur recalcule
// effectivement le flux (cf. FLUX_SESSION_START_ET/END_ET, lun-ven) --
// inutile de poller /api/flow en dehors, la donnée ne bouge pas tant que le
// prochain tir n'a pas eu lieu.
function fluxSessionOpenNow(){
  const p = new Intl.DateTimeFormat("en-US", {timeZone: "America/New_York", hour12: false,
      weekday: "short", hour: "2-digit", minute: "2-digit"})
    .formatToParts(new Date()).reduce((a, x) => (a[x.type] = x.value, a), {});
  if (p.weekday === "Sat" || p.weekday === "Sun") return false;
  const mins = (+p.hour % 24) * 60 + (+p.minute);
  const [sh, sm] = FLUX_SESSION_START_ET, [eh, em] = FLUX_SESSION_END_ET;
  return mins >= sh * 60 + sm && mins <= eh * 60 + em;
}

// Prix réalisé depuis le début de séance Flux jusqu'à maintenant -- ou
// jusqu'à la clôture si celle-ci est déjà passée. Sans ce plafond, la
// portion "historique" du panneau continuait de s'étirer avec l'horloge
// murale même séance terminée (bug vu en prod : le panneau semblait
// "avancer" tout seul le soir et le week-end alors qu'aucune nouvelle
// donnée n'arrivait). À partir des bougies DÉJÀ chargées pour le chart de
// prix (CHART.bars, cf. loadBars) -- aucun appel réseau supplémentaire.
// Mêmes timestamps décalés (tzShift) que ce chart, directement comparables
// sans nouvelle conversion. elapsedH sert à dimensionner la portion
// "historique" du panneau Flux par rapport à la portion "projection"
// (cf. fluxLayout).
function fluxSessionHistory(){
  const openShifted = tzShift(etEpochToday(...FLUX_SESSION_START_ET));
  const closeShifted = tzShift(etEpochToday(...FLUX_SESSION_END_ET));
  const bars = CHART.bars;
  if (!bars || !bars.length) return {points: [], elapsedH: 0, openShifted};
  const nowShifted = Math.min(tzShift(Math.floor(Date.now() / 1000)), closeShifted);
  const elapsedH = Math.max(0, (nowShifted - openShifted) / 3600);
  if (elapsedH <= 0.02) return {points: [], elapsedH: 0, openShifted};   // séance qui vient d'ouvrir : rien à tracer
  const points = bars
    .filter(b => b.time >= openShifted && b.time <= nowShifted)
    .map(b => ({t: (b.time - openShifted) / 3600, price: b.close}));
  return {points, elapsedH, openShifted};
}

// ---- disposition : padding, marge droite (suit le texte le plus long parmi
// les repères candidats) et liste de repères. Calculée sur la plage COMPLETE
// de prix (fullMin/fullMax), jamais la fenêtre de vue courante -- sinon gw
// bougerait à chaque pan/zoom et invaliderait le bitmap en cache à chaque
// frame, l'exact inverse de ce que ce cache doit apporter.
//
// Le panneau est scindé en deux : une portion "historique" (prix réalisé
// depuis l'ouverture cash, ligne simple) à gauche de "maintenant", une
// portion "projection" (dégradé gamma/vanna/charm, INCHANGÉE -- même
// indexation par colonne qu'avant) à droite. Largeurs proportionnelles aux
// heures écoulées / restantes, pour que l'échelle temporelle reste continue
// de part et d'autre de "maintenant" plutôt que deux échelles arbitraires
// recollées. padL/gw (donc le dégradé, le survol, les repères) ne
// désignent QUE la portion projection -- toute la logique existante qui en
// dépend reste valide telle quelle, seule sa position de départ se décale
// pour laisser la place à l'historique. ----
function fluxLayout(d, mat, w, h){
  const padL0 = 46, padT = 8, padB = 20;
  const gh = Math.max(1, h - padT - padB);
  const nH = mat.length, nP = d.price_grid.length;
  // fullMin/fullMax : EXACTEMENT l'étendue de price_grid, jamais élargie --
  // c'est le repère dont dépend le recadrage du bitmap dégradé dans
  // drawFluxBg (le bitmap ne couvre QUE cette plage-là, quoi qu'il arrive).
  // viewMin/viewMax : vue par défaut élargie pour inclure le prix réalisé
  // depuis l'ouverture (peut sortir de la bande ±3σ courante après un
  // mouvement de séance) -- utilisée par fluxCurrentView/le filtrage des
  // repères, jamais par le recadrage du bitmap.
  const fullMin = d.price_grid[0], fullMax = d.price_grid[nP - 1];
  let viewMin = fullMin, viewMax = fullMax;

  const LVL_FONT = "11px IBM Plex Mono, monospace", STRIKE_FONT = "9.5px IBM Plex Mono, monospace";
  const markers = [];
  const spot = CHART.lastBar ? CHART.lastBar.close
             : ((LAST_D && LAST_D.nq_price != null) ? LAST_D.nq_price : d.spot);

  const hist = fluxSessionHistory();
  if (spot != null && hist.points.length) hist.points.push({t: hist.elapsedH, price: spot});
  for (const p of hist.points){
    if (p.price < viewMin) viewMin = p.price;
    if (p.price > viewMax) viewMax = p.price;
  }

  if (spot != null && spot >= viewMin && spot <= viewMax){
    markers.push({price: spot, tier: 0, text: fmt(spot, 0), color: C.text, textColor: C.text,
                  dash: [4, 3], lw: 1, font: LVL_FONT});
  }
  fluxLevelMarkers().forEach(m => {
    if (m.price == null || m.price < viewMin || m.price > viewMax) return;
    markers.push({price: m.price, tier: 1, text: m.label, full: m.full, color: m.color, textColor: m.color,
                  dash: [2, 3], lw: 1, font: LVL_FONT});
  });
  if (FLUX_STRIKES_ON) fluxStrikeMarkers().forEach(m => {
    if (m.strike == null || m.strike < viewMin || m.strike > viewMax) return;
    const ratio = m.vol_oi_ratio;
    const k = Math.max(0, Math.min(1, (ratio || 0) / 2));   // ratio >= 2 sature l'échelle visuelle
    const rTxt = ratio != null ? "×" + ratio.toFixed(2) : "—";
    markers.push({
      price: m.strike, tier: 2, ratio: ratio || 0,
      strike: m.strike, isCall: m.isCall, volume: m.volume, oi: m.oi,
      vol_oi_ratio: ratio, distance: m.distance,
      text: `${m.isCall ? "C" : "P"} ${fmt(m.strike)} ${rTxt}`,
      color: `rgba(236,234,228,${0.35 + 0.45 * k})`,       // ligne : opacité ~ vol/OI
      textColor: "rgba(236,234,228,.92)",                  // étiquette : toujours lisible une fois affichée
      dash: m.isCall ? [] : [3, 2], lw: 1 + 1.4 * k, font: STRIKE_FONT,
    });
  });

  const mctx = $("fluxCvMain").getContext("2d");
  let maxTextW = 0;
  for (const m of markers){ mctx.font = m.font; maxTextW = Math.max(maxTextW, mctx.measureText(m.text).width); }
  const padR = markers.length
    ? Math.min(Math.max(maxTextW + 16, 50), Math.max(60, w * 0.42))
    : 40;

  const remainH = d.hours[d.hours.length - 1] || 0;
  const totalH = hist.elapsedH + remainH;
  const availW = Math.max(1, w - padL0 - padR);
  const gap = 10;
  // Réserve minimale pour la portion projection : sous ce seuil, les
  // étiquettes d'heure "maint." et "clôture" (posées aux deux bords de gw)
  // finissent l'une sur l'autre, illisibles -- ça arrivait en fin de séance,
  // quand il ne reste presque plus d'heures à projeter et que gw s'effondrait
  // en faveur de l'historique. 90px suffit aux deux libellés plus une marge.
  const minGw = 90;
  let histW = (hist.elapsedH > 0 && totalH > 0)
    ? Math.max(0, Math.round(availW * hist.elapsedH / totalH) - gap)
    : 0;
  histW = Math.min(histW, Math.max(0, availW - minGw - gap));
  if (histW <= 4) histW = 0;   // portion trop étroite pour être lisible : pas de marge réservée pour rien
  const histX0 = padL0;
  const padL = padL0 + (histW > 0 ? histW + gap : 0);
  const gw = Math.max(1, w - padL - padR);

  return {padL, padT, padB, padR, gw, gh, nH, nP, fullMin, fullMax, viewMin, viewMax, markers, spot,
          histX0, histW, histPoints: hist.points, elapsedH: hist.elapsedH, histOpenShifted: hist.openShifted};
}

// ---- bitmap du dégradé : grille native (nH x nP) construite dans un canevas
// hors-écran RÉUTILISÉ (FLUX_OFF), puis montée en résolution progressive dans
// des étages eux aussi réutilisés (FLUX_STAGES) -- aucune allocation de
// canevas dans cette fonction après le tout premier appel. Indépendante de
// FLUX_VIEW : couvre toujours [fullMin, fullMax] en entier. ----
function fluxBuildGradient(mat, nH, nP, gw, gh){
  // Référence(s) de normalisation : GLOBALE (toute la matrice) pour
  // gamma/vanna -- déjà validée contre gex_by_strike, ne pas y toucher. PAR
  // COLONNE HORAIRE pour le charm -- il croît réellement de ~3 à 8x entre le
  // début de séance et la clôture (la décroissance du delta s'accélère
  // mécaniquement en fin de vie de l'option, cf. bs_charm), donc une
  // référence commune tire `ref` vers la dernière heure et écrase la
  // structure par strike de toutes les autres. Par colonne, chaque heure
  // retrouve sa propre dynamique de prix -- on perd la comparaison "la
  // clôture est objectivement plus intense" mais le charm redevient lisible
  // sur toute la séance, ce qui est le but du panneau.
  const perColumn = FLUX_MODE === "charm";
  let refArr, ref99Arr;
  if (perColumn){
    refArr = new Array(nH); ref99Arr = new Array(nH);
    for (let hi = 0; hi < nH; hi++){
      const col = mat[hi].map(Math.abs).sort((a, b) => a - b);
      refArr[hi] = fluxPercentile(col, 0.95) || 1;
      ref99Arr[hi] = fluxPercentile(col, 0.99) || refArr[hi];
    }
  } else {
    const absVals = [];
    for (const row of mat) for (const v of row) absVals.push(Math.abs(v));
    absVals.sort((a, b) => a - b);
    const ref = fluxPercentile(absVals, 0.95) || 1;
    const ref99 = fluxPercentile(absVals, 0.99) || ref;
    refArr = new Array(nH).fill(ref); ref99Arr = new Array(nH).fill(ref99);
  }

  if (!FLUX_OFF) FLUX_OFF = document.createElement("canvas");
  FLUX_OFF.width = nH; FLUX_OFF.height = nP;
  const octx = FLUX_OFF.getContext("2d");
  const img = octx.createImageData(nH, nP);
  for (let pi = 0; pi < nP; pi++){
    const rowY = nP - 1 - pi;            // prix haut en haut de l'image
    for (let hi = 0; hi < nH; hi++){
      const [r, g, b, a] = fluxShade(mat[hi][pi], refArr[hi], ref99Arr[hi]);
      const idx = (rowY * nH + hi) * 4;
      img.data[idx] = r; img.data[idx + 1] = g; img.data[idx + 2] = b;
      img.data[idx + 3] = Math.round(255 * a);
    }
  }
  octx.putImageData(img, 0, 0);

  // montée en résolution PROGRESSIVE (x3 par étage, jamais un facteur unique
  // énorme) pour un lissage homogène quel que soit le moteur de rendu --
  // l'inverse d'un mipmap. Les étages sont réutilisés d'un appel à l'autre.
  let src = FLUX_OFF, sw = nH, sh = nP, stageIdx = 0;
  while (sw < gw / 3 && sh < gh / 3 && sw < 480){
    const nw = Math.min(gw, sw * 3), nh = Math.min(gh, sh * 3);
    if (!FLUX_STAGES[stageIdx]) FLUX_STAGES[stageIdx] = document.createElement("canvas");
    const stage = FLUX_STAGES[stageIdx];
    stage.width = nw; stage.height = nh;
    const sctx = stage.getContext("2d");
    sctx.imageSmoothingEnabled = true;
    if ("imageSmoothingQuality" in sctx) sctx.imageSmoothingQuality = "high";
    sctx.drawImage(src, 0, 0, nw, nh);
    src = stage; sw = nw; sh = nh; stageIdx++;
  }
  FLUX_GRAD_SRC = src;
  FLUX_GRAD_META = {sw, sh};
}

// Reconstruit le bitmap seulement si les données, le mode, la bascule
// strikes ou la taille du canevas ont changé depuis le dernier appel --
// sinon ne fait rien, réutilise FLUX_GRAD_SRC tel quel (req. 1 : pan/zoom =
// simple drawImage).
function fluxGradient(d, mat, lay){
  const key = fluxDataKey(d) + "|" + lay.gw + "|" + lay.gh;
  if (FLUX_GRAD_KEY !== key){
    fluxBuildGradient(mat, lay.nH, lay.nP, lay.gw, lay.gh);
    FLUX_GRAD_KEY = key;
  }
}

// Vue par défaut = viewMin/viewMax (élargie à l'historique, cf. fluxLayout) --
// PAS fullMin/fullMax, qui reste l'étendue stricte de price_grid pour le
// recadrage du bitmap dans drawFluxBg.
function fluxCurrentView(lay){
  const {viewMin, viewMax} = lay;
  if (!FLUX_VIEW || FLUX_VIEW.max <= viewMin || FLUX_VIEW.min >= viewMax){
    FLUX_VIEW = {min: viewMin, max: viewMax};
  }
  return FLUX_VIEW;
}

// ---- cvBg : UNIQUEMENT le dégradé. Un pan/zoom ne fait que recadrer le
// bitmap déjà en cache (fluxGradient est un no-op si rien n'a changé) --
// clip() empêche le dégradé de déborder sur la marge d'étiquettes. ----
function drawFluxBg(cv, d, mat, lay){
  const ctx = cv.getContext("2d");
  const {w, h, dpr} = FLUX_SIZE;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (!lay) return;
  fluxGradient(d, mat, lay);
  const view = fluxCurrentView(lay);
  drawFluxHistBg(ctx, lay, view.min, view.max);
  const {sw, sh} = FLUX_GRAD_META;
  const cropTop = (lay.fullMax - view.max) / (lay.fullMax - lay.fullMin || 1) * sh;
  const cropH = (view.max - view.min) / (lay.fullMax - lay.fullMin || 1) * sh;
  ctx.save();
  ctx.beginPath(); ctx.rect(lay.padL, lay.padT, lay.gw, lay.gh); ctx.clip();
  ctx.imageSmoothingEnabled = true;
  if ("imageSmoothingQuality" in ctx) ctx.imageSmoothingQuality = "high";
  ctx.drawImage(FLUX_GRAD_SRC, 0, cropTop, sw, Math.max(1, cropH), lay.padL, lay.padT, lay.gw, lay.gh);
  ctx.restore();
}

// ---- historique reconstitué : chaque tir de cron passé est un "maintenant"
// réel avec sa PROPRE price_grid (recentrée sur le spot de ce moment-là),
// donc pas empilable tel quel dans une grille commune comme le fait la
// projection. Deux essais précédents ont raté la cible : un flou à rayon
// fixe sur des rectangles déjà nets (incohérent -- rectangles très inégaux
// en taille, un même flou n'adoucit pas pareil un large bloc de tout début
// de séance et une colonne étroite de fin de séance) ; puis des cellules
// nettes avec liseré (correct mais "en escalier", jamais aussi lisse que la
// projection).
//
// Ici : VRAIE interpolation, pas un artifice visuel. (1) Chaque entrée est
// rééchantillonnée sur un axe de prix COMMUN (nRows lignes couvrant
// [priceMin, priceMax]) en prenant le bac le plus proche dans SA propre
// grille -- aligne les entrées entre elles sans jamais recalculer une
// valeur. (2) Pour chaque colonne temporelle cible, interpolation LINÉAIRE
// entre les deux tirs réels qui l'encadrent (un book de gamma dealer évolue
// bel et bien de façon continue entre deux tirs, on ne fait qu'estimer ce
// qu'on n'a pas mesuré). (3) Le champ ainsi obtenu est un vrai continu, donc
// la même montée en résolution progressive que fluxBuildGradient (x3/étage,
// imageSmoothingQuality "high") s'y applique légitimement -- résultat : même
// pipeline de rendu final que la projection, donc même qualité perçue.
// Avec 1 seule entrée (tout début de séance), aucun encadrement possible :
// le rendu tombe naturellement sur un dégradé vertical uni (le vrai gamma
// par strike de ce tir, sans variation temporelle -- il n'y a réellement
// rien de plus à montrer avant le tir suivant), jamais un bloc flou. Coupé
// net à la bordure "maintenant" par le clip (pas de bavure sur la
// projection). ----
const FLUX_HIST_FIELD = {gamma: "gamma0", vanna: "vanna0", charm: "charm0"};
let FLUX_HIST_OFF = null, FLUX_HIST_STAGES = [];
function drawFluxHistBg(ctx, lay, priceMin, priceMax){
  const hd = FLUX_HIST_ON && FLUX_HIST_DATA && FLUX_HIST_DATA.target === TARGET ? FLUX_HIST_DATA : null;
  const entries = hd && hd.entries;
  if (!entries || !entries.length || lay.histW <= 4) return;
  const field = FLUX_HIST_FIELD[FLUX_MODE];
  const {histX0, histW, elapsedH, histOpenShifted, padT, gh} = lay;

  // Référence de normalisation sur les valeurs BRUTES de la série (pas
  // rééchantillonnées) -- nature différente de la projection (cf.
  // commentaire fluxBuildGradient sur le charm) : chaque entrée est un
  // "maintenant" réel, pas de plancher temporel artificiel, donc pas besoin
  // de normaliser par colonne.
  const absVals = [];
  for (const e of entries) for (const v of (e[field] || [])) absVals.push(Math.abs(v));
  if (!absVals.length) return;
  absVals.sort((a, b) => a - b);
  const ref = fluxPercentile(absVals, 0.95) || 1;
  const ref99 = fluxPercentile(absVals, 0.99) || ref;

  // (1) rééchantillonnage de chaque entrée valide sur l'axe de prix commun.
  // rowPrice CROISSANT avec r (comme price_grid, qui est trié croissant) --
  // le pointeur gi n'avance que vers l'avant (recherche par balayage
  // unique, O(nRows+grid.length)) : le faire croiser un axe DÉCROISSANT
  // le bloquait dès r=1 sur le dernier bac (celui du prix le plus haut),
  // collapsant les 24 lignes suivantes sur une seule valeur -- exactement
  // ce qui donnait des bandes VERTICALES (un aplat par colonne/tir) au lieu
  // des bandes horizontales attendues (un aplat par ligne/prix). Prix haut
  // en haut de l'image posé séparément à l'écriture du pixel (rowY),
  // même partition des responsabilités que fluxBuildGradient.
  const nRows = 25;
  const rowPrice = r => priceMin + (r + 0.5) / nRows * (priceMax - priceMin || 1);
  const rowsByEntry = [], thByEntry = [];
  for (const e of entries){
    const grid = e.price_grid, vals = e[field];
    if (!grid || !vals || !grid.length) continue;
    const row = new Array(nRows);
    let gi = 0;
    for (let r = 0; r < nRows; r++){
      const p = rowPrice(r);
      while (gi < grid.length - 1 && Math.abs(grid[gi + 1] - p) <= Math.abs(grid[gi] - p)) gi++;
      row[r] = vals[gi];
    }
    rowsByEntry.push(row);
    thByEntry.push((tzShift(Math.floor(new Date(e.t).getTime() / 1000)) - histOpenShifted) / 3600);
  }
  if (!rowsByEntry.length) return;

  // (2) interpolation temporelle sur un tampon natif basse résolution --
  // nCols borné, la netteté vient ensuite de la montée en résolution.
  const nCols = Math.max(2, Math.min(180, Math.round(histW)));
  if (!FLUX_HIST_OFF) FLUX_HIST_OFF = document.createElement("canvas");
  const off = FLUX_HIST_OFF;
  off.width = nCols; off.height = nRows;
  const octx = off.getContext("2d");
  const img = octx.createImageData(nCols, nRows);

  let ei = 0;
  for (let c = 0; c < nCols; c++){
    const t = nCols === 1 ? elapsedH : (c / (nCols - 1)) * elapsedH;
    while (ei < thByEntry.length - 2 && thByEntry[ei + 1] < t) ei++;
    const rowsA = rowsByEntry[ei];
    let frac = 0, rowsB = rowsA;
    if (ei < thByEntry.length - 1){
      const span = thByEntry[ei + 1] - thByEntry[ei];
      frac = span > 1e-6 ? Math.min(1, Math.max(0, (t - thByEntry[ei]) / span)) : 0;
      rowsB = rowsByEntry[ei + 1];
    }
    for (let r = 0; r < nRows; r++){
      const v = rowsA[r] + (rowsB[r] - rowsA[r]) * frac;
      const [red, g, b, a] = fluxShade(v, ref, ref99);
      const rowY = nRows - 1 - r;   // prix haut en haut de l'image, cf. fluxBuildGradient
      const idx = (rowY * nCols + c) * 4;
      img.data[idx] = red; img.data[idx + 1] = g; img.data[idx + 2] = b;
      img.data[idx + 3] = Math.round(255 * a);
    }
  }
  octx.putImageData(img, 0, 0);

  // (3) montée en résolution progressive jusqu'à la taille d'affichage
  // réelle (CSS px -- le ctx cible est déjà mis à l'échelle DPR par
  // l'appelant, cf. drawFluxBg).
  let src = off, sw = nCols, sh = nRows, stageIdx = 0;
  while (sw < histW / 3 && sh < gh / 3 && sw < 480){
    const nw = Math.min(histW, sw * 3), nh = Math.min(gh, sh * 3);
    if (!FLUX_HIST_STAGES[stageIdx]) FLUX_HIST_STAGES[stageIdx] = document.createElement("canvas");
    const stage = FLUX_HIST_STAGES[stageIdx];
    stage.width = nw; stage.height = nh;
    const sctx = stage.getContext("2d");
    sctx.imageSmoothingEnabled = true;
    if ("imageSmoothingQuality" in sctx) sctx.imageSmoothingQuality = "high";
    sctx.drawImage(src, 0, 0, nw, nh);
    src = stage; sw = nw; sh = nh; stageIdx++;
  }

  ctx.save();
  ctx.beginPath(); ctx.rect(histX0, padT, histW, gh); ctx.clip();
  ctx.imageSmoothingEnabled = true;
  if ("imageSmoothingQuality" in ctx) ctx.imageSmoothingQuality = "high";
  ctx.drawImage(src, 0, 0, sw, sh, histX0, padT, histW, gh);
  ctx.restore();
}

// ---- cvMain : grille, axes, repères et étiquettes. Redessinée en vecteur à
// chaque pan/zoom (coordonnées écran dépendantes de la vue) mais SANS jamais
// retoucher au bitmap du dégradé ----
function drawFluxMain(cv, d, mat, lay){
  const ctx = cv.getContext("2d");
  const {w, h, dpr} = FLUX_SIZE;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (!lay){
    ctx.fillStyle = C.faint;
    ctx.font = "11px IBM Plex Mono, monospace";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText($("fluxNote").textContent || "", w / 2, h / 2);
    FLUX_LAYOUT = null;
    return;
  }
  const view = fluxCurrentView(lay);
  const {padL, padT, gw, gh, nH, nP, histX0, histW, histPoints, elapsedH, histOpenShifted} = lay;
  const priceMin = view.min, priceMax = view.max;
  const yFor = p => padT + gh * (1 - (p - priceMin) / (priceMax - priceMin || 1));
  const xFor = hi => padL + gw * (hi / Math.max(1, nH - 1));
  const xForHist = th => histX0 + histW * (th / Math.max(1e-6, elapsedH));
  const lineX0 = histW > 0 ? histX0 : padL;   // repères/gridlines : traversent aussi la portion historique quand elle existe


  // ---- axes : prix en unités sigma, temps en heures jusqu'à la clôture --
  // traits arrondis au demi-pixel pour rester nets à n'importe quel DPR.
  // Le multiple sigma vient de d.unit (même unité que open_grid, publiée
  // avec la matrice) plutôt que d'un pas de grille supposé en dur -- la
  // grille prix a changé une fois de résolution déjà (13 -> 25 colonnes,
  // cf. _flow_grids dans api/gex.py) et un pas figé ici se serait
  // silencieusement désynchronisé de la vraie valeur envoyée par le serveur.
  // i += 2 : une étiquette tous les 0.5σ (la grille avance par pas de 0.25σ). ----
  ctx.strokeStyle = "rgba(255,255,255,.08)"; ctx.lineWidth = 1;
  ctx.font = "10px IBM Plex Mono, monospace";
  ctx.fillStyle = C.faint;
  const sigUnit = d.unit || null;
  for (let i = 0; i < nP; i += 2){
    const mult = sigUnit ? Math.round(((d.price_grid[i] - d.spot) / sigUnit) * 2) / 2 : null;
    const y = yFor(d.price_grid[i]), yy = Math.round(y) + 0.5;
    ctx.beginPath(); ctx.moveTo(lineX0, yy); ctx.lineTo(padL + gw, yy); ctx.stroke();
    if (mult == null) continue;
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    ctx.fillText((mult > 0 ? "+" : mult === 0 ? "" : "") + mult + "σ", padL - 6, y);
  }
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  d.hours.forEach((hVal, hi) => {
    const x = xFor(hi);
    const isLast = hi === d.hours.length - 1;
    const label = hVal === 0 ? t("fluxNow")
                : (isLast && hVal % 1 !== 0) ? t("fluxClose")
                : "+" + Math.round(hVal) + "h";
    ctx.fillText(label, x, padT + gh + 4);
  });

  // ---- graduations horaires côté historique : sans ça l'axe reste vide à
  // gauche de "maintenant" et rien ne dit à quel moment de la séance
  // correspond un point du tracé. Heures rondes dans le fuseau AFFICHÉ
  // (histOpenShifted est déjà l'heure d'ouverture "décalée" pour ce fuseau,
  // cf. fluxSessionHistory) -- même convention que "Calculé HH:MM". Portion
  // trop étroite (tout début OU tout en fin de séance, où le dernier tick
  // rond tombe juste avant "maintenant") : un seuil de marge fixe ne
  // suffisait pas à empêcher le chevauchement ("07hmaint." illisible, repéré
  // en testant l'historique avec peu et avec beaucoup d'entrées) -- la
  // largeur réelle des libellés dépend de la langue (fluxNow FR "maint." vs
  // EN "now"), mesurée ici plutôt que devinée. ----
  if (histW > 60 && elapsedH > 0){
    const openDate = new Date(histOpenShifted * 1000);
    const openMin = openDate.getUTCHours() * 60 + openDate.getUTCMinutes();
    let tickMin = Math.ceil(openMin / 60) * 60;
    const nowHalfW = ctx.measureText(t("fluxNow")).width / 2;
    while ((tickMin - openMin) / 60 <= elapsedH + 1e-9){
      const th = (tickMin - openMin) / 60;
      const x = xForHist(th);
      const label = String(Math.floor(tickMin / 60) % 24).padStart(2, "0") + "h";
      const labelHalfW = ctx.measureText(label).width / 2;
      if (padL - x > nowHalfW + labelHalfW + 6){
        ctx.fillText(label, x, padT + gh + 4);
      }
      tickMin += 60;
    }
  }

  // ---- prix réalisé depuis l'ouverture cash (9h30 ET) jusqu'à "maintenant" :
  // ligne simple à gauche de la projection, mêmes bougies que le chart de prix
  // (CHART.bars, cf. fluxSessionHistory). Répond au constat "tout est à droite,
  // on ne voit pas ce qui s'est passé pendant la séance" -- la portion
  // dégradé/gamma à droite de "maintenant" reste inchangée. ----
  if (histW > 4 && histPoints.length > 1){
    ctx.beginPath();
    histPoints.forEach((p, i) => {
      const x = xForHist(p.t), y = yFor(p.price);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = "rgba(236,234,228,.55)"; ctx.lineWidth = 1.25;
    ctx.stroke();

    const nowX = Math.round(padL) + 0.5;   // séparation "maintenant" : la projection démarre pile après
    ctx.setLineDash([2, 3]); ctx.strokeStyle = "rgba(255,255,255,.15)"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(nowX, padT); ctx.lineTo(nowX, padT + gh); ctx.stroke();
    ctx.setLineDash([]);
  }

  // ---- repères : ligne toujours tracée, étiquette en pastille (fond sombre
  // + liseré coloré, même traitement que les niveaux du chart de prix) posée
  // seulement si la place le permet. tier 0/1 (prix courant, CW/PW/Flip)
  // toujours étiquetés -- rares et prioritaires ; tier 2 (strikes) triés par
  // ratio vol/OI décroissant, le reste évincé en simple point. Filtrés ici
  // par la vue courante (lay.markers porte les candidats sur la plage
  // complète, cf. fluxLayout). ----
  const markers = lay.markers.filter(m => m.price >= priceMin && m.price <= priceMax);
  markers.forEach(m => { m.y = yFor(m.price); m.shown = false; });
  const rowH = 17;
  const placedYs = [];
  const fits = y => !placedYs.some(py => Math.abs(py - y) < rowH);
  const order = markers.filter(m => m.tier < 2)
    .concat(markers.filter(m => m.tier === 2).sort((a, b) => b.ratio - a.ratio));

  for (const m of order){
    const show = m.tier < 2 || fits(m.y);
    if (!show){
      ctx.globalAlpha = 0.5; ctx.fillStyle = m.color;
      ctx.beginPath(); ctx.arc(w - 4, m.y, 1.7, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = 1;
      m.dotX = w - 4; m.dotY = m.y;
      continue;
    }
    const my2 = Math.round(m.y) + 0.5;
    // tier 0/1 (prix courant, CW/PW/Flip) traversent aussi l'historique --
    // ces niveaux étaient déjà "vrais" pendant la séance. Tier 2 (strikes,
    // nombreux) reste confiné à la projection pour ne pas surcharger la
    // portion historique.
    const mx0 = m.tier < 2 ? lineX0 : padL;
    ctx.setLineDash(m.dash); ctx.strokeStyle = m.color; ctx.lineWidth = m.lw;
    ctx.beginPath(); ctx.moveTo(mx0, my2); ctx.lineTo(padL + gw, my2); ctx.stroke();
    ctx.setLineDash([]);

    placedYs.push(m.y);
    ctx.font = m.font;
    const tw = ctx.measureText(m.text).width;
    const bh = 15, bw = tw + 13;
    const x0 = padL + gw + 5;
    ctx.fillStyle = "rgba(12,12,15,.9)";
    ctx.fillRect(x0, m.y - bh / 2, bw, bh);
    ctx.fillStyle = m.color;
    ctx.fillRect(x0, m.y - bh / 2, 2, bh);                  // liseré de couleur
    ctx.fillStyle = m.textColor;
    ctx.textAlign = "left"; ctx.textBaseline = "middle";
    ctx.fillText(m.text, x0 + 7, m.y + 0.5);
    m.shown = true; m.boxX = x0; m.boxY = m.y - bh / 2; m.boxW = bw; m.boxH = bh;
  }

  // markers + spot repassés à la couche curseur (cf. drawFluxCursorLayer) --
  // survol des niveaux/strikes, jamais un recalcul du dégradé ou un second
  // passage sur cvMain pour une simple hover.
  FLUX_LAYOUT = {w, h, dpr, padL, padT, gw, gh, priceMin, priceMax, nH, nP, hours: d.hours, rows: mat,
                 spot: lay.spot, markers: order, viewMin: lay.viewMin, viewMax: lay.viewMax};
}

// "maint." / "+1.3h" / "clôture" : même vocabulaire que les graduations de
// l'axe, mais en continu (pas de trous entre les heures pointées).
function fluxHoverTimeLabel(hVal, hours){
  if (Math.abs(hVal) < 0.05) return t("fluxNow");
  if (Math.abs(hVal - hours[hours.length - 1]) < 0.05) return t("fluxClose");
  return (hVal > 0 ? "+" : "") + hVal.toFixed(1) + "h";
}

// Police unique de la couche curseur, fixée une fois hors de toute boucle de
// dessin (jamais réassignée par cellule/marqueur -- un seul ctx.font par
// frame, measureText appelé seulement pour les libellés réellement affichés).
const FLUX_CUR_FONT = "11px IBM Plex Mono, monospace";

// Infobulle générique (fond semi-opaque + bordure fine, seule façon de rester
// lisible sur un dégradé coloré) : plusieurs lignes, positionnée près de
// (x, y), qui se retourne côté gauche/haut dès qu'elle sortirait du cadre.
function fluxDrawTooltip(ctx, x, y, lines, w, h, borderColor){
  const lh = 15, padX = 8, padY = 6;
  let tw = 0;
  for (const s of lines) tw = Math.max(tw, ctx.measureText(s).width);
  const boxW = tw + padX * 2, boxH = lines.length * lh + padY;
  let bx = x + 12, by = y - boxH / 2;
  if (bx + boxW > w) bx = x - boxW - 12;
  if (bx < 0) bx = 2;
  if (by < 0) by = 2;
  if (by + boxH > h) by = h - boxH - 2;
  ctx.fillStyle = "rgba(12,12,15,.94)";
  ctx.strokeStyle = borderColor;
  ctx.lineWidth = 1;
  ctx.fillRect(bx, by, boxW, boxH);
  ctx.strokeRect(bx + 0.5, by + 0.5, boxW - 1, boxH - 1);
  ctx.fillStyle = C.text;
  ctx.textAlign = "left"; ctx.textBaseline = "middle";
  lines.forEach((s, i) => ctx.fillText(s, bx + padX, by + padY / 2 + lh * i + lh / 2));
}

// Repère (niveau ou strike) le plus proche du pointeur : survol direct de son
// étiquette en marge, sinon proximité verticale (<=6px) avec sa ligne DANS la
// grille. Le repère de prix courant (tier 0) n'est pas un "niveau" au sens de
// l'ergonomie demandée -- exclu du survol.
function fluxHoverMarker(markers, mx, my, inBounds){
  let hover = null, bestDy = 6;
  for (const m of (markers || [])){
    if (m.tier < 1) continue;
    if (m.shown && mx >= m.boxX && mx <= m.boxX + m.boxW && my >= m.boxY && my <= m.boxY + m.boxH){
      return m;   // survol direct de l'étiquette : priorité absolue
    }
    if (inBounds){
      const dy = Math.abs(my - m.y);
      if (dy <= bestDy){ bestDy = dy; hover = m; }
    }
  }
  return hover;
}

// ---- cvCursor : réticule + infobulle, propre couche transparente au-dessus
// de cvBg/cvMain -- ne les touche jamais, donc jamais de recalcul du
// dégradé pour un simple mouvement de souris. ----
function drawFluxCursorLayer(cv){
  const ctx = cv.getContext("2d");
  const {w, h, dpr} = FLUX_SIZE;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const L = FLUX_LAYOUT, pos = FLUX_CURSOR;
  if (!L || !pos) return;
  const {mx, my} = pos;
  const {padL, padT, gw, gh, priceMin, priceMax, nH, nP, hours, rows, markers, spot} = L;
  const inBounds = mx >= padL && mx <= padL + gw && my >= padT && my <= padT + gh;
  ctx.font = FLUX_CUR_FONT;

  // ---- survol niveaux/strikes : vit aussi hors grille (étiquettes en marge),
  // donc évalué avant tout retour anticipé sur inBounds. ----
  const hover = fluxHoverMarker(markers, mx, my, inBounds);
  if (hover){
    // le reste des étiquettes s'atténue pour faire ressortir la sélection
    for (const m of markers){
      if (m === hover || !m.shown) continue;
      ctx.fillStyle = "rgba(9,9,11,.55)";
      ctx.fillRect(m.boxX, m.boxY, m.boxW, m.boxH);
    }
    // tier 2 (strikes) : couleur de base modulée par le ratio vol/OI, jamais
    // pleinement opaque -- le survol doit forcer l'opacité max quel que soit
    // ce ratio (cf. "l'étiquette passe en pleine opacité").
    const hoverColor = hover.tier === 2 ? "rgba(236,234,228,.95)" : hover.color;
    const hy = Math.round(hover.y) + 0.5;
    ctx.setLineDash(hover.dash || []);
    ctx.strokeStyle = hoverColor; ctx.lineWidth = (hover.lw || 1) + 1.6;
    ctx.beginPath(); ctx.moveTo(padL, hy); ctx.lineTo(padL + gw, hy); ctx.stroke();
    ctx.setLineDash([]);

    if (hover.shown){
      ctx.fillStyle = "rgba(20,20,24,.98)";
      ctx.fillRect(hover.boxX, hover.boxY, hover.boxW, hover.boxH);
      ctx.fillStyle = hoverColor;
      ctx.fillRect(hover.boxX, hover.boxY, 2, hover.boxH);
      ctx.strokeStyle = hoverColor; ctx.lineWidth = 1;
      ctx.strokeRect(hover.boxX + 0.5, hover.boxY + 0.5, hover.boxW - 1, hover.boxH - 1);
      ctx.fillStyle = hover.textColor;
      ctx.textAlign = "left"; ctx.textBaseline = "middle";
      ctx.fillText(hover.text, hover.boxX + 7, hover.boxY + hover.boxH / 2 + 0.5);
    }

    const distPts = spot != null ? hover.price - spot : null;
    const distPct = distPts != null && spot ? distPts / spot * 100 : null;
    const distTxt = distPts == null ? t("pts") + " —"
      : (distPts >= 0 ? "+" : "") + fmt(distPts, 0) + " " + t("pts")
        + (distPct != null ? "  (" + (distPct >= 0 ? "+" : "") + fmt(distPct, 2) + " %)" : "");
    const lines = hover.tier === 2
      ? [
          `${hover.isCall ? t("fvCalls") : t("fvPuts")} ${fmt(hover.strike)}`,
          `${fmtFluxCount(hover.volume)} ${t("fvVolUnit")} · ${t("fluxOiLbl")} ${fmtFluxCount(hover.oi)}` +
            ` · ×${hover.vol_oi_ratio != null ? hover.vol_oi_ratio.toFixed(2) : "—"}`,
          distTxt,
        ]
      : [hover.full || hover.text, `${t("th")[1]}: ${fmt(hover.price)}`, distTxt];
    fluxDrawTooltip(ctx, mx, hover.y, lines, w, h, hoverColor);
  }

  if (!inBounds) return;

  const price = priceMax - (my - padT) / gh * (priceMax - priceMin || 1);
  const hFrac = (mx - padL) / gw * Math.max(1, nH - 1);
  const hi0 = Math.max(0, Math.min(nH - 1, Math.floor(hFrac)));
  const hi1 = Math.min(nH - 1, hi0 + 1);
  const hVal = hours[hi0] + (hours[hi1] - hours[hi0]) * (hFrac - hi0);
  const hi = Math.max(0, Math.min(nH - 1, Math.round(hFrac)));
  const step = (priceMax - priceMin) / Math.max(1, nP - 1);
  const pi = Math.max(0, Math.min(nP - 1, Math.round((price - priceMin) / (step || 1))));
  const val = rows[hi] ? rows[hi][pi] : null;

  // ---- lignes pointillées, même style que le crosshair du chart de prix ----
  ctx.setLineDash([3, 3]);
  ctx.strokeStyle = "rgba(230,228,222,.35)";
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(padL, my); ctx.lineTo(padL + gw, my); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(mx, padT); ctx.lineTo(mx, padT + gh); ctx.stroke();
  ctx.setLineDash([]);

  // ---- pastille de prix, bord droit ----
  const priceTxt = fmt(price, 0);
  const ptw = ctx.measureText(priceTxt).width;
  const pbx = w - ptw - 13, pbh = 17;
  ctx.fillStyle = C.text;
  ctx.fillRect(pbx, my - pbh / 2, ptw + 10, pbh);
  ctx.fillStyle = "#0A0A0C";
  ctx.textAlign = "left"; ctx.textBaseline = "middle";
  ctx.fillText(priceTxt, pbx + 5, my + 0.5);

  // ---- pastille de temps, bord bas ----
  const timeTxt = fluxHoverTimeLabel(hVal, hours);
  const ttw = ctx.measureText(timeTxt).width;
  const tbx = Math.max(0, Math.min(w - ttw - 10, mx - ttw / 2 - 5)), tby = padT + gh + 2;
  ctx.fillStyle = C.text;
  ctx.fillRect(tbx, tby, ttw + 10, 15);
  ctx.fillStyle = "#0A0A0C";
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText(timeTxt, tbx + ttw / 2 + 5, tby + 7.5);

  // ---- tooltip de cellule : grecque + valeur $, prix, horizon -- masquée
  // pendant le survol d'un niveau/strike pour ne pas superposer deux
  // infobulles au même endroit. ----
  if (!hover && val != null){
    const lines = [
      `${FLUX_MODE_LABEL[FLUX_MODE] || FLUX_MODE}  ${fmtFluxUsd(val)}`,
      `${t("th")[1]}: ${fmt(price, 0)}`,
      fluxHoverTimeLabel(hVal, hours),
    ];
    fluxDrawTooltip(ctx, mx, my, lines, w, h, val >= 0 ? C.teal : C.red);
  }
}

// Bornes externes du pan/zoom : viewMin/viewMax du dernier fluxLayout (élargi
// à l'historique de séance, cf. fluxLayout) plutôt que price_grid brut --
// sinon la toute première interaction (molette, glisser) recadrerait
// immédiatement sur la seule bande ±3σ et couperait la portion historique
// visible jusque-là. Repli sur price_grid si aucun layout n'a encore tourné.
function fluxOuterBounds(){
  const L = FLUX_LAYOUT, d = FLUX_DATA;
  if (L) return {min: L.viewMin, max: L.viewMax};
  return {min: d.price_grid[0], max: d.price_grid[d.price_grid.length - 1]};
}

// ---- pan/zoom : clampe toujours au span minimal et aux bornes externes --
// il n'y a pas de donnée à révéler au-delà, contrairement à un vrai
// historique de chart. Partagée par la molette, le pincement tactile et
// fluxZoomAt. ----
function fluxClampView(newMin, newMax){
  const {min: fullMin, max: fullMax} = fluxOuterBounds();
  const minSpan = (fullMax - fullMin) * 0.04;      // pas de zoom infini sur une grille de 13 bandes
  if (newMax - newMin < minSpan){
    const mid = (newMin + newMax) / 2;
    newMin = mid - minSpan / 2; newMax = mid + minSpan / 2;
  }
  if (newMin < fullMin){ newMax = Math.min(fullMax, newMax + (fullMin - newMin)); newMin = fullMin; }
  if (newMax > fullMax){ newMin = Math.max(fullMin, newMin - (newMax - fullMax)); newMax = fullMax; }
  return {min: newMin, max: newMax};
}

// Zoom centré sur un prix donné (molette ou pincement) : factor < 1
// rapproche les bornes (zoom avant), > 1 les écarte.
function fluxZoomAt(price, factor){
  if (!FLUX_LAYOUT || !FLUX_DATA) return;
  const d = FLUX_DATA;
  if (!d.price_grid || !d.price_grid.length) return;
  const cur = FLUX_VIEW || fluxOuterBounds();
  const newMin = price - (price - cur.min) * factor;
  const newMax = price + (cur.max - price) * factor;
  FLUX_VIEW = fluxClampView(newMin, newMax);
  FLUX_CURSOR = null;
  invalidateFlux("bg", "main", "cursor");
}

// Écoute posée une seule fois sur cvCursor (seule couche pointer-events, cf.
// CSS), comme le ResizeObserver de setFluxView. Souris ET tactile : un doigt
// déplace comme le glisser souris, deux doigts pincent pour zoomer --
// touch-action:none en CSS empêche le navigateur de scroller/zoomer la page
// pendant le geste. Chaque interaction se contente d'invalidate(...) : un
// seul redessin par frame, jamais plus, cf. flushFluxDirty.
function bindFluxHover(){
  if (FLUX_HOVER_WIRED) return;
  FLUX_HOVER_WIRED = true;
  const cv = $("fluxCvCursor");
  if (!cv) return;
  let drag = null;    // {startY, view0} pendant un glisser (souris ou un doigt)
  let pinch = null;   // {dist0, view0, midPrice} pendant un pincement à deux doigts

  const setCursorPos = (mx, my) => { FLUX_CURSOR = {mx, my}; invalidateFlux("cursor"); };
  const clearCursorPos = () => { FLUX_CURSOR = null; invalidateFlux("cursor"); };
  const touchXY = touch => {
    const r = cv.getBoundingClientRect();
    return {mx: touch.clientX - r.left, my: touch.clientY - r.top};
  };
  // crosshair sur la zone de tracé, défaut ailleurs (comfort n°5) -- jamais
  // pendant un glisser, où le curseur reste "grabbing".
  const updateCursorIcon = (mx, my) => {
    if (drag){ cv.style.cursor = "grabbing"; return; }
    const L = FLUX_LAYOUT;
    const inGrid = L && mx >= L.padL && mx <= L.padL + L.gw && my >= L.padT && my <= L.padT + L.gh;
    cv.style.cursor = inGrid ? "crosshair" : "default";
  };

  const panTo = my => {
    const L = FLUX_LAYOUT;
    if (!L || !drag || !FLUX_DATA) return;
    // glisser le contenu VERS LE BAS doit faire descendre les prix affichés
    // (comme saisir la grille et la tirer) : la fenêtre de prix se décale
    // dans le sens opposé au mouvement.
    const dy = my - drag.startY;
    const priceDelta = (dy / L.gh) * (drag.view0.max - drag.view0.min);
    const {min: fullMin, max: fullMax} = fluxOuterBounds();
    let newMin = drag.view0.min + priceDelta, newMax = drag.view0.max + priceDelta;
    if (newMin < fullMin){ newMax += fullMin - newMin; newMin = fullMin; }
    if (newMax > fullMax){ newMin -= newMax - fullMax; newMax = fullMax; }
    FLUX_VIEW = {min: newMin, max: newMax};
    invalidateFlux("bg", "main", "cursor");
  };

  cv.addEventListener("mousemove", e => {
    const r = cv.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    if (drag) panTo(my); else setCursorPos(mx, my);
    updateCursorIcon(mx, my);
  });
  cv.addEventListener("mousedown", e => {
    if (!FLUX_LAYOUT || !FLUX_VIEW) return;
    drag = {startY: e.clientY, view0: {...FLUX_VIEW}};
    cv.style.cursor = "grabbing";
  });
  window.addEventListener("mouseup", e => {
    if (!drag) return;
    drag = null;
    const r = cv.getBoundingClientRect();
    updateCursorIcon(e.clientX - r.left, e.clientY - r.top);
  });
  cv.addEventListener("mouseleave", () => {
    if (!drag){ clearCursorPos(); cv.style.cursor = "default"; }
  });
  cv.addEventListener("wheel", e => {
    if (!FLUX_LAYOUT) return;
    e.preventDefault();
    const r = cv.getBoundingClientRect();
    const my = e.clientY - r.top;
    const L = FLUX_LAYOUT;
    const price = L.priceMax - (my - L.padT) / L.gh * (L.priceMax - L.priceMin || 1);
    fluxZoomAt(price, e.deltaY > 0 ? 1.15 : 1 / 1.15);
  }, {passive: false});
  cv.addEventListener("dblclick", () => {
    FLUX_VIEW = null; FLUX_CURSOR = null;
    invalidateFlux("bg", "main", "cursor");
  });

  const touchDist = (t0, t1) => Math.hypot(t1.clientX - t0.clientX, t1.clientY - t0.clientY);
  cv.addEventListener("touchstart", e => {
    if (!FLUX_LAYOUT || !FLUX_VIEW) return;
    if (e.touches.length === 1){
      pinch = null;
      drag = {startY: e.touches[0].clientY, view0: {...FLUX_VIEW}};
      // tactile : l'infobulle apparaît à l'appui (comfort n°5), même point
      // que le doigt qui va ensuite glisser/paner.
      const {mx, my} = touchXY(e.touches[0]);
      setCursorPos(mx, my);
    } else if (e.touches.length === 2){
      drag = null;
      clearCursorPos();
      const r = cv.getBoundingClientRect();
      const midY = (e.touches[0].clientY + e.touches[1].clientY) / 2 - r.top;
      const L = FLUX_LAYOUT;
      pinch = {
        dist0: touchDist(e.touches[0], e.touches[1]),
        view0: {...FLUX_VIEW},
        midPrice: L.priceMax - (midY - L.padT) / L.gh * (L.priceMax - L.priceMin || 1),
      };
    }
    e.preventDefault();
  }, {passive: false});
  cv.addEventListener("touchmove", e => {
    if (pinch && e.touches.length === 2){
      const d1 = touchDist(e.touches[0], e.touches[1]);
      const factor = pinch.dist0 / (d1 || 1e-6);   // écarter les doigts = zoom avant
      const price = pinch.midPrice, cur = pinch.view0;
      const newMin = price - (price - cur.min) * factor, newMax = price + (cur.max - price) * factor;
      FLUX_VIEW = fluxClampView(newMin, newMax);
      invalidateFlux("bg", "main", "cursor");
    } else if (drag && e.touches.length === 1){
      panTo(e.touches[0].clientY);
      const {mx, my} = touchXY(e.touches[0]);
      setCursorPos(mx, my);
    }
    e.preventDefault();
  }, {passive: false});
  const touchEnd = () => { drag = null; pinch = null; clearCursorPos(); };
  cv.addEventListener("touchend", touchEnd);
  cv.addEventListener("touchcancel", touchEnd);
}

// Fusionne calls/puts du contexte volume/OI (déjà top_n côté serveur, cf.
// flow_volume_context) en une seule liste taguée pour le rendu des repères.
function fluxStrikeMarkers(){
  const vc = FLUX_DATA && FLUX_DATA.volume_context;
  if (!vc) return [];
  const tag = (arr, isCall) => (arr || []).map(e => Object.assign({isCall}, e));
  return tag(vc.calls, true).concat(tag(vc.puts, false));
}

// Statut affiché dans #fluxNote, gardé à part du texte lui-même : un
// changement de langue seul (sans refetch) doit pouvoir le retraduire, cf.
// renderFluxNote() appelée aussi depuis applyLang().
let FLUX_STATUS = null;    // {kind:"loading"|"wait"|"ready"|"error", ts, msg}
let FLUX_EVER_LOADED = false;   // masque #fluxLoader pour de bon après le tout premier loadFlux()

function renderFluxNote(){
  const el = $("fluxNote");
  if (!el || !FLUX_STATUS) return;
  const s = FLUX_STATUS;
  el.textContent = s.kind === "ready" ? t("fluxReady")(s.ts)
                  : s.kind === "wait" ? t("fluxWait")
                  : s.kind === "error" ? t("chartFail")(s.msg)
                  : t("loading");
}

async function loadFlux(){
  if (!FLUX_ON) return;
  const tgt = TARGET;
  const wantHist = FLUX_HIST_ON;
  FLUX_STATUS = {kind: "loading"};
  renderFluxNote();
  try{
    const [r, rh] = await Promise.all([
      fetch(`/api/flow?target=${tgt}`),
      wantHist ? fetch(`/api/flow?target=${tgt}&hist=1`) : Promise.resolve(null),
    ]);
    const d = await r.json();
    if (tgt !== TARGET || !FLUX_ON) return;    // la cible/vue a changé pendant l'attente
    if (!d.ready){
      FLUX_DATA = null;
      FLUX_ANIM = null;
      FLUX_STATUS = {kind: "wait"};
    } else {
      const prev = FLUX_DATA;
      FLUX_ANIM = (prev && prev.target === d.target && prev.generated_utc !== d.generated_utc
                   && fluxSameShape(prev, d))
        ? {from: {gamma: prev.gamma, vanna: prev.vanna, charm: prev.charm}, t0: performance.now()}
        : null;
      FLUX_DATA = d;
      FLUX_STATUS = {kind: "ready", ts: fmtTzHM(d.generated_utc)};
    }
    if (rh){
      const dh = await rh.json();
      FLUX_HIST_DATA = (tgt === TARGET && FLUX_HIST_ON && dh && dh.ready)
        ? {target: tgt, entries: dh.history} : null;
    } else if (!wantHist){
      FLUX_HIST_DATA = null;
    }
  }catch(e){
    FLUX_DATA = null;
    FLUX_ANIM = null;
    FLUX_STATUS = {kind: "error", msg: e.message};
  }
  renderFluxNote();
  renderFluxSynth();
  renderFluxVolCtx();
  drawFlux();
  // Rond de chargement : ne couvre QUE le tout premier appel -- une fois
  // qu'on a une réponse (prête, en attente ou en erreur, peu importe), le
  // statut textuel de #fluxNote suffit ; le remontrer à chaque poll ou
  // changement de marché serait plus gênant qu'utile.
  if (!FLUX_EVER_LOADED){
    FLUX_EVER_LOADED = true;
    const el = $("fluxLoader");
    if (el) el.hidden = true;
  }
}

// Vrai pendant la dernière fenêtre où armFluxPoll a effectivement armé le
// poll -- sert au watchdog ci-dessous à détecter la bascule fermé -> ouvert
// (ouverture de séance, page laissée ouverte toute la nuit) pour recharger
// tout de suite au lieu d'attendre le premier tick des 2 minutes.
let FLUX_POLL_ON = false;

function armFluxPoll(){
  clearInterval(FLUX_POLL);
  const wasOn = FLUX_POLL_ON;
  FLUX_POLL_ON = fluxSessionOpenNow();
  // le cron serveur tourne toutes les 10 min en séance (5 min sur la
  // première et la dernière heure, cf. vercel.json) : 2 min de poll suffit
  // à récupérer chaque nouveau tir sans tarder, sans non plus interroger à
  // chaque cycle de cron pile. Hors séance (soir, nuit, week-end) le cron ne
  // tourne plus -- pas la peine de poller un cache qui ne bouge pas.
  if (FLUX_ON && FLUX_POLL_ON){
    if (!wasOn) loadFlux();
    FLUX_POLL = setInterval(loadFlux, 2 * 60 * 1000);
  }
}

let FLUX_POLL_WATCH = null;

function setFluxView(on){
  FLUX_ON = on;
  try{ localStorage.setItem("gexFluxOn", on ? "1" : "0"); }catch(_){}
  const btn = $("fluxBtn");
  if (btn){
    btn.classList.toggle("on", on);
    // libellé = destination du prochain clic, pas la vue courante
    btn.textContent = on ? "CHART" : "FLUX";
    btn.title = on ? t("chart") : t("fluxTip");
  }
  $("chartCbar").hidden = on;
  $("chartBox").hidden = on;
  $("fluxBox").hidden = !on;
  // la vue Flux n'a rien à faire du contexte dark pool ni des cartes du bas
  // (EM/Pine, niveaux) : seuls le bandeau de métriques et le sélecteur de
  // marché restent affichés. hidden, jamais display: sur ces ids.
  $("dpPanel").hidden = on;
  $("duoCards").hidden = on;
  $("lvWrap").hidden = on;
  if (on){
    $("chartNote").textContent = "";       // statut Flux affiché dans #fluxNote
    $("chartTitle").textContent = t("fluxTitle");
    renderFluxLegend();
    if (!FLUX_RESIZE_WIRED){
      FLUX_RESIZE_WIRED = true;
      new ResizeObserver(() => drawFlux()).observe($("fluxBox"));
    }
    if (!FLUX_POLL_WATCH){
      // Régime réévalué chaque minute (même granularité que CHART._pollWatch
      // pour le prix) : la bascule ouverture/clôture de séance reprend ou
      // coupe le poll sans qu'il soit nécessaire de recharger la page.
      FLUX_POLL_WATCH = setInterval(() => { if (FLUX_ON) armFluxPoll(); }, 60000);
    }
    bindFluxHover();
    loadFlux();
  } else {
    $("chartTitle").textContent = t("chart");
    setChartNote(TARGET + " · " + (TZ_CITY[PREFS.tz] || PREFS.tz));
  }
  armFluxPoll();
}
