/*!
 * TheHub Flux Widget — embeddable gamma/vanna/charm dealer-flow projection.
 * https://gexdash.wealthbuilders.group/
 *
 * Usage:
 *   <script src="https://gexdash.wealthbuilders.group/widget/flux-widget.js"></script>
 *   <div id="chart"></div>
 *   <script>
 *     const w = new TheHubFluxWidget(document.getElementById('chart'), {
 *       key: 'YOUR_KEY',      // request one from TheHub -- omit only for
 *                              // first-party same-origin pages (see below)
 *       target: 'NQ',          // NQ | ES | SPX | GC | XAU
 *       mode: 'gamma',         // gamma | vanna | charm
 *       lang: 'en',            // en | fr
 *       height: 400,
 *     });
 *     // w.setTarget('ES'); w.setMode('vanna'); w.setLang('fr'); w.destroy();
 *   </script>
 *
 * Same-origin/first-party use (e.g. gexdash's own /dash): omit `key` and set
 * `apiBase: ""` -- the widget then reads /api/flow directly (same endpoint
 * the terminal itself uses, no auth) instead of the public, key-gated
 * /api/embed/flow. External embedders keep passing a real key and are
 * unaffected.
 *
 * Ported from the Flux panel in index.html (gexdash terminal). Default scope
 * is the PROJECTION only, for external embedders (small payload, no extra
 * fetches). Pass `advanced: true` (first-party pages only, e.g. gexdash's
 * own /dash) to also pull in CW/PW/Flip levels, strike markers (both with
 * on/off toggle buttons, `strikesOn`/`histOn` opts, `w.setStrikesOn()`/
 * `w.setHistOn()`), the session "so far" history and a compact
 * compression/acceleration/derive/divergence synthesis panel (verdict +
 * gamma/vanna/charm reading only -- no call/put volume table, kept out on
 * purpose). Skips the realized-price overlay line flux-panel.js draws
 * (needs a companion candle chart the dashboard card doesn't have). All the
 * extra data (levels.json, /api/flow?hist=1) is same-origin and free of the
 * public embed endpoint's constraints, so this only makes sense together
 * with an omitted `key`. Every instance owns its state (this.state/this.gfx/
 * this.dom) instead of the module-level FLUX_* globals index.html uses,
 * since a host page may embed more than one widget at once.
 */
