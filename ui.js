/* GEX Terminal — module UI partagé : préférences, fuseau, filtres de niveaux,
   panneau de réglages latéral (drawer). Chargé par index.html et dash.html. */
"use strict";

const DEF_PREFS = {tz: "Europe/Paris", up: "#26A69A", dn: "#EF5350", px: "#FFD84D"};
let PREFS = {...DEF_PREFS};
try{ PREFS = {...DEF_PREFS, ...JSON.parse(localStorage.getItem("gexPrefs") || "{}")}; }catch(_){}
function savePrefs(){ try{ localStorage.setItem("gexPrefs", JSON.stringify(PREFS)); }catch(_){} }

let LVSHOW = {gex: true, em: true, open: true};
try{ LVSHOW = Object.assign(LVSHOW, JSON.parse(localStorage.getItem("gexLvShow") || "{}")); }catch(_){}
function saveLvShow(){ try{ localStorage.setItem("gexLvShow", JSON.stringify(LVSHOW)); }catch(_){} }

const TZ_CITY = {"Europe/Paris": "Paris", "America/New_York": "New York",
                 "Europe/London": "London", "UTC": "UTC"};

function tzShift(tSec){
  const d = new Date(tSec * 1000);
  const loc = new Date(d.toLocaleString("en-US", {timeZone: PREFS.tz}));
  const utc = new Date(d.toLocaleString("en-US", {timeZone: "UTC"}));
  return tSec + Math.round((loc - utc) / 1000);
}

const CANDLE_PAIRS = [
  ["#26A69A", "#EF5350"],
  ["#4CAF50", "#F44336"],
  ["#3B82F6", "#F59E0B"],
  ["#E8E6E1", "#5C5C66"],
];
const PRICE_COLORS = ["#FFD84D", "#F0B90B", "#FFFFFF", "#22D3EE"];

const DRAWER_TXT = {
  fr: {title: "Réglages chart", tz: "Fuseau horaire", levels: "Niveaux",
       lvGex: "GEX", lvEm: "EM · 1D", lvOpen: "Open (grille σ)",
       candles: "Bougies", palette: "Palette", bodyUp: "Corps ↑", bodyDn: "Corps ↓",
       price: "Ligne de prix", presets: "Prédéfinies", custom: "Personnalisée",
       reset: "Réinitialiser les couleurs", on: "On", off: "Off"},
  en: {title: "Chart settings", tz: "Timezone", levels: "Levels",
       lvGex: "GEX", lvEm: "EM · 1D", lvOpen: "Open (σ grid)",
       candles: "Candles", palette: "Palette", bodyUp: "Body ↑", bodyDn: "Body ↓",
       price: "Price line", presets: "Presets", custom: "Custom",
       reset: "Reset colors", on: "On", off: "Off"},
};
function drawerLang(){
  let l = "fr";
  try{ l = localStorage.getItem("gexLang") || "fr"; }catch(_){}
  return DRAWER_TXT[l] || DRAWER_TXT.fr;
}

