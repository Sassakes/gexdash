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

   Présentation : deux groupes, mais chacun avec UN traitement visuel
   cohérent en lui-même — c'est la leçon d'une itération précédente, où
   deux natures de liens (vues du produit / ressources externes) avaient
   été mélangées dans un seul bloc puis, à l'inverse, un lien avait été
   sorti du bloc sans lui donner de foyer propre : les deux erreurs
   produisent le même symptôme, un élément qui semble posé au hasard.

   — SHELL_DESTS → sélecteur segmenté (#appnav) : les VUES du produit
     (Terminal, Dashboard, Heatmap, News). Toutes vivent dans la même
     page, un clic y bascule le contenu principal.
   — SHELL_COMMUNITY → cluster de liens (#community) : les RESSOURCES
     externes au produit (Discord, TradingView) plus Doc, qui n'est pas
     une vue mais une destination d'aide — de même nature que les deux
     autres, donc du même bloc, pas de la barre de navigation.

   La destination active se distingue par son fond doré (segmenté) ou
   sa couleur dorée (cluster) — c'est la seule hiérarchie dont chacun a
   besoin.

   Le module unique ne DÉCIDE PAS de ce qui est public. Les règles de
   visibilité en place sont reprises telles quelles : News reste réservé
   aux membres connectés, Horizon n'est pas encore ouvert. Ce fichier
   les rend explicites (drapeaux auth / enabled) au lieu de les laisser
   éparpillées dans neuf pages — c'est là qu'était le vrai défaut, pas
   dans les règles elles-mêmes.

   API :
     Shell.mount(el, "dash")        → rend le sélecteur segmenté, marque
                                      l'actif
     Shell.mountCommunity(el)       → rend Discord / TradingView / Doc
     Shell.setLang("en")            → réécrit les libellés des deux (à
                                      appeler depuis l'applyLang() de la
                                      page)
     Shell.setAuth(true)            → révèle les destinations réservées
                                      aux membres (à appeler dès que
                                      l'état de session est connu)

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
  {id: "terminal", href: "/",        fr: "Terminal",  en: "Terminal"},
  {id: "dash",     href: "/dash",    fr: "Dashboard", en: "Dashboard"},
  {id: "heatmap",  href: "/heatmap", fr: "Heatmap",   en: "Heatmap"},
  /* /flux est la PAGE D'EXPLICATION du module Flux, pas une vue de
     marché : elle appartient à la documentation et s'atteint depuis le
     contenu de /doc (bloc "fluxcta"). À ne pas confondre avec le bouton
     FLUX du terminal, qui bascule le panneau central entre chart de prix
     et gamma projeté — celui-là vit dans la barre de contexte, pas ici. */
  {id: "flux",     href: "/flux",    fr: "Flux",      en: "Flow",
   enabled: false},
  /* Horizon : pas encore ouvert. Ne pas rendre. */
  {id: "horizon",  href: "/horizon", fr: "Horizon",   en: "Horizon",
   auth: true, enabled: false},
  {id: "news",     href: "/news",    fr: "News",      en: "News", auth: true},
];

/* Doc n'est pas une SURFACE D'ANALYSE — ce n'est pas une vue de marché
   comme Terminal/Dashboard/Heatmap/News, c'est une ressource externe au
   même titre que Discord ou TradingView. Elle rejoint ce cluster plutôt
   que le sélecteur segmenté : un bloc mélangeant deux natures de liens
   (vues du produit / ressources) ne se lit ni comme l'un ni comme
   l'autre, exactement le défaut déjà corrigé une fois pour News avant
   qu'elle ne rejoigne le bon groupe. */
const SHELL_COMMUNITY = [
  {id: "discord",     cls: "dc", external: true,
   href: "https://discord.gg/YfCbXDtb4", fr: "Discord", en: "Discord"},
  {id: "tradingview", cls: "tv", external: true,
   href: "https://www.tradingview.com/script/TfBS3GjM-GEX-Levels-Dealer-Gamma-Exposure/",
   fr: "TradingView", en: "TradingView"},
  {id: "doc", href: "/doc", fr: "Doc", en: "Docs"},
];

/* Destinations réellement affichables dans le sélecteur segmenté. */
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

  /* ══════════ EN-TÊTE PARTAGÉ ══════════
     Les neuf pages portaient chacune leur propre règle header{} : padding
     à 11, 12 ou 13px, titre à 12,5 ou 13px, interlettrage à .2 ou .22em.
     Des écarts invisibles isolément, mais qui font qu'en naviguant d'une
     page à l'autre l'en-tête « saute » — c'est exactement ce qui donne
     l'impression de pages assemblées plutôt que d'une application.

     Le titre passe de 13px en capitales très espacées à 18px en
     interlettrage neutre : à .22em un titre lit comme une étiquette, et
     quand tout est étiquette rien ne domine.

     ANCRAGE HORIZONTAL — le vrai bug corrigé ici, pas seulement un
     ajustement de style. index.html centrait son <header> dans <main>
     (880px), les huit autres pages rendaient .appheader en pleine
     largeur collé au bord gauche (aucun max-width). Résultat : sur un
     écran large, l'en-tête sautait visiblement d'une position centrée à
     une position collée à gauche en passant du terminal à n'importe
     quelle autre page.

     .appheader centre désormais son CONTENU sur --shell-w (theme.css),
     DÉCOUPLÉ de la largeur de la colonne de contenu propre à chaque
     page (880px sur le terminal, aucune limite sur le dashboard,
     960px sur horizon…) — c'est ce découplage qui rend l'ancre stable
     quel que soit ce que la page affiche en dessous. Le fond et le
     filet, eux, doivent rester plein-bord (l'effet "barre d'app" que
     portait déjà le design d'origine) : element::before en pleine
     largeur de viewport (left:50%, margin-left:-50vw, technique dite
     "full-bleed breakout") porte l'un, tandis que .appheader lui-même,
     limité à --shell-w et centré, porte l'autre.

     DEUX RANGÉES — .appheader-id (titre seul) puis .appheader-bar (nav +
     communauté + langue), même principe que .hdr-id/.hdr-bar sur le
     terminal. Nécessaire, pas cosmétique : sur une seule ligne, le titre
     entrait en concurrence pour la largeur avec nav/communauté/langue, et
     cette concurrence variait selon la longueur du titre de CHAQUE page
     ("Documentation" mange plus de place que "Doc") — la rupture de mise
     en page n'arrivait donc pas au même seuil sur deux pages différentes.
     En isolant le titre sur sa propre rangée, nav + communauté + langue
     disposent seules de toute la largeur de --shell-w, un budget qui leur
     suffit jusqu'à un seuil commun à toutes les pages, bien plus bas que
     ce qu'exigeait la compétition avec le titre. */
  .appheader{
    position:relative;
    display:flex; flex-direction:column; gap:var(--sp-2,8px);
    max-width:var(--shell-w,1180px); margin:0 auto;
    padding:var(--sp-3,12px) var(--sp-5,20px);
  }
  .appheader::before{
    content:""; position:absolute; top:0; bottom:0; left:50%; right:50%;
    margin-left:-50vw; margin-right:-50vw; z-index:-1;
    background:var(--bg,#0A0A0C);
    border-bottom:1px solid var(--hair,rgba(255,255,255,.06));
  }
  .appheader-id{display:flex; align-items:center; gap:var(--sp-2,8px)}
  .appheader-bar{display:flex; align-items:center; gap:var(--sp-3,12px); flex-wrap:wrap}
  .appheader .mark{width:24px; height:24px; flex:none}
  .appheader h1{
    font-family:var(--sans,sans-serif); font-size:var(--fs-lg,18px);
    font-weight:var(--fw-bold,600); letter-spacing:var(--ls-none,0);
    white-space:nowrap; margin:0;
  }
  .appheader h1 b{color:var(--gold,#F0B90B)}
  .appheader h1 span{color:var(--muted,#8A8A94); font-weight:var(--fw-medium,500)}
  /* Communauté collée à DROITE, pas à la nav : margin-left:auto sur
     #community (pas sur .appLang) fait que tout l'espace flexible se
     prend entre nav et communauté -- .appLang suit ensuite avec un
     simple gap, formant un seul groupe [communauté, langue] ancré à
     droite plutôt que deux éléments séparés par un vide inégal. */
  .appheader-bar #community{margin-left:auto}

  /* ══════════ TIROIR MOBILE "PLUS D'OPTIONS" ══════════
     Sur telephone, la nav passe a width:100% (regle 720px plus bas),
     ce qui pousse communaute (Discord/TradingView/Doc) et langue
     chacune sur sa propre rangee pleine largeur -- constat identique a
     celui fait sur le terminal (cf. index.html), mais ici il touche les
     8 autres pages a la fois puisqu'elles partagent cette meme rangee.
     Neutre par defaut (display:contents) : #community et #langSel
     restent des enfants directs de .appheader-bar, comportement
     identique a avant ce bloc. shellEnsureMoreDrawer() (plus bas) monte
     la case a cocher + le bouton "..." au premier Shell.mount() de
     chaque page -- rien a editer dans les 8 fichiers HTML. */
  .appheader-more-chk{position:absolute; opacity:0; pointer-events:none}
  .appheader-more-btn{display:none}
  .appheader-more{display:contents}

  /* ══════════ CLUSTER COMMUNAUTÉ (Discord / TradingView / Doc) ══════════
     Discord et TradingView n'existaient que dans le header bespoke du
     terminal ; les huit autres pages n'avaient aucun accès à ces liens.
     Le cluster est désormais un composant partagé au même titre que la
     nav et la bascule de langue : chaque page qui monte #community
     obtient le même trio, dans le même ordre, avec le même style. */
  .appcommunity{display:flex; align-items:center; gap:2px; min-width:0}
  .appcommunity a{
    display:inline-flex; align-items:center; gap:6px; text-decoration:none;
    color:var(--faint,#5C5C66); font-size:var(--fs-sm,12px); white-space:nowrap;
    padding:5px 9px; border-radius:var(--r-xs,3px);
    transition:color var(--t-fast,.12s ease), background var(--t-fast,.12s ease);
  }
  .appcommunity a:hover{color:var(--text,#ECEAE4); background:rgba(255,255,255,var(--a-faint,.05))}
  .appcommunity a .dot{width:6px; height:6px; border-radius:var(--r-full,999px); flex:0 0 auto}
  .appcommunity a.dc .dot{background:#5865F2}  /* bleu de marque Discord */
  .appcommunity a.tv .dot{background:#2962FF}  /* bleu de marque TradingView */
  .appcommunity a.doc[aria-current="page"]{color:var(--gold,#F0B90B); font-weight:var(--fw-bold,600)}
  .appcommunity .cdiv{
    width:1px; height:14px; background:var(--hair-strong,rgba(255,255,255,.10));
    margin:0 4px; flex:0 0 auto;
  }

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
    .appnav-seg a{min-height:40px; padding:8px 12px}
    .appheader{padding:var(--sp-2,8px) var(--sp-4,16px)}
    .appheader h1{font-size:var(--fs-md,15px)}
    .appLang button{min-height:40px; padding:8px 12px}
    .appcommunity a{min-height:40px; padding:8px 10px}
    /* Active le tiroir : la nav prend deja toute la largeur (regle
       ci-dessus), donc communaute+langue repassent forcement a la ligne
       -- autant les regrouper derriere un seul bouton compact plutot
       que deux rangees pleine largeur. */
    .appheader-bar{position:relative}
    .appheader-more-btn{
      display:inline-flex; align-items:center; justify-content:center;
      width:40px; height:40px; flex:none; margin-left:auto;
      background:var(--surface2,#16161A); border:1px solid var(--hair-strong,rgba(255,255,255,.10));
      color:var(--muted,#8A8A94); font-size:18px; line-height:1; cursor:pointer;
      border-radius:var(--r-xs,3px);
    }
    .appheader-more-btn:hover{color:var(--gold,#F0B90B); border-color:rgba(var(--gold-rgb,240,185,11),.5)}
    .appheader-more-chk:checked ~ .appheader-more-btn{color:var(--gold,#F0B90B); border-color:rgba(var(--gold-rgb,240,185,11),.5)}
    .appheader-more-chk:focus-visible ~ .appheader-more-btn{outline:2px solid var(--gold,#F0B90B); outline-offset:2px}
    .appheader-more{
      display:none; flex-direction:column; align-items:flex-start; gap:var(--sp-2,8px);
      width:100%; margin-top:var(--sp-1,4px); padding-top:var(--sp-2,8px);
      border-top:1px solid var(--hair,rgba(255,255,255,.06));
    }
    .appheader-more-chk:checked ~ .appheader-more{display:flex}
    .appheader-more #community{margin-left:0}
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
  // Cherche dans les deux listes : Doc doit s'allumer dans le cluster
  // communauté même s'il ne fait plus partie du sélecteur segmenté.
  const hit = SHELL_DESTS.concat(SHELL_COMMUNITY).find((d) => d.href === p);
  return hit ? hit.id : null;
}

function shellRender(el, current){
  const lang = SHELL_LANG === "en" ? "en" : "fr";
  const link = (d) => {
    const on = d.id === current ? ' aria-current="page"' : "";
    return `<a href="${d.href}"${on} data-shell="${d.id}">${d[lang]}</a>`;
  };
  el.className = "appnav";
  el.innerHTML = `<div class="appnav-seg">${shellVisible().map(link).join("")}</div>`;
}

function shellCommunityRender(el, current){
  const lang = SHELL_LANG === "en" ? "en" : "fr";
  const parts = SHELL_COMMUNITY.map((d) => {
    if (d.external){
      return `<a class="${d.cls}" target="_blank" rel="noopener" href="${d.href}" data-shell="${d.id}">` +
             `<span class="dot" aria-hidden="true"></span>${d[lang]}</a>`;
    }
    // Doc : seul lien interne du cluster, seul à porter l'état actif.
    const on = d.id === current ? ' aria-current="page"' : "";
    return `<span class="cdiv" aria-hidden="true"></span>` +
           `<a class="doc"${on} href="${d.href}" data-shell="${d.id}">${d[lang]}</a>`;
  });
  el.className = "appcommunity";
  el.innerHTML = parts.join("");
}

/* Restructure .appheader-bar en tiroir mobile : deplace tout ce qui suit
   la nav (communaute, langue) dans un wrapper .appheader-more precede
   d'une case a cocher + un bouton "...", cf. bloc CSS ci-dessus. Sans
   effet sur le terminal (index.html), dont le <nav> vit dans .hdr-bar,
   pas .appheader-bar -- closest() n'y trouve rien et sort aussitot.
   navEl est deja monte (id stable) quand cette fonction s'execute : le
   deplacer dans le DOM ne casse aucun getElementById ulterieur. */
let shellMoreSeq = 0;
function shellEnsureMoreDrawer(navEl){
  const bar = navEl.closest(".appheader-bar");
  if (!bar || bar._shellMoreDone) return;
  bar._shellMoreDone = true;
  const rest = Array.from(bar.children).filter((c) => c !== navEl);
  if (!rest.length) return;
  const chk = document.createElement("input");
  chk.type = "checkbox";
  chk.className = "appheader-more-chk";
  chk.id = "apMoreChk" + (shellMoreSeq++);
  const label = document.createElement("label");
  label.setAttribute("for", chk.id);
  label.className = "appheader-more-btn";
  label.setAttribute("aria-label", "Plus d'options");
  label.textContent = "⋯";
  const wrap = document.createElement("div");
  wrap.className = "appheader-more";
  rest.forEach((el) => wrap.appendChild(el));
  bar.appendChild(chk);
  bar.appendChild(label);
  bar.appendChild(wrap);
}

/* /api/links est public, léger et mis en cache 60s côté edge (cf.
   api/gex.py) : un fetch partagé entre toutes les pages qui montent le
   cluster ne coûte donc rien de plus qu'un seul appel index.html en
   faisait déjà. La promesse est mémoïsée pour ne jamais tirer deux fois. */
let SHELL_LINKS_PROMISE = null;
function shellApplyLinkOverrides(){
  if (!SHELL_LINKS_PROMISE){
    SHELL_LINKS_PROMISE = fetch("/api/links")
      .then((r) => (r.ok ? r.json() : {}))
      .catch(() => ({}));
  }
  SHELL_LINKS_PROMISE.then((d) => {
    if (!d) return;
    SHELL_COMM_EL.forEach((el) => {
      if (d.discord){ const a = el.querySelector("a.dc"); if (a) a.href = d.discord; }
      if (d.tradingview){ const a = el.querySelector("a.tv"); if (a) a.href = d.tradingview; }
    });
  });
}

/* Bascules de langue montées, et rappels enregistrés par les pages. */
const SHELL_LANG_EL = [];
const SHELL_LANG_CB = [];
/* Clusters communauté montés (redessinés au changement de langue, pour
   que le libellé de Doc suive Doc/Docs comme dans le sélecteur). */
const SHELL_COMM_EL = [];

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
    shellEnsureMoreDrawer(el);
    if (!Shell._mounted.includes(el)) Shell._mounted.push(el);
    return el;
  },
  /* Monte le cluster Discord / TradingView / Doc. Sur les 8 pages qui ne
     rendaient jusqu'ici que nav + langue, l'ajouter donne à chacune le
     même accès aux mêmes ressources que le terminal — c'est ce qui
     manquait pour que Doc ait un endroit cohérent où vivre partout,
     plutôt qu'une exception propre à une seule page.

     Applique aussi les surcharges d'URL éventuelles (/api/links,
     configurables depuis l'admin) : avant, seul index.html les lisait
     via sa propre loadLinks() qui écrivait sur des ids fixes
     #dcLink/#tvLink. Ces ids ont disparu avec le markup codé en dur —
     shell.js reprend cette responsabilité pour TOUTES les pages qui
     montent le cluster, pas seulement le terminal, avec un seul fetch
     partagé (mise en cache de la promesse) plutôt qu'un par page. */
  mountCommunity(el, current){
    if (typeof el === "string") el = document.getElementById(el);
    if (!el) return null;
    shellCss();
    el._shellCurrent = current || shellCurrent();
    shellCommunityRender(el, el._shellCurrent);
    if (!SHELL_COMM_EL.includes(el)) SHELL_COMM_EL.push(el);
    shellApplyLinkOverrides();
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
    SHELL_COMM_EL.forEach((el) => shellCommunityRender(el, el._shellCurrent));
    // shellCommunityRender vient de repartir des URL par défaut : la
    // promesse est déjà résolue à ce stade (sauf tout premier appel),
    // donc réappliquer les surcharges ici ne coûte rien et évite qu'un
    // changement de langue efface une URL personnalisée.
    if (SHELL_COMM_EL.length) shellApplyLinkOverrides();
  },
  _mounted: [],
};

window.Shell = Shell;
