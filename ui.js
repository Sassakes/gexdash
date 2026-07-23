/* GEX Terminal — module UI partagé : préférences, fuseau, filtres de niveaux,
   panneau de réglages latéral (drawer). Chargé par index.html et dash.html. */
"use strict";

const DEF_PREFS = {tz: "Europe/Paris", up: "#26A69A", dn: "#EF5350", px: "#FFD84D",
                   lblSize: 10, lblMode: ""};
let PREFS = {...DEF_PREFS};
try{ PREFS = {...DEF_PREFS, ...JSON.parse(localStorage.getItem("gexPrefs") || "{}")}; }catch(_){}
function savePrefs(){ try{ localStorage.setItem("gexPrefs", JSON.stringify(PREFS)); }catch(_){} }

let LVSHOW = {gex: true, em: true, open: true};
try{ LVSHOW = Object.assign(LVSHOW, JSON.parse(localStorage.getItem("gexLvShow") || "{}")); }catch(_){}
function saveLvShow(){ try{ localStorage.setItem("gexLvShow", JSON.stringify(LVSHOW)); }catch(_){} }

const TZ_CITY = {"Europe/Paris": "Paris", "America/New_York": "New York",
                 "Europe/London": "London", "UTC": "UTC"};

function tzShiftAs(tz, tSec){
  const d = new Date(tSec * 1000);
  const loc = new Date(d.toLocaleString("en-US", {timeZone: tz}));
  const utc = new Date(d.toLocaleString("en-US", {timeZone: "UTC"}));
  return tSec + Math.round((loc - utc) / 1000);
}

/* ══════════ SELECTS STYLÉS ══════════
   Le <select> natif fait rendre sa liste par l'OS (fond blanc, surlignage
   bleu système) : impossible à thémer. On garde donc le select d'origine
   dans le DOM — masqué — et on pilote une liste maison par-dessus. Tout le
   code existant qui lit .value ou écoute "change" continue de fonctionner
   sans modification. */
function injectSelectCss(){
  if (document.getElementById("selCss")) return;
  const s = document.createElement("style");
  s.id = "selCss";
  s.textContent = `
  select.enh{position:absolute!important; opacity:0!important; pointer-events:none!important;
             width:0!important; height:0!important; margin:0!important; padding:0!important}
  .selw{position:relative; display:inline-flex}
  .selb{display:inline-flex; align-items:center; gap:8px; white-space:nowrap;
    background:var(--surface2,#16161A); border:1px solid var(--line,#212127);
    color:var(--text,#ECEAE4); font:inherit; font-size:11.5px; padding:6px 10px;
    min-height:30px; cursor:pointer; transition:border-color .12s ease, color .12s ease}
  .selb:hover{border-color:var(--line-strong,#2E2E36); color:var(--gold,#F0B90B)}
  .selw.open .selb{border-color:var(--gold,#F0B90B); color:var(--gold,#F0B90B)}
  .selb .cv{font-size:8px; opacity:.65; transition:transform .14s ease}
  .selw.open .selb .cv{transform:rotate(180deg)}
  .sell{position:absolute; z-index:90; top:calc(100% + 4px); left:0; min-width:100%;
    background:#0E0E12; border:1px solid var(--line-strong,#2E2E36);
    box-shadow:0 14px 34px -12px rgba(0,0,0,.85); padding:3px; display:none;
    max-height:min(60vh,320px); overflow-y:auto}
  .sell.up{top:auto; bottom:calc(100% + 4px)}
  .selw.open .sell{display:block}
  .sell button{display:block; width:100%; text-align:left; background:none; border:none;
    color:var(--muted,#8A8A94); font:inherit; font-size:11.5px; padding:8px 12px;
    cursor:pointer; white-space:nowrap; border-left:2px solid transparent}
  .sell button:hover{background:rgba(255,255,255,.05); color:var(--text,#ECEAE4)}
  .sell button.on{color:var(--gold,#F0B90B); border-left-color:var(--gold,#F0B90B);
    background:rgba(240,185,11,.08)}
  @media (max-width:640px){
    .selb{min-height:40px; font-size:13px; padding:8px 12px}
    .sell button{padding:11px 14px; font-size:13px}
  }`;
  document.head.appendChild(s);
}