(function(){
"use strict";

const API_BASE_DEFAULT = "https://gexdash.wealthbuilders.group";
const POLL_MS_DEFAULT = 120000;   // 2 min, matches the terminal's own cadence
const ANIM_MS = 1000;             // cross-fade duration between two cron ticks

// ───── palette : identique au terminal, RGB en dur (aucune dépendance aux
// variables CSS du site hôte) ─────
const TEAL_BASE = [15, 26, 24], TEAL_HOT = [38, 166, 154];
const RED_BASE = [28, 18, 18], RED_HOT = [239, 83, 80];
const GOLD = [240, 185, 11];
const C = {text: "#ECEAE4", faint: "#5C5C66", teal: "#26A69A", red: "#EF5350", gold: "#F0B90B"};

const LOCALES = {
  en: {now: "now", close: "close", loading: "Loading…", wait: "Waiting for the next update",
       ready: ts => "Updated " + ts, error: m => "Unavailable (" + m + ")", price: "Price",
       modes: {gamma: "Gamma", vanna: "Vanna", charm: "Charm"}, badge: "Powered by TheHub",
       fsVerdict: "Flux synthesis", fsV_compression: "COMPRESSION", fsV_acceleration: "ACCELERATION",
       fsV_derive: "DRIFT", fsV_divergence: "DIVERGENCE",
       fs_gamma: "Gamma", fs_vanna: "Vanna", fs_charm: "Charm",
       fs_gamma_pos: "dampens", fs_gamma_neg: "amplifies", fs_gamma_flat: "flat",
       fs_vanna_pos: "supports if IV ↑", fs_vanna_neg: "destabilizes if IV ↑", fs_vanna_flat: "flat",
       fs_charm_pos: "bullish drift", fs_charm_neg: "bearish drift", fs_charm_flat: "flat",
       fs_unitGamma: "/1%", fs_unitVanna: "/pt IV", fs_unitCharm: "/day",
       fvCalls: "CALLS", fvPuts: "PUTS", fvVolUnit: "vol", fvOiLbl: "OI",
       fluxStrikesChip: "Strikes", fluxStrikesTip: "Today's active strike markers (volume/OI) on the gradient",
       fluxHistChip: "History", fluxHistTip: "Dealer gamma exposure over the elapsed part of the session",
       pts: "pts"},
  fr: {now: "maint.", close: "clôture", loading: "Chargement…", wait: "En attente du prochain calcul",
       ready: ts => "Calculé " + ts, error: m => "Indisponible (" + m + ")", price: "Prix",
       modes: {gamma: "Gamma", vanna: "Vanna", charm: "Charm"}, badge: "Propulsé par TheHub",
       fsVerdict: "Synthèse flux", fsV_compression: "COMPRESSION", fsV_acceleration: "ACCÉLÉRATION",
       fsV_derive: "DÉRIVE", fsV_divergence: "DIVERGENCE",
       fs_gamma: "Gamma", fs_vanna: "Vanna", fs_charm: "Charm",
       fs_gamma_pos: "amortit", fs_gamma_neg: "amplifie", fs_gamma_flat: "neutre",
       fs_vanna_pos: "soutien si IV ↑", fs_vanna_neg: "fragilise si IV ↑", fs_vanna_flat: "neutre",
       fs_charm_pos: "dérive haussière", fs_charm_neg: "dérive baissière", fs_charm_flat: "neutre",
       fs_unitGamma: "/1%", fs_unitVanna: "/pt IV", fs_unitCharm: "/jour",
       fvCalls: "CALLS", fvPuts: "PUTS", fvVolUnit: "vol", fvOiLbl: "OI",
       fluxStrikesChip: "Strikes", fluxStrikesTip: "Repères des strikes actifs (volume/OI du jour) sur le dégradé",
       fluxHistChip: "Historique", fluxHistTip: "Exposition dealer réelle sur la portion de séance déjà écoulée",
       pts: "pts"},
};

let _cssInjected = false;
function injectCss(){
  if (_cssInjected) return;
  _cssInjected = true;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&display=swap";
  document.head.appendChild(link);
  const css = `
.thub-flux{position:relative;background:#0A0A0C;border:1px solid #212127;font-family:"IBM Plex Mono",monospace;box-sizing:border-box}
.thub-flux *{box-sizing:border-box}
.thub-flux .tb{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 12px;border-bottom:1px solid #212127}
.thub-flux .note{font-size:10.5px;color:#5C5C66;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.thub-flux .modes{display:flex;border:1px solid #212127;flex:none}
.thub-flux .modes button{background:none;border:none;color:#5C5C66;font:inherit;font-size:10.5px;
  padding:5px 10px;cursor:pointer;letter-spacing:.02em}
.thub-flux .modes button.on{background:#F0B90B;color:#0A0A0C;font-weight:600}
.thub-flux .modes button:not(.on):hover{color:#ECEAE4}
.thub-flux .tb2{display:flex;gap:6px;padding:6px 12px;border-bottom:1px solid #212127}
.thub-flux .tgl{display:inline-flex;align-items:center;gap:5px;background:none;border:1px solid #212127;
  color:#5C5C66;font:inherit;font-size:10px;padding:4px 9px;cursor:pointer;letter-spacing:.02em}
.thub-flux .tgl .led{width:5px;height:5px;border-radius:50%;background:#33333B;flex:none}
.thub-flux .tgl:hover{color:#ECEAE4}
.thub-flux .tgl.on{color:#F0B90B;border-color:rgba(240,185,11,.5)}
.thub-flux .tgl.on .led{background:#F0B90B;box-shadow:0 0 5px rgba(240,185,11,.5)}
.thub-flux .wrap{position:relative;width:100%}
.thub-flux .wrap canvas{position:absolute;inset:0;width:100%;height:100%;display:block}
.thub-flux .wrap canvas.bg,.thub-flux .wrap canvas.main{pointer-events:none}
.thub-flux .wrap canvas.cur{cursor:default;touch-action:none}
.thub-flux .badge{position:absolute;right:6px;bottom:6px;font-size:9px;letter-spacing:.03em;
  color:rgba(236,234,228,.4);text-decoration:none;padding:2px 6px;background:rgba(10,10,12,.55);
  border:1px solid rgba(255,255,255,.08);pointer-events:auto}
.thub-flux .badge:hover{color:rgba(236,234,228,.75)}
.thub-flux .loader{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  background:#0A0A0C;z-index:5;transition:opacity .25s ease}
.thub-flux .loader[hidden]{display:none}
.thub-flux .loader svg, .thub-flux .loader-mark{position:absolute}
.thub-flux .loader-ring{width:52px;height:52px;animation:thub-flux-spin 1.1s linear infinite}
.thub-flux .loader-mark{width:34px;height:34px;object-fit:contain;border-radius:50%}
@keyframes thub-flux-spin{to{transform:rotate(360deg)}}
.thub-flux .synth{padding:10px 12px;border-top:1px solid #212127;font-size:11px}
.thub-flux .synth[hidden]{display:none}
.thub-flux .verdict{display:flex;align-items:baseline;gap:10px;margin-bottom:9px}
.thub-flux .verdict .vlab{font-size:9px;letter-spacing:.13em;text-transform:uppercase;color:#5C5C66}
.thub-flux .verdict .vval{font-weight:700;font-size:12.5px;letter-spacing:.03em}
.thub-flux .verdict.cmp .vval{color:#26A69A}
.thub-flux .verdict.acc .vval{color:#EF5350}
.thub-flux .verdict.drf .vval{color:#F0B90B}
.thub-flux .verdict.dvg .vval{color:#8A8A94}
.thub-flux .fsrows{display:flex;gap:16px;flex-wrap:wrap}
.thub-flux .fsrow{display:flex;align-items:baseline;gap:6px;font-size:11px}
.thub-flux .fsk{color:#5C5C66;letter-spacing:.06em;text-transform:uppercase;font-size:9.5px}
.thub-flux .fsval{font-weight:600}
.thub-flux .fsrow.pos .fsval{color:#26A69A}
.thub-flux .fsrow.neg .fsval{color:#EF5350}
.thub-flux .fsrow.flat .fsval{color:#8A8A94}
.thub-flux .fssens{color:#8A8A94;font-size:10px}
`;
  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);
}

function fluxPercentile(sortedAsc, p){
  if (!sortedAsc.length) return 0;
  const idx = Math.min(sortedAsc.length - 1, Math.max(0, Math.round(p * (sortedAsc.length - 1))));
  return sortedAsc[idx];
}

function fluxShade(v, ref, ref99){
  const av = Math.abs(v);
  const x = Math.pow(Math.min(1, av / ref), 1.35);
  const base = v >= 0 ? TEAL_BASE : RED_BASE, hot = v >= 0 ? TEAL_HOT : RED_HOT;
  let r = base[0] + (hot[0] - base[0]) * x, g = base[1] + (hot[1] - base[1]) * x, b = base[2] + (hot[2] - base[2]) * x;
  if (ref99 && av >= ref99){
    const g2 = Math.min(1, (av - ref99) / (ref * 0.5 || 1));
    r += (GOLD[0] - r) * g2; g += (GOLD[1] - g) * g2; b += (GOLD[2] - b) * g2;
  }
  return [Math.round(r), Math.round(g), Math.round(b), 0.20 + 0.80 * x];
}

function fluxSameShape(a, b){
  return !!(a && b && a.price_grid && b.price_grid && a.hours && b.hours
    && a.price_grid.length === b.price_grid.length
    && a.hours.length === b.hours.length);
}

// Repère (niveau ou strike) le plus proche du pointeur : survol direct de
// son étiquette en marge, sinon proximité verticale (≤6px) avec sa ligne
// DANS la grille. Le repère de prix courant (tier 0) n'est pas un "niveau"
// au sens ergonomique -- exclu du survol.
function hoverMarker(markers, mx, my, inBounds){
  let hover = null, bestDy = 6;
  for (const m of (markers || [])){
    if (m.tier < 1) continue;
    if (m.shown && mx >= m.boxX && mx <= m.boxX + m.boxW && my >= m.boxY && my <= m.boxY + m.boxH){
      return m;
    }
    if (inBounds){
      const dy = Math.abs(my - m.y);
      if (dy <= bestDy){ bestDy = dy; hover = m; }
    }
  }
  return hover;
}

function fluxLerpMat(from, to, t){
  return to.map((row, hi) => row.map((v, pi) => {
    const v0 = from[hi] ? from[hi][pi] : v;
    return v0 + (v - v0) * t;
  }));
}

// $ bruts, jamais moyennés entre grecques (unités différentes).
function fmtUsd(v){
  if (v == null) return "—";
  const av = Math.abs(v), sign = v < 0 ? "−" : "+";
  if (av >= 1e9) return sign + (av / 1e9).toFixed(2) + " $Bn";
  if (av >= 1e6) return sign + (av / 1e6).toFixed(1) + " $M";
  if (av >= 1e3) return sign + (av / 1e3).toFixed(1) + " $K";
  return sign + av.toFixed(0) + " $";
}

// -- comptage brut (pas de $) : volume/OI en nombre de contrats.
function fmtCount(v){
  if (v == null) return "—";
  if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + "k";
  return String(Math.round(v));
}

// ───── mode avancé (advanced:true) : CW/PW/Flip, strikes, synthèse ─────
// Mêmes calculs et mêmes seuils que flux-panel.js (docs/BRIEF-flux.md) --
// gamma reste l'axe primaire (seule grecque validée contre gex_by_strike),
// vanna/charm ne font que confirmer ou contredire.
const LEVEL_COLOR = {res: "#26A69A", sup: "#EF5350", flip: "#F0B90B"};
const LEVEL_KIND_LABEL = {res: "CW", sup: "PW", flip: "Flip"};

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
const NEUTRAL_FRAC = 0.15;   // sous 15% du p95 propre à sa grecque : lu comme plat

// Synthèse au point (maintenant, prix courant) : verdict unique + une lecture
// par grecque, jamais fondues (unités différentes). Une contradiction nette
// avec le gamma affiche DIVERGENCE plutôt que d'être lissée.
function computeSynthesis(d, spot){
  if (!d || !d.gamma || !d.vanna || !d.charm || !d.price_grid || !d.price_grid.length) return null;
  const pi = fluxNearestIdx(d.price_grid, spot);
  if (pi < 0 || !d.gamma[0] || !d.vanna[0] || !d.charm[0]) return null;
  const g = d.gamma[0][pi], v = d.vanna[0][pi], c = d.charm[0][pi];
  const refG = fluxRef95(d.gamma), refV = fluxRef95(d.vanna), refC = fluxRef95(d.charm);
  const dirOf = (val, ref) => ref <= 0 ? 0 : (Math.abs(val) < NEUTRAL_FRAC * ref ? 0 : Math.sign(val));
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
  return {verdict, gamma: {val: g, dir: gDir}, vanna: {val: v, dir: vDir}, charm: {val: c, dir: cDir}};
}
const VERDICT_CLASS = {compression: "cmp", acceleration: "acc", derive: "drf", divergence: "dvg"};

// levels.json partagé entre instances (mêmes niveaux quel que soit le
// widget qui les demande) -- clé apiBase+target, jamais réécrit.
const LEVELS_CACHE = {};
async function fetchLevels(apiBase, target){
  const key = apiBase + "|" + target;
  if (LEVELS_CACHE[key] !== undefined) return LEVELS_CACHE[key];
  try{
    const d = await (await fetch(`${apiBase}/levels.json?target=${encodeURIComponent(target)}`)).json();
    LEVELS_CACHE[key] = d && !d.error ? d : null;
  }catch(_){ LEVELS_CACHE[key] = null; }
  return LEVELS_CACHE[key];
}

// Vrai uniquement pendant la fenêtre où le cron serveur recalcule le flux
// (8h30-16h ET, lun-ven) -- inutile de poller en dehors, la donnée ne bouge
// pas tant que le prochain tir n'a pas eu lieu. Même bornes que le terminal
// (FLUX_SESSION_START_ET/END_ET, index.html) et le cron lui-même
// (vercel.json) -- ne pas les faire dériver l'une de l'autre.
const SESSION_START_ET = [8, 30], SESSION_END_ET = [16, 0];
function sessionOpenNow(){
  const p = new Intl.DateTimeFormat("en-US", {timeZone: "America/New_York", hour12: false,
      weekday: "short", hour: "2-digit", minute: "2-digit"})
    .formatToParts(new Date()).reduce((a, x) => (a[x.type] = x.value, a), {});
  if (p.weekday === "Sat" || p.weekday === "Sun") return false;
  const mins = (+p.hour % 24) * 60 + (+p.minute);
  const [sh, sm] = SESSION_START_ET, [eh, em] = SESSION_END_ET;
  return mins >= sh * 60 + sm && mins <= eh * 60 + em;
}

// Epoch UTC réel d'une heure ET donnée, pour le jour courant -- même
// approche que flux-panel.js (re-mesure l'offset ET à chaque appel via
// Intl.DateTimeFormat plutôt que de supposer -4h/-5h en dur, correct des
// deux côtés d'un changement d'heure d'été).
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

const LVL_FONT = "11px IBM Plex Mono, monospace";
const AXIS_FONT = "10px IBM Plex Mono, monospace";
const CUR_FONT = "11px IBM Plex Mono, monospace";
const STRIKE_FONT = "9.5px IBM Plex Mono, monospace";

class TheHubFluxWidget{
  constructor(el, opts){
    opts = opts || {};
    this.el = typeof el === "string" ? document.getElementById(el) : el;
    if (!this.el) throw new Error("TheHubFluxWidget: target element not found");

    this.opts = {
      key: opts.key || null,
      apiBase: opts.apiBase != null ? opts.apiBase : API_BASE_DEFAULT,
      target: opts.target || "NQ",
      mode: opts.mode || "gamma",
      lang: opts.lang === "fr" ? "fr" : "en",
      height: opts.height || 400,
      pollMs: opts.pollMs || POLL_MS_DEFAULT,
      showBadge: opts.showBadge !== false,
      advanced: !!opts.advanced,
    };

    this.state = {
      data: null, anim: null, view: null, layout: null, cursor: null,
      dirty: {bg: false, main: false, cursor: false}, raf: null,
      size: {w: 0, h: 0, dpr: 0}, padRCur: null, sizeTries: 0,
      status: {kind: "loading"}, poll: null, pollArmed: false, watchWired: false, watch: null,
      destroyed: false, fatalError: false, everLoaded: false, resizeObs: null, boundMouseUp: null,
      levels: null, histData: null,
      strikesOn: opts.strikesOn !== false, histOn: opts.histOn !== false,
      dragging: false,
    };
    this.gfx = {off: null, stages: [], gradSrc: null, gradMeta: null, gradKey: null};

    injectCss();
    this._buildDom();
    this._bindInteraction();
    this._bindModeButtons();
    if (this.opts.advanced) this._bindToggleButtons();
    this._renderNote();

    this.load();
    this.armPoll();
  }

  // ───── locale/format ─────
  _loc(){ return LOCALES[this.opts.lang]; }
  _fmt(v, d){
    d = d == null ? 1 : d;
    if (v == null) return "—";
    return Number(v).toLocaleString(this.opts.lang === "fr" ? "fr-FR" : "en-US",
      {minimumFractionDigits: d, maximumFractionDigits: d});
  }

  // ───── DOM ─────
  _buildDom(){
    const root = document.createElement("div");
    root.className = "thub-flux";
    const L = this._loc();
    root.innerHTML = `
      <div class="tb">
        <div class="note"></div>
        <div class="modes">
          <button type="button" data-m="gamma">${L.modes.gamma}</button>
          <button type="button" data-m="vanna">${L.modes.vanna}</button>
          <button type="button" data-m="charm">${L.modes.charm}</button>
        </div>
      </div>
      ${this.opts.advanced ? `<div class="tb2">
        <button type="button" class="tgl${this.state.strikesOn ? " on" : ""}" data-tgl="strikes"
          title="${L.fluxStrikesTip}"><i class="led"></i>${L.fluxStrikesChip}</button>
        <button type="button" class="tgl${this.state.histOn ? " on" : ""}" data-tgl="hist"
          title="${L.fluxHistTip}"><i class="led"></i>${L.fluxHistChip}</button>
      </div>` : ""}
      <div class="wrap" style="height:${this.opts.height}px">
        <canvas class="bg"></canvas>
        <canvas class="main"></canvas>
        <canvas class="cur"></canvas>
        <div class="loader">
          <svg class="loader-ring" viewBox="0 0 60 60">
            <circle cx="30" cy="30" r="26" fill="none" stroke="rgba(240,185,11,.15)" stroke-width="3"/>
            <circle cx="30" cy="30" r="26" fill="none" stroke="#F0B90B" stroke-width="3"
                    stroke-linecap="round" stroke-dasharray="42 200"/>
          </svg>
          <img class="loader-mark" src="${this.opts.apiBase}/thehub-mark.png" alt="" aria-hidden="true">
        </div>
      </div>
      ${this.opts.advanced ? '<div class="synth" hidden></div>' : ""}
      <a class="badge" href="${API_BASE_DEFAULT}/" target="_blank" rel="noopener"></a>
    `;
    this.el.appendChild(root);
    this.dom = {
      root, note: root.querySelector(".note"), wrap: root.querySelector(".wrap"),
      cvBg: root.querySelector("canvas.bg"), cvMain: root.querySelector("canvas.main"),
      cvCur: root.querySelector("canvas.cur"), badge: root.querySelector(".badge"),
      loader: root.querySelector(".loader"),
      modeBtns: [...root.querySelectorAll(".modes button")],
      synth: root.querySelector(".synth"),
      tglBtns: [...root.querySelectorAll(".tgl")],
    };
    this.dom.badge.textContent = this._loc().badge;
    this.dom.badge.hidden = !this.opts.showBadge;
    this.dom.modeBtns.forEach(b => b.classList.toggle("on", b.dataset.m === this.opts.mode));

    this.state.resizeObs = new ResizeObserver(() => this.redrawAll());
    this.state.resizeObs.observe(this.dom.wrap);
  }

  _bindModeButtons(){
    this.dom.modeBtns.forEach(b => b.addEventListener("click", () => this.setMode(b.dataset.m)));
  }

  _bindToggleButtons(){
    this.dom.tglBtns.forEach(b => b.addEventListener("click", () => {
      if (b.dataset.tgl === "strikes") this.setStrikesOn(!this.state.strikesOn);
      else this.setHistOn(!this.state.histOn);
    }));
  }

  _renderNote(){
    const L = this._loc(), s = this.state.status;
    this.dom.note.textContent = s.kind === "ready" ? L.ready(s.ts)
      : s.kind === "wait" ? L.wait
      : s.kind === "error" ? L.error(s.msg)
      : L.loading;
  }

  // ───── mode avancé : synthèse (verdict + lecture gamma/vanna/charm) ─────
  renderSynth(){
    const box = this.dom.synth;
    if (!box) return;
    const d = this.state.data;
    const spot = d && d.spot;
    const s = d ? computeSynthesis(d, spot) : null;
    if (!s){ box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    const L = this._loc();
    const row = (key, item, unit) => {
      const tone = item.dir > 0 ? "pos" : item.dir < 0 ? "neg" : "flat";
      const sensKey = "fs_" + key + "_" + (item.dir > 0 ? "pos" : item.dir < 0 ? "neg" : "flat");
      return `<div class="fsrow ${tone}">
        <span class="fsk">${L["fs_" + key]}</span>
        <span class="fsval">${fmtUsd(item.val)} ${L[unit]}</span>
        <span class="fssens">${L[sensKey]}</span>
      </div>`;
    };
    box.innerHTML =
      `<div class="verdict ${VERDICT_CLASS[s.verdict]}">
         <div class="vlab">${L.fsVerdict}</div>
         <div class="vval">${L["fsV_" + s.verdict]}</div>
       </div>
       <div class="fsrows">` +
       row("gamma", s.gamma, "fs_unitGamma") +
       row("vanna", s.vanna, "fs_unitVanna") +
       row("charm", s.charm, "fs_unitCharm") +
      `</div>`;
  }

  // ───── public API ─────
  setTarget(target){
    if (target === this.opts.target) return;
    this.opts.target = target;
    this.state.view = null;
    this.load();
  }
  setMode(mode){
    if (mode === this.opts.mode) return;
    this.opts.mode = mode;
    this.dom.modeBtns.forEach(b => b.classList.toggle("on", b.dataset.m === mode));
    this.gfx.gradKey = null;
    this.redrawAll();
  }
  // ───── mode avancé : bascules Strikes/Historique -- les strikes ne
  // demandent aucun fetch (déjà dans volume_context), l'historique en
  // déclenche un (skip tant que la bascule est OFF, cf. load()) ; l'éteindre
  // libère aussi la donnée en cache, comme flux-panel.js. ─────
  setStrikesOn(on){
    if (!this.opts.advanced || on === this.state.strikesOn) return;
    this.state.strikesOn = on;
    const b = this.dom.tglBtns.find(x => x.dataset.tgl === "strikes");
    if (b) b.classList.toggle("on", on);
    this.redrawAll();
  }
  setHistOn(on){
    if (!this.opts.advanced || on === this.state.histOn) return;
    this.state.histOn = on;
    const b = this.dom.tglBtns.find(x => x.dataset.tgl === "hist");
    if (b) b.classList.toggle("on", on);
    if (on) this.load();
    else { this.state.histData = null; this.redrawAll(); }
  }
  setLang(lang){
    this.opts.lang = lang === "fr" ? "fr" : "en";
    const L = this._loc();
    this.dom.badge.textContent = L.badge;
    this.dom.modeBtns.forEach(b => { b.textContent = L.modes[b.dataset.m] || b.dataset.m; });
    this._renderNote();
    if (this.opts.advanced) this.renderSynth();
    this.redrawAll();
  }
  destroy(){
    if (this.state.destroyed) return;
    this.state.destroyed = true;
    clearInterval(this.state.poll);
    clearInterval(this.state.watch);
    if (this.state.raf != null) cancelAnimationFrame(this.state.raf);
    if (this.state.resizeObs) this.state.resizeObs.disconnect();
    if (this.state.boundMouseUp) window.removeEventListener("mouseup", this.state.boundMouseUp);
    this.dom.root.remove();
  }

  // ───── data loading ─────
  async load(){
    if (this.state.destroyed) return;
    const tgt = this.opts.target;
    this.state.status = {kind: "loading"};
    this._renderNote();
    try{
      const url = this.opts.key
        ? `${this.opts.apiBase}/api/embed/flow?target=${encodeURIComponent(tgt)}`
          + `&key=${encodeURIComponent(this.opts.key)}`
        : `${this.opts.apiBase}/api/flow?target=${encodeURIComponent(tgt)}`;
      // Niveaux + historique de séance : uniquement en mode avancé (pages
      // même origine), en parallèle de la matrice -- jamais sur le chemin
      // public /api/embed/flow (cf. commentaire d'en-tête). L'historique
      // n'est fetché que si la bascule Historique est active (cf.
      // setHistOn) -- pas d'appel réseau pour rien quand elle est coupée.
      const extra = this.opts.advanced
        ? Promise.all([
            fetchLevels(this.opts.apiBase, tgt),
            this.state.histOn
              ? fetch(`${this.opts.apiBase}/api/flow?target=${encodeURIComponent(tgt)}&hist=1`)
                  .then(x => x.json()).catch(() => null)
              : Promise.resolve(null),
          ])
        : Promise.resolve([null, null]);
      const [r, [levels, hist]] = await Promise.all([fetch(url), extra]);
      if (this.state.destroyed || tgt !== this.opts.target) return;
      if (this.opts.advanced){
        this.state.levels = levels;
        if (this.state.histOn){
          this.state.histData = (hist && hist.ready) ? {target: tgt, entries: hist.history || []} : null;
        }
      }
      if (r.status === 401 || r.status === 403){
        this.state.status = {kind: "error", msg: "invalid key"};
        // clé invalide : ne repartira jamais d'elle-même -- arrête aussi le
        // watchdog minute (armPoll seul ne suffit pas, il re-crée l'interval
        // à chaque passage), sinon le widget re-tente en boucle indéfiniment.
        this.state.fatalError = true;
        clearInterval(this.state.poll);
        clearInterval(this.state.watch);
        this._renderNote();
        return;
      }
      const d = await r.json();
      if (!r.ok || !d.ready){
        this.state.data = null; this.state.anim = null;
        this.state.status = {kind: "wait"};
      } else {
        const prev = this.state.data;
        this.state.anim = (prev && prev.target === d.target && prev.generated_utc !== d.generated_utc
                            && fluxSameShape(prev, d))
          ? {from: {gamma: prev.gamma, vanna: prev.vanna, charm: prev.charm}, t0: performance.now()}
          : null;
        this.state.data = d;
        const ts = new Date(d.generated_utc).toLocaleTimeString(
          this.opts.lang === "fr" ? "fr-FR" : "en-GB", {hour: "2-digit", minute: "2-digit"});
        this.state.status = {kind: "ready", ts};
      }
    }catch(e){
      this.state.data = null; this.state.anim = null;
      this.state.status = {kind: "error", msg: e.message};
    }
    this._renderNote();
    if (this.opts.advanced) this.renderSynth();
    this.redrawAll();
    // Le rond de chargement (logo + anneau) ne couvre QUE le tout premier
    // appel -- une fois qu'on a une réponse (prête, en attente ou en erreur,
    // peu importe), le statut textuel existant (#note) suffit ; le re-montrer
    // à chaque switch de marché serait plus gênant qu'utile.
    if (!this.state.everLoaded){
      this.state.everLoaded = true;
      this.dom.loader.hidden = true;
    }
  }

  armPoll(){
    if (this.state.fatalError) return;
    clearInterval(this.state.poll);
    const wasOn = this.state.pollArmed;
    this.state.pollArmed = sessionOpenNow();
    if (this.state.pollArmed){
      // garde document.hidden, comme partout ailleurs dans le dépôt (index/
      // dash/heatmap/news) : un widget embarqué sur un onglet en arrière-plan
      // ne doit pas continuer à interroger /api/embed/flow en boucle.
      if (!wasOn && !document.hidden) this.load();
      this.state.poll = setInterval(() => { if (!document.hidden) this.load(); }, this.opts.pollMs);
    }
    if (!this.state.watchWired){
      this.state.watchWired = true;
      this.state.watch = setInterval(() => this.armPoll(), 60000);
    }
  }

  // ───── render scheduling (dirty flags -> one rAF -> shared layout) ─────
  invalidate(...layers){
    for (const l of layers) this.state.dirty[l] = true;
    if (this.state.raf != null) return;
    this.state.raf = requestAnimationFrame(() => this.flushDirty());
  }
  redrawAll(){ this.invalidate("bg", "main", "cursor"); }

  ensureSize(){
    const dpr = window.devicePixelRatio || 1;
    const w = this.dom.cvMain.clientWidth, h = this.dom.cvMain.clientHeight;
    if (!w || !h) return false;
    const s = this.state.size;
    if (w === s.w && h === s.h && dpr === s.dpr) return true;
    this.state.size = {w, h, dpr};
    [this.dom.cvBg, this.dom.cvMain, this.dom.cvCur].forEach(cv => {
      cv.width = Math.max(1, Math.round(w * dpr));
      cv.height = Math.max(1, Math.round(h * dpr));
    });
    this.gfx.gradKey = null;
    return true;
  }

  // Lisse la réserve de droite (padR) plutôt qu'un saut instantané -- change
  // de mode/marché peut changer la largeur du libellé prix réservé.
  easePadR(lay, w){
    const target = lay.padR;
    if (this.state.padRCur == null || Math.abs(target - this.state.padRCur) > 150){
      this.state.padRCur = target;
      return lay;
    }
    let padR = this.state.padRCur + (target - this.state.padRCur) * 0.3;
    if (Math.abs(target - padR) < 0.5) padR = target;
    this.state.padRCur = padR;
    if (padR === target) return lay;
    this.invalidate("bg", "main");
    return Object.assign({}, lay, {padR, gw: Math.max(1, w - lay.padL - padR)});
  }

  flushDirty(){
    this.state.raf = null;
    if (this.state.destroyed) return;
    const cvMain = this.dom.cvMain;
    if (cvMain.offsetParent === null){
      this.state.dirty.bg = this.state.dirty.main = this.state.dirty.cursor = false;
      this.state.sizeTries = 0;
      return;
    }
    if (!this.ensureSize()){
      if (this.state.sizeTries++ < 20) this.state.raf = requestAnimationFrame(() => this.flushDirty());
      return;
    }
    this.state.sizeTries = 0;

    const d = this.displayData(), mat = d && d[this.opts.mode];
    let lay = (d && mat && mat.length && d.price_grid && d.price_grid.length)
      ? this.layout(d, mat, this.state.size.w, this.state.size.h) : null;
    if (lay) lay = this.easePadR(lay, this.state.size.w);

    if (this.state.dirty.bg){ this.state.dirty.bg = false; this.drawBg(this.dom.cvBg, d, mat, lay); }
    if (this.state.dirty.main){ this.state.dirty.main = false; this.drawMain(this.dom.cvMain, d, mat, lay); }
    if (this.state.dirty.cursor){ this.state.dirty.cursor = false; this.drawCursor(this.dom.cvCur, lay); }
  }

  // Objet à afficher CE FRAME : data telle quelle hors transition, ou une
  // copie interpolée pendant les ~ANIM_MS qui suivent un nouveau tir de cron.
  displayData(){
    const st = this.state;
    if (!st.anim || !st.data) return st.data;
    const t = Math.min(1, (performance.now() - st.anim.t0) / ANIM_MS);
    if (t >= 1){ st.anim = null; return st.data; }
    this.invalidate("bg", "main");
    return Object.assign({}, st.data, {
      generated_utc: st.data.generated_utc + "|anim" + t.toFixed(3),
      gamma: fluxLerpMat(st.anim.from.gamma, st.data.gamma, t),
      vanna: fluxLerpMat(st.anim.from.vanna, st.data.vanna, t),
      charm: fluxLerpMat(st.anim.from.charm, st.data.charm, t),
    });
  }

  // ───── mode avancé : CW/PW/Flip (levels.json, en cache par target) et
  // strikes (déjà dans volume_context) fusionnés en une liste de repères
  // tagués -- même schéma que flux-panel.js. ─────
  levelMarkers(){
    const lv = this.state.levels;
    if (!lv || !lv.levels) return [];
    const kinds = LEVEL_KIND_LABEL;
    return Object.keys(kinds).map(k => lv.levels.find(x => x.kind === k))
      .filter(Boolean)
      .map(x => ({price: x.price_nq, label: kinds[x.kind], full: x.label, color: LEVEL_COLOR[x.kind]}));
  }
  strikeMarkers(){
    const vc = this.state.data && this.state.data.volume_context;
    if (!vc) return [];
    const tag = (arr, isCall) => (arr || []).map(e => Object.assign({isCall}, e));
    return tag(vc.calls, true).concat(tag(vc.puts, false));
  }

  // Portion de séance déjà écoulée, reconstituée à partir de /api/flow?hist=1
  // (chaque tir de cron passé = un "maintenant" réel avec sa propre grille de
  // prix). Pas de ligne de prix réalisé ici (contrairement au terminal) :
  // ça demanderait un flux de bougies séparé que la carte dashboard n'a pas
  // -- le champ gamma/vanna/charm historique porte déjà l'essentiel.
  sessionElapsed(){
    const hd = this.state.histData;
    if (!hd || !hd.entries || !hd.entries.length) return null;
    const sessionStart = etEpochToday(SESSION_START_ET[0], SESSION_START_ET[1]);
    const sessionEnd = etEpochToday(SESSION_END_ET[0], SESSION_END_ET[1]);
    const nowS = Math.min(Math.floor(Date.now() / 1000), sessionEnd);
    const elapsedH = Math.max(0, (nowS - sessionStart) / 3600);
    if (elapsedH <= 0.02) return null;
    const entries = hd.entries
      .map(e => ({...e, th: (Math.floor(new Date(e.t).getTime() / 1000) - sessionStart) / 3600}))
      .filter(e => isFinite(e.th))
      .sort((a, b) => a.th - b.th);
    if (!entries.length) return null;
    return {elapsedH, entries};
  }

  // ───── layout : padding, marge droite (repères), et scission
  // historique/projection en mode avancé ─────
  layout(d, mat, w, h){
    const padL0 = 46, padT = 8, padB = 20;
    const gh = Math.max(1, h - padT - padB);
    const nH = mat.length, nP = d.price_grid.length;
    const fullMin = d.price_grid[0], fullMax = d.price_grid[nP - 1];
    const spot = d.spot;

    const markers = [];
    if (spot != null && spot >= fullMin && spot <= fullMax){
      markers.push({price: spot, tier: 0, text: this._fmt(spot, 0), full: null,
                    color: C.text, textColor: C.text, dash: [4, 3], lw: 1, font: LVL_FONT});
    }
    let hist = null;
    if (this.opts.advanced){
      this.levelMarkers().forEach(m => {
        if (m.price == null || m.price < fullMin || m.price > fullMax) return;
        markers.push({price: m.price, tier: 1, text: m.label, full: m.full, color: m.color,
                      textColor: m.color, dash: [2, 3], lw: 1, font: LVL_FONT});
      });
      if (this.state.strikesOn) this.strikeMarkers().forEach(m => {
        if (m.strike == null || m.strike < fullMin || m.strike > fullMax) return;
        const ratio = m.vol_oi_ratio;
        const k = Math.max(0, Math.min(1, (ratio || 0) / 2));
        const rTxt = ratio != null ? "×" + ratio.toFixed(2) : "—";
        markers.push({
          price: m.strike, tier: 2, ratio: ratio || 0,
          strike: m.strike, isCall: m.isCall, volume: m.volume, oi: m.oi,
          vol_oi_ratio: ratio, distance: m.distance,
          text: `${m.isCall ? "C" : "P"} ${this._fmt(m.strike, 0)} ${rTxt}`,
          color: `rgba(236,234,228,${0.35 + 0.45 * k})`,
          textColor: "rgba(236,234,228,.92)",
          dash: m.isCall ? [] : [3, 2], lw: 1 + 1.4 * k, font: STRIKE_FONT,
        });
      });
      if (this.state.histOn) hist = this.sessionElapsed();
    }

    const mctx = this.dom.cvMain.getContext("2d");
    let maxTextW = 0;
    for (const m of markers){ mctx.font = m.font; maxTextW = Math.max(maxTextW, mctx.measureText(m.text).width); }
    const padR = markers.length
      ? Math.min(Math.max(maxTextW + 16, 50), Math.max(60, w * 0.42))
      : 40;

    const remainH = d.hours[d.hours.length - 1] || 0;
    const elapsedH = hist ? hist.elapsedH : 0;
    const totalH = elapsedH + remainH;
    const availW = Math.max(1, w - padL0 - padR);
    const gap = 10;
    const minGw = 90;   // sous ce seuil, "maint."/"clôture" finissent l'un sur l'autre
    let histW = (elapsedH > 0 && totalH > 0)
      ? Math.max(0, Math.round(availW * elapsedH / totalH) - gap)
      : 0;
    histW = Math.min(histW, Math.max(0, availW - minGw - gap));
    if (histW <= 4) histW = 0;
    const histX0 = padL0;
    const padL = padL0 + (histW > 0 ? histW + gap : 0);
    const gw = Math.max(1, w - padL - padR);
    // gradW : largeur RÉELLEMENT proportionnelle aux heures restantes -- gw
    // peut être élargi de force par minGw en fin de séance, mais le dégradé
    // ne doit pas s'étirer sur cet espace en trop (cf. commentaire équivalent
    // dans flux-panel.js).
    const gradW = totalH > 0 ? Math.min(gw, Math.max(1, Math.round(availW * remainH / totalH))) : gw;

    return {padL, padT, padB, padR, gw, gradW, gh, nH, nP, fullMin, fullMax,
            viewMin: fullMin, viewMax: fullMax, spot, markers,
            histX0, histW, elapsedH, histEntries: hist ? hist.entries : null};
  }

  currentView(lay){
    const {viewMin, viewMax} = lay;
    const v = this.state.view;
    if (!v){
      this.state.view = {min: viewMin, max: viewMax};
      return this.state.view;
    }
    // Pendant un glisser-déposer actif, panTo() clampe déjà lui-même à
    // chaque frame -- retoucher la vue ICI (parce qu'un poll est arrivé
    // entre-temps) écrase drag.view0 sous le geste en cours et produit
    // exactement le bug remonté : zoom puis déplacement qui se bloque,
    // "revient" tout seul, ou déforme l'affichage.
    if (this.state.dragging) return v;
    if (v.max <= viewMin || v.min >= viewMax){
      // aucun recouvrement avec la grille courante (recentrée sur le spot à
      // chaque tir serveur, cf. price_grid côté _gex_core.py) -- rien à
      // préserver, on repart du plein cadre.
      this.state.view = {min: viewMin, max: viewMax};
      return this.state.view;
    }
    // Recouvrement partiel : la grille a glissé sous une vue déjà zoomée
    // sans en sortir complètement -- on la RETRANSLATE à l'intérieur du
    // nouveau cadre (même span, donc même niveau de zoom) plutôt que
    // d'écraser le zoom de l'utilisateur à chaque léger recentrage.
    let span = Math.min(v.max - v.min, viewMax - viewMin);
    let min = v.min, max = v.min + span;
    if (min < viewMin){ min = viewMin; max = min + span; }
    if (max > viewMax){ max = viewMax; min = max - span; }
    this.state.view = {min, max};
    return this.state.view;
  }

  // ───── gradient bitmap : grille native construite hors-écran, puis montée
  // en résolution progressive (x3/étage) -- jamais de flou artificiel. ─────
  buildGradient(mat, nH, nP, gw, gh){
    const perColumn = this.opts.mode === "charm";
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

    if (!this.gfx.off) this.gfx.off = document.createElement("canvas");
    this.gfx.off.width = nH; this.gfx.off.height = nP;
    const octx = this.gfx.off.getContext("2d");
    const img = octx.createImageData(nH, nP);
    for (let pi = 0; pi < nP; pi++){
      const rowY = nP - 1 - pi;
      for (let hi = 0; hi < nH; hi++){
        const [r, g, b, a] = fluxShade(mat[hi][pi], refArr[hi], ref99Arr[hi]);
        const idx = (rowY * nH + hi) * 4;
        img.data[idx] = r; img.data[idx + 1] = g; img.data[idx + 2] = b;
        img.data[idx + 3] = Math.round(255 * a);
      }
    }
    octx.putImageData(img, 0, 0);

    let src = this.gfx.off, sw = nH, sh = nP, stageIdx = 0;
    while (sw < gw / 3 && sh < gh / 3 && sw < 480){
      const nw = Math.min(gw, sw * 3), nh = Math.min(gh, sh * 3);
      if (!this.gfx.stages[stageIdx]) this.gfx.stages[stageIdx] = document.createElement("canvas");
      const stage = this.gfx.stages[stageIdx];
      stage.width = nw; stage.height = nh;
      const sctx = stage.getContext("2d");
      sctx.imageSmoothingEnabled = true;
      if ("imageSmoothingQuality" in sctx) sctx.imageSmoothingQuality = "high";
      sctx.drawImage(src, 0, 0, nw, nh);
      src = stage; sw = nw; sh = nh; stageIdx++;
    }
    this.gfx.gradSrc = src;
    this.gfx.gradMeta = {sw, sh};
  }

  gradient(d, mat, lay){
    const key = (d.generated_utc || "") + "|" + this.opts.mode + "|" + lay.gradW + "|" + lay.gh;
    if (this.gfx.gradKey !== key){
      this.buildGradient(mat, lay.nH, lay.nP, lay.gradW, lay.gh);
      this.gfx.gradKey = key;
    }
  }

  // ───── mode avancé : dégradé de la portion déjà écoulée de la séance,
  // reconstitué à partir de /api/flow?hist=1 (docs/BRIEF-flux.md). Version
  // simplifiée du pipeline de flux-panel.js : un seul passage de montée en
  // résolution (imageSmoothingQuality "high") au lieu de plusieurs étages
  // x3 -- légèrement moins net en zoom extrême, code très inférieur en
  // complexité pour un panneau qui reste petit (carte du dashboard). Jamais
  // mis en cache par clé (contrairement à buildGradient) : nRows×nCols reste
  // ≤ 25×180, recalculer à chaque frame est négligeable. ─────
  drawHistBg(ctx, lay, priceMin, priceMax){
    const entries = lay.histEntries;
    if (!entries || !entries.length || lay.histW <= 4) return;
    const field = {gamma: "gamma0", vanna: "vanna0", charm: "charm0"}[this.opts.mode];
    const {histX0, histW, elapsedH, padT, gh} = lay;

    const absVals = [];
    for (const e of entries) for (const v of (e[field] || [])) absVals.push(Math.abs(v));
    if (!absVals.length) return;
    absVals.sort((a, b) => a - b);
    const ref = fluxPercentile(absVals, 0.95) || 1;
    const ref99 = fluxPercentile(absVals, 0.99) || ref;

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
      thByEntry.push(e.th);
    }
    if (!rowsByEntry.length) return;

    const nCols = Math.max(2, Math.min(180, Math.round(histW)));
    if (!this.gfx.histOff) this.gfx.histOff = document.createElement("canvas");
    const off = this.gfx.histOff;
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
        const rowY = nRows - 1 - r;
        const idx = (rowY * nCols + c) * 4;
        img.data[idx] = red; img.data[idx + 1] = g; img.data[idx + 2] = b;
        img.data[idx + 3] = Math.round(255 * a);
      }
    }
    octx.putImageData(img, 0, 0);

    ctx.save();
    ctx.beginPath(); ctx.rect(histX0, padT, histW, gh); ctx.clip();
    ctx.imageSmoothingEnabled = true;
    if ("imageSmoothingQuality" in ctx) ctx.imageSmoothingQuality = "high";
    ctx.drawImage(off, 0, 0, nCols, nRows, histX0, padT, histW, gh);
    ctx.restore();
  }

  drawBg(cv, d, mat, lay){
    const ctx = cv.getContext("2d");
    const {w, h, dpr} = this.state.size;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (!lay) return;
    this.gradient(d, mat, lay);
    const view = this.currentView(lay);
    if (this.opts.advanced) this.drawHistBg(ctx, lay, view.min, view.max);
    const {sw, sh} = this.gfx.gradMeta;
    const cropTop = (lay.fullMax - view.max) / (lay.fullMax - lay.fullMin || 1) * sh;
    const cropH = (view.max - view.min) / (lay.fullMax - lay.fullMin || 1) * sh;
    ctx.save();
    ctx.beginPath(); ctx.rect(lay.padL, lay.padT, lay.gw, lay.gh); ctx.clip();
    ctx.imageSmoothingEnabled = true;
    if ("imageSmoothingQuality" in ctx) ctx.imageSmoothingQuality = "high";
    ctx.drawImage(this.gfx.gradSrc, 0, cropTop, sw, Math.max(1, cropH), lay.padL, lay.padT, lay.gradW, lay.gh);
    ctx.restore();
  }

  // ───── axes, repères (prix courant + CW/PW/Flip + strikes en mode
  // avancé), scission historique/projection ─────
  drawMain(cv, d, mat, lay){
    const ctx = cv.getContext("2d");
    const {w, h, dpr} = this.state.size;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (!lay){
      ctx.fillStyle = C.faint;
      ctx.font = CUR_FONT;
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(this.dom.note.textContent || "", w / 2, h / 2);
      this.state.layout = null;
      return;
    }
    const L = this._loc();
    const view = this.currentView(lay);
    const {padL, padT, gw, gh, nH, nP, histX0, histW, elapsedH} = lay;
    const priceMin = view.min, priceMax = view.max;
    const yFor = p => padT + gh * (1 - (p - priceMin) / (priceMax - priceMin || 1));
    const xFor = hi => padL + gw * (hi / Math.max(1, nH - 1));
    const lineX0 = histW > 0 ? histX0 : padL;   // grille/repères traversent aussi l'historique

    ctx.strokeStyle = "rgba(255,255,255,.08)"; ctx.lineWidth = 1;
    ctx.font = AXIS_FONT;
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
      const label = hVal === 0 ? L.now
                  : (isLast && hVal % 1 !== 0) ? L.close
                  : "+" + Math.round(hVal) + "h";
      ctx.fillText(label, x, padT + gh + 4);
    });

    // ---- graduations horaires côté historique, en heures relatives à
    // "maintenant" (−1h, −2h...) -- volontairement pas d'heure ronde en
    // horloge murale (contrairement au terminal) : le widget n'a pas de
    // fuseau d'affichage réglable, une heure ronde ET/locale exigerait une
    // conversion qui n'a pas sa place ici. ----
    if (histW > 60 && elapsedH > 0.5){
      for (let hBack = 1; hBack <= elapsedH - 0.3; hBack++){
        const x = histX0 + histW * ((elapsedH - hBack) / elapsedH);
        if (x < histX0 + 16) continue;
        ctx.fillText("−" + hBack + "h", x, padT + gh + 4);
      }
    }
    // ---- séparation "maintenant" entre historique et projection ----
    if (histW > 4){
      const nowX = Math.round(padL) + 0.5;
      ctx.setLineDash([2, 3]); ctx.strokeStyle = "rgba(255,255,255,.15)"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(nowX, padT); ctx.lineTo(nowX, padT + gh); ctx.stroke();
      ctx.setLineDash([]);
    }

    // ---- repères : prix courant (tier 0) + CW/PW/Flip (tier 1) + strikes
    // (tier 2, mode avancé) -- ligne toujours tracée, étiquette en pastille
    // posée seulement si la place le permet ; tier 2 trié par ratio vol/OI
    // décroissant, le reste évincé en simple point (cf. flux-panel.js). ----
    const markers = (lay.markers || []).filter(m => m.price >= priceMin && m.price <= priceMax);
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
        continue;
      }
      const my2 = Math.round(m.y) + 0.5;
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
      ctx.fillRect(x0, m.y - bh / 2, 2, bh);
      ctx.fillStyle = m.textColor;
      ctx.textAlign = "left"; ctx.textBaseline = "middle";
      ctx.fillText(m.text, x0 + 7, m.y + 0.5);
      m.shown = true; m.boxX = x0; m.boxY = m.y - bh / 2; m.boxW = bw; m.boxH = bh;
    }

    this.state.layout = {w, h, dpr, padL, padT, gw, gh, priceMin, priceMax, nH, nP,
                          hours: d.hours, rows: mat, spot: lay.spot, markers: order,
                          viewMin: lay.viewMin, viewMax: lay.viewMax};
  }

  hoverTimeLabel(hVal, hours){
    const L = this._loc();
    if (Math.abs(hVal) < 0.05) return L.now;
    if (Math.abs(hVal - hours[hours.length - 1]) < 0.05) return L.close;
    return (hVal > 0 ? "+" : "") + hVal.toFixed(1) + "h";
  }

  drawTooltip(ctx, x, y, lines, w, h, borderColor){
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

  // ───── cvCursor : réticule + infobulle, couche transparente, ne touche
  // jamais bg/main -- jamais de recalcul du dégradé pour un survol. ─────
  drawCursor(cv){
    const ctx = cv.getContext("2d");
    const {w, h, dpr} = this.state.size;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const L = this.state.layout, pos = this.state.cursor;
    if (!L || !pos) return;
    const {mx, my} = pos;
    const {padL, padT, gw, gh, priceMin, priceMax, nH, nP, hours, rows, markers, spot} = L;
    const inBounds = mx >= padL && mx <= padL + gw && my >= padT && my <= padT + gh;
    ctx.font = CUR_FONT;

    // ---- survol niveaux/strikes (mode avancé) : vit aussi hors grille
    // (étiquettes en marge), évalué avant tout retour anticipé sur inBounds.
    // advHover mémorisé pour éviter d'empiler l'infobulle de cellule
    // (val != null plus bas) par-dessus celle d'un repère survolé. ----
    let advHover = null;
    if (this.opts.advanced){
      const hover = hoverMarker(markers, mx, my, inBounds);
      advHover = hover;
      if (hover){
        const Lo = this._loc();
        for (const m of (markers || [])){
          if (m === hover || !m.shown) continue;
          ctx.fillStyle = "rgba(9,9,11,.55)";
          ctx.fillRect(m.boxX, m.boxY, m.boxW, m.boxH);
        }
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
        const distTxt = distPts == null ? Lo.pts + " —"
          : (distPts >= 0 ? "+" : "") + Math.round(distPts) + " " + Lo.pts
            + (distPct != null ? "  (" + (distPct >= 0 ? "+" : "") + distPct.toFixed(2) + " %)" : "");
        const lines = hover.tier === 2
          ? [
              `${hover.isCall ? Lo.fvCalls : Lo.fvPuts} ${this._fmt(hover.strike, 0)}`,
              `${fmtCount(hover.volume)} ${Lo.fvVolUnit} · ${Lo.fvOiLbl} ${fmtCount(hover.oi)}` +
                ` · ×${hover.vol_oi_ratio != null ? hover.vol_oi_ratio.toFixed(2) : "—"}`,
              distTxt,
            ]
          : [hover.full || hover.text, `${Lo.price}: ${this._fmt(hover.price, 0)}`, distTxt];
        this.drawTooltip(ctx, mx, hover.y, lines, w, h, hoverColor);
      }
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

    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = "rgba(230,228,222,.35)";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, my); ctx.lineTo(padL + gw, my); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(mx, padT); ctx.lineTo(mx, padT + gh); ctx.stroke();
    ctx.setLineDash([]);

    const priceTxt = this._fmt(price, 0);
    const ptw = ctx.measureText(priceTxt).width;
    const pbx = w - ptw - 13, pbh = 17;
    ctx.fillStyle = C.text;
    ctx.fillRect(pbx, my - pbh / 2, ptw + 10, pbh);
    ctx.fillStyle = "#0A0A0C";
    ctx.textAlign = "left"; ctx.textBaseline = "middle";
    ctx.fillText(priceTxt, pbx + 5, my + 0.5);

    const timeTxt = this.hoverTimeLabel(hVal, hours);
    const ttw = ctx.measureText(timeTxt).width;
    const tbx = Math.max(0, Math.min(w - ttw - 10, mx - ttw / 2 - 5)), tby = padT + gh + 2;
    ctx.fillStyle = C.text;
    ctx.fillRect(tbx, tby, ttw + 10, 15);
    ctx.fillStyle = "#0A0A0C";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(timeTxt, tbx + ttw / 2 + 5, tby + 7.5);

    if (val != null && !advHover){
      const L2 = this._loc();
      const lines = [
        `${L2.modes[this.opts.mode] || this.opts.mode}  ${fmtUsd(val)}`,
        `${L2.price}: ${this._fmt(price, 0)}`,
        this.hoverTimeLabel(hVal, hours),
      ];
      this.drawTooltip(ctx, mx, my, lines, w, h, val >= 0 ? C.teal : C.red);
    }
  }

  // ───── pan/zoom ─────
  // Bornes externes du pan/zoom : viewMin/viewMax (recalculées à chaque
  // layout() depuis price_grid brut, cf. layout()) -- PAS priceMin/priceMax,
  // qui sont la fenêtre COURANTE affichée (rétrécit à chaque zoom avant).
  // Les confondre clampait le zoom/pan sur la vue déjà zoomée au lieu de la
  // plage complète : impossible de dézoomer ou de continuer à se déplacer
  // une fois zoomé.
  outerBounds(){
    const L = this.state.layout, d = this.state.data;
    if (L) return {min: L.viewMin, max: L.viewMax};
    return {min: d.price_grid[0], max: d.price_grid[d.price_grid.length - 1]};
  }
  clampView(newMin, newMax){
    const {min: fullMin, max: fullMax} = this.outerBounds();
    const minSpan = (fullMax - fullMin) * 0.04;
    if (newMax - newMin < minSpan){
      const mid = (newMin + newMax) / 2;
      newMin = mid - minSpan / 2; newMax = mid + minSpan / 2;
    }
    if (newMin < fullMin){ newMax = Math.min(fullMax, newMax + (fullMin - newMin)); newMin = fullMin; }
    if (newMax > fullMax){ newMin = Math.max(fullMin, newMin - (newMax - fullMax)); newMax = fullMax; }
    return {min: newMin, max: newMax};
  }
  zoomAt(price, factor){
    if (!this.state.layout || !this.state.data) return;
    const cur = this.state.view || this.outerBounds();
    const newMin = price - (price - cur.min) * factor;
    const newMax = price + (cur.max - price) * factor;
    this.state.view = this.clampView(newMin, newMax);
    this.state.cursor = null;
    this.invalidate("bg", "main", "cursor");
  }

  _bindInteraction(){
    const cv = this.dom.cvCur;
    let drag = null, pinch = null;

    const setCursorPos = (mx, my) => { this.state.cursor = {mx, my}; this.invalidate("cursor"); };
    const clearCursorPos = () => { this.state.cursor = null; this.invalidate("cursor"); };
    const touchXY = touch => {
      const r = cv.getBoundingClientRect();
      return {mx: touch.clientX - r.left, my: touch.clientY - r.top};
    };
    const updateCursorIcon = (mx, my) => {
      if (drag){ cv.style.cursor = "grabbing"; return; }
      const L = this.state.layout;
      const inGrid = L && mx >= L.padL && mx <= L.padL + L.gw && my >= L.padT && my <= L.padT + L.gh;
      cv.style.cursor = inGrid ? "crosshair" : "default";
    };
    const panTo = my => {
      const L = this.state.layout;
      if (!L || !drag || !this.state.data) return;
      const dy = my - drag.startY;
      const priceDelta = (dy / L.gh) * (drag.view0.max - drag.view0.min);
      const {min: fullMin, max: fullMax} = this.outerBounds();
      let newMin = drag.view0.min + priceDelta, newMax = drag.view0.max + priceDelta;
      if (newMin < fullMin){ newMax += fullMin - newMin; newMin = fullMin; }
      if (newMax > fullMax){ newMin -= newMax - fullMax; newMax = fullMax; }
      this.state.view = {min: newMin, max: newMax};
      this.invalidate("bg", "main", "cursor");
    };

    cv.addEventListener("mousemove", e => {
      const r = cv.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      if (drag) panTo(my); else setCursorPos(mx, my);
      updateCursorIcon(mx, my);
    });
    cv.addEventListener("mousedown", e => {
      if (!this.state.layout || !this.state.view) return;
      drag = {startY: e.clientY, view0: {...this.state.view}};
      this.state.dragging = true;
      cv.style.cursor = "grabbing";
    });
    this.state.boundMouseUp = e => {
      if (!drag) return;
      drag = null;
      this.state.dragging = false;
      const r = cv.getBoundingClientRect();
      updateCursorIcon(e.clientX - r.left, e.clientY - r.top);
    };
    window.addEventListener("mouseup", this.state.boundMouseUp);
    cv.addEventListener("mouseleave", () => {
      if (!drag){ clearCursorPos(); cv.style.cursor = "default"; }
    });
    cv.addEventListener("wheel", e => {
      if (!this.state.layout) return;
      e.preventDefault();
      const r = cv.getBoundingClientRect();
      const my = e.clientY - r.top;
      const L = this.state.layout;
      const price = L.priceMax - (my - L.padT) / L.gh * (L.priceMax - L.priceMin || 1);
      this.zoomAt(price, e.deltaY > 0 ? 1.15 : 1 / 1.15);
    }, {passive: false});
    cv.addEventListener("dblclick", () => {
      this.state.view = null; this.state.cursor = null;
      this.invalidate("bg", "main", "cursor");
    });

    const touchDist = (t0, t1) => Math.hypot(t1.clientX - t0.clientX, t1.clientY - t0.clientY);
    cv.addEventListener("touchstart", e => {
      if (!this.state.layout || !this.state.view) return;
      if (e.touches.length === 1){
        pinch = null;
        drag = {startY: e.touches[0].clientY, view0: {...this.state.view}};
        this.state.dragging = true;
        const {mx, my} = touchXY(e.touches[0]);
        setCursorPos(mx, my);
      } else if (e.touches.length === 2){
        drag = null;
        clearCursorPos();
        const r = cv.getBoundingClientRect();
        const midY = (e.touches[0].clientY + e.touches[1].clientY) / 2 - r.top;
        const L = this.state.layout;
        pinch = {
          dist0: touchDist(e.touches[0], e.touches[1]),
          view0: {...this.state.view},
          midPrice: L.priceMax - (midY - L.padT) / L.gh * (L.priceMax - L.priceMin || 1),
        };
        this.state.dragging = true;
      }
      e.preventDefault();
    }, {passive: false});
    cv.addEventListener("touchmove", e => {
      if (pinch && e.touches.length === 2){
        const d1 = touchDist(e.touches[0], e.touches[1]);
        const factor = pinch.dist0 / (d1 || 1e-6);
        const price = pinch.midPrice, cur = pinch.view0;
        const newMin = price - (price - cur.min) * factor, newMax = price + (cur.max - price) * factor;
        this.state.view = this.clampView(newMin, newMax);
        this.invalidate("bg", "main", "cursor");
      } else if (drag && e.touches.length === 1){
        panTo(e.touches[0].clientY);
        const {mx, my} = touchXY(e.touches[0]);
        setCursorPos(mx, my);
      }
      e.preventDefault();
    }, {passive: false});
    const touchEnd = () => { drag = null; pinch = null; this.state.dragging = false; clearCursorPos(); };
    cv.addEventListener("touchend", touchEnd);
    cv.addEventListener("touchcancel", touchEnd);
  }
}

window.TheHubFluxWidget = TheHubFluxWidget;
})();
