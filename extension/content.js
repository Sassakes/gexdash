/*!
 * TheHub GEX Levels Autofill — content script (tradingview.com).
 *
 * Remplit les 5 champs texte de l'indicateur "GEX Daily Levels" (onglet
 * Inputs : NQ / ES / SPX / GOLD GC / GOLD XAUUSD — levels string) avec les
 * strings Pine reçues du service worker.
 *
 * Fragilité assumée : TradingView n'expose aucune API publique pour piloter
 * ses dialogues, et ses classes CSS sont générées/obfusquées à chaque build.
 * Chaque étape (ouvrir les paramètres, trouver l'onglet Inputs, retrouver
 * chaque champ, valider) tente plusieurs heuristiques dans l'ordre et
 * n'échoue jamais en silence : au premier maillon cassé, tout part sur le
 * repli presse-papiers + notification (cf. fallbackClipboard). C'est la
 * partie la plus susceptible de casser à une future mise à jour TradingView
 * — si le remplissage automatique s'arrête un jour, commencer par revérifier
 * les sélecteurs ci-dessous dans les devtools. Chaque étape logge sous le
 * préfixe [GEX] dans la console de la page — c'est la première chose à
 * regarder en cas d'échec.
 */
"use strict";

const DEFAULT_INDICATOR_NAME = "GEX Daily Levels";

function log(...args) {
  console.log("[GEX]", ...args);
}

log("script injecté sur", location.hostname);

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Construit un pattern tolérant aux espaces multiples/variables à partir du
// nom configuré par l'utilisateur (ex. "GEX  Daily Levels" == "GEX Daily Levels").
function buildNamePattern(name) {
  const escaped = name.trim().split(/\s+/).map(escapeRegExp).join("\\s*");
  return new RegExp(escaped, "i");
}

async function getIndicatorName() {
  try {
    const { indicatorName } = await chrome.storage.local.get(["indicatorName"]);
    const name = (indicatorName && indicatorName.trim()) || DEFAULT_INDICATOR_NAME;
    log("nom d'indicateur configuré :", JSON.stringify(name));
    return name;
  } catch (e) {
    log("lecture du nom d'indicateur impossible, repli sur la valeur par défaut :", String(e));
    return DEFAULT_INDICATOR_NAME;
  }
}

function findIndicatorLegendItem(name) {
  const items = document.querySelectorAll('[data-name="legend-source-item"]');
  const pattern = buildNamePattern(name);
  const seen = [];
  for (const item of items) {
    const text = (item.textContent || "").trim();
    seen.push(text);
    if (pattern.test(text)) {
      log("indicateur trouvé (correspondance exacte sur le nom configuré) :", JSON.stringify(text));
      return item;
    }
  }
  for (const item of items) {
    const text = (item.textContent || "").trim();
    if (/GEX/i.test(text)) {
      log("nom exact non trouvé, repli sur le premier indicateur contenant 'GEX' :", JSON.stringify(text));
      return item;
    }
  }
  log(
    "indicateur introuvable. Éléments de légende vus sur ce graphique :",
    seen.length ? seen : "(aucun élément de légende détecté — le graphique a-t-il fini de charger ?)"
  );
  return null;
}

async function openSettingsDialog(item) {
  // Les icônes d'action (dont l'engrenage "Settings") ne sont souvent
  // présentes/actives dans le DOM qu'après un survol du legend item.
  item.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
  item.dispatchEvent(new MouseEvent("mouseenter", { bubbles: true }));
  await sleep(120);

  const gearSelectors = [
    '[data-name="legend-settings-action"]',
    '[data-name="legend-more-action-settings"]',
    'button[aria-label="Settings"]',
    'button[data-tooltip="Settings"]',
    '[data-name*="settings" i]',
  ];
  for (const sel of gearSelectors) {
    const btn = item.querySelector(sel);
    if (btn) {
      log("paramètres ouverts via le sélecteur", sel);
      btn.click();
      return;
    }
  }

  // Repli : le double-clic sur le titre de l'indicateur ouvre aussi ses
  // paramètres — pas de vérification possible ici, waitForDialog() tranche.
  log("aucune icône réglages trouvée, repli sur double-clic du titre de l'indicateur");
  const title = item.querySelector('[data-name="legend-source-title"]') || item;
  title.dispatchEvent(new MouseEvent("dblclick", { bubbles: true, cancelable: true }));
}