function enhanceSelect(sel){
  if (!sel || sel.classList.contains("enh")) return;
  injectSelectCss();
  sel.classList.add("enh");
  const w = document.createElement("span");
  w.className = "selw";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "selb";
  btn.setAttribute("aria-haspopup", "listbox");
  const lab = document.createElement("span");
  const cv = document.createElement("span");
  cv.className = "cv"; cv.textContent = "▼";
  btn.append(lab, cv);
  const list = document.createElement("div");
  list.className = "sell";
  list.setAttribute("role", "listbox");
  sel.parentNode.insertBefore(w, sel);
  w.append(sel, btn, list);
  if (sel.title) btn.title = sel.title;

  const sync = () => {
    const o = sel.options[sel.selectedIndex];
    lab.textContent = o ? o.textContent : "";
    list.querySelectorAll("button").forEach(b =>
      b.classList.toggle("on", b.dataset.v === sel.value));
  };
  const build = () => {
    list.innerHTML = "";
    for (const o of sel.options){
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.v = o.value;
      b.textContent = o.textContent;
      b.onclick = e => {
        e.stopPropagation();
        sel.value = o.value;
        sel.dispatchEvent(new Event("change", {bubbles: true}));
        close(); sync();
      };
      list.appendChild(b);
    }
    sync();
  };
  const close = () => { w.classList.remove("open"); list.classList.remove("up"); };
  btn.onclick = e => {
    e.stopPropagation();
    document.querySelectorAll(".selw.open").forEach(x => {
      if (x !== w) x.classList.remove("open");
    });
    const opening = !w.classList.contains("open");
    w.classList.toggle("open", opening);
    if (opening){
      // s'ouvre vers le haut s'il n'y a pas la place en dessous
      const r = btn.getBoundingClientRect();
      list.classList.toggle("up", window.innerHeight - r.bottom < 190 && r.top > 190);
    }
  };
  document.addEventListener("click", close);
  sel.addEventListener("change", sync);
  sel._sync = sync;
  build();
  sel._rebuild = build;
}

function enhanceAllSelects(root){
  (root || document).querySelectorAll("select:not(.enh)").forEach(enhanceSelect);
}


/* ══════════ RYTHME DU FLUX SELON LA SESSION ══════════
   Chaque requête compte comme une Edge Request chez Vercel, même servie
   depuis le cache. On calibre donc le rafraîchissement sur l'utilité réelle :
     • séance US (9h30-16h ET)  -> 3 s, le flux doit être beau
     • Asie / London / overnight -> 60 s, le future différé bouge peu et
       les niveaux du jour sont figés depuis 00h11 : rien à surveiller
     • week-end (ven 22h -> dim 23h50 Paris) -> 0 = flux totalement coupé
   Renvoie l'intervalle en ms, ou 0 si le marché est fermé. */
function marketPollMs(){
  const P = (tz) => new Intl.DateTimeFormat("en-US", {timeZone: tz, hour12: false,
      weekday: "short", hour: "2-digit", minute: "2-digit"})
    .formatToParts(new Date()).reduce((a, x) => (a[x.type] = x.value, a), {});
  const pa = P("Europe/Paris");
  const pm = (+pa.hour % 24) * 60 + (+pa.minute);
  if (pa.weekday === "Sat") return 0;
  if (pa.weekday === "Fri" && pm >= 22 * 60) return 0;
  if (pa.weekday === "Sun" && pm < 23 * 60 + 50) return 0;
  const et = P("America/New_York");
  const em = (+et.hour % 24) * 60 + (+et.minute);
  if (et.weekday !== "Sat" && et.weekday !== "Sun" && em >= 570 && em <= 960) return 3000;
  return 30000;
}

function tzShift(tSec){ return tzShiftAs(PREFS.tz, tSec); }

const CANDLE_PAIRS = [
  ["#26A69A", "#EF5350"],
  ["#4CAF50", "#F44336"],
  ["#3B82F6", "#F59E0B"],
  ["#E8E6E1", "#5C5C66"],
];
const PRICE_COLORS = ["#FFD84D", "#F0B90B", "#FFFFFF", "#22D3EE"];

