/*!
 * TheHub Flux Widget — embeddable gamma/vanna/charm dealer-flow projection.
 * https://gexdash.wealthbuilders.group/
 *
 * Usage:
 *   <script src="https://gexdash.wealthbuilders.group/widget/flux-widget.js"></script>
 *   <div id="chart"></div>
 *   <script>
 *     const w = new TheHubFluxWidget(document.getElementById('chart'), {
 *       key: 'YOUR_KEY',      // required, request one from TheHub
 *       target: 'NQ',          // NQ | ES | SPX | GC | XAU
 *       mode: 'gamma',         // gamma | vanna | charm
 *       lang: 'en',            // en | fr
 *       height: 400,
 *     });
 *     // w.setTarget('ES'); w.setMode('vanna'); w.setLang('fr'); w.destroy();
 *   </script>
 *
 * Ported from the Flux panel in index.html (gexdash terminal) -- v1 scope is
 * the PROJECTION only (no historique: that needs a second live data feed,
 * candle bars, that the public embed endpoint does not expose). No CW/PW/
 * Flip levels, no strike markers, no synthesis panel -- those pull in data
 * sources and layout complexity out of scope for a first embeddable cut.
 * Every instance owns its state (this.state/this.gfx/this.dom) instead of
 * the module-level FLUX_* globals index.html uses, since a host page may
 * embed more than one widget at once.
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
       modes: {gamma: "Gamma", vanna: "Vanna", charm: "Charm"}, badge: "Powered by TheHub"},
  fr: {now: "maint.", close: "clôture", loading: "Chargement…", wait: "En attente du prochain calcul",
       ready: ts => "Calculé " + ts, error: m => "Indisponible (" + m + ")", price: "Prix",
       modes: {gamma: "Gamma", vanna: "Vanna", charm: "Charm"}, badge: "Propulsé par TheHub"},
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
.thub-flux .loader svg{position:absolute}
.thub-flux .loader-ring{width:52px;height:52px;animation:thub-flux-spin 1.1s linear infinite}
.thub-flux .loader-mark{width:24px;height:24px}
@keyframes thub-flux-spin{to{transform:rotate(360deg)}}
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

const LVL_FONT = "11px IBM Plex Mono, monospace";
const AXIS_FONT = "10px IBM Plex Mono, monospace";
const CUR_FONT = "11px IBM Plex Mono, monospace";

class TheHubFluxWidget{
  constructor(el, opts){
    opts = opts || {};
    if (!opts.key) throw new Error("TheHubFluxWidget: 'key' option is required");
    this.el = typeof el === "string" ? document.getElementById(el) : el;
    if (!this.el) throw new Error("TheHubFluxWidget: target element not found");

    this.opts = {
      key: opts.key,
      apiBase: opts.apiBase || API_BASE_DEFAULT,
      target: opts.target || "NQ",
      mode: opts.mode || "gamma",
      lang: opts.lang === "fr" ? "fr" : "en",
      height: opts.height || 400,
      pollMs: opts.pollMs || POLL_MS_DEFAULT,
    };

    this.state = {
      data: null, anim: null, view: null, layout: null, cursor: null,
      dirty: {bg: false, main: false, cursor: false}, raf: null,
      size: {w: 0, h: 0, dpr: 0}, padRCur: null, sizeTries: 0,
      status: {kind: "loading"}, poll: null, pollArmed: false, watchWired: false, watch: null,
      destroyed: false, fatalError: false, everLoaded: false, resizeObs: null, boundMouseUp: null,
    };
    this.gfx = {off: null, stages: [], gradSrc: null, gradMeta: null, gradKey: null};

    injectCss();
    this._buildDom();
    this._bindInteraction();
    this._bindModeButtons();
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
    root.innerHTML = `
      <div class="tb">
        <div class="note"></div>
        <div class="modes">
          <button type="button" data-m="gamma">Γ</button>
          <button type="button" data-m="vanna">V</button>
          <button type="button" data-m="charm">Θ</button>
        </div>
      </div>
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
          <svg class="loader-mark" viewBox="0 0 100 100" aria-hidden="true">
            <g stroke="#F0B90B" stroke-width="6" fill="none">
              <line x1="50" y1="50" x2="50" y2="16"/><line x1="50" y1="50" x2="79" y2="33"/>
              <line x1="50" y1="50" x2="79" y2="67"/><line x1="50" y1="50" x2="50" y2="84"/>
              <line x1="50" y1="50" x2="21" y2="67"/><line x1="50" y1="50" x2="21" y2="33"/>
            </g>
            <circle cx="50" cy="50" r="13" fill="#F0B90B"/>
          </svg>
        </div>
      </div>
      <a class="badge" href="${API_BASE_DEFAULT}/" target="_blank" rel="noopener"></a>
    `;
    this.el.appendChild(root);
    this.dom = {
      root, note: root.querySelector(".note"), wrap: root.querySelector(".wrap"),
      cvBg: root.querySelector("canvas.bg"), cvMain: root.querySelector("canvas.main"),
      cvCur: root.querySelector("canvas.cur"), badge: root.querySelector(".badge"),
      loader: root.querySelector(".loader"),
      modeBtns: [...root.querySelectorAll(".modes button")],
    };
    this.dom.badge.textContent = this._loc().badge;
    this.dom.modeBtns.forEach(b => b.classList.toggle("on", b.dataset.m === this.opts.mode));

    this.state.resizeObs = new ResizeObserver(() => this.redrawAll());
    this.state.resizeObs.observe(this.dom.wrap);
  }

  _bindModeButtons(){
    this.dom.modeBtns.forEach(b => b.addEventListener("click", () => this.setMode(b.dataset.m)));
  }

  _renderNote(){
    const L = this._loc(), s = this.state.status;
    this.dom.note.textContent = s.kind === "ready" ? L.ready(s.ts)
      : s.kind === "wait" ? L.wait
      : s.kind === "error" ? L.error(s.msg)
      : L.loading;
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
  setLang(lang){
    this.opts.lang = lang === "fr" ? "fr" : "en";
    this.dom.badge.textContent = this._loc().badge;
    this._renderNote();
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
      const url = `${this.opts.apiBase}/api/embed/flow?target=${encodeURIComponent(tgt)}`
        + `&key=${encodeURIComponent(this.opts.key)}`;
      const r = await fetch(url);
      if (this.state.destroyed || tgt !== this.opts.target) return;
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
      if (!wasOn) this.load();
      this.state.poll = setInterval(() => this.load(), this.opts.pollMs);
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

  // ───── layout (v1 : projection seule, pas d'historique/niveaux/strikes) ─────
  layout(d, mat, w, h){
    const padL = 46, padT = 8, padB = 20;
    const gh = Math.max(1, h - padT - padB);
    const nH = mat.length, nP = d.price_grid.length;
    const fullMin = d.price_grid[0], fullMax = d.price_grid[nP - 1];
    const spot = d.spot;
    const spotText = spot != null ? this._fmt(spot, 0) : null;
    const mctx = this.dom.cvMain.getContext("2d");
    mctx.font = LVL_FONT;
    const spotTextW = spotText ? mctx.measureText(spotText).width : 0;
    const padR = spotText ? Math.min(Math.max(spotTextW + 16, 50), Math.max(60, w * 0.42)) : 40;
    const gw = Math.max(1, w - padL - padR);
    return {padL, padT, padB, padR, gw, gh, nH, nP, fullMin, fullMax,
            viewMin: fullMin, viewMax: fullMax, spot, spotText};
  }

  currentView(lay){
    const {viewMin, viewMax} = lay;
    if (!this.state.view || this.state.view.max <= viewMin || this.state.view.min >= viewMax){
      this.state.view = {min: viewMin, max: viewMax};
    }
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
    const key = (d.generated_utc || "") + "|" + this.opts.mode + "|" + lay.gw + "|" + lay.gh;
    if (this.gfx.gradKey !== key){
      this.buildGradient(mat, lay.nH, lay.nP, lay.gw, lay.gh);
      this.gfx.gradKey = key;
    }
  }

  drawBg(cv, d, mat, lay){
    const ctx = cv.getContext("2d");
    const {w, h, dpr} = this.state.size;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (!lay) return;
    this.gradient(d, mat, lay);
    const view = this.currentView(lay);
    const {sw, sh} = this.gfx.gradMeta;
    const cropTop = (lay.fullMax - view.max) / (lay.fullMax - lay.fullMin || 1) * sh;
    const cropH = (view.max - view.min) / (lay.fullMax - lay.fullMin || 1) * sh;
    ctx.save();
    ctx.beginPath(); ctx.rect(lay.padL, lay.padT, lay.gw, lay.gh); ctx.clip();
    ctx.imageSmoothingEnabled = true;
    if ("imageSmoothingQuality" in ctx) ctx.imageSmoothingQuality = "high";
    ctx.drawImage(this.gfx.gradSrc, 0, cropTop, sw, Math.max(1, cropH), lay.padL, lay.padT, lay.gw, lay.gh);
    ctx.restore();
  }

  // ───── axes, spot marker ─────
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
    const {padL, padT, gw, gh, nH, nP} = lay;
    const priceMin = view.min, priceMax = view.max;
    const yFor = p => padT + gh * (1 - (p - priceMin) / (priceMax - priceMin || 1));
    const xFor = hi => padL + gw * (hi / Math.max(1, nH - 1));

    ctx.strokeStyle = "rgba(255,255,255,.08)"; ctx.lineWidth = 1;
    ctx.font = AXIS_FONT;
    ctx.fillStyle = C.faint;
    const sigUnit = d.unit || null;
    for (let i = 0; i < nP; i += 2){
      const mult = sigUnit ? Math.round(((d.price_grid[i] - d.spot) / sigUnit) * 2) / 2 : null;
      const y = yFor(d.price_grid[i]), yy = Math.round(y) + 0.5;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + gw, yy); ctx.stroke();
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

    // ---- repère : prix courant, ligne + pastille bord droit ----
    if (lay.spot != null && lay.spot >= priceMin && lay.spot <= priceMax){
      const sy = Math.round(yFor(lay.spot)) + 0.5;
      ctx.setLineDash([4, 3]); ctx.strokeStyle = C.text; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(padL, sy); ctx.lineTo(padL + gw, sy); ctx.stroke();
      ctx.setLineDash([]);
      ctx.font = LVL_FONT;
      const tw = ctx.measureText(lay.spotText).width;
      const bh = 15, bw = tw + 13, x0 = padL + gw + 5;
      ctx.fillStyle = "rgba(12,12,15,.9)";
      ctx.fillRect(x0, sy - bh / 2, bw, bh);
      ctx.fillStyle = C.text;
      ctx.fillRect(x0, sy - bh / 2, 2, bh);
      ctx.textAlign = "left"; ctx.textBaseline = "middle";
      ctx.fillText(lay.spotText, x0 + 7, sy + 0.5);
    }

    this.state.layout = {w, h, dpr, padL, padT, gw, gh, priceMin, priceMax, nH, nP,
                          hours: d.hours, rows: mat, spot: lay.spot};
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
    const {padL, padT, gw, gh, priceMin, priceMax, nH, nP, hours, rows} = L;
    const inBounds = mx >= padL && mx <= padL + gw && my >= padT && my <= padT + gh;
    ctx.font = CUR_FONT;
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

    if (val != null){
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
  outerBounds(){
    const L = this.state.layout, d = this.state.data;
    if (L) return {min: L.priceMin, max: L.priceMax};
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
      cv.style.cursor = "grabbing";
    });
    this.state.boundMouseUp = e => {
      if (!drag) return;
      drag = null;
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
    const touchEnd = () => { drag = null; pinch = null; clearCursorPos(); };
    cv.addEventListener("touchend", touchEnd);
    cv.addEventListener("touchcancel", touchEnd);
  }
}

window.TheHubFluxWidget = TheHubFluxWidget;
})();
