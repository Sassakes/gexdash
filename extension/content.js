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

// Le DOM réel de la légende TradingView n'expose aucun data-name stable
// (relevé en prod : <div class="withCustomTextColor-quatTGAC item-quatTGAC
// study-quatTGAC has5Buttons-quatTGAC">GEX Daily Levels</div>). Le suffixe
// "-quatTGAC" est un hash de build qui change à chaque déploiement — cf.
// règle générale dans README.md. On ne cible donc que des PRÉFIXES de
// classe, en cascade du plus précis au plus large.
function findIndicatorLegendItem(name) {
  const pattern = buildNamePattern(name);
  const exactPattern = new RegExp("^" + pattern.source + "$", "i");
  const seen = [];

  // Stratégie 1 : élément dont une classe commence par "study-".
  const studyEls = document.querySelectorAll('[class*="study-"]');
  for (const el of studyEls) {
    const text = (el.textContent || "").trim();
    if (text) seen.push(text);
    if (pattern.test(text)) {
      log('indicateur trouvé via [class*="study-"] :', JSON.stringify(text));
      return el;
    }
  }

  // Stratégie 2 : élément dont une classe commence par "item-".
  const itemEls = document.querySelectorAll('[class*="item-"]');
  for (const el of itemEls) {
    const text = (el.textContent || "").trim();
    if (text) seen.push(text);
    if (pattern.test(text)) {
      log('indicateur trouvé via [class*="item-"] :', JSON.stringify(text));
      return el;
    }
  }

  // Stratégie 3 (repli le plus large) : tout élément sans enfant dont le
  // texte correspond exactement au nom configuré.
  const leafCandidates = document.querySelectorAll("div, span");
  for (const el of leafCandidates) {
    if (el.children.length > 0) continue;
    const text = (el.textContent || "").trim();
    if (!text) continue;
    if (exactPattern.test(text)) {
      log("indicateur trouvé via repli (élément feuille, texte exact) :", JSON.stringify(text));
      return el;
    }
  }

  log(
    "indicateur introuvable. Éléments candidats vus sur ce graphique :",
    seen.length ? seen.slice(0, 30) : "(aucun élément candidat détecté — le graphique a-t-il fini de charger ?)"
  );
  return null;
}

async function openSettingsDialog(item) {
  // 1) Geste utilisateur habituel pour ouvrir les paramètres d'un
  //    indicateur : double-clic sur la légende. Testé en premier.
  log("ouverture des paramètres — tentative : double-clic sur la légende");
  item.dispatchEvent(new MouseEvent("dblclick", { bubbles: true, cancelable: true }));
  let dialog = await waitForDialog(1500, 100);
  if (dialog) return dialog;

  // 2) Repli : bouton d'options révélé au survol. La classe "has5Buttons"
  //    vue sur l'élément de légende suggère des boutons d'action qui
  //    n'apparaissent dans le DOM (ou ne deviennent cliquables) qu'après un
  //    mouseover.
  log("double-clic sans résultat, repli sur le bouton d'options au survol");
  item.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
  item.dispatchEvent(new MouseEvent("mouseenter", { bubbles: true }));
  await sleep(150);

  const gearSelectors = [
    '[data-name="legend-settings-action"]',
    '[data-name="legend-more-action-settings"]',
    'button[aria-label="Settings"]',
    'button[data-tooltip="Settings"]',
    '[data-name*="settings" i]',
    '[class*="settings"]',
  ];
  const searchRoots = [item, item.parentElement, item.closest('[class*="item-"]')].filter(Boolean);
  let clicked = false;
  for (const root of searchRoots) {
    for (const sel of gearSelectors) {
      const btn = root.querySelector(sel);
      if (btn) {
        log("clic sur le bouton de réglages via le sélecteur", sel);
        btn.click();
        clicked = true;
        break;
      }
    }
    if (clicked) break;
  }
  if (!clicked) log("aucune icône de réglages trouvée au survol");

  dialog = await waitForDialog();
  return dialog;
}

