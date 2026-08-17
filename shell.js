/* GEX Terminal — châssis de navigation partagé.
   ─────────────────────────────────────────────
   Avant ce fichier, chaque page maintenait sa propre liste de liens dans
   son <header>. Neuf listes écrites à la main, qui avaient dérivé :
   aucune page n'affichait le même jeu ni le même ordre (doc listait
   Terminal·Dashboard·Heatmap·News, heatmap listait
   Terminal·Dashboard·News·Doc — News et Doc inversés), et surtout deux
   modules entiers étaient tombés du bateau : /horizon n'était atteignable
   depuis AUCUNE page, /flux depuis la seule page doc. Un module livré,
   documenté, avec son cron et son API, qu'aucun utilisateur ne pouvait
   atteindre en cliquant.

   La cause n'est pas l'inattention : c'est qu'ajouter une destination
   demandait neuf éditions cohérentes. DESTS ci-dessous est désormais la
   seule liste. Ajouter un module = une ligne, et il apparaît partout.

   Regroupement : les cinq surfaces d'analyse (Terminal, Dashboard,
   Heatmap, Flux, Horizon) sont des vues des mêmes données — présentées
   en sélecteur segmenté, elles se lisent comme UN commutateur d'app et
   non comme cinq liens en vrac. C'est ce qui désencombre la zone : le
   nombre d'éléments ne change pas, leur poids visuel oui. Doc et News
   sont du contexte, pas des vues de marché : ils reculent à droite en
   liens simples.

   API :
     Shell.mount(el, "dash")   → rend la nav dans el, marque l'actif
     Shell.setLang("en")       → réécrit les libellés (à appeler depuis
                                 l'applyLang() de la page)

   Le CSS est injecté une fois par ce module, pas recopié dans chaque
   page — sinon on recrée le problème qu'on vient de résoudre, un cran
   plus bas. */
"use strict";

/* auth:true = route gardée par session côté serveur, qui redirige vers
   /?login=1&next=... Le lien reste affiché : la redirection revient sur
   la destination après connexion, donc l'exposer est le bon comportement.
   Le drapeau sert à l'infobulle, pas à masquer. */
const SHELL_DESTS = [
  {id: "terminal", href: "/",        fr: "Terminal",  en: "Terminal", group: "app"},
  {id: "dash",     href: "/dash",    fr: "Dashboard", en: "Dashboard", group: "app"},
  {id: "heatmap",  href: "/heatmap", fr: "Heatmap",   en: "Heatmap",  group: "app"},
  {id: "flux",     href: "/flux",    fr: "Flux",      en: "Flow",     group: "app"},
  {id: "horizon",  href: "/horizon", fr: "Horizon",   en: "Horizon",  group: "app", auth: true},
  {id: "news",     href: "/news",    fr: "News",      en: "News",     group: "ctx", auth: true},
  {id: "doc",      href: "/doc",     fr: "Doc",       en: "Docs",     group: "ctx"},
];

const SHELL_TXT = {
  fr: {locked: "Connexion requise"},
  en: {locked: "Sign-in required"},
};

let SHELL_LANG = "fr";
try{ SHELL_LANG = localStorage.getItem("gexLang") || "fr"; }catch(_){}

/* Les pages masquaient le lien News tant que l'utilisateur n'était pas
   connecté (hidden par défaut, révélé par JS). Choix différent ici : les
   destinations gardées restent VISIBLES, marquées d'un cadenas discret
   qui disparaît une fois connecté.

   Raison : la garde serveur redirige vers /?login=1&next=... donc le clic
   ramène sur la destination après connexion — le parcours fonctionne. Et
   masquer n'apporte aucune sécurité, la page est gardée côté serveur de
   toute façon. Or c'est précisément le masquage qui rendait Horizon
   invisible : un module qu'on cache aux visiteurs non connectés ne se
   découvre jamais. Un cadenas informe, une absence n'informe pas.

   Repasser au masquage, si tu préfères ne rien montrer aux non-membres,
   ne demande que de remettre `el.hidden = d.auth && !SHELL_AUTH` dans
   shellRender au lieu de la classe .locked. */
let SHELL_AUTH = false;

