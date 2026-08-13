/*!
 * TheHub GEX Levels Autofill — service worker (MV3).
 * Poll /api/mylevels every 5 min, compare the lot hash, push the 5 Pine
 * strings to every TradingView tab's content script when it changes.
 */
"use strict";

const API_BASE = "https://gexdash.wealthbuilders.group";
const POLL_ALARM = "gex-mylevels-poll";
const POLL_MINUTES = 5;
const TV_URL_PATTERNS = ["*://*.tradingview.com/*"];
const DEFAULT_INDICATOR_NAME = "GEX Daily Levels";

// Backoff: doublement du délai après chaque échec réseau/429, plafonné à
// 60 min, remis à zéro au premier succès. L'alarme périodique reste à 5 min ;
// le backoff est appliqué en sautant les tirs tant que Date.now() < until.
const BACKOFF_BASE_MIN = 5;
const BACKOFF_CAP_MIN = 60;

async function getState() {
  const d = await chrome.storage.local.get([
    "apiKey", "indicatorName", "enabled", "lastHash", "lastLevels", "lastSync",
    "failCount", "backoffUntil", "lastNotifiedError",
  ]);
  return {
    apiKey: d.apiKey || "",
    indicatorName: d.indicatorName || DEFAULT_INDICATOR_NAME,
    enabled: d.enabled !== false, // défaut: actif
    lastHash: d.lastHash || null,
    lastLevels: d.lastLevels || null,
    lastSync: d.lastSync || null, // {ok, ts, error, code}
    failCount: d.failCount || 0,
    backoffUntil: d.backoffUntil || 0,
    lastNotifiedError: d.lastNotifiedError || null,
  };
}

async function setState(patch) {
  await chrome.storage.local.set(patch);
}

function errorLabel(status, err) {
  if (status === 401) return "Clé API invalide ou manquante";
  if (status === 403) return "Accès bloqué (clé désactivée ou révoquée)";
  if (status === 429) return "Trop de requêtes — patiente un peu";
  if (status === 0) return "Réseau injoignable (" + (err || "erreur") + ")";
  return err || ("Erreur " + status);
}

async function fetchLevels(key) {
  if (!key) return { ok: false, status: 401, error: "Aucune clé API enregistrée" };
  let res;
  try {
    res = await fetch(
      API_BASE + "/api/mylevels?key=" + encodeURIComponent(key),
      { cache: "no-store" }
    );
  } catch (e) {
    return { ok: false, status: 0, error: String(e && e.message || e) };
  }
  let body = null;
  try { body = await res.json(); } catch (e) { /* réponse non-JSON */ }
  if (!res.ok) {
    const msg = (body && body.error) || errorLabel(res.status);
    return { ok: false, status: res.status, error: msg };
  }
  return { ok: true, status: 200, levels: body.levels, hash: body.hash };
}

async function tvTabs() {
  return chrome.tabs.query({ url: TV_URL_PATTERNS });
}

async function broadcastLevels(levels) {
  const tabs = await tvTabs();
  if (!tabs.length) return { applied: 0, failed: 0, outcomes: [] };
  const outcomes = await Promise.all(
    tabs.map((tab) =>
      chrome.tabs
        .sendMessage(tab.id, { type: "GEX_LEVELS_UPDATE", levels })
        .catch((e) => ({ ok: false, error: String(e && e.message || e) }))
    )
  );
  const applied = outcomes.filter((o) => o && o.ok).length;
  return { applied, failed: outcomes.length - applied, outcomes };
}

const CDP_PROTOCOL_VERSION = "1.3";

function cdpAttach(tabId) {
  return new Promise((resolve, reject) => {
    chrome.debugger.attach({ tabId }, CDP_PROTOCOL_VERSION, () => {
      if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
      else resolve();
    });
  });
}

function cdpDetach(tabId) {
  return new Promise((resolve) => {
    chrome.debugger.detach({ tabId }, () => resolve());
  });
}

function cdpSendCommand(tabId, method, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand({ tabId }, method, params, (result) => {
      if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
      else resolve(result);
    });
  });
}