async function waitForDialog(timeoutMs = 4000, stepMs = 150) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const dialog =
      document.querySelector('[data-name="indicator-properties-dialog"]') ||
      Array.from(document.querySelectorAll('div[role="dialog"]')).find(
        (d) => d.querySelector('textarea, [role="tab"]')
      );
    if (dialog) {
      log("fenêtre de paramètres détectée après", Date.now() - start, "ms");
      return dialog;
    }
    await sleep(stepMs);
  }
  log("fenêtre de paramètres non détectée après", timeoutMs, "ms");
  return null;
}

async function ensureInputsTabWithTextareas(dialog) {
  if (dialog.querySelectorAll("textarea").length >= 5) {
    log("onglet Inputs déjà actif (5+ zones de texte visibles sans clic d'onglet)");
    return true;
  }
  const tabs = dialog.querySelectorAll('[role="tab"]');
  log("onglet Inputs non actif par défaut, essai de", tabs.length, "onglet(s)");
  for (const tab of tabs) {
    tab.click();
    await sleep(150);
    const n = dialog.querySelectorAll("textarea").length;
    if (n >= 5) {
      log("onglet Inputs trouvé :", (tab.textContent || "").trim() || "(sans libellé)");
      return true;
    }
  }
  log("aucun onglet ne révèle 5 zones de texte");
  return false;
}

function nearbyLabelText(el) {
  let node = el;
  for (let i = 0; i < 6 && node && node.parentElement; i++, node = node.parentElement) {
    const container = node.parentElement;
    const clone = container.cloneNode(true);
    clone.querySelectorAll("textarea, input, select, button").forEach((n) => n.remove());
    const text = clone.textContent.replace(/\s+/g, " ").trim();
    if (text.length > 3) return text;
  }
  return "";
}

function mapFieldsToTextareas(areas) {
  // GOLD GC / GOLD XAUUSD vérifiés avant NQ/ES/SPX pour ne jamais laisser un
  // pattern générique capturer le mauvais champ.
  const patterns = {
    GC: /GOLD\s*GC/i,
    XAU: /GOLD\s*XAUUSD/i,
    NQ: /\bNQ\b/i,
    ES: /\bES\b/i,
    SPX: /\bSPX\b/i,
  };
  const out = {};
  for (const el of areas) {
    const label = nearbyLabelText(el);
    for (const key of ["GC", "XAU", "NQ", "ES", "SPX"]) {
      if (!out[key] && patterns[key].test(label)) {
        out[key] = el;
        log("champ associé :", key, "<-", JSON.stringify(label));
        break;
      }
    }
  }
  return out;
}

function findSubmitButton(dialog) {
  const named = dialog.querySelector('[data-name="submit-button"]');
  if (named) return named;
  const buttons = Array.from(dialog.querySelectorAll("button")).filter(
    (b) => b.offsetParent !== null
  );
  if (!buttons.length) return null;
  // Repli : dans les dialogues TradingView, l'action primaire ("Ok") est
  // conventionnellement le dernier bouton visible du pied de la boîte.
  return buttons[buttons.length - 1];
}

