"use strict";

const T = {
  fr: {
    apiKeyLabel: "Clé API personnelle",
    indicatorNameLabel: "Nom de l'indicateur",
    test: "Tester la connexion",
    save: "Enregistrer",
    enabledLabel: "Synchronisation active",
    lastSyncLabel: "Dernière synchro",
    syncNowLabel: "Synchroniser maintenant",
    whereKeyLabel: "Où trouver ma clé ?",
    never: "jamais",
    testing: "Test en cours…",
    syncing: "Synchronisation…",
    testOk: "Connexion OK — clé valide",
    saved: "Clé enregistrée",
    err401: "Clé invalide ou manquante",
    err403: "Accès bloqué (clé désactivée ou révoquée)",
    err429: "Trop de requêtes — réessaie dans une minute",
    errNet: "Réseau injoignable",
    errGeneric: "Erreur",
    noKey: "Renseigne une clé d'abord",
    unchanged: "à jour (aucun changement)",
    applied: (n) => "appliqué sur " + n + " onglet(s) TradingView",
    fallback: "remplissage manuel requis — voir la notification",
    noTab: "aucun onglet TradingView ouvert",
  },
  en: {
    apiKeyLabel: "Personal API key",
    indicatorNameLabel: "Indicator name",
    test: "Test connection",
    save: "Save",
    enabledLabel: "Sync enabled",
    lastSyncLabel: "Last sync",
    syncNowLabel: "Sync now",
    whereKeyLabel: "Where's my key?",
    never: "never",
    testing: "Testing…",
    syncing: "Syncing…",
    testOk: "Connection OK — key valid",
    saved: "Key saved",
    err401: "Invalid or missing key",
    err403: "Access blocked (key disabled or revoked)",
    err429: "Too many requests — try again in a minute",
    errNet: "Network unreachable",
    errGeneric: "Error",
    noKey: "Enter a key first",
    unchanged: "up to date (no change)",
    applied: (n) => "applied to " + n + " TradingView tab(s)",
    fallback: "manual paste needed — see notification",
    noTab: "no TradingView tab open",
  },
};

const DEFAULT_INDICATOR_NAME = "GEX Daily Levels";

let lang = (navigator.language || "fr").toLowerCase().startsWith("fr") ? "fr" : "en";

function t(key) {
  return T[lang][key];
}

function paintStaticText() {
  document.querySelectorAll("[data-t]").forEach((el) => {
    const v = t(el.getAttribute("data-t"));
    if (typeof v === "string") el.textContent = v;
  });
  $("langBtn").textContent = lang === "fr" ? "EN" : "FR";
  $("apiKey").placeholder = lang === "fr" ? "colle ta clé ici" : "paste your key here";
}

function $(id) {
  return document.getElementById(id);
}

function errLabel(status, err) {
  if (status === 401) return t("err401");
  if (status === 403) return t("err403");
  if (status === 429) return t("err429");
  if (status === 0) return t("errNet");
  return err || t("errGeneric");
}

function showResult(ok, text) {
  const el = $("testResult");
  el.hidden = false;
  el.className = "result " + (ok ? "ok" : "err");
  el.textContent = text;
}

function fmtTs(ts) {
  if (!ts) return t("never");
  const d = new Date(ts);
  return d.toLocaleString(lang === "fr" ? "fr-FR" : "en-US", {
    hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit",
  });
}

function send(msg) {
  return chrome.runtime.sendMessage(msg);
}

function paintState(st) {
  $("enabledToggle").checked = st.enabled;
  const ls = st.lastSync;
  const errLine = $("lastSyncErr");
  if (!ls) {
    $("lastSyncVal").textContent = t("never");
    errLine.hidden = true;
  } else {
    $("lastSyncVal").textContent = fmtTs(ls.ts);
    if (ls.ok) {
      errLine.hidden = true;
    } else {
      errLine.hidden = false;
      errLine.textContent = errLabel(ls.code, ls.error);
    }
  }
}

async function loadState() {
  const st = await send({ type: "GET_STATE" });
  $("apiKey").value = st.apiKey || "";
  $("indicatorName").value = st.indicatorName || DEFAULT_INDICATOR_NAME;
  paintState(st);
}

$("langBtn").addEventListener("click", () => {
  lang = lang === "fr" ? "en" : "fr";
  paintStaticText();
});

$("toggleShow").addEventListener("click", () => {
  const el = $("apiKey");
  el.type = el.type === "password" ? "text" : "password";
});

$("testBtn").addEventListener("click", async () => {
  const key = $("apiKey").value.trim();
  if (!key) return showResult(false, t("noKey"));
  $("testBtn").disabled = true;
  showResult(true, t("testing"));
  const r = await send({ type: "TEST_KEY", key });
  $("testBtn").disabled = false;
  if (r.ok) showResult(true, t("testOk"));
  else showResult(false, errLabel(r.status, r.error));
});

$("saveBtn").addEventListener("click", async () => {
  const key = $("apiKey").value.trim();
  if (!key) return showResult(false, t("noKey"));
  const indicatorName = $("indicatorName").value.trim() || DEFAULT_INDICATOR_NAME;
  $("saveBtn").disabled = true;
  showResult(true, t("syncing"));
  const r = await send({ type: "SAVE_KEY", key, indicatorName });
  $("saveBtn").disabled = false;
  if (r.ok) {
    showResult(true, t("saved") + (r.unchanged ? " — " + t("unchanged") : ""));
  } else if (r.skipped) {
    showResult(true, t("saved") + " — " + t("noTab"));
  } else {
    showResult(false, errLabel(r.status, r.error));
  }
  loadState();
});

$("enabledToggle").addEventListener("change", async (e) => {
  await send({ type: "SET_ENABLED", enabled: e.target.checked });
  loadState();
});

$("syncBtn").addEventListener("click", async () => {
  $("syncBtn").disabled = true;
  showResult(true, t("syncing"));
  const r = await send({ type: "MANUAL_SYNC" });
  $("syncBtn").disabled = false;
  if (r.skipped) showResult(false, t("noTab"));
  else if (!r.ok) showResult(false, errLabel(r.status, r.error));
  else if (r.unchanged) showResult(true, t("unchanged"));
  else if (r.failed > 0 && r.applied === 0) showResult(false, t("fallback"));
  else showResult(true, t("applied")(r.applied || 0));
  loadState();
});

paintStaticText();
loadState();