async function waitForDialog(timeoutMs = 4000, stepMs = 150) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const dialog =
      document.querySelector('[data-name="indicator-properties-dialog"]') ||
      Array.from(document.querySelectorAll('div[role="dialog"]')).find(
        (d) => d.querySelector('[role="tab"]') || Array.from(d.querySelectorAll("input")).some((el) => el.type === "text")
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

// TradingView ne pose pas type="text" en dur dans le markup (valeur par
// défaut implicite du navigateur). Le sélecteur CSS input[type="text"] ne
// matche que l'attribut HTML littéral -> 0 résultat même si le champ est
// bien un champ texte. Il faut filtrer sur la propriété IDL `el.type`
// (normalisée à "text" par défaut), pas sur getAttribute/le sélecteur CSS.
function textInputs(dialog) {
  return Array.from(dialog.querySelectorAll("input")).filter((el) => el.type === "text");
}

async function ensureInputsTabWithTextFields(dialog) {
  if (textInputs(dialog).length >= 5) {
    log("onglet Inputs déjà actif (5+ champs texte visibles sans clic d'onglet)");
    return true;
  }
  const tabs = dialog.querySelectorAll('[role="tab"]');
  log("onglet Inputs non actif par défaut, essai de", tabs.length, "onglet(s)");
  for (const tab of tabs) {
    tab.click();
    await sleep(150);
    const n = textInputs(dialog).length;
    if (n >= 5) {
      log("onglet Inputs trouvé :", (tab.textContent || "").trim() || "(sans libellé)");
      return true;
    }
  }
  log("aucun onglet ne révèle 5 champs texte");
  return false;
}

// Repérage vérifié en prod (13/08/2026) : sur les ~40 champs de la fenêtre
// de paramètres, les 5 champs de niveaux sont les 5 PREMIERS input[type=text]
// dans l'ordre de déclaration du script Pine (NQ, ES, SPX, GOLD GC, GOLD
// XAUUSD). Ce ne sont PAS des textarea. Tous les autres inputs texte portent
// une valeur numérique courte (paramètres de config) — cf. isSafeLevelsField
// pour la garde qui empêche d'écraser l'un d'eux par erreur si TradingView
// change un jour cet ordre.
function pickLevelFields(dialog) {
  const inputs = textInputs(dialog);
  log("champs texte détectés dans l'onglet Inputs :", inputs.length);
  return inputs.slice(0, 5);
}

// Une string de niveaux Pine contient toujours virgules (séparateur de
// valeurs) et points-virgules (séparateur de lignes) ; une valeur de
// configuration (0, 1, 2, 8, ...) n'en contient jamais. Champ vide accepté
// (première utilisation de l'indicateur).
function isSafeLevelsField(el) {
  const v = (el.value || "").trim();
  if (v === "") return true;
  return v.includes(",") && v.includes(";");
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

    const dialog = await openSettingsDialog(item);
    if (!dialog) return fail("Fenêtre de paramètres introuvable");

    const tabOk = await ensureInputsTabWithTextFields(dialog);
    if (!tabOk) return fail("Onglet Inputs introuvable ou vide");

    const fields = pickLevelFields(dialog);
    if (fields.length < 5) return fail("Moins de 5 champs texte trouvés dans l'onglet Inputs (" + fields.length + ")");

    const order = ["NQ", "ES", "SPX", "GC", "XAU"];
    const mapping = {};
    order.forEach((k, i) => (mapping[k] = fields[i]));

    const unsafe = order.filter((k) => !isSafeLevelsField(mapping[k]));
    if (unsafe.length) {
      log(
        "champ(s) jugé(s) dangereux à écraser (valeur numérique de config, pas une string de niveaux) :",
        unsafe.map((k) => k + "=" + JSON.stringify(mapping[k].value)).join(", ")
      );
      return fail("Champ(s) de configuration détecté(s) à la place d'un champ de niveaux : " + unsafe.join(", "));
    }

    const mismatched = [];
    for (const k of order) {
      const pine = (levels[k] && levels[k].pine) || "";
      if (!pine) continue; // rien de publié pour ce marché : ne pas écraser l'existant
      setNativeValue(mapping[k], pine);
      await sleep(30);
      const ok = mapping[k].value.trim() === pine.trim();
      log("champ", k, "— valeur écrite :", JSON.stringify(pine), "— vérification :", ok ? "OK" : "ÉCHEC");
      if (!ok) mismatched.push(k);
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
