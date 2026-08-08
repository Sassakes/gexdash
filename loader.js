/* loader.js — overlay de marque unifié (anneau doré + logo TheHub), partagé
   par toutes les pages pour masquer les micro-temps de chargement (switch de
   marché, ouverture de page) au lieu de laisser voir un contenu vide/figé.
   Chargé par index.html, dash.html, heatmap.html, news.html. Même motif
   visuel que #fluxLoader (flux-panel.js), généralisé et sans le flag
   "une seule fois" : ici chaque appel a sa propre durée plancher. */
(function(){
  const MIN_MS = 140;   // sous ce seuil l'oeil perçoit un flash plutôt qu'un chargement --
                         // volontairement bas : le terminal sert des switches de marché
                         // fréquents et rapides, un plancher plus haut se sentirait comme
                         // du lag ajouté plutôt que comme une transition

  function injectCss(){
    if (document.getElementById("pageloader-css")) return;
    const style = document.createElement("style");
    style.id = "pageloader-css";
    style.textContent = `
.pageloader{position:absolute; inset:0; display:flex; align-items:center;
  justify-content:center; background:var(--bg,#0A0A0C); z-index:40;
  opacity:1; transition:opacity .18s ease}
.pageloader[hidden]{display:none}
.pageloader svg, .pageloader .pageloader-mark{position:absolute}
.pageloader-ring{width:52px; height:52px; animation:pageloader-spin 1.1s linear infinite}
.pageloader-mark{width:34px; height:34px; object-fit:contain; border-radius:50%}
@keyframes pageloader-spin{to{transform:rotate(360deg)}}
`;
    document.head.appendChild(style);
  }

  function markup(){
    return `<svg class="pageloader-ring" viewBox="0 0 60 60">
      <circle cx="30" cy="30" r="26" fill="none" stroke="rgba(240,185,11,.15)" stroke-width="3"/>
      <circle cx="30" cy="30" r="26" fill="none" stroke="#F0B90B" stroke-width="3"
              stroke-linecap="round" stroke-dasharray="42 200"/>
    </svg>
    <img class="pageloader-mark" src="/thehub-mark.png" alt="" aria-hidden="true">`;
  }

  // container doit être position:relative (ou absolute/fixed) — l'overlay
  // s'y ancre en position:absolute; inset:0 et le recouvre entièrement.
  function mount(container){
    injectCss();
    let el = container.querySelector(":scope > .pageloader");
    if (!el){
      el = document.createElement("div");
      el.className = "pageloader";
      el.hidden = true;   // masqué par défaut : si begin() a déjà tourné et que
                           // le conteneur a été vidé (innerHTML) entre-temps,
                           // end() recrée l'overlay ici -- il ne doit pas
                           // réapparaître une fraction de seconde après coup
      el.innerHTML = markup();
      container.appendChild(el);
    }
    return el;
  }

  // Affiche l'overlay et retourne un jeton d'horodatage à passer à end().
  function begin(container){
    const el = mount(container);
    el.hidden = false;
    el.style.opacity = "1";
    return performance.now();
  }

  // Masque l'overlay, en garantissant MIN_MS d'affichage total depuis begin()
  // pour éviter un flash sur les réponses très rapides (cache chaud, etc.).
  async function end(container, t0){
    const el = mount(container);
    const elapsed = performance.now() - t0;
    if (elapsed < MIN_MS) await new Promise(r => setTimeout(r, MIN_MS - elapsed));
    el.hidden = true;
  }

  // Sucre : enveloppe une opération async, garantit l'overlay pendant sa
  // durée (+ plancher), retourne son résultat (ou relance son erreur).
  async function run(container, fn){
    const t0 = begin(container);
    try{
      return await fn();
    } finally {
      await end(container, t0);
    }
  }

  window.PageLoader = {mount, begin, end, run, MIN_MS};
})();
