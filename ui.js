/* GEX Terminal — module UI partagé (prefs, fuseau, menu ⚙ des charts).
   Chargé par index.html et dash.html. Préférences communes via localStorage. */
"use strict";

const DEF_PREFS = {tz: "Europe/Paris", up: "#26A69A", dn: "#EF5350", px: "#FFD84D"};
let PREFS = {...DEF_PREFS};
try{ PREFS = {...DEF_PREFS, ...JSON.parse(localStorage.getItem("gexPrefs") || "{}")}; }catch(_){}
function savePrefs(){ try{ localStorage.setItem("gexPrefs", JSON.stringify(PREFS)); }catch(_){} }

const TZ_CITY = {"Europe/Paris": "Paris", "America/New_York": "New York",
                 "Europe/London": "London", "UTC": "UTC"};

/* Le moteur de chart affiche de l'UTC brut : décalage vers le fuseau choisi,
   heure d'été gérée par Intl (aucun offset codé en dur). */
function tzShift(tSec){
  const d = new Date(tSec * 1000);
  const loc = new Date(d.toLocaleString("en-US", {timeZone: PREFS.tz}));
  const utc = new Date(d.toLocaleString("en-US", {timeZone: "UTC"}));
  return tSec + Math.round((loc - utc) / 1000);
}

const CANDLE_PAIRS = [
  ["#26A69A", "#EF5350"],   // terminal (défaut)
  ["#4CAF50", "#F44336"],   // classique
  ["#3B82F6", "#F59E0B"],   // bleu / ambre
  ["#E8E6E1", "#5C5C66"],   // monochrome
];
const PRICE_COLORS = ["#FFD84D", "#F0B90B", "#FFFFFF", "#22D3EE"];

const GEAR_TXT = {
  fr: {tz: "Fuseau horaire", candles: "Bougies", price: "Ligne de prix",
       custom: "perso", reset: "↺ Réinitialiser"},
  en: {tz: "Timezone", candles: "Candles", price: "Price line",
       custom: "custom", reset: "↺ Reset"},
};
function gearLang(){
  let l = "fr";
  try{ l = localStorage.getItem("gexLang") || "fr"; }catch(_){}
  return GEAR_TXT[l] || GEAR_TXT.fr;
}

/* CSS du composant injecté une fois */
(function(){
  const css = `
  .cgear{
    position:absolute; top:8px; right:8px; z-index:6;
    width:30px; height:30px; display:flex; align-items:center; justify-content:center;
    background:rgba(17,17,20,.85); border:1px solid var(--line, #212127);
    color:var(--faint, #5C5C66); font-size:13px; cursor:pointer; backdrop-filter:blur(2px);
    transition:color .12s ease, border-color .12s ease;
  }
  .cgear:hover{color:var(--gold, #F0B90B); border-color:rgba(240,185,11,.45)}
  .cmenu{
    position:absolute; top:44px; right:8px; z-index:7; min-width:238px;
    background:#131316; border:1px solid var(--line-strong, #2B2B33);
    box-shadow:0 14px 44px rgba(0,0,0,.65); padding:14px;
  }
  .cmenu h4{
    font-family:"Space Grotesk", monospace; font-size:9.5px; font-weight:600;
    letter-spacing:.18em; text-transform:uppercase; color:var(--faint, #5C5C66);
    margin:0 0 8px;
  }
  .cmenu section + section{margin-top:14px; padding-top:12px; border-top:1px solid var(--line, #212127)}
  .cmenu select{
    width:100%; background:#17171B; border:1px solid var(--line, #212127);
    color:var(--text, #ECEAE4); font:inherit; font-size:12px; padding:7px 8px; cursor:pointer;
  }
  .swrow{display:flex; gap:8px; align-items:center; flex-wrap:wrap}
  .sw{
    width:26px; height:26px; border:1px solid var(--line-strong, #2B2B33);
    cursor:pointer; padding:0; position:relative; flex:none;
  }
  .sw.pair{background:linear-gradient(135deg, var(--a) 50%, var(--b) 50%)}
  .sw.on{outline:2px solid var(--gold, #F0B90B); outline-offset:1px}
  .swcustom{
    display:inline-flex; align-items:center; gap:5px; color:var(--faint, #5C5C66); font-size:10px;
  }
  .swcustom input[type=color]{
    width:26px; height:26px; padding:0; border:1px solid var(--line-strong, #2B2B33);
    background:#17171B; cursor:pointer;
  }
  .cmenu .rst{
    width:100%; margin-top:14px; background:#17171B; border:1px solid var(--line, #212127);
    color:var(--muted, #8A8A94); font:inherit; font-size:11px; padding:7px; cursor:pointer;
  }
  .cmenu .rst:hover{color:var(--gold, #F0B90B)}`;
  const st = document.createElement("style");
  st.textContent = css;
  document.head.appendChild(st);
})();