/**
 * Clic (ou double-clic) réellement "trusted" au niveau navigateur, via
 * Chrome DevTools Protocol. Nécessaire car TradingView ignore les
 * événements souris synthétiques (dispatchEvent/.click() depuis un content
 * script ont toujours isTrusted:false) pour ouvrir la fenêtre de paramètres
 * de l'indicateur — vérifié empiriquement, pas une hypothèse. attach/detach
 * sont brefs (une poignée de commandes) pour limiter le temps d'affichage
 * du bandeau Chrome "ce navigateur est en cours de débogage".
 */
async function cdpClickAt(tabId, x, y, double) {
  await cdpAttach(tabId);
  try {
    const dispatch = (type, clickCount) =>
      cdpSendCommand(tabId, "Input.dispatchMouseEvent", {
        type, x, y, button: "left", buttons: 1, clickCount,
      });
    await cdpSendCommand(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
    await dispatch("mousePressed", 1);
    await dispatch("mouseReleased", 1);
    if (double) {
      await dispatch("mousePressed", 2);
      await dispatch("mouseReleased", 2);
    }
  } finally {
    await cdpDetach(tabId);
  }
}

async function notify(id, title, message) {
  try {
    await chrome.notifications.create(id, {
      type: "basic",
      iconUrl: "https://gexdash.wealthbuilders.group/thehub-mark.png",
      title,
      message,
      priority: 2,
    });
  } catch (e) {
    // best-effort : une notification ratée ne doit jamais faire échouer le sync
  }
}

/**
 * @param {boolean} force  ignore le hash déjà connu et le backoff (sync manuel)
 */
async function runSync(force) {
  const st = await getState();

  if (!st.enabled && !force) {
    return { ok: false, skipped: true, reason: "disabled" };
  }
  if (!force && Date.now() < st.backoffUntil) {
    return { ok: false, skipped: true, reason: "backoff" };
  }

  const tabs = await tvTabs();
  if (!tabs.length && !force) {
    // Pas d'onglet TradingView ouvert : inutile d'interroger l'API.
    return { ok: false, skipped: true, reason: "no-tradingview-tab" };
  }

  const result = await fetchLevels(st.apiKey);

  if (!result.ok) {
    const failCount = st.failCount + 1;
    const backoffMin = Math.min(BACKOFF_BASE_MIN * Math.pow(2, failCount - 1), BACKOFF_CAP_MIN);
    await setState({
      failCount,
      backoffUntil: Date.now() + backoffMin * 60000,
      lastSync: { ok: false, ts: Date.now(), error: result.error, code: result.status },
    });
    // Une seule notification par type d'erreur tant qu'il persiste, pas une
    // toutes les 5 min ("ne jamais boucler sur une erreur").
    const label = errorLabel(result.status, result.error);
    if (label !== st.lastNotifiedError && (result.status === 401 || result.status === 403)) {
      await notify("gex-auth-error", "TheHub GEX Levels — " + label,
        "Ouvre la popup de l'extension pour vérifier ta clé API.");
      await setState({ lastNotifiedError: label });
    }
    return { ok: false, error: result.error, status: result.status };
  }

  await setState({ failCount: 0, backoffUntil: 0, lastNotifiedError: null });

  const unchanged = !force && st.lastHash === result.hash;
  await setState({
    lastHash: result.hash,
    lastLevels: result.levels,
    lastSync: { ok: true, ts: Date.now(), error: null, code: 200, unchanged },
  });

  if (unchanged) {
    return { ok: true, unchanged: true };
  }

  const bc = await broadcastLevels(result.levels);
  if (bc.applied === 0 && bc.failed > 0) {
    await notify("gex-fill-failed",
      "TheHub GEX Levels — remplissage impossible",
      "Les niveaux ont été copiés dans le presse-papiers sur l'onglet TradingView concerné. Colle-les dans les paramètres de l'indicateur (onglet Inputs).");
  }
  return { ok: true, unchanged: false, applied: bc.applied, failed: bc.failed };
}

async function ensureAlarm() {
  const st = await getState();
  if (st.enabled) {
    chrome.alarms.create(POLL_ALARM, { periodInMinutes: POLL_MINUTES });
  } else {
    chrome.alarms.clear(POLL_ALARM);
  }
}

// Les niveaux/pine ne changent réellement qu'aux publications QStash
// canoniques (00h11 et 15h25 UTC — cf. CLAUDE.md), pas en continu. Le
// sondage toutes les 5 min (POLL_ALARM) reste un filet de sécurité
// générique, mais n'est pas calé sur l'horloge serveur : pile après une
// publication, il peut laisser jusqu'à ~5 min de retard. Ces deux alarmes
// dédiées se recalent chaque jour sur l'horaire exact + une marge de
// propagation, pour un remplissage quasi immédiat après chaque publication
// réelle plutôt que d'attendre le prochain tick générique.
const ALIGN_ALARMS = [
  { name: "gex-align-0011", h: 0, m: 13 },   // 00h11 UTC + 2 min de marge
  { name: "gex-align-1525", h: 15, m: 27 },  // 15h25 UTC + 2 min de marge
];

function nextUtcOccurrence(hourUTC, minuteUTC) {
  const now = new Date();
  const next = new Date(Date.UTC(
    now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), hourUTC, minuteUTC, 0, 0
  ));
  if (next.getTime() <= Date.now()) next.setUTCDate(next.getUTCDate() + 1);
  return next.getTime();
}

