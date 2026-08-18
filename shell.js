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

   Regroupement : les surfaces d'analyse (Terminal, Dashboard, Heatmap)
   sont des vues des mêmes données — présentées en sélecteur segmenté,
   elles se lisent comme UN commutateur d'app et non comme des liens en
   vrac. C'est ce qui désencombre la zone : le nombre d'éléments ne
   change pas, leur poids visuel oui. Doc et News sont du contexte, pas
   des vues de marché : ils reculent à droite en liens simples.

   Le module unique ne DÉCIDE PAS de ce qui est public. Les règles de
   visibilité en place sont reprises telles quelles : News reste réservé
   aux membres connectés, Horizon n'est pas encore ouvert. Ce fichier
   les rend explicites (drapeaux auth / enabled) au lieu de les laisser
   éparpillées dans neuf pages — c'est là qu'était le vrai défaut, pas
   dans les règles elles-mêmes.

   API :
     Shell.mount(el, "dash")   → rend la nav dans el, marque l'actif
     Shell.setLang("en")       → réécrit les libellés (à appeler depuis
                                 l'applyLang() de la page)
     Shell.setAuth(true)       → révèle les destinations réservées aux
                                 membres (à appeler dès que l'état de
                                 session est connu)

   Le CSS est injecté une fois par ce module, pas recopié dans chaque
   page — sinon on recrée le problème qu'on vient de résoudre, un cran
   plus bas. */
"use strict";

/* Deux drapeaux gouvernent l'affichage d'une destination :

   auth:true    — réservée aux membres connectés. Le lien reste MASQUÉ
                  tant que Shell.setAuth(true) n'a pas été appelé. C'est
                  le comportement que portaient les pages avant ce module
                  (lien News en `hidden` dans le markup, révélé par JS) :
                  le reproduire à l'identique est le contrat, pas une
                  décision à reprendre ici.

   enabled:false — module non encore ouvert au public. Il reste décrit
                  ici pour que la connaissance ne se perde pas, mais
                  n'est jamais rendu. Le rouvrir = retirer le drapeau. */
const SHELL_DESTS = [
  {id: "terminal", href: "/",        fr: "Terminal",  en: "Terminal",  group: "app"},
  {id: "dash",     href: "/dash",    fr: "Dashboard", en: "Dashboard", group: "app"},
  {id: "heatmap",  href: "/heatmap", fr: "Heatmap",   en: "Heatmap",   group: "app"},
  /* /flux est la PAGE D'EXPLICATION du module Flux, pas une vue de
     marché : elle appartient à la documentation et s'atteint depuis le
     contenu de /doc (bloc "fluxcta"). À ne pas confondre avec le bouton
     FLUX du terminal, qui bascule le panneau central entre chart de prix
     et gamma projeté — celui-là vit dans la barre de contexte, pas ici. */
  {id: "flux",     href: "/flux",    fr: "Flux",      en: "Flow",      group: "ctx",
   enabled: false},
  /* Horizon : pas encore ouvert. Ne pas rendre. */
  {id: "horizon",  href: "/horizon", fr: "Horizon",   en: "Horizon",   group: "app",
   auth: true, enabled: false},
  {id: "news",     href: "/news",    fr: "News",      en: "News",      group: "ctx", auth: true},
  {id: "doc",      href: "/doc",     fr: "Doc",       en: "Docs",      group: "ctx"},
];

/* Destinations réellement affichables dans l'état courant. */
function shellVisible(){
  return SHELL_DESTS.filter((d) => d.enabled !== false && (!d.auth || SHELL_AUTH));
}

let SHELL_LANG = "fr";
try{ SHELL_LANG = localStorage.getItem("gexLang") || "fr"; }catch(_){}

/* Faux jusqu'à preuve du contraire : une destination réservée aux membres
   ne doit JAMAIS apparaître le temps que l'état de session soit connu.
   Partir de true la ferait clignoter à l'écran avant d'être retirée. */
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

  /* ══════════ EN-TÊTE PARTAGÉ ══════════
     Les neuf pages portaient chacune leur propre règle header{} : padding
     à 11, 12 ou 13px, titre à 12,5 ou 13px, interlettrage à .2 ou .22em.
     Des écarts invisibles isolément, mais qui font qu'en naviguant d'une
     page à l'autre l'en-tête « saute » — c'est exactement ce qui donne
     l'impression de pages assemblées plutôt que d'une application.

     Le titre passe de 13px en capitales très espacées à 18px en
     interlettrage neutre : à .22em un titre lit comme une étiquette, et
     quand tout est étiquette rien ne domine. */
  .appheader{
    display:flex; align-items:center; gap:var(--sp-3,12px); flex-wrap:wrap;
    padding:var(--sp-3,12px) var(--sp-5,20px);
    border-bottom:1px solid var(--hair,rgba(255,255,255,.06));
    background:var(--bg,#0A0A0C);
  }
  .appheader .mark{width:24px; height:24px; flex:none}
  .appheader h1{
    font-family:var(--sans,sans-serif); font-size:var(--fs-lg,18px);
    font-weight:var(--fw-bold,600); letter-spacing:var(--ls-none,0);
    white-space:nowrap; margin:0;
  }
  .appheader h1 b{color:var(--gold,#F0B90B)}
  .appheader h1 span{color:var(--muted,#8A8A94); font-weight:var(--fw-medium,500)}
  .appheader #appnav{margin-left:var(--sp-2,8px)}
  .appheader .appLang{margin-left:auto}

  /* ══════════ BASCULE DE LANGUE ══════════
     Recopiée dans neuf pages, sous deux formes differentes (#langSel a
     boutons data-l, et #bFr/#bEn) avec chacune son CSS et son cablage.
     Le module la porte desormais : une seule apparence, un seul etat
     actif, un seul ecrivain de localStorage. */
  .appLang{
    display:inline-flex; align-items:center; gap:1px; padding:2px;
    background:var(--surface2,#16161A);
    border:1px solid var(--hair,rgba(255,255,255,.06));
    border-radius:var(--r-sm,6px);
  }
  .appLang button{
    font-family:var(--mono,monospace); font-size:var(--fs-xs,11px);
    letter-spacing:var(--ls-label,.08em); color:var(--muted,#8A8A94);
    background:transparent; border:0; cursor:pointer;
    padding:4px 9px; min-height:26px; border-radius:var(--r-xs,3px);
    transition:color var(--t-fast,.12s ease), background var(--t-fast,.12s ease);
  }
  .appLang button:hover{color:var(--text,#ECEAE4)}
  .appLang button.on{
    background:rgba(var(--gold-rgb,240,185,11),var(--a-subtle,.10));
    color:var(--gold,#F0B90B); font-weight:var(--fw-bold,600);
  }
  .appLang button:focus-visible{outline:2px solid var(--gold,#F0B90B); outline-offset:-2px}

  /* Cibles tactiles >= 40px et nav défilable sur mobile (cf. CLAUDE.md) */
  @media (max-width:720px){
    .appnav{gap:var(--sp-2,8px); width:100%; overflow-x:auto; scrollbar-width:none}
    .appnav::-webkit-scrollbar{display:none}
    .appnav-seg a, .appnav-ctx a{min-height:40px; padding:8px 12px}
    .appheader{padding:var(--sp-2,8px) var(--sp-4,16px)}
    .appheader h1{font-size:var(--fs-md,15px)}
    .appLang button{min-height:40px; padding:8px 12px}
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
    return `<a href="${d.href}"${on} data-shell="${d.id}">${d[lang]}</a>`;
  };
  const vis = shellVisible();
  const app = vis.filter((d) => d.group === "app").map(link).join("");
  const ctx = vis.filter((d) => d.group === "ctx").map(link).join("");
  el.className = "appnav";
  el.innerHTML = `<div class="appnav-seg">${app}</div><div class="appnav-ctx">${ctx}</div>`;
}

/* Bascules de langue montées, et rappels enregistrés par les pages. */
const SHELL_LANG_EL = [];
const SHELL_LANG_CB = [];

function shellLangRender(el){
  const on = (l) => (l === SHELL_LANG ? ' class="on"' : "");
  el.className = "appLang";
  el.innerHTML = `<button type="button" data-l="fr"${on("fr")}>FR</button>` +
                 `<button type="button" data-l="en"${on("en")}>EN</button>`;
  el.querySelectorAll("button").forEach((b) => {
    b.onclick = () => Shell.setLang(b.dataset.l, true);
  });
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
  /* persist : true quand le changement vient d'un clic sur la bascule —
     le module ecrit alors gexLang et previent les pages. Appelee sans ce
     drapeau depuis l'applyLang() d'une page, elle se contente de suivre
     la langue que la page a deja decidee, sans re-declencher de rappel
     (ce qui bouclerait). */
  setLang(lang, persist){
    SHELL_LANG = lang === "en" ? "en" : "fr";
    if (persist){
      try{ localStorage.setItem("gexLang", SHELL_LANG); }catch(_){}
    }
    Shell._redraw();
    if (persist) SHELL_LANG_CB.forEach((fn) => { try{ fn(SHELL_LANG); }catch(_){} });
  },
  /* Monte la bascule FR/EN. Le module possede l'apparence, l'etat actif
     et l'ecriture de gexLang ; la page ne fournit que sa reaction. */
  mountLang(el){
    if (typeof el === "string") el = document.getElementById(el);
    if (!el) return null;
    shellCss();
    shellLangRender(el);
    if (!SHELL_LANG_EL.includes(el)) SHELL_LANG_EL.push(el);
    return el;
  },
  /* Enregistre la reaction de la page a un changement de langue. */
  onLang(fn){ if (typeof fn === "function") SHELL_LANG_CB.push(fn); },
  /* La langue retenue au chargement, pour que la page parte du bon pied. */
  lang(){ return SHELL_LANG; },
  /* À appeler dès que l'état de session est connu : révèle les
     destinations réservées aux membres. Remplace exactement le
     `nl.hidden = !ok` que chaque page portait sur son lien News.
     Une page elle-même gardée par session peut appeler setAuth(true)
     sans condition : si elle s'affiche, l'utilisateur est connecté. */
  setAuth(ok){
    SHELL_AUTH = !!ok;
    Shell._redraw();
  },
  _redraw(){
    Shell._mounted.forEach((el) => shellRender(el, el._shellCurrent));
    SHELL_LANG_EL.forEach(shellLangRender);
  },
  _mounted: [],
};

window.Shell = Shell;