function setNativeValue(el, value) {
  el.focus();
  try {
    el.setSelectionRange(0, el.value.length);
  } catch (e) {
    /* certains inputs ne supportent pas setSelectionRange */
  }
  const inserted = document.execCommand("insertText", false, value);
  if (!inserted || el.value !== value) {
    // Repli : composant contrôlé (React) — assigner .value directement ne
    // déclenche pas son onChange interne, il faut passer par le setter natif
    // du prototype puis dispatcher les événements nous-mêmes.
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value");
    if (setter && setter.set) setter.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
  el.dispatchEvent(new Event("change", { bubbles: true }));
  el.blur();
}

function levelsToText(levels) {
  const order = [
    ["NQ", "NQ"],
    ["ES", "ES"],
    ["SPX", "SPX"],
    ["GC", "GOLD GC"],
    ["XAU", "GOLD XAUUSD"],
  ];
  return order
    .map(([k, label]) => label + " — levels string:\n" + ((levels[k] && levels[k].pine) || "(vide)"))
    .join("\n\n");
}

async function fallbackClipboard(levels, reason) {
  log("repli presse-papiers — raison :", reason);
  const text = levelsToText(levels);
  let copied = false;
  try {
    await navigator.clipboard.writeText(text);
    copied = true;
  } catch (e) {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      copied = document.execCommand("copy");
      ta.remove();
    } catch (e2) {
      copied = false;
    }
  }
  log(copied ? "niveaux copiés dans le presse-papiers" : "copie presse-papiers également échouée");
  chrome.runtime.sendMessage({
    type: "NOTIFY",
    id: "gex-fallback-" + Date.now(),
    title: copied
      ? "TheHub GEX Levels — remplissage manuel requis"
      : "TheHub GEX Levels — échec du remplissage",
    message:
      (copied ? "Niveaux copiés dans le presse-papiers. " : "Copie automatique impossible aussi. ") +
      "Ouvre les paramètres de l'indicateur (onglet Inputs) et colle-les toi-même. Raison : " + reason,
  });
}

async function applyLevels(levels) {
  const fail = (reason) => {
    log("échec :", reason);
    fallbackClipboard(levels, reason);
    return { ok: false, error: reason, fallback: true };
  };

  try {
    const indicatorName = await getIndicatorName();

    const item = findIndicatorLegendItem(indicatorName);
    if (!item) return fail("Indicateur « " + indicatorName + " » introuvable sur ce graphique");

    await openSettingsDialog(item);
    const dialog = await waitForDialog();
    if (!dialog) return fail("Fenêtre de paramètres introuvable");

    const tabOk = await ensureInputsTabWithTextareas(dialog);
    if (!tabOk) return fail("Onglet Inputs introuvable ou vide");

    const areas = Array.from(dialog.querySelectorAll("textarea"));
    log("zones de texte détectées dans l'onglet Inputs :", areas.length);
    const mapping = mapFieldsToTextareas(areas);
    const order = ["NQ", "ES", "SPX", "GC", "XAU"];

    if (order.some((k) => !mapping[k]) && areas.length === 5) {
      // Repli : correspondance par libellé incomplète mais exactement 5
      // champs trouvés -> on suppose l'ordre de déclaration du script Pine
      // (NQ, ES, SPX, GOLD GC, GOLD XAUUSD), inchangé depuis sa création.
      log("association par libellé incomplète, repli sur l'ordre de déclaration du script Pine");
      order.forEach((k, i) => {
        mapping[k] = mapping[k] || areas[i];
      });
    }
    const missing = order.filter((k) => !mapping[k]);
    if (missing.length) return fail("Champs introuvables dans l'indicateur : " + missing.join(", "));

    const mismatched = [];
    for (const k of order) {
      const pine = (levels[k] && levels[k].pine) || "";
      if (!pine) continue; // rien de publié pour ce marché : ne pas écraser l'existant
      setNativeValue(mapping[k], pine);
      await sleep(30);
      if (mapping[k].value.trim() !== pine.trim()) mismatched.push(k);
    }
    if (mismatched.length) return fail("Valeur non retenue par l'indicateur pour : " + mismatched.join(", "));

    const submit = findSubmitButton(dialog);
    if (!submit) return fail("Bouton de validation introuvable dans la fenêtre de paramètres");
    log("validation — clic sur le bouton :", (submit.textContent || "").trim() || "(sans libellé)");
    submit.click();

    log("remplissage terminé avec succès, champs mis à jour :", order.join(", "));
    return { ok: true, filled: order.length };
  } catch (e) {
    return fail("Erreur inattendue : " + String((e && e.message) || e));
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "GEX_LEVELS_UPDATE") {
    log("message GEX_LEVELS_UPDATE reçu du service worker");
    applyLevels(msg.levels).then(sendResponse);
    return true; // réponse asynchrone
  }
  return false;
});