function shellCss(){
  if (document.getElementById("shellCss")) return;
  const s = document.createElement("style");
  s.id = "shellCss";
  /* Replis en dur derrière chaque token : shell.js peut être chargé par
     une page qui n'aurait pas encore theme.css, et une nav sans couleur
     est illisible plutôt que simplement moins jolie. */
  s.textContent = `
  .appnav{display:flex; align-items:center; gap:var(--sp-3,12px); min-width:0}
  .appnav-seg{display:flex; align-items:center; gap:2px; padding:2px;
    background:var(--surface2,#16161A); border:1px solid var(--hair,rgba(255,255,255,.06));
    border-radius:var(--r-sm,6px); overflow-x:auto; scrollbar-width:none}
  .appnav-seg::-webkit-scrollbar{display:none}
  .appnav-seg a{
    font-family:var(--sans,sans-serif); font-size:var(--fs-sm,12px);
    font-weight:var(--fw-medium,500); color:var(--muted,#8A8A94);
    text-decoration:none; white-space:nowrap; padding:5px 11px;
    border-radius:var(--r-xs,3px); min-height:30px; display:inline-flex;
    align-items:center; transition:background var(--t-fast,.12s ease), color var(--t-fast,.12s ease)}
  .appnav-seg a:hover{color:var(--text,#ECEAE4); background:rgba(255,255,255,.05)}
  .appnav-seg a[aria-current="page"]{
    background:rgba(var(--gold-rgb,240,185,11),var(--a-subtle,.10));
    color:var(--gold,#F0B90B); font-weight:var(--fw-bold,600)}
  .appnav-ctx{display:flex; align-items:center; gap:1px}
  .appnav-ctx a{
    font-family:var(--sans,sans-serif); font-size:var(--fs-sm,12px);
    color:var(--faint,#5C5C66); text-decoration:none; white-space:nowrap;
    padding:5px 9px; border-radius:var(--r-xs,3px); min-height:30px;
    display:inline-flex; align-items:center; transition:color var(--t-fast,.12s ease)}
  .appnav-ctx a:hover{color:var(--text,#ECEAE4)}
  .appnav-ctx a[aria-current="page"]{color:var(--gold,#F0B90B)}
  /* Destination gardée : le libellé recule et un point doré signale la
     connexion requise. Assez visible pour informer, assez discret pour
     ne pas concurrencer la destination active. */
  .appnav a.locked{opacity:.72}
  .appnav a.locked .lk{font-style:normal; margin-left:5px; color:var(--gold,#F0B90B);
    font-size:15px; line-height:0; opacity:.8}
  /* Cibles tactiles >= 40px et nav défilable sur mobile (cf. CLAUDE.md) */
  @media (max-width:720px){
    .appnav{gap:var(--sp-2,8px); width:100%; overflow-x:auto; scrollbar-width:none}
    .appnav::-webkit-scrollbar{display:none}
    .appnav-seg a, .appnav-ctx a{min-height:40px; padding:8px 12px}
  }`;
  document.head.appendChild(s);
}

function shellCurrent(){
  /* Déduit la destination active du chemin. Les alias (/index.html,
     /dash.html, /wiki) doivent résoudre comme leur route canonique,
     sinon aucun onglet ne s'allume selon l'URL par laquelle on est
     arrivé. */
  let p = location.pathname.replace(/\/+$/, "") || "/";
  const ALIAS = {"/index.html": "/", "/dash.html": "/dash", "/heatmap.html": "/heatmap",
                 "/news.html": "/news", "/doc.html": "/doc", "/wiki": "/doc",
                 "/flux.html": "/flux", "/horizon.html": "/horizon",
                 "/profile.html": "/profile", "/privacy.html": "/privacy"};
  p = ALIAS[p] || p;
  const hit = SHELL_DESTS.find((d) => d.href === p);
  return hit ? hit.id : null;
}

function shellRender(el, current){
  const lang = SHELL_LANG === "en" ? "en" : "fr";
  const link = (d) => {
    const on = d.id === current ? ' aria-current="page"' : "";
    const gated = d.auth && !SHELL_AUTH;
    const tip = gated ? ` title="${SHELL_TXT[lang].locked}"` : "";
    const cls = gated ? ' class="locked"' : "";
    const lock = gated ? '<i class="lk" aria-hidden="true">·</i>' : "";
    return `<a href="${d.href}"${on}${tip}${cls} data-shell="${d.id}">${d[lang]}${lock}</a>`;
  };
  const app = SHELL_DESTS.filter((d) => d.group === "app").map(link).join("");
  const ctx = SHELL_DESTS.filter((d) => d.group === "ctx").map(link).join("");
  el.className = "appnav";
  el.innerHTML = `<div class="appnav-seg">${app}</div><div class="appnav-ctx">${ctx}</div>`;
}

const Shell = {
  /* current : id de destination, ou omis pour le déduire de l'URL. */
  mount(el, current){
    if (typeof el === "string") el = document.getElementById(el);
    if (!el) return null;
    shellCss();
    el._shellCurrent = current || shellCurrent();
    shellRender(el, el._shellCurrent);
    if (!Shell._mounted.includes(el)) Shell._mounted.push(el);
    return el;
  },
  /* Réécrit les libellés de toutes les navs montées. À appeler depuis
     l'applyLang() de la page : sans ça la nav resterait en français
     après une bascule, et une nav à moitié traduite est pire qu'une
     nav non traduite. */
  setLang(lang){
    SHELL_LANG = lang === "en" ? "en" : "fr";
    Shell._redraw();
  },
  /* À appeler quand l'état de session est connu : retire le cadenas des
     destinations gardées. Remplace le `nl.hidden = !ok` que chaque page
     portait sur son lien News. */
  setAuth(ok){
    SHELL_AUTH = !!ok;
    Shell._redraw();
  },
  _redraw(){
    Shell._mounted.forEach((el) => shellRender(el, el._shellCurrent));
  },
  _mounted: [],
};

window.Shell = Shell;