/* CSS injecté une fois */
(function(){
  const css = `
  .cgear{
    width:30px; height:30px; display:inline-flex; align-items:center; justify-content:center;
    background:var(--surface2, #16161A); border:1px solid var(--line, #212127);
    color:var(--faint, #5C5C66); font-size:13px; cursor:pointer; flex:none;
    transition:color .12s ease, border-color .12s ease;
  }
  .cgear:hover{color:var(--gold, #F0B90B); border-color:rgba(240,185,11,.45)}
  #gexDrawerVeil{
    position:fixed; inset:0; z-index:60; background:rgba(4,4,6,.55);
    opacity:0; pointer-events:none; transition:opacity .18s ease;
  }
  #gexDrawer{
    position:fixed; top:0; right:0; bottom:0; z-index:61; width:294px; max-width:88vw;
    background:#0F0F12; border-left:1px solid var(--line-strong, #2B2B33);
    box-shadow:-18px 0 60px rgba(0,0,0,.55);
    transform:translateX(100%); transition:transform .2s ease;
    overflow-y:auto; overscroll-behavior:contain;
    font-family:"IBM Plex Mono", monospace; font-size:12px; color:#ECEAE4;
  }
  body.gexDrawerOpen #gexDrawer{transform:none}
  body.gexDrawerOpen #gexDrawerVeil{opacity:1; pointer-events:auto}
  #gexDrawer .dhead{
    display:flex; align-items:center; justify-content:space-between;
    padding:16px 18px 14px; border-bottom:1px solid var(--line, #212127);
    position:sticky; top:0; background:#0F0F12; z-index:2;
  }
  #gexDrawer .dhead b{
    font-family:"Space Grotesk", monospace; font-size:12px; font-weight:700;
    letter-spacing:.12em; text-transform:uppercase;
  }
  #gexDrawer .dhead button{
    background:none; border:none; color:var(--faint, #5C5C66);
    font-size:15px; cursor:pointer; padding:4px 6px;
  }
  #gexDrawer .dhead button:hover{color:#ECEAE4}
  #gexDrawer .dsec{padding:14px 18px 4px}
  #gexDrawer h4{
    font-family:"Space Grotesk", monospace; font-size:10px; font-weight:700;
    letter-spacing:.2em; text-transform:uppercase; color:var(--gold, #F0B90B);
    padding-bottom:8px; margin-bottom:4px; border-bottom:1px solid var(--line, #212127);
  }
  #gexDrawer .drow{
    display:flex; align-items:center; justify-content:space-between;
    gap:12px; padding:9px 0;
  }
  #gexDrawer .drow > span:first-child{color:var(--muted, #8A8A94)}
  #gexDrawer select{
    background:#16161A; border:1px solid var(--line, #212127); color:#ECEAE4;
    font:inherit; font-size:12px; padding:6px 8px; cursor:pointer; min-width:140px;
  }
  .dtog{
    background:#16161A; border:1px solid var(--line, #212127); color:var(--faint, #5C5C66);
    font:inherit; font-size:11px; padding:4px 14px; cursor:pointer; min-width:52px;
    transition:all .12s ease;
  }
  .dtog.on{
    background:rgba(240,185,11,.14); color:var(--gold, #F0B90B);
    border-color:var(--gold, #F0B90B); font-weight:600;
  }
  .dsw{
    width:24px; height:24px; border:1px solid var(--line-strong, #2B2B33);
    cursor:pointer; padding:0; flex:none; border-radius:4px;
  }
  .dsw.pair{background:linear-gradient(135deg, var(--a) 50%, var(--b) 50%)}
  .dsw.on{outline:2px solid var(--gold, #F0B90B); outline-offset:1px}
  #gexDrawer .swrow{display:flex; gap:8px; justify-content:flex-end}
  #gexDrawer input[type=color]{
    width:24px; height:24px; padding:0; border:1px solid var(--line-strong, #2B2B33);
    background:#16161A; cursor:pointer; border-radius:4px;
  }
  #gexDrawer .rst{
    display:block; width:calc(100% - 36px); margin:16px 18px 22px;
    background:#16161A; border:1px solid var(--line, #212127);
    color:var(--muted, #8A8A94); font:inherit; font-size:11.5px; padding:9px; cursor:pointer;
  }
  #gexDrawer .rst:hover{color:var(--gold, #F0B90B); border-color:rgba(240,185,11,.45)}
  #gexDrawer .dhead::after{
    content:""; position:absolute; left:0; right:0; bottom:-1px; height:1px;
    background:linear-gradient(90deg, transparent, rgba(240,185,11,.4), transparent);
  }
  #gexDrawer .dhead{position:sticky}
  #gexDrawer .drow{transition:background .12s ease; margin:0 -8px; padding:9px 8px}
  #gexDrawer .drow:hover{background:rgba(255,255,255,.02)}
  .dsw{transition:transform .1s ease, outline-color .1s ease}
  .dsw:hover{transform:scale(1.12)}
  .dtog:active, .dsw:active{transform:translateY(1px)}`;
  const st = document.createElement("style");
  st.textContent = css;
  document.head.appendChild(st);
})();

/* Réglages globaux : toutes les molettes ouvrent le même panneau ;
   chaque page/chart enregistre ses hooks {applyColors, reloadBars, onLevelsChanged}. */
const GEX_HOOKS = [];
let DRAWER = null;

function callHooks(fn){
  for (const h of GEX_HOOKS){ try{ h[fn] && h[fn](); }catch(_){} }
}

function ensureDrawer(){
  if (DRAWER) return DRAWER;
  const veil = document.createElement("div");
  veil.id = "gexDrawerVeil";
  const dr = document.createElement("div");
  dr.id = "gexDrawer";
  document.body.appendChild(veil);
  document.body.appendChild(dr);
  veil.onclick = closeDrawer;
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeDrawer(); });
  DRAWER = dr;
  return dr;
}
function closeDrawer(){ document.body.classList.remove("gexDrawerOpen"); }

