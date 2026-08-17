/* GEX Terminal — accès JavaScript aux tokens de theme.css.
   ────────────────────────────────────────────────────────
   var(--x) ne résout pas dans une string JavaScript : une couleur
   passée à lightweight-charts, à un ctx.fillStyle de canvas ou à un
   style inline construit en JS doit être une valeur littérale. Les
   pages recopiaient donc les hex à la main — 91 occurrences réparties
   sur 8 pages, dont 74 étaient des doublons exacts de tokens déjà
   déclarés en CSS. Résultat : changer --gold mettait à jour le CSS et
   laissait les graphiques sur l'ancien or.

   window.THEME lit les tokens depuis :root, donc theme.css reste la
   seule source de vérité, y compris pour le JS.

     ctx.fillStyle = THEME.gold;                    // et non "#F0B90B"
     chart.applyOptions({upColor: THEME.teal});

   Lecture PARESSEUSE et mémoïsée : la valeur est résolue au premier
   accès, pas au chargement. Le script peut donc être placé avant ou
   après <link rel="stylesheet" href="/theme.css"> sans que l'ordre
   change le résultat — au chargement, le token n'est pas encore
   forcément appliqué.

   Chaque token porte un repli codé en dur, identique à theme.css :
   si la feuille ne charge pas, les graphiques gardent la bonne teinte
   au lieu de tomber sur du transparent (une string vide passée à un
   canvas est ignorée silencieusement et dessine en noir). */
"use strict";

/* Replis — DOIVENT rester synchronisés avec :root dans theme.css.
   Clés en camelCase, converties en --kebab-case à la lecture. */
const THEME_FALLBACK = {
  bg:            "#0A0A0C",
  surface:       "#111114",
  surface2:      "#16161A",
  surface3:      "#1B1B20",
  cardTop:       "#141419",
  cardBottom:    "#101014",

  line:          "#212127",
  lineStrong:    "#2B2B33",
  lineHi:        "#33333D",
  scrollThumb:   "#26262E",

  text:          "#ECEAE4",
  muted:         "#8A8A94",
  faint:         "#5C5C66",

  gold:          "#F0B90B",
  goldHi:        "#FFD23F",
  goldPale:      "#FFE9A3",
  goldDim:       "#B9903A",
  goldSoft:      "rgba(240,185,11,.13)",
  goldGlow:      "rgba(240,185,11,.32)",

  teal:          "#26A69A",
  tealSoft:      "rgba(38,166,154,.14)",
  red:           "#EF5350",
  redSoft:       "rgba(239,83,80,.12)",

  blue:          "#5B8DEF",
  violet:        "#8B5CF6",
  orange:        "#F59E0B",
  yellow:        "#EAC54F",

  onTealDark:    "#05140F",
  onTealLight:   "#7FCDBF",
  onRedDark:     "#1C0707",
  onRedLight:    "#E6A6A4",

  mono:          '"IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, monospace',
  sans:          '"Space Grotesk", "IBM Plex Mono", ui-monospace, monospace',
};

function themeCssName(key){
  return "--" + key.replace(/[A-Z]/g, (c) => "-" + c.toLowerCase());
}

const THEME_CACHE = {};

function themeRead(key){
  if (key in THEME_CACHE) return THEME_CACHE[key];
  let v = "";
  try{
    v = getComputedStyle(document.documentElement)
          .getPropertyValue(themeCssName(key)).trim();
  }catch(_){}
  /* Un token absent renvoie "" : on retombe sur le repli plutôt que
     de propager une string vide, qu'un canvas dessinerait en noir. */
  THEME_CACHE[key] = v || THEME_FALLBACK[key] || "";
  return THEME_CACHE[key];
}

const THEME = {};
for (const key of Object.keys(THEME_FALLBACK)){
  Object.defineProperty(THEME, key, {get: () => themeRead(key), enumerable: true});
}

/* Pendant JS des canaux --*-rgb de theme.css : construit une couleur
   translucide sans recopier les composantes.

     ctx.strokeStyle = THEME.alpha("gold", .28);   // rgba(240,185,11,.28)

   Indispensable côté canvas, où l'alpha est réglé par valeur et non
   par une classe CSS : sans ce pendant, les composantes de l'or
   resteraient recopiées dans le JS de dessin et la refonte
   s'arrêterait au bord du graphique. */
Object.defineProperty(THEME, "alpha", {
  value: function(key, a){
    let ch = "";
    try{
      ch = getComputedStyle(document.documentElement)
             .getPropertyValue("--" + key + "-rgb").trim();
    }catch(_){}
    if (!ch){
      /* Repli : on dérive les composantes du hex mémoïsé. */
      const hex = themeRead(key);
      const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
      if (!m) return hex;
      ch = [m[1], m[2], m[3]].map((h) => parseInt(h, 16)).join(",");
    }
    return "rgba(" + ch + "," + a + ")";
  },
  enumerable: false,
});

/* À appeler si :root change en cours de session (bascule de thème).
   Sans ce vidage, les valeurs mémoïsées resteraient sur l'ancien jeu. */
Object.defineProperty(THEME, "refresh", {
  value: function(){ for (const k in THEME_CACHE) delete THEME_CACHE[k]; },
  enumerable: false,
});

window.THEME = THEME;