async function ensureAlignedAlarms() {
  const st = await getState();
  for (const a of ALIGN_ALARMS) {
    if (st.enabled) {
      chrome.alarms.create(a.name, { when: nextUtcOccurrence(a.h, a.m), periodInMinutes: 24 * 60 });
    } else {
      chrome.alarms.clear(a.name);
    }
  }
}

async function ensureAllAlarms() {
  await ensureAlarm();
  await ensureAlignedAlarms();
}

// Au démarrage réel du navigateur (ou install/mise à jour de l'extension) —
// pas à chaque réveil du service worker MV3, qui peut être bien plus
// fréquent — on vérifie la fraîcheur tout de suite au lieu d'attendre le
// premier tick de l'alarme périodique (jusqu'à 5 min d'attente sinon).
chrome.runtime.onInstalled.addListener(async () => {
  await ensureAllAlarms();
  runSync(false);
});
chrome.runtime.onStartup.addListener(async () => {
  await ensureAllAlarms();
  runSync(false);
});
ensureAllAlarms();

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === POLL_ALARM || ALIGN_ALARMS.some((a) => a.name === alarm.name)) runSync(false);
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || !msg.type) return false;

  if (msg.type === "NOTIFY") {
    notify(msg.id || "gex-content-notify", msg.title, msg.message);
    return false;
  }

  if (msg.type === "CDP_CLICK") {
    const tabId = sender.tab && sender.tab.id;
    if (!tabId) {
      sendResponse({ ok: false, error: "id d'onglet manquant" });
      return false;
    }
    cdpClickAt(tabId, msg.x, msg.y, !!msg.double)
      .then(() => sendResponse({ ok: true }))
      .catch((e) => sendResponse({ ok: false, error: String((e && e.message) || e) }));
    return true;
  }

  if (msg.type === "TEST_KEY") {
    fetchLevels(msg.key).then((r) => sendResponse(r));
    return true;
  }

  if (msg.type === "SAVE_KEY") {
    setState({
      apiKey: msg.key,
      indicatorName: (msg.indicatorName && msg.indicatorName.trim()) || DEFAULT_INDICATOR_NAME,
      failCount: 0,
      backoffUntil: 0,
      lastNotifiedError: null,
    })
      .then(() => runSync(true))
      .then((r) => sendResponse(r));
    return true;
  }

  if (msg.type === "SET_ENABLED") {
    setState({ enabled: !!msg.enabled })
      .then(ensureAllAlarms)
      .then(() => (msg.enabled ? runSync(true) : Promise.resolve({ ok: true })))
      .then((r) => sendResponse(r));
    return true;
  }

  if (msg.type === "MANUAL_SYNC") {
    runSync(true).then((r) => sendResponse(r));
    return true;
  }

  if (msg.type === "GET_STATE") {
    getState().then((st) => sendResponse(st));
    return true;
  }

  return false;
});