function renderDrawer(){
  const L = drawerLang();
  const dr = ensureDrawer();
  const tzOpts = Object.entries(TZ_CITY).map(([v, n]) =>
    `<option value="${v}"${PREFS.tz === v ? " selected" : ""}>${n}</option>`).join("");
  const tog = (key, label) =>
    `<div class="drow"><span>${label}</span>
       <button class="dtog${LVSHOW[key] ? " on" : ""}" data-lv="${key}">${LVSHOW[key] ? L.on : L.off}</button></div>`;
  const pairs = CANDLE_PAIRS.map(([a, b], i) =>
    `<button class="dsw pair${PREFS.up === a && PREFS.dn === b ? " on" : ""}"
      style="--a:${a};--b:${b}" data-pair="${i}" title="${a} / ${b}"></button>`).join("");
  const prices = PRICE_COLORS.map(c =>
    `<button class="dsw${PREFS.px === c ? " on" : ""}" style="background:${c}" data-px="${c}"></button>`).join("");

  dr.innerHTML = `
    <div class="dhead"><b>${L.title}</b><button data-close>✕</button></div>
    <div class="dsec"><h4>${L.tz}</h4>
      <div class="drow"><span>${L.tz}</span><select data-tz>${tzOpts}</select></div>
    </div>
    <div class="dsec"><h4>${L.levels}</h4>
      ${tog("gex", L.lvGex)}${tog("em", L.lvEm)}${tog("open", L.lvOpen)}
    </div>
    <div class="dsec"><h4>${L.candles}</h4>
      <div class="drow"><span>${L.presets}</span><span class="swrow">${pairs}</span></div>
      <div class="drow"><span>${L.bodyUp}</span><input type="color" data-cup value="${PREFS.up}"></div>
      <div class="drow"><span>${L.bodyDn}</span><input type="color" data-cdn value="${PREFS.dn}"></div>
    </div>
    <div class="dsec"><h4>${L.price}</h4>
      <div class="drow"><span>${L.presets}</span><span class="swrow">${prices}</span></div>
      <div class="drow"><span>${L.custom}</span><input type="color" data-cpx value="${PREFS.px}"></div>
    </div>
    <button class="rst">↺ ${L.reset}</button>`;

  dr.querySelector("[data-close]").onclick = closeDrawer;
  dr.querySelector("[data-tz]").onchange = e => {
    PREFS.tz = e.target.value; savePrefs(); callHooks("reloadBars"); renderDrawer();
  };
  dr.querySelectorAll("[data-lv]").forEach(b => b.onclick = () => {
    LVSHOW[b.dataset.lv] = !LVSHOW[b.dataset.lv];
    saveLvShow(); callHooks("onLevelsChanged"); renderDrawer();
  });
  dr.querySelectorAll("[data-pair]").forEach(b => b.onclick = () => {
    const [a, c] = CANDLE_PAIRS[+b.dataset.pair];
    PREFS.up = a; PREFS.dn = c; savePrefs(); callHooks("applyColors"); renderDrawer();
  });
  dr.querySelectorAll("[data-px]").forEach(b => b.onclick = () => {
    PREFS.px = b.dataset.px; savePrefs(); callHooks("applyColors"); renderDrawer();
  });
  dr.querySelector("[data-cup]").oninput = e => { PREFS.up = e.target.value; savePrefs(); callHooks("applyColors"); };
  dr.querySelector("[data-cdn]").oninput = e => { PREFS.dn = e.target.value; savePrefs(); callHooks("applyColors"); };
  dr.querySelector("[data-cpx]").oninput = e => { PREFS.px = e.target.value; savePrefs(); callHooks("applyColors"); };
  dr.querySelector(".rst").onclick = () => {
    Object.assign(PREFS, {up: DEF_PREFS.up, dn: DEF_PREFS.dn, px: DEF_PREFS.px});
    savePrefs(); callHooks("applyColors"); renderDrawer();
  };
}

function openDrawer(){
  renderDrawer();
  document.body.classList.add("gexDrawerOpen");
}

/* Ajoute une molette dans un conteneur (barre d'outils / en-tête de carte)
   et enregistre les hooks de ce chart. */
function buildGearMenu(holder, hooks){
  if (hooks) GEX_HOOKS.push(hooks);
  const btn = document.createElement("button");
  btn.className = "cgear";
  btn.textContent = "⚙";
  btn.title = drawerLang().title;
  btn.onclick = e => { e.stopPropagation(); openDrawer(); };
  holder.appendChild(btn);
  return btn;
}