const DRAWER_TXT = {
  fr: {title: "Réglages chart", tz: "Fuseau horaire", levels: "Niveaux",
       readab: "Lisibilité", lblSize: "Taille des étiquettes",
       lblMode: "Prix des niveaux", mdAuto: "Auto", mdAll: "Toujours", mdHov: "Au survol",
       szS: "Normal", szM: "Grand", szL: "Très grand",
       lvGex: "GEX", lvEm: "EM · 1D", lvOpen: "Open (grille σ)",
       candles: "Bougies", palette: "Palette", bodyUp: "Corps ↑", bodyDn: "Corps ↓",
       price: "Ligne de prix", presets: "Prédéfinies", custom: "Personnalisée",
       reset: "Réinitialiser les couleurs", on: "On", off: "Off"},
  en: {title: "Chart settings", tz: "Timezone", levels: "Levels",
       readab: "Readability", lblSize: "Label size",
       lblMode: "Level prices", mdAuto: "Auto", mdAll: "Always", mdHov: "On hover",
       szS: "Normal", szM: "Large", szL: "Extra large",
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
    background:rgba(14,14,18,.82);
    backdrop-filter:blur(16px) saturate(1.2); -webkit-backdrop-filter:blur(16px) saturate(1.2);
    border-left:1px solid var(--line-strong, #2B2B33);
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
    position:sticky; top:0; background:rgba(14,14,18,.9); z-index:2;
    backdrop-filter:blur(8px); -webkit-backdrop-filter:blur(8px);
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
    <div class="dsec"><h4>${L.readab}</h4>
      <div class="drow"><span>${L.lblMode}</span><span class="swrow">
        ${[["auto", L.mdAuto], ["all", L.mdAll], ["hover", L.mdHov]].map(([v, n]) =>
          `<button class="dtog${(PREFS.lblMode || "auto") === v ? " on" : ""}" data-lmd="${v}">${n}</button>`).join("")}
      </span></div>
      <div class="drow"><span>${L.lblSize}</span><span class="swrow">
        ${[[10, L.szS], [12, L.szM], [14, L.szL]].map(([v, n]) =>
          `<button class="dtog${PREFS.lblSize === v ? " on" : ""}" data-lsz="${v}">${n}</button>`).join("")}
      </span></div>
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
  enhanceAllSelects(dr);
  dr.querySelectorAll("[data-lmd]").forEach(b => b.onclick = () => {
    PREFS.lblMode = b.dataset.lmd; savePrefs();
    callHooks("applyLabelSize"); renderDrawer();
  });
  dr.querySelectorAll("[data-lsz]").forEach(b => b.onclick = () => {
    PREFS.lblSize = +b.dataset.lsz; savePrefs();
    callHooks("applyLabelSize"); renderDrawer();
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
    Object.assign(PREFS, {up: DEF_PREFS.up, dn: DEF_PREFS.dn, px: DEF_PREFS.px,
                          lblSize: DEF_PREFS.lblSize, lblMode: ""});
    savePrefs(); callHooks("applyColors"); callHooks("applyLabelSize"); renderDrawer();
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

/* ═══════════ Outils de dessin (toutes les charts, dessins par chart) ═══════════ */
const DRAW_COLORS = ["#F0B90B", "#26A69A", "#EF5350", "#E8E6E1", "#3B82F6", "#A855F7"];
let DRAW_DEF = {color: "#F0B90B", width: 1, style: "solid",
                fill: "#F0B90B", fillOp: 0.12, mid: false};
try{ DRAW_DEF = {...DRAW_DEF, ...JSON.parse(localStorage.getItem("gexDrawStyle") || "{}")}; }catch(_){}
function saveDrawDef(){ try{ localStorage.setItem("gexDrawStyle", JSON.stringify(DRAW_DEF)); }catch(_){} }

(function(){
  const css = `
  .dcanvas{position:absolute; inset:0; pointer-events:none; z-index:5}
  .dtoolbar{position:absolute; top:8px; left:8px; z-index:8; display:flex; flex-direction:column; gap:4px}
  .dtoolbar button{
    width:28px; height:28px; display:flex; align-items:center; justify-content:center;
    background:rgba(17,17,20,.92); border:1px solid var(--line, #212127);
    color:var(--faint, #5C5C66); cursor:pointer; font-size:12px; padding:0;
    transition:color .12s ease, border-color .12s ease;
  }
  .dtoolbar button:hover{color:var(--text, #ECEAE4)}
  .dtoolbar button.on{color:var(--gold, #F0B90B); border-color:rgba(240,185,11,.55)}
  .dpanel{
    position:absolute; top:8px; left:50%; transform:translateX(-50%); z-index:9;
    display:flex; gap:5px; align-items:center;
    background:rgba(14,14,18,.96); border:1px solid var(--line-strong, #2B2B33);
    padding:7px 9px; box-shadow:0 10px 30px rgba(0,0,0,.6);
    flex-direction:column; align-items:stretch;
  }
  .dpanel[hidden]{display:none}
  .dprow{display:flex; gap:5px; align-items:center}
  .dlab{font-size:9px; color:var(--faint, #5C5C66); letter-spacing:.1em; text-transform:uppercase; min-width:44px}
  .dpanel input[type=range]{width:64px; accent-color:#F0B90B}
  .dtoolbar .dtools{display:flex; flex-direction:column; gap:4px}
  .dtoolbar .dtools[hidden]{display:none}
  .dtoolbar .dmore{
    height:16px; font-size:9px; line-height:1; color:var(--faint, #5C5C66);
  }
  .dpanel .dswatch{width:16px; height:16px; border-radius:3px; border:1px solid #2B2B33; cursor:pointer; padding:0; flex:none}
  .dpanel .dswatch.on{outline:2px solid var(--gold, #F0B90B); outline-offset:1px}
  .dpanel input[type=color]{width:20px; height:20px; padding:0; border:1px solid #2B2B33; background:#16161A; cursor:pointer; border-radius:3px}
  .dwb{background:#16161A; border:1px solid #212127; color:#8A8A94; font-family:inherit; font-size:10px; padding:3px 7px; cursor:pointer; flex:none}
  .dwb.on{color:var(--gold, #F0B90B); border-color:rgba(240,185,11,.5)}
  .dwb.ddel{color:#EF5350}
  .dsep{width:1px; height:16px; background:#212127; flex:none}`;
  const st = document.createElement("style");
  st.textContent = css;
  document.head.appendChild(st);
})();

/* Monte les outils de dessin sur un chart Lightweight Charts.
   Ancrage temps/prix : les tracés suivent zoom & scroll, et s'étendent
   au-delà de la dernière bougie (extrapolation via barSpacing).
   storeKey = identité du chart -> ses dessins lui sont propres. */
function attachDrawTools(box, chart, series, storeKey, ivSec){
  const ts = chart.timeScale();
  const st = {key: storeKey, iv: ivSec || 300, times: [], tz: PREFS.tz,
              shapes: [], tool: null, sel: -1, draft: null, _draftMove: null};
  function remapTz(from, to){
    if (!from || from === to) return;
    for (const s of st.shapes){
      for (const P of [s.p1, s.p2]){
        if (!P || P.t == null) continue;
        const raw = P.t - (tzShiftAs(from, P.t) - P.t);
        P.t = tzShiftAs(to, raw);
      }
    }
  }
  function load(){
    let d = null;
    try{ d = JSON.parse(localStorage.getItem(st.key) || "[]"); }catch(_){ d = []; }
    if (Array.isArray(d)){ st.shapes = d; st.tz = PREFS.tz; }        /* héritage */
    else {
      st.shapes = Array.isArray(d.shapes) ? d.shapes : [];
      st.tz = d.tz || PREFS.tz;
      if (st.tz !== PREFS.tz){ remapTz(st.tz, PREFS.tz); st.tz = PREFS.tz; }
    }
  }
  function save(){
    try{ localStorage.setItem(st.key, JSON.stringify({tz: st.tz, shapes: st.shapes})); }catch(_){}
  }
  load();

  const canvas = document.createElement("canvas");
  canvas.className = "dcanvas";
  box.appendChild(canvas);
  const ctx = canvas.getContext("2d");

  /* ---- barre d'outils ---- */
  const bar = document.createElement("div");
  bar.className = "dtoolbar";
  bar.innerHTML = `
    <button data-tool="" class="on" title="Curseur / déplacer">✥</button>
    <button class="dmore" title="Outils de dessin">▾</button>
    <div class="dtools" hidden>
      <button data-tool="trend" title="Ligne de tendance">╱</button>
      <button data-tool="hline" title="Ligne horizontale">─</button>
      <button data-tool="box" title="Rectangle">▭</button>
      <button data-tool="_clear" title="Tout effacer sur ce chart">🗑</button>
    </div>`;
  box.appendChild(bar);
  const more = bar.querySelector(".dmore");
  const tools = bar.querySelector(".dtools");
  function foldTools(fold){
    tools.hidden = fold;
    more.textContent = fold ? "▾" : "▴";
  }
  more.onclick = e => { e.stopPropagation(); foldTools(!tools.hidden); };
  function cancelDraft(){
    if (st._draftMove){ window.removeEventListener("pointermove", st._draftMove); st._draftMove = null; }
    st.draft = null;
  }
  function toolReset(){
    st.tool = null;
    chart.applyOptions({handleScroll: true, handleScale: true});
    bar.querySelectorAll("[data-tool]").forEach(x => x.classList.toggle("on", x.dataset.tool === ""));
    foldTools(true);   // tracé terminé -> le menu se replie, curseur seul visible
  }
  bar.querySelectorAll("[data-tool]").forEach(b => b.onclick = e => {
    e.stopPropagation();
    cancelDraft();
    if (b.dataset.tool === "_clear"){
      if (st.shapes.length && confirm("Effacer tous les dessins de ce chart ?")){
        st.shapes = []; st.sel = -1; save(); panelSync(); redraw();
      }
      foldTools(true);
      return;
    }
    st.tool = b.dataset.tool || null;
    st.sel = -1;
    bar.querySelectorAll("[data-tool]").forEach(x => x.classList.toggle("on", x === b));
    if (!st.tool) foldTools(true);
    chart.applyOptions({handleScroll: !st.tool, handleScale: !st.tool});
    panelSync(); redraw();
  });

  /* ---- panneau de style ---- */
  const panel = document.createElement("div");
  panel.className = "dpanel";
  panel.hidden = true;
  box.appendChild(panel);
  function curStyle(){ return st.sel >= 0 ? st.shapes[st.sel] : DRAW_DEF; }
  function panelSync(){
    const show = st.sel >= 0 || !!st.tool;
    panel.hidden = !show;
    if (!show) return;
    const s = curStyle();
    const isBox = st.sel >= 0 ? st.shapes[st.sel].type === "box" : st.tool === "box";
    const row1 =
      DRAW_COLORS.map(c => `<button class="dswatch${s.color===c ? " on" : ""}" style="background:${c}" data-c="${c}"></button>`).join("") +
      `<input type="color" value="${s.color}" data-cc>` +
      `<span class="dsep"></span>` +
      [1, 2, 3].map(w => `<button class="dwb${s.width===w ? " on" : ""}" data-w="${w}">${w}px</button>`).join("") +
      `<span class="dsep"></span>` +
      [["solid","—"],["dashed","╌"],["dotted","┄"]].map(([v, l]) =>
        `<button class="dwb${s.style===v ? " on" : ""}" data-s="${v}">${l}</button>`).join("") +
      (st.sel >= 0 ? `<span class="dsep"></span><button class="dwb ddel" data-del title="Supprimer">✕</button>` : "");
    const opPct = Math.round((s.fillOp == null ? 0.12 : s.fillOp) * 100);
    const row2 = !isBox ? "" :
      `<span class="dlab">Fond</span>` +
      `<input type="color" value="${s.fill || s.color}" data-fill>` +
      `<input type="range" min="0" max="60" value="${opPct}" data-op title="Opacité du fond">` +
      `<span class="dlab" data-oplbl style="min-width:26px">${opPct}%</span>` +
      `<button class="dwb${s.mid ? " on" : ""}" data-mid title="Ligne médiane">┼ médiane</button>`;
    panel.innerHTML = `<div class="dprow">${row1}</div>` + (row2 ? `<div class="dprow">${row2}</div>` : "");
    const apply = (k, v, keepPanel) => {
      if (st.sel >= 0){ st.shapes[st.sel][k] = v; save(); }
      DRAW_DEF[k] = v; saveDrawDef();
      if (!keepPanel) panelSync();
      redraw();
    };
    panel.querySelectorAll("[data-c]").forEach(x => x.onclick = e => { e.stopPropagation(); apply("color", x.dataset.c); });
    panel.querySelector("[data-cc]").oninput = e => apply("color", e.target.value, true);
    panel.querySelectorAll("[data-w]").forEach(x => x.onclick = e => { e.stopPropagation(); apply("width", +x.dataset.w); });
    panel.querySelectorAll("[data-s]").forEach(x => x.onclick = e => { e.stopPropagation(); apply("style", x.dataset.s); });
    const fill = panel.querySelector("[data-fill]");
    if (fill) fill.oninput = e => apply("fill", e.target.value, true);
    const op = panel.querySelector("[data-op]");
    if (op) op.oninput = e => {
      apply("fillOp", +e.target.value / 100, true);
      const l = panel.querySelector("[data-oplbl]");
      if (l) l.textContent = e.target.value + "%";
    };
    const mid = panel.querySelector("[data-mid]");
    if (mid) mid.onclick = e => { e.stopPropagation(); apply("mid", !curStyle().mid); };
    const del = panel.querySelector("[data-del]");
    if (del) del.onclick = e => { e.stopPropagation(); st.shapes.splice(st.sel, 1); st.sel = -1; save(); panelSync(); redraw(); };
  }

  /* ---- conversions temps/prix <-> pixels via l'INDEX LOGIQUE des bougies :
          défini partout (entre les bougies, hors écran, dans le futur), donc
          un dessin déplacé ne peut jamais tomber dans un trou de séance ---- */
  const yOf = p => series.priceToCoordinate(p);
  const pOf = y => series.coordinateToPrice(y);
  function idxOf(t){
    const T = st.times, n = T.length;
    if (!n) return null;
    if (t <= T[0]) return 0 - (T[0] - t) / st.iv;
    if (t >= T[n - 1]) return (n - 1) + (t - T[n - 1]) / st.iv;
    let lo = 0, hi = n - 1;
    while (hi - lo > 1){
      const m = (lo + hi) >> 1;
      if (T[m] <= t) lo = m; else hi = m;
    }
    const span = T[hi] - T[lo] || st.iv;
    return lo + (t - T[lo]) / span;
  }
  function xOf(t){
    const l = idxOf(t);
    return l == null ? null : ts.logicalToCoordinate(l);
  }
  function tOf(x){
    const l = ts.coordinateToLogical(x);
    const T = st.times, n = T.length;
    if (l == null || !n) return null;
    if (l <= 0) return Math.round(T[0] + l * st.iv);
    if (l >= n - 1) return Math.round(T[n - 1] + (l - (n - 1)) * st.iv);
    const i = Math.floor(l), f = l - i;
    return Math.round(T[i] + (T[i + 1] - T[i]) * f);
  }
  function geo(s){
    if (s.type === "hline"){
      const y = yOf(s.p1.price);
      return y == null ? null : {y};
    }
    const x1 = xOf(s.p1.t), y1 = yOf(s.p1.price), x2 = xOf(s.p2.t), y2 = yOf(s.p2.price);
    if ([x1, y1, x2, y2].some(v => v == null)) return null;
    return {x1, y1, x2, y2};
  }

  /* ---- rendu ---- */
  function redraw(){
    if (st.tz !== PREFS.tz){ remapTz(st.tz, PREFS.tz); st.tz = PREFS.tz; save(); }
    const w = box.clientWidth, h = box.clientHeight, dpr = window.devicePixelRatio || 1;
    if (!w || !h) return;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)){
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.width = w + "px";
      canvas.style.height = h + "px";
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const list = st.draft ? st.shapes.concat([st.draft]) : st.shapes;
    list.forEach((s, i) => {
      const g = geo(s);
      if (!g) return;
      ctx.lineWidth = s.width;
      ctx.strokeStyle = s.color;
      ctx.setLineDash(s.style === "dashed" ? [7, 5] : s.style === "dotted" ? [2, 4] : []);
      ctx.beginPath();
      if (s.type === "hline"){
        ctx.moveTo(0, g.y); ctx.lineTo(w, g.y); ctx.stroke();
      } else if (s.type === "trend"){
        ctx.moveTo(g.x1, g.y1); ctx.lineTo(g.x2, g.y2); ctx.stroke();
      } else {
        const x = Math.min(g.x1, g.x2), y = Math.min(g.y1, g.y2),
              bw = Math.abs(g.x2 - g.x1), bh = Math.abs(g.y2 - g.y1);
        ctx.globalAlpha = s.fillOp == null ? .12 : s.fillOp;
        ctx.fillStyle = s.fill || s.color;
        ctx.fillRect(x, y, bw, bh);
        ctx.globalAlpha = 1;
        ctx.strokeRect(x, y, bw, bh);
        if (s.mid){
          const ym = y + bh / 2;
          ctx.setLineDash([4, 4]);
          ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(x, ym); ctx.lineTo(x + bw, ym); ctx.stroke();
          ctx.lineWidth = s.width;
          ctx.setLineDash(s.style === "dashed" ? [7, 5] : s.style === "dotted" ? [2, 4] : []);
        }
      }
      ctx.setLineDash([]);
      if (i === st.sel && i < st.shapes.length){
        const pts = s.type === "hline" ? [[10, g.y], [w - 10, g.y]] : [[g.x1, g.y1], [g.x2, g.y2]];
        for (const [px, py] of pts){
          ctx.fillStyle = "#0A0A0C"; ctx.fillRect(px - 4, py - 4, 8, 8);
          ctx.fillStyle = s.color;  ctx.fillRect(px - 3, py - 3, 6, 6);
        }
      }
    });
  }

  /* ---- hit-test & interactions ---- */
  function distSeg(px, py, x1, y1, x2, y2){
    const dx = x2 - x1, dy = y2 - y1, L2 = dx*dx + dy*dy;
    let t = L2 ? ((px - x1)*dx + (py - y1)*dy) / L2 : 0;
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(px - (x1 + t*dx), py - (y1 + t*dy));
  }
  function hit(px, py){
    for (let i = st.shapes.length - 1; i >= 0; i--){
      const s = st.shapes[i], g = geo(s);
      if (!g) continue;
      if (s.type === "hline"){
        if (Math.abs(py - g.y) < 6) return {i, part: "body"};
        continue;
      }
      if (Math.hypot(px - g.x1, py - g.y1) < 9) return {i, part: "p1"};
      if (Math.hypot(px - g.x2, py - g.y2) < 9) return {i, part: "p2"};
      if (s.type === "trend"){
        if (distSeg(px, py, g.x1, g.y1, g.x2, g.y2) < 6) return {i, part: "body"};
      } else {
        const x = Math.min(g.x1, g.x2), y = Math.min(g.y1, g.y2),
              bw = Math.abs(g.x2 - g.x1), bh = Math.abs(g.y2 - g.y1);
        if (px > x - 6 && px < x + bw + 6 && py > y - 6 && py < y + bh + 6) return {i, part: "body"};
      }
    }
    return null;
  }
  const evPos = e => {
    const r = box.getBoundingClientRect();
    return {x: e.clientX - r.left, y: e.clientY - r.top};
  };

  function onDown(e){
    if (e.target.closest && (e.target.closest(".dtoolbar") || e.target.closest(".dpanel")
        || e.target.closest(".cpanel") || e.target.closest(".cgear"))) return;
    const {x, y} = evPos(e);
    if (st.tool){                                     /* — création — */
      e.preventDefault(); e.stopPropagation();
      const t = tOf(x), p = pOf(y);
      if (t == null || p == null) return;
      if (st.tool === "hline"){
        st.shapes.push({type: "hline", p1: {t, price: p}, p2: {t, price: p}, ...DRAW_DEF});
        st.sel = st.shapes.length - 1;
        toolReset(); save(); panelSync(); redraw();
        return;
      }
      if (!st.draft){
        st.draft = {type: st.tool, p1: {t, price: p}, p2: {t, price: p}, ...DRAW_DEF};
        st._draftMove = ev => {
          const q = evPos(ev);
          const t2 = tOf(q.x), p2 = pOf(q.y);
          if (t2 != null && p2 != null){ st.draft.p2 = {t: t2, price: p2}; redraw(); }
        };
        window.addEventListener("pointermove", st._draftMove);
      } else {
        const t2 = tOf(x), p2 = pOf(y);
        if (t2 != null && p2 != null) st.draft.p2 = {t: t2, price: p2};
        const gg = geo(st.draft);
        if (gg && Math.abs(gg.x2 - gg.x1) < 3 && Math.abs(gg.y2 - gg.y1) < 3){
          cancelDraft(); toolReset(); panelSync(); redraw();   /* fantôme : rejeté */
          return;
        }
        st.shapes.push(st.draft);
        st.sel = st.shapes.length - 1;
        cancelDraft(); toolReset(); save(); panelSync(); redraw();
      }
      return;
    }
    /* — curseur : sélection & drag si contact, sinon on laisse le chart panner — */
    const hs = hit(x, y);
    if (!hs){
      if (st.sel !== -1){ st.sel = -1; panelSync(); redraw(); }
      return;
    }
    e.preventDefault(); e.stopPropagation();
    st.sel = hs.i; panelSync(); redraw();
    const s = st.shapes[hs.i];
    const g0 = geo(s);
    if (!g0) return;
    /* drag en espace PIXEL : chaque ancre est reconvertie à SA propre
       position, donc les trous de séance ne créent plus aucun saut ;
       clamp au cadre : une forme ne peut jamais devenir irrécupérable */
    const start = {x, y, g: {...g0}};
    const cl = (v, m) => Math.max(4, Math.min(m - 4, v));
    const put = (P, px, py) => {
      const t = tOf(cl(px, box.clientWidth)), pr = pOf(cl(py, box.clientHeight));
      if (t != null) P.t = t;
      if (pr != null) P.price = pr;
    };
    const move = ev => {
      const q = evPos(ev);
      const dx = q.x - start.x, dy = q.y - start.y;
      if (s.type === "hline"){
        const pr = pOf(cl(start.g.y + dy, box.clientHeight));
        if (pr != null){ s.p1.price = pr; s.p2 = {...s.p1}; }
        redraw(); return;
      }
      if (hs.part === "p1")      put(s.p1, start.g.x1 + dx, start.g.y1 + dy);
      else if (hs.part === "p2") put(s.p2, start.g.x2 + dx, start.g.y2 + dy);
      else {
        put(s.p1, start.g.x1 + dx, start.g.y1 + dy);
        put(s.p2, start.g.x2 + dx, start.g.y2 + dy);
      }
      redraw();
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      save();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }
  box.addEventListener("pointerdown", onDown, true);

  function onKey(e){
    if (!box.isConnected){ document.removeEventListener("keydown", onKey); return; }
    if (e.key === "Escape"){
      cancelDraft(); toolReset(); st.sel = -1; panelSync(); redraw();
    }
    if ((e.key === "Delete" || e.key === "Backspace") && st.sel >= 0
        && !/INPUT|TEXTAREA|SELECT/.test((document.activeElement || {}).tagName || "")){
      e.preventDefault();
      st.shapes.splice(st.sel, 1); st.sel = -1; save(); panelSync(); redraw();
    }
  }
  document.addEventListener("keydown", onKey);

  ts.subscribeVisibleLogicalRangeChange(redraw);
  new ResizeObserver(redraw).observe(box);
  redraw();

  return {
    setBars(times, iv){
      if (Array.isArray(times) && times.length) st.times = times;
      if (iv) st.iv = iv;
      redraw();
    },
    pushTime(t){
      const T = st.times;
      if (!T.length) st.times = [t];
      else if (t > T[T.length - 1]) T.push(t);
      redraw();
    },
    setKey(k){ st.key = k; st.sel = -1; cancelDraft(); load(); panelSync(); redraw(); },
    redraw,
  };
}

/* ═══ Séparateurs de session : une fine verticale pointillée à chaque
   open daily (détectés par les gaps > 45 min entre bougies — robuste
   quel que soit le fuseau d'affichage). ═══ */
function sessionStarts(times){
  const out = [];
  for (let i = 1; i < times.length; i++)
    if (times[i] - times[i - 1] > 2700) out.push(times[i]);
  return out;
}
function drawSessionLines(ctx, chart, times, w, h){
  if (!times || times.length < 2) return;
  const ts = chart.timeScale();
  ctx.save();
  ctx.strokeStyle = "rgba(232,230,225,.16)";
  ctx.lineWidth = 1;
  ctx.setLineDash([2, 4]);
  for (const t of sessionStarts(times)){
    const x = ts.timeToCoordinate(t);
    if (x == null || x < 0 || x > w) continue;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
  }
  ctx.restore();
}

/* ═══ Interpolation du tick : la bougie GLISSE vers le nouveau prix (easing)
   au lieu de sauter — l'effet de fluidité TradingView, côté rendu. ═══ */
function animatePrice(holder, series, bar, newClose, ms){
  if (holder._anim) cancelAnimationFrame(holder._anim);
  const start = performance.now(), from = bar.close;
  if (!isFinite(from) || Math.abs(newClose - from) < 1e-9){
    bar.close = newClose;
    bar.high = Math.max(bar.high, newClose);
    bar.low = Math.min(bar.low, newClose);
    series.update(bar);
    return;
  }
  const step = now => {
    const k = Math.min(1, (now - start) / ms);
    const e = 1 - Math.pow(1 - k, 3);          /* easeOutCubic */
    const p = from + (newClose - from) * e;
    bar.close = p;
    bar.high = Math.max(bar.high, p);
    bar.low = Math.min(bar.low, p);
    try{ series.update(bar); }catch(_){ holder._anim = null; return; }
    holder._anim = k < 1 ? requestAnimationFrame(step) : null;
  };
  holder._anim = requestAnimationFrame(step);
}