/* Monte une molette ⚙ + menu sur un conteneur de chart (position:relative requis).
   hooks: { applyColors(), reloadBars() } — applyColors doit relire PREFS. */
function buildGearMenu(box, hooks){
  const btn = document.createElement("button");
  btn.className = "cgear";
  btn.textContent = "⚙";
  btn.title = "Réglages du chart";
  const menu = document.createElement("div");
  menu.className = "cmenu";
  menu.hidden = true;
  box.appendChild(btn);
  box.appendChild(menu);

  function render(){
    const L = gearLang();
    const pairs = CANDLE_PAIRS.map(([a, b], i) =>
      `<button class="sw pair${PREFS.up===a && PREFS.dn===b ? " on" : ""}"
        style="--a:${a};--b:${b}" data-pair="${i}" title="${a} / ${b}"></button>`).join("");
    const prices = PRICE_COLORS.map(c =>
      `<button class="sw${PREFS.px===c ? " on" : ""}" style="background:${c}" data-px="${c}"></button>`).join("");
    const tzOpts = Object.entries(TZ_CITY).map(([v, n]) =>
      `<option value="${v}"${PREFS.tz===v ? " selected" : ""}>${n}</option>`).join("");
    menu.innerHTML = `
      <section><h4>${L.tz}</h4><select data-tz>${tzOpts}</select></section>
      <section><h4>${L.candles}</h4>
        <div class="swrow">${pairs}
          <span class="swcustom"><input type="color" data-cup value="${PREFS.up}">
            <input type="color" data-cdn value="${PREFS.dn}">${L.custom}</span>
        </div></section>
      <section><h4>${L.price}</h4>
        <div class="swrow">${prices}
          <span class="swcustom"><input type="color" data-cpx value="${PREFS.px}">${L.custom}</span>
        </div></section>
      <button class="rst">${L.reset}</button>`;

    menu.querySelector("[data-tz]").onchange = e => {
      PREFS.tz = e.target.value; savePrefs(); hooks.reloadBars(); render();
    };
    menu.querySelectorAll("[data-pair]").forEach(b => b.onclick = () => {
      const [a, c] = CANDLE_PAIRS[+b.dataset.pair];
      PREFS.up = a; PREFS.dn = c; savePrefs(); hooks.applyColors(); render();
    });
    menu.querySelectorAll("[data-px]").forEach(b => b.onclick = () => {
      PREFS.px = b.dataset.px; savePrefs(); hooks.applyColors(); render();
    });
    menu.querySelector("[data-cup]").oninput = e => { PREFS.up = e.target.value; savePrefs(); hooks.applyColors(); };
    menu.querySelector("[data-cdn]").oninput = e => { PREFS.dn = e.target.value; savePrefs(); hooks.applyColors(); };
    menu.querySelector("[data-cpx]").oninput = e => { PREFS.px = e.target.value; savePrefs(); hooks.applyColors(); };
    menu.querySelector(".rst").onclick = () => {
      Object.assign(PREFS, {up: DEF_PREFS.up, dn: DEF_PREFS.dn, px: DEF_PREFS.px});
      savePrefs(); hooks.applyColors(); render();
    };
  }

  btn.onclick = e => {
    e.stopPropagation();
    if (menu.hidden){ render(); menu.hidden = false; }
    else menu.hidden = true;
  };
  document.addEventListener("click", ev => {
    if (!menu.hidden && !menu.contains(ev.target) && ev.target !== btn) menu.hidden = true;
  });
}
