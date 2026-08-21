"""Vercel Python entrypoint (app mode): routes ALL requests.

Vercel's new Python runtime loads this handler as the whole application
(pyproject.toml -> [tool.vercel] entrypoint = "api.gex:handler"), so every
path lands here. We therefore route explicitly:

  /                 -> index.html            (the dashboard)
  /index.html       -> index.html
  /nq_levels.json   -> daily snapshot        (committed by GitHub Actions)
  /history.json     -> rolling history       (committed by GitHub Actions)
  /nq_levels.txt    -> Pine string
  /api/gex          -> LIVE recompute (CBOE + basis), query params below
  anything else     -> 404

/api/gex query params:
  ?basis=145.5   manual NQ-NDX basis override (skips Yahoo)
  ?symbol=_NDX   _NDX (default) or QQQ
  ?n=10          number of nearest expiries (1-16)
"""

import hmac
import datetime as dt
import json
import math
import base64
import secrets
import hashlib
import mimetypes
import os
import time
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from api._gex_core import (TARGETS, build_payload, discord_news,
                           discord_notify, discord_send, et_today,
                           fetch_webhooks, kv_get, kv_set, kv_set_nx, kv_del,
                           refresh_daily_anchor, save_webhooks, parse_chain, per_strike_gex, fetch_cboe, atm_iv, build_pine, yahoo_spot, _stooq_spot, _goldapi_spot,
                           flow_gamma_matrix, flow_gamma_sanity, flow_volume_context,
                           horizon_envelope, horizon_hours,
                           fetch_embed_keys, save_embed_keys,
                           fetch_api_keys, save_api_keys)

CRON_LOG_KEY = "gex:cron:log"
# Journal SEPARE pour les tirs intrajournaliers (?intraday=1) : cadence 5-10
# min sur toute la seance (cf. vercel.json), le plus gros volume de hits cron
# de loin -- les melanger au journal principal (15 entrees) noyait en
# quelques heures toute trace d'un event rare et important (publication
# canonique, erreur, recalage Daily Open). Cap plus large (rotation, pas de
# cout Redis supplementaire par requete utilisateur : une seule cle, jamais
# lue hors admin) pour couvrir plusieurs heures de seance d'un coup.
FLOW_CRON_LOG_KEY = "gex:cron:flowlog"
FLOW_CRON_LOG_MAX = 40
# Filet de securite pour le cron GitHub Actions "Macro snapshot" (macro.json,
# cf. scripts/macro_snapshot.py) : GitHub peut retarder ou sauter un
# declenchement schedule sous charge (documente, deja observe en prod le
# 2026-08-12 -- le tir de 12h30 UTC prevu pour capter un CPI n'est jamais
# parti). Garde anti-spam : un seul redeclenchement par heure, le temps que
# le run GitHub (~1-2 min) commite un macro.json frais.
MACRO_STALE_HOURS = 5
MACRO_DISPATCH_GUARD_KEY = "gex:cron:macro_dispatch_guard"
MACRO_GH_REPO = "Sassakes/gexdash"
FINNHUB_CACHE_S = 2.5
_BASIS_ADJ = {}          # cache mémoire du correctif de basis (par marché)
_QUOTE_GUARD = {}        # dernier prix "confirmé" /api/quote (par marché) --
                          # filtre les prints isolés aberrants (cf. usage plus bas)
_GOLD_OFF = {"v": None, "at": 0.0}


def _gold_offset():
    """Ecart GC - XAUUSD, mesure en direct et memorise 60 s.

    Yahoo ne publie AUCUNE bougie pour l'or comptant : XAUUSD=X renvoie 404.
    On construit donc la chart du comptant a partir de celle du future,
    decalee de cet ecart — exactement le mecanisme deja utilise pour convertir
    les bougies ETF a l'echelle d'un future. L'ecart de portage bouge tres peu
    en seance, l'approximation est sans consequence visible."""
    now = time.time()
    if _GOLD_OFF["v"] is not None and now - _GOLD_OFF["at"] < 60:
        return _GOLD_OFF["v"]
    off = None
    try:
        gc = yahoo_spot("GC=F")
        xau = _goldapi_spot("XAU")
        if gc and xau:
            off = round(gc - xau, 2)
    except Exception:
        off = None
    if off is None:                      # repli : valeur saisie dans l'admin
        try:
            off = float(json.loads(kv_get("gex:goldbasis") or "null"))
        except Exception:
            off = None
    if off is not None:
        _GOLD_OFF.update({"v": off, "at": now})
    return off
_MEM_FH = {}             # cache mémoire des cotes Finnhub : sym -> (prix, ts, at)
_MEM_FH_ERR = {}         # dernier échec par symbole (anti-martèlement)
_MEM_CTX = {}            # cache mémoire du contexte quote : target -> (ctx, at)
# Préférences d'affichage par membre (échelle future/indice). Mises en cache
# EN MÉMOIRE : /api/auth est appelé au chargement de CHAQUE page du terminal,
# et CLAUDE.md interdit une lecture Redis par requête utilisateur. Un réglage
# d'affichage supporte parfaitement 60 s de latence ; l'écriture (POST
# /api/profile op=scale) invalide l'entrée pour un effet immédiat.
_USER_PREF = {}          # user -> (prefs, at)
_USER_PREF_TTL = 60.0


def _calibrated_basis(target):
    """Basis la PLUS à jour connue : celle du payload publié, plus le
    correctif mesuré en continu sur le chevauchement future/ETF. C'est
    exactement l'écart future -> indice, donc ce qu'il faut retrancher pour
    afficher NAS100/US500 au lieu de NQ/ES. Lecture du correctif mise en
    cache mémoire (déjà le cas pour /api/quote), jamais d'exception."""
    try:
        b = float((_quote_ctx(target) or {}).get("basis") or 0.0)
    except Exception:
        return 0.0
    try:
        now = time.time()
        c = _BASIS_ADJ.get(target)
        if not c or now - c[1] > 60:
            a = kv_get(f"gex:basisadj:{target}")
            v = (json.loads(a).get("adj") or 0.0) if a else 0.0
            _BASIS_ADJ[target] = (v, now)
            c = _BASIS_ADJ[target]
        b += float(c[0] or 0.0)
    except Exception:
        pass
    return b


def _user_scale(handler_self, user):
    """Échelle d'affichage du membre, "fut" par défaut. Jamais d'exception :
    une préférence illisible ne doit pas casser l'authentification."""
    if not user:
        return "fut"
    try:
        now = time.time()
        c = _USER_PREF.get(user)
        if not c or now - c[1] > _USER_PREF_TTL:
            u = handler_self._users().get(user) or {}
            _USER_PREF[user] = ({"scale": u.get("scale") or "fut"}, now)
            c = _USER_PREF[user]
        return c[0].get("scale") or "fut"
    except Exception:
        return "fut"


def _quote_ctx(target):
    """basis + scale du marché pour la conversion ETF -> future.
    Ces valeurs ne changent qu'aux recalculs (00h11 / 15h25) : les relire dans
    Redis à chaque poll était le principal poste de consommation. Mémorisées
    60 s en mémoire du process."""
    import time as _t
    now = _t.time()
    c = _MEM_CTX.get(target)
    if c and now - c[1] < 60:
        return c[0]
    pay = _latest_payload(target) or {}
    ctx = {"basis": pay.get("basis") or 0.0,
           "scale": next((s.get("scale") for s in pay.get("sources", [])
                          if s.get("chain") == YETF.get(target) and s.get("scale")),
                         None)}
    _MEM_CTX[target] = (ctx, now)
    return ctx
DP_SYMS = {"NQ": "QQQ", "ES": "SPY", "SPX": "SPY"}


_FINRA_MEM = {}          # ymd -> {"QQQ": (short, total), ...} ; fichiers immuables


# ═══════════════ TERMINAL NEWS ═══════════════
# Sources : Finnhub (clé serveur, jamais exposée) et FairEconomy (sans clé).
# Caches mémoire courts : on reste très loin des limites des fournisseurs.
_NEWS_MEM = {}
_NEWS_SHOCK = ["nuclear", "missile", "airstrike", "air strike", "invasion", "invade",
               "declares war", "act of war", "military strike", "ceasefire",
               "retaliat", "escalat", "tariff", "sanction", "state of emergency",
               "government shutdown", "debt default", "rate cut", "rate hike",
               "emergency cut", "market crash", "circuit breaker", "bank failure",
               "bankruptc", "recession fears"]


def _news_cached(key, ttl, fn):
    now = time.time()
    hit = _NEWS_MEM.get(key)
    if hit and now - hit[1] < ttl:
        return hit[0]
    data = fn()
    _NEWS_MEM[key] = (data, now)
    return data


# ═══════════════ /api/nqlive : cotation NQ1! via le websocket public
# TradingView (data.tradingview.com), page /test uniquement (non repertoriee).
# Auth par cookie de session en variable d'environnement (TV_SESSIONID,
# TV_SESSIONID_SIGN) -- JAMAIS dans le code/repo, jamais renvoye au client.
# Vercel serverless ne peut pas garder un websocket ouvert entre deux
# requetes : chaque appel ouvre, lit une poignee de frames, ferme. Usage
# experimental assume avec le proprietaire du compte -- cf. conversation,
# pas une source de donnees officielle/licenciee. ═══════════════
def _tv_auth_token():
    """Echange le cookie de session contre le auth_token JWT que TradingView
    embarque dans la page d'accueil pour un visiteur connecte. Cache memoire
    5 min : ce GET sur tradingview.com/ est trop lourd pour le refaire a
    chaque poll client (/test poll /api/nqlive toutes les 1-2s)."""
    def fetch():
        import re
        import requests
        sid = os.environ.get("TV_SESSIONID")
        sign = os.environ.get("TV_SESSIONID_SIGN")
        if not sid or not sign:
            return None
        r = requests.get(
            "https://www.tradingview.com/",
            cookies={"sessionid": sid, "sessionid_sign": sign},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/124.0.0.0 Safari/537.36"},
            timeout=8,
        )
        m = re.search(r'"auth_token":"(.*?)"', r.text)
        return m.group(1) if m else None
    return _news_cached("tv:authtoken", 300, fetch)


def _tv_live_quote(symbol):
    """Une cotation quasi temps reel pour `symbol` (ex. CME_MINI:NQ1!).
    Cache memoire 1s : deroute les polls concurrents (plusieurs onglets/
    utilisateurs sur le meme process chaud) vers un seul aller-retour
    websocket au lieu d'un par requete."""
    def fetch():
        import re
        import json as _json
        from websocket import create_connection
        token = _tv_auth_token() or "unauthorized_user_token"
        ws = create_connection(
            "wss://data.tradingview.com/socket.io/websocket?from=screener%2F",
            header=["Origin: https://www.tradingview.com"],
            timeout=6,
        )
        try:
            def send(func, params):
                body = _json.dumps({"m": func, "p": params}, separators=(",", ":"))
                ws.send(f"~m~{len(body)}~m~{body}")
            qsess = "qs_" + secrets.token_hex(6)
            send("set_auth_token", [token])
            send("set_locale", ["en", "US"])
            send("quote_create_session", [qsess])
            send("quote_set_fields", [qsess, "ch", "chp", "lp", "lp_time",
                                       "original_name", "update_mode", "volume",
                                       "is_tradable"])
            resolve = _json.dumps({"adjustment": "splits", "currency-id": "USD",
                                    "session": "regular", "symbol": symbol})
            send("quote_add_symbols", [qsess, f"={resolve}"])
            send("quote_fast_symbols", [qsess, f"={resolve}"])

            out = {}
            deadline = time.time() + 5
            while time.time() < deadline:
                raw = ws.recv()
                if re.match(r"~m~\d+~m~~h~\d+$", raw):
                    ws.send(raw)
                    continue
                for item in [x for x in re.split(r"~m~\d+~m~", raw) if x]:
                    try:
                        packet = _json.loads(item)
                    except Exception:
                        continue
                    if isinstance(packet, dict) and packet.get("m") == "qsd":
                        v = packet["p"][1].get("v", {})
                        out.update(v)
                if "lp" in out and "update_mode" in out:
                    break
            return out
        finally:
            ws.close()
    return _news_cached(f"tv:quote:{symbol}", 1, fetch)


def _news_finnhub(path_, params, key, ttl):
    def fetch():
        import requests
        p = dict(params)
        p["token"] = os.environ.get("FINNHUB_API_KEY", "")
        try:
            return requests.get(f"https://finnhub.io/api/v1/{path_}", params=p,
                                timeout=10).json()
        except Exception:
            # Un symbole en échec (timeout, rate-limit) ne doit pas faire
            # tomber tout le payload MAG7 en 502 : {} laisse le champ à None,
            # géré explicitement en aval (agg.complete).
            return {}
    return _news_cached(key, ttl, fetch)


def _news_impact(headline):
    h = (headline or "").lower()
    return "high" if any(k in h for k in _NEWS_SHOCK) else "none"


FJ_RSS = "https://www.financialjuice.com/feed.ashx?xy=rss"


def _fetch_status(url, name):
    """Récupère un flux ET renvoie la raison d'un éventuel échec : sans cela,
    une colonne vide ne dit pas si la source est bloquée, injoignable ou
    simplement sans contenu."""
    try:
        import requests
        r = requests.get(url, timeout=9, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0 Safari/537.36",
            "Accept": "application/rss+xml,application/xml,text/xml,*/*",
        })
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}"
        rows = _feed_parse(r.content, {"n": name, "t": "temps réel"})
        return rows, ("aucun élément" if not rows else "")
    except Exception as e:
        return [], type(e).__name__


_FJ_BACKOFF = {"until": 0.0}
FJ_KEY = "gex:fjnews"
FJ_TTL = 60          # fraicheur visee du fil, en secondes


def _news_fj_shared():
    """Fil FinancialJuice avec cache PARTAGE entre toutes les instances.

    Le 429 venait de la : chaque instance Vercel avait son propre cache
    memoire et interrogeait leur RSS pour son compte, donc N instances =
    N requetes depuis la meme plage d'adresses. En passant par Redis, une
    seule requete par minute alimente tout le deploiement — largement sous
    leur limite, et le fil reste reellement live."""
    now = time.time()
    try:
        blob = kv_get(FJ_KEY)
        if blob:
            data = json.loads(blob)
            if now - data.get("at", 0) < FJ_TTL:
                return data.get("rows") or []
    except Exception:
        data = None

    if now < _FJ_BACKOFF["until"]:
        try:                              # perime mais mieux que rien
            return json.loads(kv_get(FJ_KEY) or "{}").get("rows") or []
        except Exception:
            return []

    # "Live" plutot que le nom du fournisseur : c'est ce texte qui finit tel
    # quel dans le champ "source" de chaque depeche (_feed_parse), affiche a
    # l'utilisateur sur /news et sur la carte Actualites du dashboard -- pas
    # une marque a exposer, juste indiquer que c'est le fil temps reel.
    rows, err = _fetch_status(FJ_RSS, "Live")
    # Leur RSS prefixe CHAQUE titre par "FinancialJuice: " en dur (verifie sur
    # le flux brut) -- ca ne vit pas dans un champ separe qu'on controle, ca
    # fait partie du texte du titre lui-meme. Retire ici, une seule fois a la
    # source, plutot que de laisser chaque consommateur (liste de secours
    # /news, carte Actualites du dashboard) le repeter.
    if rows:
        import re as _re
        for _row in rows:
            _row["title"] = _re.sub(r"^financialjuice\s*[:\-]\s*", "",
                                     _row.get("title") or "", flags=_re.IGNORECASE)
    if err.startswith("HTTP 429") or err.startswith("HTTP 5"):
        _FJ_BACKOFF["until"] = now + 300
        try:                              # on ressert le dernier fil connu
            return json.loads(kv_get(FJ_KEY) or "{}").get("rows") or []
        except Exception:
            return []
    if rows:
        try:
            kv_set(FJ_KEY, json.dumps({"at": now, "rows": rows[:60]}), ex=1800)
        except Exception:
            pass
    return rows


def _news_fj():
    """FinancialJuice n'est qu'un REPLI : leur RSS limite le debit par IP
    (HTTP 429 depuis Vercel, dont les adresses sont partagees). On respecte
    cette limite plutot que de la forcer — apres un refus, on s'abstient dix
    minutes."""
    now = time.time()
    if now < _FJ_BACKOFF["until"]:
        return []
    rows, err = _fetch_status(FJ_RSS, "FinancialJuice")
    if err.startswith("HTTP 429") or err.startswith("HTTP 5"):
        _FJ_BACKOFF["until"] = now + 600
    return rows


def _news_headlines(cat):
    raw = _news_finnhub("news", {"category": cat}, f"news:{cat}", ttl=90)
    if not isinstance(raw, list):
        return []
    now = time.time()
    out = []
    for x in raw[:70]:
        # Le filtre anti-bruit ne s'appliquait qu'au flux RSS : les depeches
        # Finnhub laissaient donc passer les listes d'actions et les conseils
        # perso dans la colonne Actualites.
        if _feed_is_noise(x.get("headline")):
            continue
        imp = _news_impact(x.get("headline"))
        ts = x.get("datetime") or 0
        out.append({"headline": x.get("headline"), "source": x.get("source"),
                    "url": x.get("url"), "datetime": ts,
                    "summary": (x.get("summary") or "")[:260],
                    "impact": imp, "pinned": imp == "high" and (now - ts) < 3600})
    return out


# ─────────── FLUX EN DIRECT (agrégateur RSS) ───────────
# Les comptes X ne sont pas accessibles gratuitement (l'API officielle est
# payante et le scraping est interdit) : on passe donc par des flux RSS, qui
# sont publics, stables et pensés pour ça. Un pont RSS vers X peut être ajouté
# depuis l'admin comme n'importe quelle autre source.
DEFAULT_FEEDS = [
    # Sources primaires : ce que disent les institutions elles-mêmes.
    # Aucune source « conseils perso / lifestyle » — ces flux-là noient le
    # signal sous des articles sans valeur pour un terminal.
    {"n": "Fed · communiqués", "u": "https://www.federalreserve.gov/feeds/press_all.xml", "t": "banque centrale"},
    {"n": "Fed · politique monétaire", "u": "https://www.federalreserve.gov/feeds/press_monetary.xml", "t": "banque centrale"},
    {"n": "Trésor US", "u": "https://home.treasury.gov/rss/press.xml", "t": "gouvernement"},
    {"n": "BLS · statistiques", "u": "https://www.bls.gov/feed/bls_latest.rss", "t": "macro"},
    {"n": "SEC · communiqués", "u": "https://www.sec.gov/news/pressreleases.rss", "t": "régulateur"},
    {"n": "Maison-Blanche", "u": "https://www.whitehouse.gov/presidential-actions/feed/", "t": "politique"},
    {"n": "BCE", "u": "https://www.ecb.europa.eu/rss/press.html", "t": "banque centrale"},
    {"n": "CNBC · économie", "u": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258", "t": "macro"},
    {"n": "CNBC · marchés", "u": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20409666", "t": "marché"},
]


def _feed_sources():
    """Sources du flux : celles de l'admin si définies, sinon la liste par défaut."""
    try:
        cfg = json.loads(kv_get("gex:feeds") or "null")
        if isinstance(cfg, list) and cfg:
            return [f for f in cfg if f.get("u")]
    except Exception:
        pass
    return DEFAULT_FEEDS


# Un flux grand public mélange dépêches et « conseils perso » (retraite,
# crédit, listes d'actions à acheter). Ces titres n'ont aucune valeur ici et
# noient le signal : on les écarte.
_FEED_NOISE = [
    "i'm ", "i am ", "my wife", "my husband", "should i", "how to", "here's how",
    "here's why you", "retirement", "401(k)", "roth ira", "my savings", "nest egg",
    "credit card", "mortgage rate", "best stocks to", "stocks to buy",
    "top picks", "dividend stocks to", "personal finance", "suze orman",
    "dave ramsey", "moneywise", "quiz", "horoscope", "recipe", "celebrity",
    "worth it?", "millionaire next", "afraid to", "am i ready to retire",
]


def _feed_is_noise(title):
    t = (title or "").lower()
    return any(k in t for k in _FEED_NOISE)


def _feed_one(srcdef):
    """Lit un flux RSS/Atom. Tolérant : un flux mort n'en casse aucun autre."""
    try:
        import requests
        r = requests.get(srcdef["u"], timeout=8,
                         headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; "
                                                "x64) AppleWebKit/537.36 (KHTML, like "
                                                "Gecko) Chrome/124.0 Safari/537.36"})
        if r.status_code != 200:
            return []
        return _feed_parse(r.content, srcdef)
    except Exception:
        return []


def _feed_parse(content, srcdef):
    """Décode un contenu RSS ou Atom en liste d'entrées normalisées."""
    import re as _re
    import xml.etree.ElementTree as ET
    import email.utils as eut
    try:
        root = ET.fromstring(content)
    except Exception:
        return []
    out = []
    items = root.iter("item")
    entries = list(items) or [e for e in root.iter()
                              if e.tag.endswith("}entry") or e.tag == "entry"]
    for it in entries[:25]:
        def txt(*names):
            for n in names:
                el = it.find(n)
                if el is None:
                    for c in it:
                        if c.tag.endswith("}" + n) or c.tag == n:
                            el = c
                            break
                if el is not None:
                    if el.text:
                        return el.text.strip()
                    if el.get("href"):
                        return el.get("href")
            return ""
        title = txt("title")
        if not title or _feed_is_noise(title):
            continue
        link = txt("link", "id")
        date = txt("pubDate", "published", "updated", "date")
        ts = 0
        try:
            ts = int(eut.parsedate_to_datetime(date).timestamp())
        except Exception:
            try:
                ts = int(dt.datetime.fromisoformat(
                    date.replace("Z", "+00:00")).timestamp())
            except Exception:
                ts = 0
        desc = _re.sub(r"<[^>]+>", "", txt("description", "summary", "content"))[:220]
        out.append({"title": title[:200], "url": link, "source": srcdef.get("n", "?"),
                    "tag": srcdef.get("t", ""), "ts": ts, "desc": desc.strip(),
                    "impact": _news_impact(title + " " + desc)})
    return out


def _news_feed():
    from concurrent.futures import ThreadPoolExecutor
    srcs = _feed_sources()

    def fetch():
        with ThreadPoolExecutor(max_workers=min(10, len(srcs) or 1)) as ex:
            batches = list(ex.map(_feed_one, srcs))
        seen, rows = set(), []
        for b in batches:
            for x in b:
                key = (x["title"] or "")[:90].lower()
                if key in seen:          # même dépêche reprise par plusieurs sources
                    continue
                seen.add(key)
                rows.append(x)
        rows.sort(key=lambda x: x["ts"], reverse=True)
        return rows[:80]

    return _news_cached("feed", 120, fetch)


def _news_calendar():
    """Calendrier économique. La source est derrière Cloudflare et refuse
    souvent les IP de datacenter (donc Vercel) : on tente le direct, puis on
    retombe sur calendar.json, produit par la GitHub Action — même parade que
    pour macro.json avec FRED."""
    def fetch():
        import requests
        try:
            r = requests.get(
                "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                                       "Chrome/124.0 Safari/537.36",
                         "Accept": "application/json,text/plain,*/*"},
                timeout=10)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    return data
        except Exception:
            pass
        try:                                  # repli : instantané committé
            return json.loads((ROOT / "calendar.json").read_text())
        except Exception:
            return []
    raw = _news_cached("cal", 900, fetch)
    out = []
    if isinstance(raw, list):
        for x in raw:
            if x.get("country") != "USD":
                continue
            out.append({"time": x.get("date"), "title": x.get("title"),
                        "impact": (x.get("impact") or "").lower(),
                        "forecast": x.get("forecast"), "previous": x.get("previous"),
                        "actual": x.get("actual")})
    return out


_MAG7 = ["NVDA", "MSFT", "META", "GOOGL", "TSLA", "AMZN", "AAPL", "COIN"]


def _news_mag_one(sym):
    # 45 s : le client rafraîchit toutes les 30 s, mais 16 symboles à 2 appels
    # dépasseraient la limite de 60 requêtes/minute du palier gratuit. Le cache
    # absorbe, sans que l'utilisateur perçoive de retard.
    q = _news_finnhub("quote", {"symbol": sym}, f"q:{sym}", ttl=45)
    m = _news_finnhub("stock/metric", {"symbol": sym, "metric": "price"},
                      f"m:{sym}", ttl=1800)
    met = m.get("metric", {}) if isinstance(m, dict) else {}
    return {"sym": sym, "price": q.get("c"), "dp": q.get("dp"),
            "week": met.get("5DayPriceReturnDaily"),
            "month": met.get("monthToDatePriceReturnDaily")}


# Panorama de marché : indices, taux, dollar, or, pétrole et volatilité, via
# leurs ETF de référence (les indices bruts ne sont pas servis sur le palier
# gratuit). Complète le MAG7, qui ne couvre que la tech méga-capitalisation.
_MARKETS = [
    ("SPY", "S&P 500"), ("QQQ", "Nasdaq 100"), ("SOXX", "Semis · SOX"),
    ("IWM", "Russell 2000"), ("TLT", "Taux 20 ans"), ("UUP", "Dollar"),
    ("GLD", "Or"), ("USO", "Pétrole"), ("VIXY", "Volatilité"),
]


def _news_markets():
    """Panorama : seulement la cotation du jour. Les métriques hebdomadaires et
    mensuelles ne sont affichées que pour le MAG7 — les demander ici doublerait
    les appels pour une information qu'on n'affiche pas."""
    from concurrent.futures import ThreadPoolExecutor

    def one(p):
        q = _news_finnhub("quote", {"symbol": p[0]}, f"q:{p[0]}", ttl=45)
        return {"sym": p[0], "name": p[1], "price": q.get("c"), "dp": q.get("dp")}

    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(one, _MARKETS))


def _news_mag7():
    from concurrent.futures import ThreadPoolExecutor

    # Les 8 lignes MAG7 tournaient en SERIE (simple liste en compréhension,
    # 2 appels Finnhub chacune) pendant que _news_markets() juste à côté fait
    # exactement le même travail en parallèle sur 9 symboles -- l'incohérence
    # faisait trainer ce bloc jusqu'à 8x plus longtemps que necessaire, avec
    # un risque bien plus élevé qu'un symbole isolé (rate-limit, latence
    # Finnhub) dépasse encore la fenêtre de cache et laisse un "échantillon
    # incomplet" (agg.complete=False) affiché côté dashboard/news.
    def f_rows():
        with ThreadPoolExecutor(max_workers=8) as ex2:
            return list(ex2.map(_news_mag_one, _MAG7))

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_mag = ex.submit(f_rows)
        f_mkt = ex.submit(_news_markets)
        rows, markets = f_mag.result(), f_mkt.result()

    # Un appel Finnhub en échec (rate-limit, timeout) laisse un champ à None
    # sans lever d'exception — cf. _news_finnhub. Moyenner ou compter la
    # largeur sur un échantillon partiel produirait un chiffre qui A L'AIR
    # complet mais ne l'est pas : la moyenne comme la largeur exigent donc la
    # couverture TOTALE du groupe, jamais un sous-ensemble silencieux.
    def avg_strict(k, group):
        vals = [r.get(k) for r in group]
        if not vals or any(v is None for v in vals):
            return None
        return round(sum(vals) / len(vals), 2)

    mk = {m["sym"]: m.get("dp") for m in markets}
    score_syms = ("SOXX", "IWM", "VIXY")
    mag_complete = all(r.get("dp") is not None for r in rows) and bool(rows)
    complete = mag_complete and all(mk.get(s) is not None for s in score_syms)

    # Lecture de régime : on ne se contente pas de la moyenne, qui masque une
    # hausse portée par une seule valeur. On mesure aussi la LARGEUR (combien
    # de titres montent) et on croise semis, small caps et volatilité — les
    # trois signaux qui distinguent une vraie prise de risque d'un rebond
    # étroit sur quelques méga-capitalisations. Le tout exige un groupe MAG7
    # complet : sinon "6 titres sur 8 en hausse" et "8 sur 8" produiraient la
    # même lecture par accident.
    breadth = (round(100.0 * len([r for r in rows if (r.get("dp") or 0) > 0])
                      / len(rows)) if mag_complete else None)
    mag_d = avg_strict("dp", rows)
    score = 0
    bias = "incomplete"
    if complete:
        d = mag_d or 0
        if d > 0.15:
            score += 1
        elif d < -0.15:
            score -= 1
        if (breadth or 0) >= 70:
            score += 1
        elif breadth is not None and breadth <= 30:
            score -= 1
        if mk["SOXX"] > 0.3:
            score += 1                  # semis en tête = appétit pour le risque
        elif mk["SOXX"] < -0.3:
            score -= 1
        if mk["IWM"] > 0.3:
            score += 1                  # small caps suivent = hausse large
        elif mk["IWM"] < -0.3:
            score -= 1
        if mk["VIXY"] < -1:
            score += 1                  # volatilité qui reflue
        elif mk["VIXY"] > 3:
            score -= 1
        bias = ("risk_on" if score >= 3 else "risk_off" if score <= -3
                else "lean_on" if score > 0 else "lean_off" if score < 0 else "neutral")
    return {"rows": rows, "markets": markets,
            "agg": {"day": mag_d, "week": avg_strict("week", rows),
                    "month": avg_strict("month", rows),
                    "breadth": breadth, "score": score, "bias": bias,
                    "complete": complete,
                    "missing": [r["sym"] for r in rows if r.get("dp") is None],
                    "sox": mk.get("SOXX"), "iwm": mk.get("IWM"),
                    "vix": mk.get("VIXY")}}


# Cache PARTAGE (Redis) pour le meme motif que _news_fj_shared : sans lui,
# chaque instance Vercel maintient son propre cache mémoire de 45 s pour les
# 17 symboles (8 MAG7 + 9 marchés) — N instances concurrentes = N x 17 appels
# Finnhub, au-delà de la limite de 60 requêtes/minute du palier gratuit. Les
# appels en excès échouaient silencieusement (cases vides, biais faussé). Un
# seul calcul par fenêtre de TTL, partagé par tout le déploiement, suffit.
MAG7_KEY = "gex:mag7"
MAG7_TTL = 45
MAG7_STALE_OK_S = 1200  # au-delà, un vieux snapshot complet ne vaut plus mieux qu'un instantané incomplet mais frais


def _news_mag7_shared():
    now = time.time()
    cached = None
    try:
        blob = kv_get(MAG7_KEY)
        if blob:
            cached = json.loads(blob)
            if now - cached.get("at", 0) < MAG7_TTL:
                return cached["mag7"]
    except Exception:
        cached = None

    data = _news_mag7()
    if not data["agg"]["complete"] and cached \
            and cached.get("mag7", {}).get("agg", {}).get("complete") \
            and now - cached.get("at", 0) < MAG7_STALE_OK_S:
        return cached["mag7"]           # chiffre un peu périmé plutôt qu'un biais fabriqué

    try:
        kv_set(MAG7_KEY, json.dumps({"at": now, "mag7": data}), ex=180)
    except Exception:
        pass
    return data


# ═══════════════════ EM RESTANT (cône intrajournalier) ═══════════════════
#
# Deux objets DISTINCTS, souvent confondus :
#   · la BANDE du jour, posee a l'open, centree sur l'ouverture — combien le
#     marche est cense parcourir sur la seance ;
#   · le CONE restant, centre sur le prix COURANT — ce qu'il reste
#     techniquement possible d'ici la cloture.
# Un NQ deja a +0.8 sigma peut encore avoir 0.5 sigma de cone : la projection
# depasse alors la bande du jour. Les confondre fait rater les tendances.
#
# La decroissance suit la RACINE du temps, pas une droite : a midi il ne reste
# pas 50% de l'EM mais ~71%. Et le temps CALENDAIRE ment — la nuit Globex ne
# consomme presque pas de variance, 15h30-17h en brule une grosse part. On
# calibre donc une horloge ponderee sur les donnees reelles du produit.

VOLPROF_KEY = "gex:volprof:{t}"
SESSION_END_ET = (16, 0)      # cloture cash US
OPTIONS_OPEN_ET_MIN = 9 * 60 + 30      # 9h30 ET : ouverture des cotations d'options


def _et_minutes(ts):
    """Minutes ecoulees depuis minuit ET pour un horodatage epoch."""
    from zoneinfo import ZoneInfo
    d = dt.datetime.fromtimestamp(ts, ZoneInfo("America/New_York"))
    return d.hour * 60 + d.minute


def _et_now_minutes():
    from zoneinfo import ZoneInfo
    d = dt.datetime.now(ZoneInfo("America/New_York"))
    return d.hour * 60 + d.minute


def _quote_price(target):
    """Dernier prix connu du produit, a son echelle d'affichage."""
    sym = YCHART.get(target)
    if not sym:
        return None
    px = yahoo_spot(sym)
    if px and target == "XAU":
        # la serie est celle du future : on retire l'ecart de portage
        try:
            off = _gold_offset()
            if off:
                px -= off
        except Exception:
            pass
    return px


def _build_vol_profile(target):
    """Part de variance par tranche de 30 min, calibree sur ~1 mois de bougies
    5 min du produit lui-meme. Retourne un dict {minute_debut: poids} dont la
    somme vaut 1 sur la journee."""
    sym = YCHART.get(target, "NQ=F")
    res = _yahoo_chart(sym, "5m", "1mo")
    ts = res.get("timestamp") or []
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    closes = q.get("close") or []
    buckets = {}
    prev = None
    for i, t in enumerate(ts):
        c = closes[i] if i < len(closes) else None
        if c is None or c <= 0:
            prev = None
            continue
        if prev:
            r = math.log(c / prev)
            b = (_et_minutes(t) // 30) * 30
            buckets.setdefault(b, []).append(r * r)
        prev = c
    if not buckets:
        return None
    # variance moyenne par tranche, puis normalisation
    var = {b: (sum(v) / len(v)) for b, v in buckets.items() if len(v) >= 5}
    tot = sum(var.values())
    if tot <= 0:
        return None
    return {str(b): round(v / tot, 6) for b, v in sorted(var.items())}


def _refresh_vol_profile(target):
    """Calibration reelle, appelee par le cron uniquement."""
    try:
        prof = _build_vol_profile(target)
    except Exception:
        prof = None
    if prof:
        try:
            kv_set(VOLPROF_KEY.format(t=target),
                   json.dumps({"d": et_today().isoformat(), "p": prof, "cal": True}),
                   ex=30 * 86400)
        except Exception:
            pass
    return bool(prof)


def _vol_profile_unused(target):
    key = VOLPROF_KEY.format(t=target)
    try:
        cached = json.loads(kv_get(key) or "null")
        if cached and cached.get("d") == et_today().isoformat():
            return cached["p"]
    except Exception:
        pass
    try:
        prof = _build_vol_profile(target)
    except Exception:
        prof = None
    if prof:
        try:
            kv_set(key, json.dumps({"d": et_today().isoformat(), "p": prof}),
                   ex=7 * 86400)
        except Exception:
            pass
    return prof


INTRA_KEY = "gex:intra:{t}:{d}"
INTRA_MAX = 40          # ~1 point/heure sur une seance, large marge


def _track_intraday(payload):
    """Ajoute un point a l'historique du jour : Net GEX, regime, put/call et
    IV. Les NIVEAUX ne sont pas concernes — ils restent verrouilles — donc
    la string Pine ne change pas et il n'y a rien a recoller cote
    TradingView."""
    tgt = payload.get("target")
    if not tgt:
        return
    key = INTRA_KEY.format(t=tgt, d=et_today().isoformat())
    try:
        hist = json.loads(kv_get(key) or "[]")
    except Exception:
        hist = []
    flip = next((l["price_nq"] for l in payload.get("levels", [])
                 if l.get("kind") == "flip"), None)
    point = {
        "t": dt.datetime.now(dt.timezone.utc).strftime("%H:%M"),
        "net": payload.get("net_gex_bn"),
        "reg": payload.get("regime"),
        "pc": payload.get("pc_oi"),
        "iv": payload.get("iv_atm"),
        "flip": flip,
    }
    # un seul point par minute : evite les doublons si deux crons se croisent
    if hist and hist[-1].get("t") == point["t"]:
        hist[-1] = point
    else:
        hist.append(point)
    hist = hist[-INTRA_MAX:]
    kv_set(key, json.dumps(hist), ex=2 * 86400)
    payload["intraday"] = hist


def _read_intraday(tgt):
    def fetch():
        try:
            return json.loads(kv_get(INTRA_KEY.format(
                t=tgt, d=et_today().isoformat())) or "[]")
        except Exception:
            return []
    # meme cache memoire court que _latest_payload : _track_intraday n'ecrit
    # qu'a chaque tir de cron (5-10 min), pas la peine d'une lecture Redis
    # par tick de poll utilisateur.
    return _news_cached(f"intraday:{tgt}", 5, fetch)


# ═══════════════════ FLUX — projection prix x temps (ETAPE 1 : gamma) ═══════
# Voir docs/BRIEF-flux.md. Calcul fait UNIQUEMENT dans le cron intrajournalier
# (?intraday=1, 3x/jour) sur la chaine DEJA recuperee pour le payload — pas de
# second fetch. Cache dedie (jamais dans le payload publie : gex_by_strike/
# levels/open_grid/expected_move/pine restent seuls maitres du verrou GEX).
# Endpoint /api/flow en lecture seule sur ce cache.
FLOW_KEY = "gex:flow:{t}:{d}"
FLOW_CHECK_TOL_PCT = 10.0   # ecart au-dela duquel on journalise une alerte -- le
# controle compare desormais des totaux BRUTS (jamais un residu de deux grands
# nombres proches, cf. flow_gamma_sanity), mesures a 0.5-3.8% d'ecart naturel
# sur chaine reelle (NQ/SPX) ; 10% laisse une marge large sur ce bruit tout en
# restant assez serre pour attraper une vraie erreur de convention/echelle

# Historique intrajournalier de la colonne "maintenant" (rang 0 de flow_gamma_
# matrix, deja calculee pour chaque tir -- aucun calcul supplementaire). Sert
# a reconstituer, cote client, l'exposition dealer REELLE (prix reel, temps
# reel) sur la portion de seance deja ecoulee, a cote de la ligne de prix
# realise (cf. panneau Flux, bouton historique). Cle separee de FLOW_KEY :
# ne doit jamais interferer avec le payload/matrice de projection courants.
# TTL volontairement court (~26h, pas les 2 jours de FLOW_KEY) : l'historique
# de la veille ne sert plus une fois la nouvelle seance ouverte, pas besoin
# d'un cron de nettoyage dedie. FLOW_HIST_MAX borne la taille meme en cas
# d'appels hors cadence normale (test manuel ?intraday=1, etc.).
FLOW_HIST_KEY = "gex:flowhist:{t}:{d}"
FLOW_HIST_TTL = 26 * 3600
FLOW_HIST_MAX = 150


def _flow_cached(target):
    """Lecture FLOW_KEY mise en cache memoire courte -- partagee entre
    /api/flow et /api/embed/flow (meme cle de cache), donc une seule
    lecture Redis sert les deux endpoints sur une instance chaude. Le
    cron ne republie ce cache qu'a chaque tir intrajournalier (5-10 min),
    une lecture par instance toutes les 5s n'introduit aucun retard percu."""
    def fetch():
        try:
            return json.loads(kv_get(
                FLOW_KEY.format(t=target, d=et_today().isoformat())) or "null")
        except Exception:
            return None
    return _news_cached(f"flow:{target}", 5, fetch)


def _flow_hist_cached(target):
    def fetch():
        try:
            return json.loads(kv_get(
                FLOW_HIST_KEY.format(t=target, d=et_today().isoformat())) or "null")
        except Exception:
            return None
    return _news_cached(f"flowhist:{target}", 5, fetch)


# Widget Flux embarquable (/api/embed/flow) : verification de cle + rate-
# limit, sur le meme chemin de lecture seule que /api/flow (aucun calcul
# declenche ici, meme cache FLOW_KEY). La cle est un controle de
# distribution/attribution, pas un secret a proteger -- /api/flow lui-meme
# n'a aucune authentification aujourd'hui, la matrice n'est pas confidentielle
# -- d'ou une lecture simple du blob EMBED_KEYS_KEY, mise en cache memoire
# courte (_news_cached, deja utilise pour ce type de lookup) plutot qu'un
# schema cryptographique disproportionne par rapport a ce qu'il y a
# reellement a proteger.
EMBED_RATE_LIMIT = 30   # requetes/minute/cle -- tres au-dessus du polling normal (1/2min)


def _check_embed_key(given):
    if not given:
        return False, "missing key"
    keys = _news_cached("embedkeys", 60, fetch_embed_keys)
    meta = keys.get(given)
    if not meta or meta.get("revoked"):
        return False, "invalid key"
    return True, None


def _embed_rate_limited(given):
    """Non-atomique (lecture puis ecriture separees) -- meme tolerance que le
    lockout de connexion (gex:lockout:*), pas la peine de faire plus robuste
    ici qu'ailleurs dans ce fichier pour ce niveau de risque."""
    bucket = int(time.time() // 60)
    rl_key = f"gex:embedrl:{given}:{bucket}"
    count = int(kv_get(rl_key) or 0)
    if count >= EMBED_RATE_LIMIT:
        return True
    kv_set(rl_key, str(count + 1), ex=90)
    return False


# ═══════════════ CLES API PERSONNELLES (/api/mylevels) ═══════════════
# Une cle par membre (blob APIKEYS_KEY, cf. _gex_core.py), consommee par une
# extension Chrome qui remplit l'indicateur Pine -- lecture seule stricte,
# aucun calcul declenche sur ce chemin (meme regle que /api/embed/flow).
APIKEY_RATE_LIMIT = 20   # requetes/minute/cle -- tres au-dessus du polling normal d'une extension
APILOG_TTL = 26 * 3600   # marge sur 24h pour que l'agregation admin voie toujours 24 tranches pleines
APILOG_MAX = 4000        # garde-fou dur par tranche horaire, jamais atteint en usage normal


def _check_api_key(given):
    """(meta, code, motif). motif=None si la cle est utilisable."""
    if not given:
        return None, 401, "missing key"
    keys = _news_cached("apikeys", 60, fetch_api_keys)
    meta = keys.get(given)
    if not meta:
        return None, 401, "invalid key"
    state = meta.get("state", "active")
    if state in ("blocked", "revoked"):
        return None, 403, f"key {state}"
    return meta, 200, None


def _apikey_rate_limited(given):
    """Meme idiome non-atomique que _embed_rate_limited."""
    bucket = int(time.time() // 60)
    rl_key = f"gex:mlrl:{given}:{bucket}"
    count = int(kv_get(rl_key) or 0)
    if count >= APIKEY_RATE_LIMIT:
        return True
    kv_set(rl_key, str(count + 1), ex=90)
    return False


def _apikey_hour_key(given, when=None):
    when = when or dt.datetime.now(dt.timezone.utc)
    return f"gex:apilog:{given}:{when.strftime('%Y%m%d%H')}"


def _apikey_stats(given):
    """Agrege les 24 dernieres tranches horaires du journal (lecture admin
    uniquement -- cout sans importance ici, cf. discussion de design). Chaque
    tranche est une liste [{"t": epoch, "o": empreinte}], jamais un compteur
    global en lecture-modification-ecriture : deux appels concurrents dans la
    MEME tranche restent chacun un ajout independant, le signal de partage
    (origines distinctes) ne se perd donc pas sur une collision d'ecriture."""
    now = dt.datetime.now(dt.timezone.utc)
    now_ts = now.timestamp()
    origins, calls_1h, calls_24h, last_call = set(), 0, 0, None
    for i in range(24):
        raw = kv_get(_apikey_hour_key(given, now - dt.timedelta(hours=i)))
        if not raw:
            continue
        try:
            log = json.loads(raw)
        except Exception:
            continue
        if not isinstance(log, list):
            continue
        for e in log:
            t = e.get("t")
            if not isinstance(t, (int, float)) or now_ts - t > 24 * 3600:
                continue
            calls_24h += 1
            origins.add(e.get("o"))
            if now_ts - t <= 3600:
                calls_1h += 1
            if last_call is None or t > last_call:
                last_call = t
    return {
        "calls_1h": calls_1h, "calls_24h": calls_24h,
        "origins_24h": len(origins),
        "last_call": (dt.datetime.fromtimestamp(last_call, dt.timezone.utc)
                      .isoformat(timespec="seconds") if last_call else None),
    }


def _flow_grids(payload):
    """Grille prix (echelle produit), CENTREE SUR LE PRIX COURANT — pas sur
    l'ouverture du jour — pour que la colonne du controle de justesse
    (recalcul a T=maintenant) tombe exactement sur une colonne de la grille,
    sans interpolation. Meme unite que l'open_grid deja publie (reprise telle
    quelle, jamais recalculee) : le module Flux se lit avec le meme repere
    sigma que la grille d'Open deja affichee sur le chart plutot que
    d'inventer une troisieme echelle. Pas = 0.25 sigma (25 colonnes sur la
    meme amplitude +/-3 sigma qu'avant, deux fois plus fin) : depuis que
    flow_gamma_matrix classe chaque option dans SA colonne de strike au lieu
    de recalculer toute la chaine a chaque prix candidat (cf. commentaire
    dans _gex_core.py), la resolution de cette grille determine directement
    a quel point les concentrations par strike restent lisibles plutot que
    fondues dans une colonne trop large.
    Grille temps : heures pleines de maintenant jusqu'a la cloture cash
    (16h ET), plus le point exact de cloture si ce n'est pas deja un entier —
    l'heure de fin reprend SESSION_END_ET, la meme constante que le reste
    du fichier, pas un calcul independant."""
    spot_prod = payload.get("nq_price")
    unit = (payload.get("open_grid") or {}).get("unit")
    if not spot_prod or not unit:
        return None, None, None
    price_grid = [round(spot_prod + i * 0.25 * unit, 2) for i in range(-12, 13)]
    now_h = _et_now_minutes() / 60.0
    close_h = SESSION_END_ET[0] + SESSION_END_ET[1] / 60.0
    remaining = close_h - now_h
    if remaining <= 0:                     # hors seance : rien a projeter
        return price_grid, None, 0.0
    hours = [float(h) for h in range(0, int(remaining) + 1)]
    if not hours or hours[-1] < remaining:
        hours.append(round(remaining, 2))
    return price_grid, hours, remaining


def _refresh_flow(target, payload, capture):
    """capture = {'opts','spot','basis','net_total_bn'} rempli par
    build_payload(capture=...) : memes options (deja blend ETF + scale) que
    celles agregees dans gex_by_strike.

    Retourne (check, reason) : check est le dict de controle de justesse (ou
    None si non calculable), reason est None en cas de succes ou une chaine
    courte quand check est None -- distingue un saut legitime (hors seance,
    payload incomplet) d'un echec reel (chaine sans IV exploitable) d'un
    controle simplement non calculable alors que la matrice, elle, a bien ete
    ecrite en KV. Un `None` nu ne permettait pas de faire cette difference a
    l'appelant -- reason alimente flow_check/flow_skip_reason dans la reponse
    JSON de /api/cron."""
    price_grid, hours, hours_left = _flow_grids(payload)
    if not price_grid:
        return None, "payload incomplet (nq_price/open_grid manquant)"
    if not hours:
        return None, "hors seance : rien a projeter"
    basis = capture.get("basis") or 0.0
    price_grid_idx = [p - basis for p in price_grid]
    opts = capture.get("opts") or []
    mats = flow_gamma_matrix(opts, price_grid_idx, hours, hours_left)
    if not mats:
        return None, "chaine sans option a IV exploitable"
    # iv_now/iv_ref : deja calcules et persistes par le module EM (cf.
    # _stamp_iv_ref) -- exposes ici tels quels pour que le panneau Flux
    # puisse situer la vanna (delta/point d'IV) par rapport au deplacement
    # d'IV reellement observe depuis la publication canonique, sans que ce
    # module ne recalcule sa propre reference.
    iv_now = payload.get("iv_atm")
    iv_ref = payload.get("iv_ref") or iv_now
    dsigma = (iv_now - iv_ref) if (iv_now is not None and iv_ref is not None) else None
    out = {
        "target": target, "date": et_today().isoformat(),
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "spot": payload.get("nq_price"), "basis": round(basis, 2),
        "unit": (payload.get("open_grid") or {}).get("unit"),
        "price_grid": price_grid, "hours": hours,
        "gamma": mats["gamma"], "vanna": mats["vanna"], "charm": mats["charm"],
        "iv_now": iv_now, "iv_ref": iv_ref,
        "dsigma": round(dsigma, 4) if dsigma is not None else None,
        "volume_context": flow_volume_context(
            opts, payload.get("nq_price"), basis),
    }
    # Controle de justesse (obligatoire, brief etape 1) : le gamma dollar
    # BRUT (somme des |gamma$| par contrat, cf. flow_gamma_sanity) recalcule
    # en BS doit rester proche du meme total calcule avec le gamma natif
    # CBOE -- MEMES options non-0DTE, seul le gamma differe (recalcule BS
    # ici, fourni par CBOE sinon). Un ecart important signale une erreur de
    # convention/echelle/formule, pas une divergence de marche normale.
    #
    # BRUT, PAS le net signe (calls - puts) : mesure sur chaine reelle, le
    # net publie est un residu proche de zero entre deux totaux qui
    # s'annulent presque (~12Bn de chaque cote pour <1Bn de net sur NQ) --
    # le champ gamma de CBOE est arrondi a 4 decimales sur TOUTE la chaine
    # (pas seulement le 0DTE), un bruit negligeable par jambe qui s'amplifie
    # jusqu'a 78% d'ecart une fois divise par un net minuscule, sans aucun
    # rapport avec une erreur de notre cote (cf. flow_gamma_sanity pour le
    # detail chiffre). Le brut, jamais un residu de deux grands nombres
    # proches, reste lui stable a quelques % -- c'est le signal qui detecte
    # reellement une erreur de convention/echelle/formule.
    #
    # 0DTE EXCLU du controle, des DEUX cotes : mesure sur chaine reelle, le
    # champ gamma de CBOE pour ces echeances est quantifie a 0.0001 -- plat
    # a "0.002" sur toute une bande de 60 points autour du spot, aucune
    # variance reelle capturee pres de la monnaie. Une fois notre T corrige
    # (cf. flow_gamma_matrix), notre recalcul y est structurellement PLUS
    # juste que CBOE, donc un ecart residuel sur le 0DTE est attendu et
    # legitime -- le comparer serait mesurer la mauvaise reference, pas une
    # erreur de notre cote.
    check = None
    check_reason = None
    cboe_gross_bn, bs_gross_bn = flow_gamma_sanity(opts, capture.get("spot"))
    if cboe_gross_bn is None:
        check_reason = "non-0DTE sans IV exploitable pour le controle"
    else:
        denom = cboe_gross_bn if cboe_gross_bn > 1e-6 else 1e-6
        dev_pct = round(100.0 * abs(bs_gross_bn - cboe_gross_bn) / denom, 1)
        check = {"cboe_gross_bn": round(cboe_gross_bn, 3),
                 "flow_gross_bn": round(bs_gross_bn, 3),
                 "deviation_pct": dev_pct, "excl_0dte": True}
    out["check"] = check
    kv_set(FLOW_KEY.format(t=target, d=out["date"]), json.dumps(out), ex=2 * 86400)
    # matrice + check ecrits en KV meme si check est None (check_reason
    # explique pourquoi) : seule la matrice compte pour le panneau Flux, le
    # check n'est qu'un controle de justesse secondaire.

    # Historique : colonne "maintenant" (rang 0, deja calculee ci-dessus,
    # aucun recalcul) ajoutee a la serie du jour. Best-effort : une panne de
    # lecture/ecriture ici ne doit jamais faire echouer le tir de flux
    # principal (deja publie juste au-dessus).
    try:
        hist_key = FLOW_HIST_KEY.format(t=target, d=out["date"])
        raw = kv_get(hist_key)
        hist = json.loads(raw) if raw else []
        if not isinstance(hist, list):
            hist = []
        hist.append({
            "t": out["generated_utc"], "spot": out["spot"], "price_grid": price_grid,
            "gamma0": mats["gamma"][0], "vanna0": mats["vanna"][0], "charm0": mats["charm"][0],
        })
        if len(hist) > FLOW_HIST_MAX:
            hist = hist[-FLOW_HIST_MAX:]
        kv_set(hist_key, json.dumps(hist), ex=FLOW_HIST_TTL)
    except Exception:
        pass

    return check, check_reason


# Module Horizon (docs/BRIEF-horizon.md) : cle separee de FLOW_KEY, meme
# TTL/forme -- ne doit jamais interferer avec le payload principal ni avec
# la matrice Flux. Lecture seule pour /api/horizon, jamais recalculee hors
# du cron intrajournalier (meme regle que FLOW_KEY).
HORIZON_KEY = "gex:horizon:{t}:{d}"

# Etape 5 (calibration) : journal du jour (une entree par tir, un sous-objet
# par horizon emis) + stats CUMULATIVES inter-seances (pas de suffixe date --
# persistent tant que HORIZON_STATS_TTL n'expire pas). Cles separees : le
# journal du jour tourne (26h, comme FLOW_HIST_KEY), les stats doivent
# survivre des mois pour que l'echantillon ait un sens (brief : "l'echantillon
# met des semaines a se constituer").
HORIZON_LOG_KEY = "gex:horizonlog:{t}:{d}"
HORIZON_LOG_TTL = 26 * 3600
HORIZON_LOG_MAX = 200
HORIZON_STATS_KEY = "gex:horizonstats:{t}"
HORIZON_STATS_TTL = 400 * 86400
# Biais juge negligeable (bruit, pas un vrai signal directionnel) en-dessous
# de ce seuil -- exclu du taux de realisation du biais pour ne pas diluer
# la stat avec des predictions quasi nulles.
HORIZON_BIAS_MIN_PT = 0.5
# "Sous 20 seances : afficher calibration en cours" (brief, section
# Calibration) -- comptees en JOURS DE BOURSE distincts par bucket (regime x
# horizon), pas en tirs bruts : un seul jour genere deja des dizaines de
# tirs intrajournaliers, compter les tirs ferait passer le seuil de 20 en
# quelques heures alors que le brief attend explicitement plusieurs
# semaines d'echantillon.
HORIZON_MIN_SAMPLE_DAYS = 20


def _horizon_cached(target):
    """Mirror exact de _flow_cached : cache memoire courte (5s) devant
    HORIZON_KEY, republie a chaque tir intrajournalier par _refresh_horizon."""
    def fetch():
        try:
            return json.loads(kv_get(
                HORIZON_KEY.format(t=target, d=et_today().isoformat())) or "null")
        except Exception:
            return None
    return _news_cached(f"horizon:{target}", 5, fetch)


def _horizon_stats_cached(target):
    """Lecture seule (memoire 5s) des stats de calibration cumulatives --
    utilisee uniquement par /api/horizon. L'ecriture (cf. _horizon_record_
    and_verify) lit toujours HORIZON_STATS_KEY directement, jamais via ce
    cache, pour ne jamais ecraser une mise a jour concurrente avec une
    version perimee."""
    def fetch():
        try:
            raw = kv_get(HORIZON_STATS_KEY.format(t=target))
            d = json.loads(raw) if raw else None
            return d if isinstance(d, dict) and isinstance(d.get("buckets"), dict) else None
        except Exception:
            return None
    return _news_cached(f"horizonstats:{target}", 5, fetch)


def _horizon_walls(payload):
    """Extrait (prix, gamma$Bn) des murs deja publies + le gamma$Bn BRUT
    total de la chaine (gex_by_strike, echelle produit) pour l'attenuation
    aux murs (brief etape 4) -- entierement depuis le payload deja
    construit, aucun acces reseau supplementaire. None pour un mur si son
    kind est absent ou introuvable dans gex_by_strike (strike hors fenetre
    +/-4%, cf. _strike_profile) : horizon_envelope degrade gracieusement
    (pas d'attenuation appliquee de ce cote)."""
    strikes = payload.get("gex_by_strike") or []
    if not strikes:
        return None
    gross_bn = sum(abs(g) for _, g in strikes)

    def nearest_gamma(price):
        if price is None:
            return None
        p, g = min(strikes, key=lambda row: abs(row[0] - price))
        return g if abs(p - price) <= max(abs(price) * 0.01, 5.0) else None

    levels = payload.get("levels") or []
    call_price = next((lv.get("price_nq") for lv in levels if lv.get("kind") == "res"), None)
    put_price = next((lv.get("price_nq") for lv in levels if lv.get("kind") == "sup"), None)
    return {
        "gross_bn": gross_bn,
        "call": (call_price, nearest_gamma(call_price)),
        "put": (put_price, nearest_gamma(put_price)),
    }


def _horizon_stats_load(target):
    """Lecture FRAICHE (pas de cache memoire) des stats cumulatives --
    utilisee uniquement dans le chemin d'ecriture du cron, cf. note sur
    _horizon_stats_cached ci-dessus."""
    try:
        raw = kv_get(HORIZON_STATS_KEY.format(t=target))
        d = json.loads(raw) if raw else None
        if isinstance(d, dict) and isinstance(d.get("buckets"), dict):
            return d
    except Exception:
        pass
    return {"buckets": {}}


def _horizon_record_and_verify(target, out):
    """Etape 5 (calibration) : enregistre les projections de ce tir dans le
    journal du jour, puis verifie celles emises lors d'un tir precedent dont
    l'echeance (target_utc) est desormais passee -- comparaison au prix REEL
    de CE tir (out['spot']). Best-effort strict : jamais d'exception
    propagee, appelee en toute fin de _refresh_horizon, ne doit jamais
    casser le tir de cron (meme regle que _track_intraday)."""
    if not out.get("horizons"):
        return
    try:
        gen = dt.datetime.fromisoformat(out["generated_utc"])
    except Exception:
        return
    log_key = HORIZON_LOG_KEY.format(t=target, d=out["date"])
    try:
        log = json.loads(kv_get(log_key) or "[]")
        if not isinstance(log, list):
            log = []
    except Exception:
        log = []

    now_price = out.get("spot")
    stats = None   # charge paresseusement (une lecture Redis), seulement s'il y a reellement une echeance a verifier
    for entry in log:
        for hz in entry.get("horizons", []):
            if hz.get("verified") or now_price is None:
                continue
            try:
                target_utc = dt.datetime.fromisoformat(hz["target_utc"])
            except Exception:
                continue
            if target_utc > gen:
                continue
            if stats is None:
                stats = _horizon_stats_load(target)
            lo70, hi70 = hz["zone70"]
            bucket_key = f"{entry.get('regime') or 'unknown'}:{hz['horizon_h']}h"
            b = stats["buckets"].setdefault(
                bucket_key, {"n": 0, "cov70": 0, "bias_n": 0, "bias_hit": 0, "dates": {}})
            b["n"] += 1
            b["dates"][out["date"]] = True
            if lo70 <= now_price <= hi70:
                b["cov70"] += 1
            bias = hz.get("bias") or 0.0
            if abs(bias) >= HORIZON_BIAS_MIN_PT and entry.get("spot") is not None:
                b["bias_n"] += 1
                if (now_price - entry["spot"] > 0) == (bias > 0):
                    b["bias_hit"] += 1
            hz["verified"] = True

    log.append({
        "generated_utc": out["generated_utc"], "spot": out.get("spot"),
        "regime": out.get("regime"),
        "horizons": [
            {"horizon_h": hz["horizon_h"],
             "target_utc": (gen + dt.timedelta(hours=hz["horizon_h"])).isoformat(),
             "median": hz["median"], "bias": hz["bias"],
             "zone70": hz["zone70"], "zone90": hz["zone90"], "verified": False}
            for hz in out["horizons"]
        ],
    })
    if len(log) > HORIZON_LOG_MAX:
        log = log[-HORIZON_LOG_MAX:]
    kv_set(log_key, json.dumps(log), ex=HORIZON_LOG_TTL)
    if stats is not None:
        kv_set(HORIZON_STATS_KEY.format(t=target), json.dumps(stats), ex=HORIZON_STATS_TTL)


def _refresh_horizon(target, payload, capture=None):
    """Module Horizon (docs/BRIEF-horizon.md), etapes 1-5 : projection
    symetrique + biais charm + modulation gamma + attenuation aux murs,
    enregistrement/verification pour la calibration. `capture` (memes
    options que _refresh_flow, deja blend ETF + scale) est OPTIONNEL -- si
    absent/vide (Flow a echoue sur ce tir), degrade gracieusement vers la
    projection symetrique seule (etape 1), sans biais ni modulation. Ne lit
    ni n'ecrit jamais levels/gex_by_strike/open_grid/expected_move/pine."""
    spot = payload.get("nq_price")
    sigma1d = (payload.get("open_grid") or {}).get("unit")
    iv_now = payload.get("iv_atm")
    iv_ref = payload.get("iv_ref") or iv_now
    now_h = _et_now_minutes() / 60.0
    close_h = SESSION_END_ET[0] + SESSION_END_ET[1] / 60.0
    remaining_h = close_h - now_h
    session_length_h = close_h - OPTIONS_OPEN_ET_MIN / 60.0

    gamma_bn_by_h = charm_bn_by_h = None
    if capture and capture.get("opts") and spot:
        try:
            basis = capture.get("basis") or 0.0
            hours = horizon_hours(remaining_h)
            mats = flow_gamma_matrix(capture["opts"], [spot - basis], hours, remaining_h)
            if mats:
                gamma_bn_by_h = {h: mats["gamma"][i][0] / 1e9 for i, h in enumerate(hours)}
                charm_bn_by_h = {h: mats["charm"][i][0] / 1e9 for i, h in enumerate(hours)}
        except Exception:
            gamma_bn_by_h = charm_bn_by_h = None

    horizons = horizon_envelope(spot, sigma1d, iv_now, iv_ref, remaining_h,
                                session_length_h, gamma_bn_by_h=gamma_bn_by_h,
                                charm_bn_by_h=charm_bn_by_h,
                                walls=_horizon_walls(payload))
    out = {
        "target": target, "date": et_today().isoformat(),
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "spot": spot, "regime": payload.get("regime"), "horizons": horizons,
    }
    kv_set(HORIZON_KEY.format(t=target, d=out["date"]), json.dumps(out), ex=2 * 86400)
    try:
        _horizon_record_and_verify(target, out)
    except Exception:
        pass


def _finra_dp_day(ymd):
    """Volume off-exchange FINRA (fichier CNMS quotidien) pour QQQ et SPY.
    C'est le volume exécuté hors bourses (dark pools + internalisation),
    avec sa part shortée — la matière première du ratio type DIX.
    Retourne {"QQQ": (short, total), "SPY": (...)} ou None. Jamais d'exception.

    NB parsing : FINRA publie désormais les volumes en DÉCIMAL
    ("9478967.850783"). int() lève alors ValueError et faisait tomber
    silencieusement toutes les lignes — d'où un panneau Dark Pool vide.
    On passe donc par int(float(...))."""
    if ymd in _FINRA_MEM:
        return _FINRA_MEM[ymd]
    try:
        import requests
        r = requests.get(
            f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{ymd}.txt",
            timeout=8)
        if r.status_code != 200 or "|" not in (r.text[:200] or ""):
            return None
        out = {}
        for line in r.text.splitlines():
            p = line.split("|")
            if len(p) >= 5 and p[1] in ("QQQ", "SPY"):
                try:
                    out[p[1]] = (int(float(p[2])), int(float(p[4])))
                except ValueError:
                    pass
                if len(out) == 2:
                    break
        if out:
            _FINRA_MEM[ymd] = out      # publié = définitif, on garde en mémoire
        return out or None
    except Exception:
        return None


def _finnhub_quote(sym):
    """Cote actions US quasi temps réel via Finnhub (env FINNHUB_API_KEY).
    Micro-cache EN MÉMOIRE du process (et non dans Redis) : les instances
    restent chaudes, donc le cache est efficace sans coûter deux opérations
    Redis à chaque poll. L'API tierce reste loin sous la limite du palier
    gratuit (60 req/min).
    Retourne (prix, ts_dernier_trade) ou None — jamais d'exception."""
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return None
    import time as _t
    now = _t.time()
    c = _MEM_FH.get(sym)
    if c and now - c[2] < FINNHUB_CACHE_S and c[0]:
        return c[0], c[1]
    # L'ETF ne cote pas la nuit : inutile d'appeler l'API (et de payer la
    # latence) hors de la fenêtre 4h-20h ET, où le repli future est de toute
    # façon la bonne source.
    try:
        _et = dt.datetime.now(ZoneInfo("America/New_York"))
        if _et.weekday() > 4 or not (4 <= _et.hour < 20):
            return None
    except Exception:
        pass
    # Anti-martèlement : après un échec (timeout, quota), on ne réessaie pas
    # avant quelques secondes — sinon chaque poll relance un appel qui échoue.
    if now - _MEM_FH_ERR.get(sym, 0) < 5:
        return (c[0], c[1]) if c and c[0] and now - c[2] < 60 else None
    try:
        import requests
        r = requests.get("https://finnhub.io/api/v1/quote",
                         params={"symbol": sym, "token": key}, timeout=4)
        j = r.json()
        p, t = j.get("c"), j.get("t")
        if not p:
            raise ValueError("réponse vide")
        _MEM_FH[sym] = (p, t or int(now), now)
        _MEM_FH_ERR.pop(sym, None)
        return p, t or int(now)
    except Exception:
        # SERVIR LE PÉRIMÉ PLUTÔT QUE DE DÉCROCHER : sur un échec ponctuel, on
        # renvoie la dernière cote connue (jusqu'à 60 s) au lieu de retomber
        # sur le future différé — ça évite le saut de prix visible à l'écran.
        _MEM_FH_ERR[sym] = now
        if c and c[0] and now - c[2] < 60:
            return c[0], c[1]
        return None


def _utc_now_iso():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

ROOT = Path(__file__).resolve().parent.parent

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/admin": ("admin.html", "text/html; charset=utf-8"),
    "/dash": ("dash.html", "text/html; charset=utf-8"),
    "/heatmap": ("heatmap.html", "text/html; charset=utf-8"),
    # instantané macro produit par la GitHub Action (FRED est injoignable
    # depuis Vercel) et lu par la bannière du terminal news
    "/macro.json": ("macro.json", "application/json"),
    "/calendar.json": ("calendar.json", "application/json"),
    "/doc": ("doc.html", "text/html; charset=utf-8"),
    "/wiki": ("doc.html", "text/html; charset=utf-8"),
    "/privacy": ("privacy.html", "text/html; charset=utf-8"),
    "/privacy.html": ("privacy.html", "text/html; charset=utf-8"),
    "/flux": ("flux.html", "text/html; charset=utf-8"),
    "/flux.html": ("flux.html", "text/html; charset=utf-8"),
    "/ui.js": ("ui.js", "application/javascript; charset=utf-8"),
    "/flux-panel.js": ("flux-panel.js", "application/javascript; charset=utf-8"),
    "/loader.js": ("loader.js", "application/javascript; charset=utf-8"),
    # socle de thème partagé : theme.css porte le :root canonique (source
    # unique de la palette), theme.js l'expose au JS via window.THEME —
    # var() ne résolvant pas dans une string passée à un canvas ou à
    # lightweight-charts. Toute page qui affiche une couleur charge les deux.
    "/theme.css": ("theme.css", "text/css; charset=utf-8"),
    "/theme.js": ("theme.js", "application/javascript; charset=utf-8"),
    # chassis de navigation partage : une seule liste de destinations pour
    # toutes les pages. Avant, chacune maintenait la sienne -- /horizon
    # n'etait atteignable depuis aucune page et /flux depuis une seule.
    "/shell.js": ("shell.js", "application/javascript; charset=utf-8"),
    "/favicon.png": ("favicon.png", "image/png"),
    "/favicon.ico": ("favicon.png", "image/png"),
    "/thehub-mark.png": ("thehub-mark.png", "image/png"),
    "/dash.html": ("dash.html", "text/html; charset=utf-8"),
    "/admin.html": ("admin.html", "text/html; charset=utf-8"),
    "/history.json": ("history.json", "application/json"),
    "/nq_levels.txt": ("nq_levels.txt", "text/plain; charset=utf-8"),
    "/widget/flux-widget.js": ("widget/flux-widget.js", "application/javascript; charset=utf-8"),
    # fichier de vérification Google Search Console — doit rester servi tel
    # quel à ce chemin exact, jamais renommé/déplacé
    "/google52746d77fd306898.html": ("google52746d77fd306898.html", "text/html; charset=utf-8"),
}

def _upstash_key(target):
    return f"gex:latest:{target}"


def _upstash_conf():
    """Accept both naming schemes: direct Upstash vars and Vercel
    Marketplace/KV aliases (KV_REST_API_*)."""
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
    return (url.rstrip("/"), token) if url and token else (None, None)


def _upstash_get(target="NQ"):
    """Latest published payload for a target from Redis, or None (never raises)."""
    url, token = _upstash_conf()
    if not url:
        return None
    try:
        import requests

        for key in ((_upstash_key(target), "gex:latest") if target == "NQ"
                    else (_upstash_key(target),)):
            r = requests.get(f"{url}/get/{key}",
                             headers={"Authorization": f"Bearer {token}"}, timeout=5)
            r.raise_for_status()
            v = r.json().get("result")
            if v:
                return json.loads(v)
        return None
    except Exception:
        traceback.print_exc()
        return None


def _upstash_set(payload):
    """Publish payload to Redis under its target key. Returns (ok, reason)."""
    url, token = _upstash_conf()
    if not url:
        return False, "no-credentials (variables KV_/UPSTASH_ absentes du déploiement)"
    try:
        import requests

        target = payload.get("target", "NQ")
        key = _upstash_key(target)
        r = requests.post(f"{url}/set/{key}",
                          headers={"Authorization": f"Bearer {token}"},
                          data=json.dumps(payload), timeout=5)
        r.raise_for_status()
        # invalide le cache memoire de _latest_payload sur cette instance
        # chaude : sans ca, une lecture juste apres publication pourrait
        # resservir le payload precedent jusqu'a expiration du TTL court.
        _NEWS_MEM.pop(f"payload:{target}", None)
        return True, "ok"
    except Exception as e:
        traceback.print_exc()
        return False, f"{type(e).__name__}: {e}"


def _load_file_payload(target="NQ"):
    names = [f"levels_{target}.json"] + (["nq_levels.json"] if target == "NQ" else [])
    for name in names:
        p = ROOT / name
        if p.is_file():
            try:
                return json.loads(p.read_text())
            except Exception:
                continue
    return None


def _latest_payload(target="NQ"):
    """Newest of: committed daily snapshot vs last published live refresh.
    ISO timestamps compare correctly as strings.
    Lecture Redis mise en cache memoire courte (meme mecanisme que
    _check_embed_key, cf. _news_cached) : le terminal poll /levels.json
    toutes les 3s en seance active, or le payload publie ne change pas a
    cette cadence -- une lecture par instance chaude toutes les 5s suffit
    largement et evite une lecture Redis par tick utilisateur.

    Piege corrige le 2026-08-18 : le fichier GitHub Actions est une
    PRE-publication du jour (souvent 1h-3h avant le refresh canonique de
    15h25), pas le refresh canonique lui-meme. Avant ce correctif, des que
    son generated_utc depassait celui du payload Redis (systematique des
    la fin du run GH Actions, puisque sa date est "aujourd'hui" contre
    "hier" pour Redis), il ecrasait immediatement les niveaux affiches --
    donc AVANT le refresh de 15h25, en violation directe du verrou des
    niveaux (le flip a saute de 29902 a 29713 en pleine matinee, cf.
    session du 2026-08-18). Tant que gex:lock=1 (etat normal), Redis reste
    seul autoritaire ; le fichier ne reprend la main que si Redis est vide
    (cold start / panne) ou si le verrou est explicitement leve -- memes
    conventions que le reste du module (cf. _gex_locked)."""
    file_p = _load_file_payload(target)
    up_p = _news_cached(f"payload:{target}", 5, lambda: _upstash_get(target))
    if file_p and up_p:
        try:
            locked = kv_get("gex:lock") == "1"
        except Exception:
            locked = False
        if locked:
            return up_p
        return up_p if up_p.get("generated_utc", "") >= file_p.get("generated_utc", "") else file_p
    return up_p or file_p


def _q_target(qs):
    t = (qs.get("target", ["NQ"])[0] or "NQ").upper()
    return t if t in TARGETS else None


# XAU : Yahoo ne publie pas de serie pour l'or comptant (XAUUSD=X -> 404).
# On part donc du future et on decale (voir _gold_offset).
YCHART = {"NQ": "NQ=F", "ES": "ES=F", "SPX": "^GSPC", "GC": "GC=F",
          "XAU": "GC=F"}
# ETF servant de proxy temps réel pendant la séance US (le future est différé)
YETF = {"NQ": "QQQ", "ES": "SPY", "SPX": "SPY", "GC": "GLD", "XAU": "GLD"}
CHART_INTERVALS = {"1m": "1d", "5m": "5d", "15m": "5d"}  # interval -> range
# Dernière matrice gamma connue bonne, par cible (repli de /api/matrix quand
# la chaîne CBOE ne répond pas — cf. le handler pour le raisonnement).
MATRIX_LAST_KEY = "gex:matrix:last:{tgt}"


def _clean_bars(bars):
    """Nettoyage ADAPTATIF des prints hors marché (pré/post ETF).
    Le seuil est LOCAL (fenêtre glissante ±12 bougies), pas global : en M5
    sur 5 jours, la médiane globale est gonflée par les séances US et laisse
    passer des spikes de 120 pts en zone calme. Localement : une mèche est
    bornée à max(4x la médiane des voisines, 0.08% du prix) ; une bougie
    dont le corps dévie fortement de la médiane locale des closes est un
    print isolé -> supprimée. Un vrai mouvement entraîne ses voisines, donc
    la médiane locale suit et il est conservé."""
    n = len(bars)
    if n < 10:
        return bars
    closes = [b["close"] for b in bars]
    ranges = [b["high"] - b["low"] for b in bars]
    out = []
    for i, b in enumerate(bars):
        px = max(abs(b["close"]), 1.0)
        lo_w, hi_w = max(0, i - 12), min(n, i + 13)
        loc_r = sorted(r for j, r in enumerate(ranges[lo_w:hi_w], lo_w)
                       if j != i and r > 0)
        locmed = loc_r[len(loc_r) // 2] if loc_r else 1.0
        # bougie-print isolée : corps loin de la médiane locale des closes
        neigh = sorted(closes[lo_w:i] + closes[i + 1:hi_w])
        if neigh:
            ref = neigh[len(neigh) // 2]
            body_far = max(abs(b["open"] - ref), abs(b["close"] - ref))
            if body_far > max(8.0 * locmed, px * 0.002):
                continue
        # mèches bornées au contexte local
        lim = max(4.0 * locmed, px * 0.0008)
        top = max(b["open"], b["close"])
        bot = min(b["open"], b["close"])
        if b["high"] - top > lim:
            b["high"] = round(top + lim, 2)
        if bot - b["low"] > lim:
            b["low"] = round(bot - lim, 2)
        out.append(b)
    return out


def _yahoo_chart(sym, interval, rng, prepost=False):
    """Fetch Yahoo chart JSON (candles + meta). Isolated for testability."""
    import requests
    from urllib.parse import quote as _q

    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{_q(sym)}"
        f"?interval={interval}&range={rng}"
        + ("&includePrePost=true" if prepost else ""),
        headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    r.raise_for_status()
    return r.json()["chart"]["result"][0]


LINKS_KEY = "gex:links"
DEFAULT_LINKS = {
    "discord": "https://discord.gg/YfCbXDtb4",
    "tradingview": "https://www.tradingview.com/script/TfBS3GjM-GEX-Levels-Dealer-Gamma-Exposure/",
}
LINK_PREFIXES = {
    "discord": ("https://discord.gg/", "https://discord.com/invite/"),
    "tradingview": ("https://www.tradingview.com/", "https://tradingview.com/"),
    # don : Stripe Payment Link, Ko-fi, PayPal, Buy Me a Coffee...
    # pas de défaut -> tant que c'est vide, le bouton n'existe pas sur le site
    "donate": ("https://",),
}


def _links():
    try:
        stored = json.loads(kv_get(LINKS_KEY) or "{}")
    except Exception:
        stored = {}
    return {**DEFAULT_LINKS, **{k: v for k, v in stored.items() if v}}


# --------------------------------------------------------------------------- #
# Auto-level : indicateurs prets a poser (TradingView = extension Chrome,     #
# lien seul ; Quantower/MotiveWave = fichier binaire uploade par l'admin).    #
# --------------------------------------------------------------------------- #
# Meme forme de stockage que LINKS_KEY (un seul blob JSON en clair) -- pas de
# schema signe, ce n'est pas un secret cryptographique. TradingView est un
# lien VALIDE (URL Chrome Web Store), pas un fichier : traite a part de
# AUTOLEVEL_PLATFORMS, qui ne couvre que les deux plateformes a fichier.
AUTOLEVEL_KEY = "gex:autolevel"
AUTOLEVEL_DEFAULT_TV_URL = ("https://chromewebstore.google.com/detail/"
                            "the-hub-%E2%80%94-gex-levels-for/cjgojmkgocbahkenanplgcehikcoigdc")
AUTOLEVEL_PLATFORMS = ("quantower", "motivewave")
# 2 Mo decodes : tres large pour un indicateur compile (.dll/.jar, typiquement
# quelques centaines de Ko), tout en restant loin de la limite de payload
# Vercel (~4.5 Mo) une fois re-inflate par le base64 (~+33%) + l'enveloppe JSON.
AUTOLEVEL_MAX_FILE_BYTES = 2 * 1024 * 1024


def _autolevel():
    try:
        stored = json.loads(kv_get(AUTOLEVEL_KEY) or "{}")
    except Exception:
        stored = {}
    return stored if isinstance(stored, dict) else {}


def _autolevel_meta():
    """Vue jamais accompagnee de data_b64 -- tout ce qu'il faut pour peindre
    un bouton de telechargement ou un lien, jamais le contenu du fichier."""
    stored = _autolevel()
    tv = stored.get("tradingview") or {}
    out = {"tradingview": {"url": tv.get("url") or AUTOLEVEL_DEFAULT_TV_URL}}
    for p in AUTOLEVEL_PLATFORMS:
        f = stored.get(p)
        out[p] = ({"filename": f.get("filename"), "size": f.get("size"),
                   "uploaded_at": f.get("uploaded_at")} if f else None)
    return out


VALID_HOOK_PREFIXES = ("https://discord.com/api/webhooks/",
                       "https://discordapp.com/api/webhooks/",
                       "https://ptb.discord.com/api/webhooks/",
                       "https://canary.discord.com/api/webhooks/")


def paris_hhmm():
    """Current Europe/Paris local time as HH:MM (DST handled by zoneinfo)."""
    from zoneinfo import ZoneInfo
    import datetime as _dt

    return _dt.datetime.now(ZoneInfo("Europe/Paris")).strftime("%H:%M")


def _mask(url):
    return ("…" + url[-6:]) if url else None


class handler(BaseHTTPRequestHandler):
    def _gex_locked(self):
        try:
            return kv_get("gex:lock") == "1"
        except Exception:
            return False

    @staticmethod
    def _preserve_daily(new_p, old_p):
        """Le bloc DAILY (grille Open + EM) est calculé UNE SEULE FOIS, au
        recalcul nocturne. Tout refresh ultérieur portant sur le même open le
        reprend tel quel : les bandes ne bougent pas en cours de séance.
        Si l'ancre a changé (nocturne raté), on laisse passer les valeurs
        fraîches — auto-réparation."""
        a_new = (new_p.get("open_grid") or {}).get("anchor")
        a_old = (old_p.get("open_grid") or {}).get("anchor")
        if a_new is None or a_old is None or abs(a_new - a_old) > 0.6:
            return False
        new_p["open_grid"] = old_p["open_grid"]
        if old_p.get("expected_move") is not None:
            new_p["expected_move"] = old_p["expected_move"]
        old_map = {(L.get("kind"), L.get("label")): L.get("price_nq")
                   for L in old_p.get("levels", [])
                   if L.get("kind") in ("emh", "eml", "emb")}
        for L in new_p.get("levels", []):
            key = (L.get("kind"), L.get("label"))
            if key in old_map:
                L["price_nq"] = old_map[key]
        rows = [(L["price_nq"], L["label"], L["kind"])
                for L in new_p.get("levels", [])]
        new_p["pine"] = build_pine(rows, new_p["open_grid"])
        new_p["daily_from"] = old_p.get("daily_refresh_utc")
        return True

    @staticmethod
    def _freeze_levels(new_p, old_p):
        """Verrou GEX actif : le nouveau payload garde les NIVEAUX du
        précédent (GEX, grille Open, EM, pine) — prix/basis/IV/badges
        continuent de se rafraîchir.

        Piège : expected_move.anchor/anchor_idx sont figés avec le `basis`
        du moment du calcul original, alors que new_p["basis"] (top-niveau)
        continue lui de bouger à chaque refresh. `anchor - anchor_idx` ne
        redonnera donc PLUS le basis courant une fois gelé -- ce n'est pas
        une incohérence de calcul (straddle/em_pct/bandes restent tous
        cohérents ENTRE EUX), juste ce champ diagnostic isolé qui devient
        irreconstituable depuis payload["basis"]. Ne pas "corriger" en
        recalculant anchor_idx à la volée : ça romprait la cohérence avec
        le straddle/em_pct déjà gelés à côté."""
        for k in ("levels", "gex_by_strike", "open_grid",
                  "expected_move", "pine"):
            if old_p.get(k) is not None:
                new_p[k] = old_p[k]
        new_p["levels_locked"] = True

    @staticmethod
    def _stamp_iv_ref(new_p, old_p, canonical):
        """iv_atm FIGE au moment de la publication canonique (15h25) —
        distinct de new_p["iv_atm"], qui continue lui de bouger à chaque
        refresh intrajournalier. /api/flow compare les deux (dsigma) pour
        mesurer le crush de vol en cours de séance ; sans ce gel, la
        référence dériverait avec le marché et l'écart resterait toujours
        à zéro."""
        same_day = bool(old_p) and old_p.get("date") == new_p.get("date")
        if canonical or not same_day or old_p.get("iv_ref") is None:
            new_p["iv_ref"] = new_p.get("iv_atm")
        else:
            new_p["iv_ref"] = old_p.get("iv_ref")

    def _auth_key(self, qs=None):
        """True if the request carries a valid GEX_REFRESH_KEY."""
        secret = os.environ.get("GEX_REFRESH_KEY")
        if not secret:
            return True
        given = self.headers.get("x-gex-key") or ((qs or {}).get("key", [None])[0] or "")
        return bool(given) and hmac.compare_digest(given, secret)

    # ═══════════════ AUTHENTIFICATION ═══════════════
    # Mots de passe : PBKDF2-HMAC-SHA256, 200 000 itérations, sel aléatoire par
    # utilisateur — jamais de mot de passe en clair, jamais réversible.
    # Sessions : jeton SIGNÉ (HMAC) dans un cookie HttpOnly. Rien n'est stocké
    # côté serveur, donc aucune lecture Redis par requête ; la validité est
    # portée par la signature et l'horodatage d'expiration.

    @staticmethod
    def _auth_secret():
        return (os.environ.get("GEX_AUTH_SECRET")
                or os.environ.get("GEX_REFRESH_KEY") or "gexdash-dev").encode()

    @staticmethod
    def _pw_hash(password, salt=None):
        salt = salt or secrets.token_hex(16)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
        return salt, dk.hex()

    @classmethod
    def _pw_verify(cls, password, salt, expected):
        _, got = cls._pw_hash(password, salt)
        return hmac.compare_digest(got, expected)   # comparaison à temps constant

    @staticmethod
    def _valid_username(user):
        return (3 <= len(user) <= 32) and user.replace("_", "").replace("-", "").isalnum()

    @staticmethod
    def _valid_email(email):
        # validation volontairement simple : on veut une adresse exploitable
        # pour recontacter le membre, pas un filtrage exhaustif
        return not ("@" not in email or "." not in email.split("@")[-1]
                    or len(email) < 6 or len(email) > 120 or " " in email)

    @classmethod
    def _mk_token(cls, user, days=30):
        exp = int(time.time()) + days * 86400
        body = f"{user}|{exp}"
        sig = hmac.new(cls._auth_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
        return base64.urlsafe_b64encode(f"{body}|{sig}".encode()).decode().rstrip("=")

    @classmethod
    def _read_token(cls, tok):
        """Retourne le nom d'utilisateur si le jeton est valide et non expiré."""
        try:
            pad = "=" * (-len(tok) % 4)
            raw = base64.urlsafe_b64decode(tok + pad).decode()
            user, exp, sig = raw.rsplit("|", 2)
            body = f"{user}|{exp}"
            good = hmac.new(cls._auth_secret(), body.encode(),
                            hashlib.sha256).hexdigest()[:32]
            if not hmac.compare_digest(sig, good):
                return None
            if int(exp) < time.time():
                return None
            return user
        except Exception:
            return None

    def _cookie(self, name):
        raw = self.headers.get("Cookie", "") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def _current_user(self):
        tok = self._cookie("gexauth")
        return self._read_token(tok) if tok else None

    @staticmethod
    def _users():
        try:
            return json.loads(kv_get("gex:users") or "{}")
        except Exception:
            return {}

    @staticmethod
    def _invites():
        try:
            return json.loads(kv_get("gex:invites") or "{}")
        except Exception:
            return {}

    def _client_ip(self):
        fwd = (self.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        return (fwd or self.headers.get("x-real-ip")
                or (self.client_address[0] if self.client_address else "")
                or "")

    def _origin_hash(self):
        """Empreinte non reversible (IP + user-agent), JAMAIS l'IP en clair --
        prefixe de domaine pour ne pas reutiliser cette meme paire (secret,
        message) que pour les jetons de session, meme si les deux partagent
        _auth_secret()."""
        raw = f"apiorigin|{self._client_ip()}|{self.headers.get('user-agent', '')}"
        return hmac.new(self._auth_secret(), raw.encode(), hashlib.sha256).hexdigest()[:12]

    def _apikey_log_call(self, given):
        """Best-effort : une panne d'ecriture ici ne doit jamais faire
        echouer l'appel /api/mylevels principal. Une seule tranche horaire
        touchee (cf. _apikey_hour_key) -- lecture-modification-ecriture, mais
        le risque de collision est borne a la meme cle ET la meme heure."""
        hkey = _apikey_hour_key(given)
        try:
            raw = kv_get(hkey)
            log = json.loads(raw) if raw else []
            if not isinstance(log, list):
                log = []
        except Exception:
            log = []
        log.append({"t": int(time.time()), "o": self._origin_hash()})
        if len(log) > APILOG_MAX:
            log = log[-APILOG_MAX:]
        try:
            kv_set(hkey, json.dumps(log), ex=APILOG_TTL)
        except Exception:
            pass

    def _apikey_regenerate(self, user):
        """Cree ou remplace la cle d'un `user` (gex:users). L'ancienne cle
        est retiree du blob APIKEYS_KEY -- invalide immediatement, comme pour
        les cles du widget Flux. Retourne la nouvelle cle, ou None si le
        compte n'existe pas."""
        users = self._users()
        if user not in users:
            return None
        keys = fetch_api_keys()
        old = users[user].get("apikey")
        if old:
            keys.pop(old, None)
        new_key = secrets.token_urlsafe(32)
        keys[new_key] = {
            "user": user, "state": "active",
            "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        save_api_keys(keys)
        users[user]["apikey"] = new_key
        kv_set("gex:users", json.dumps(users))
        return new_key

    def _apikey_set_state(self, user, state):
        """state in {"active", "blocked", "revoked"} -- reversible pour les
        deux premiers, definitif pour "revoked" (la cle reste dans le blob
        mais /api/mylevels la refusera pour toujours)."""
        users = self._users()
        key = (users.get(user) or {}).get("apikey")
        if not key:
            return False
        keys = fetch_api_keys()
        if key not in keys:
            return False
        keys[key]["state"] = state
        save_api_keys(keys)
        return True

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # ── inscription / connexion / déconnexion ──
        if path == "/api/auth":
            qs = parse_qs(parsed.query)
            act = (qs.get("action", ["login"])[0] or "login").lower()
            body = self._read_json()
            user = (body.get("user") or "").strip().lower()
            pwd = body.get("pass") or ""

            def fail(code, msg):
                self._send(code, json.dumps({"error": msg}).encode(),
                           "application/json")

            if act == "logout":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie",
                                 "gexauth=; Path=/; Max-Age=0; HttpOnly; "
                                 "Secure; SameSite=Lax")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                return

            if act == "register":
                code = (body.get("code") or "").strip()
                email = (body.get("email") or "").strip().lower()
                if not self._valid_username(user):
                    return fail(400, "Identifiant invalide (3-32 caractères, lettres/chiffres)")
                if not self._valid_email(email):
                    return fail(400, "Adresse e-mail invalide")
                if any(v.get("email") == email for v in self._users().values()):
                    return fail(409, "Cette adresse est déjà utilisée")
                if len(pwd) < 8:
                    return fail(400, "Mot de passe : 8 caractères minimum")
                invites = self._invites()
                inv = invites.get(code)
                if not inv or inv.get("used_by"):
                    return fail(403, "Code d'invitation invalide ou déjà utilisé")
                users = self._users()
                if user in users:
                    return fail(409, "Cet identifiant existe déjà")
                salt, h = self._pw_hash(pwd)
                users[user] = {"salt": salt, "hash": h, "email": email,
                               "note": inv.get("note", ""),
                               "created": dt.datetime.now(dt.timezone.utc)
                                            .isoformat(timespec="seconds")}
                inv["used_by"] = user
                inv["used_at"] = users[user]["created"]
                kv_set("gex:users", json.dumps(users))
                kv_set("gex:invites", json.dumps(invites))
                tok = self._mk_token(user)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie",
                                 f"gexauth={tok}; Path=/; Max-Age={30*86400}; "
                                 "HttpOnly; Secure; SameSite=Lax")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "user": user}).encode())
                return

            # connexion — verrou après échecs répétés
            lock_key = f"gex:lockout:{user}"
            try:
                fails = int(kv_get(lock_key) or 0)
            except Exception:
                fails = 0
            if fails >= 8:
                return fail(429, "Trop de tentatives — réessaie dans 15 minutes")
            # Base injoignable : on refuse l'accès (sens de défaillance sûr),
            # mais avec un message distinct — sinon l'utilisateur croit s'être
            # trompé de mot de passe alors que le service est en panne.
            try:
                all_users = self._users()
                if not all_users and kv_get("gex:users") is None:
                    pass          # base réellement vide : premier compte à créer
            except Exception:
                return fail(503, "Service temporairement indisponible, réessaie")
            u = all_users.get(user)
            if not u or not self._pw_verify(pwd, u.get("salt", ""), u.get("hash", "")):
                try:
                    kv_set(lock_key, str(fails + 1), ex=900)
                except Exception:
                    pass
                return fail(401, "Identifiant ou mot de passe incorrect")
            try:
                kv_set(lock_key, "0", ex=1)
            except Exception:
                pass
            tok = self._mk_token(user)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie",
                             f"gexauth={tok}; Path=/; Max-Age={30*86400}; "
                             "HttpOnly; Secure; SameSite=Lax")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "user": user}).encode())
            return

        # ── page profil (membre connecté) : e-mail / mot de passe / pseudo /
        # régénération de la clé API personnelle -- gardé par la SESSION
        # (cookie gexauth), jamais la clé admin : c'est un compte qui gère
        # son propre compte, pas une action d'administration. Indépendant du
        # mot de passe : régénérer/bloquer/révoquer la clé API ne touche
        # jamais gex:users (salt/hash), et changer le mot de passe ici ne
        # touche jamais la clé API. ──
        if path == "/api/profile":
            me = self._current_user()
            if not me:
                self._send(401, json.dumps({"error": "connexion requise"}).encode(),
                           "application/json")
                return
            users = self._users()
            u = users.get(me)
            if not u:
                self._send(401, json.dumps({"error": "compte introuvable"}).encode(),
                           "application/json")
                return
            body = self._read_json()
            op = (body.get("op") or "").lower()

            def fail(code, msg):
                self._send(code, json.dumps({"error": msg}).encode(), "application/json")

            # Échelle d'affichage du terminal : "fut" (NQ/ES, le future —
            # défaut historique) ou "idx" (NAS100/US500, l'indice cash). Ce
            # n'est PAS un marché de plus : c'est le même produit lu à
            # l'autre bout de la basis, exactement comme GC vs XAUUSD. Le
            # moteur calcule d'ailleurs déjà tout en échelle indice (les
            # strikes CBOE SONT des strikes d'indice) et n'ajoute la basis
            # qu'en dernière étape -- afficher l'indice, c'est retirer cette
            # étape, pas recalculer quoi que ce soit.
            if op == "scale":
                sc = (body.get("scale") or "").strip().lower()
                if sc not in ("fut", "idx"):
                    return fail(400, "scale doit valoir fut ou idx")
                u["scale"] = sc
                kv_set("gex:users", json.dumps(users))
                _USER_PREF.pop(me, None)      # invalide le cache mémoire
                self._send(200, json.dumps({"ok": True, "scale": sc}).encode(),
                           "application/json")
                return

            if op == "email":
                email = (body.get("email") or "").strip().lower()
                if not self._valid_email(email):
                    return fail(400, "Adresse e-mail invalide")
                if any(k != me and v.get("email") == email for k, v in users.items()):
                    return fail(409, "Cette adresse est déjà utilisée")
                u["email"] = email
                kv_set("gex:users", json.dumps(users))
                self._send(200, json.dumps({"ok": True, "email": email}).encode(),
                           "application/json")
                return

            if op == "password":
                current = body.get("current") or ""
                new = body.get("new") or ""
                if not self._pw_verify(current, u.get("salt", ""), u.get("hash", "")):
                    return fail(401, "Mot de passe actuel incorrect")
                if len(new) < 8:
                    return fail(400, "Nouveau mot de passe : 8 caractères minimum")
                salt, h = self._pw_hash(new)
                u.update({"salt": salt, "hash": h})
                kv_set("gex:users", json.dumps(users))
                self._send(200, json.dumps({"ok": True}).encode(), "application/json")
                return

            if op == "username":
                new_user = (body.get("new_user") or "").strip().lower()
                if not self._valid_username(new_user):
                    return fail(400, "Identifiant invalide (3-32 caractères, lettres/chiffres)")
                if new_user == me:
                    return fail(400, "Identique à l'identifiant actuel")
                if new_user in users:
                    return fail(409, "Cet identifiant existe déjà")
                users[new_user] = users.pop(me)
                kv_set("gex:users", json.dumps(users))
                apikey = users[new_user].get("apikey")
                if apikey:
                    api_keys = fetch_api_keys()
                    if apikey in api_keys:
                        api_keys[apikey]["user"] = new_user
                        save_api_keys(api_keys)
                tok = self._mk_token(new_user)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie",
                                 f"gexauth={tok}; Path=/; Max-Age={30*86400}; "
                                 "HttpOnly; Secure; SameSite=Lax")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "user": new_user}).encode())
                return

            if op == "apikey_regenerate":
                # un blocage/révocation admin doit garder ses dents : sinon
                # l'auto-régénération le contournerait instantanément.
                cur_key = u.get("apikey")
                cur_state = fetch_api_keys().get(cur_key, {}).get("state") if cur_key else None
                if cur_state in ("blocked", "revoked"):
                    return fail(403, "Clé bloquée ou révoquée par un administrateur "
                                     "— contacte-le pour la rétablir")
                new_key = self._apikey_regenerate(me)
                self._send(200, json.dumps({"ok": True, "key": new_key}).encode(),
                           "application/json")
                return

            return fail(400, "opération inconnue")

        # ── administration des comptes (protégée par la clé admin) ──
        if path == "/api/users":
            if not self._auth_key():
                self._send(401, json.dumps({"error": "unauthorized"}).encode(),
                           "application/json")
                return
            body = self._read_json()
            op = (body.get("op") or "").lower()
            users, invites = self._users(), self._invites()
            if op == "invite":
                code = secrets.token_urlsafe(9)
                invites[code] = {"note": (body.get("note") or "").strip()[:80],
                                 "created": dt.datetime.now(dt.timezone.utc)
                                              .isoformat(timespec="seconds"),
                                 "used_by": None}
                kv_set("gex:invites", json.dumps(invites))
                self._send(200, json.dumps({"ok": True, "code": code}).encode(),
                           "application/json")
                return
            if op == "delete":
                users.pop((body.get("user") or "").lower(), None)
                kv_set("gex:users", json.dumps(users))
            elif op == "reset":
                u = (body.get("user") or "").lower()
                new_pwd = body.get("pass") or ""
                if u not in users or len(new_pwd) < 8:
                    self._send(400, json.dumps({"error": "utilisateur inconnu ou "
                                                "mot de passe trop court"}).encode(),
                               "application/json")
                    return
                salt, h = self._pw_hash(new_pwd)
                users[u].update({"salt": salt, "hash": h})
                kv_set("gex:users", json.dumps(users))
            elif op == "note":
                u = (body.get("user") or "").lower()
                if u in users:
                    users[u]["note"] = (body.get("note") or "").strip()[:80]
                    kv_set("gex:users", json.dumps(users))
            elif op == "revoke":
                invites.pop(body.get("code") or "", None)
                kv_set("gex:invites", json.dumps(invites))
            elif op in ("apikey_block", "apikey_unblock", "apikey_revoke"):
                u = (body.get("user") or "").lower()
                state = {"apikey_block": "blocked", "apikey_unblock": "active",
                         "apikey_revoke": "revoked"}[op]
                if not self._apikey_set_state(u, state):
                    self._send(400, json.dumps(
                        {"error": "utilisateur inconnu ou sans clé API"}).encode(),
                        "application/json")
                    return
            elif op == "apikey_regenerate":
                u = (body.get("user") or "").lower()
                new_key = self._apikey_regenerate(u)
                if new_key is None:
                    self._send(400, json.dumps({"error": "utilisateur inconnu"}).encode(),
                               "application/json")
                    return
                self._send(200, json.dumps({"ok": True, "key": new_key}).encode(),
                           "application/json")
                return
            else:
                self._send(400, json.dumps({"error": "opération inconnue"}).encode(),
                           "application/json")
                return
            self._send(200, json.dumps({"ok": True}).encode(), "application/json")
            return

        if path == "/api/goldbasis":
            if not self._auth_key():
                self._send(401, json.dumps({"error": "unauthorized"}).encode(),
                           "application/json")
                return
            body = self._read_json()
            v = body.get("basis")
            if v in (None, ""):
                kv_set("gex:goldbasis", "null")
                self._send(200, json.dumps({"ok": True, "basis": None}).encode(),
                           "application/json")
                return
            try:
                v = float(str(v).replace(",", "."))
            except Exception:
                self._send(400, json.dumps({"error": "valeur invalide"}).encode(),
                           "application/json")
                return
            kv_set("gex:goldbasis", json.dumps(v))
            self._send(200, json.dumps({"ok": True, "basis": v}).encode(),
                       "application/json")
            return

        if path == "/api/feeds":
            if not self._auth_key():
                self._send(401, json.dumps({"error": "unauthorized"}).encode(),
                           "application/json")
                return
            body = self._read_json()
            feeds = body.get("feeds")
            if not isinstance(feeds, list):
                self._send(400, json.dumps({"error": "liste attendue"}).encode(),
                           "application/json")
                return
            clean = [{"n": (f.get("n") or "?")[:40], "u": (f.get("u") or "")[:300],
                      "t": (f.get("t") or "")[:24]}
                     for f in feeds if (f.get("u") or "").startswith("http")][:30]
            kv_set("gex:feeds", json.dumps(clean))
            _NEWS_MEM.pop("feed", None)         # purge immédiate du cache
            self._send(200, json.dumps({"ok": True, "n": len(clean)}).encode(),
                       "application/json")
            return

        if path == "/api/webhooks":
            if not self._auth_key():
                self._send(401, json.dumps({"error": "unauthorized"}).encode(), "application/json")
                return
            body = self._read_json()
            cfg = fetch_webhooks()
            changed = []
            for tgt in list(TARGETS) + ["default", "news", "horizon"]:
                if tgt not in body:
                    continue
                v = (body.get(tgt) or "").strip()
                if v == "":
                    if tgt in cfg:
                        cfg.pop(tgt)
                        changed.append(tgt)
                elif v.startswith(VALID_HOOK_PREFIXES):
                    cfg[tgt] = v
                    changed.append(tgt)
                else:
                    self._send(400, json.dumps(
                        {"error": f"{tgt}: URL invalide (doit commencer par discord.com/api/webhooks/)"}
                    ).encode(), "application/json")
                    return
            ok = save_webhooks(cfg)
            self._send(200 if ok else 500, json.dumps({
                "saved": ok, "changed": changed,
                "config": {k: _mask(v) for k, v in cfg.items()},
            }).encode(), "application/json")
            return

        # ── widget Flux embarquable : creation/revocation de cles,
        # provisoire (pas de panel de gestion pour l'instant) -- scriptable
        # en curl, meme porte admin que /api/webhooks. Le futur panel
        # n'aura qu'a habiller cet endpoint d'une UI, aucune migration a
        # prevoir. ──
        if path == "/api/embed-keys":
            if not self._auth_key():
                self._send(401, json.dumps({"error": "unauthorized"}).encode(), "application/json")
                return
            body = self._read_json()
            action = (body.get("action") or "").strip()
            keys = fetch_embed_keys()
            if action == "create":
                label = (body.get("label") or "").strip()
                new_key = secrets.token_urlsafe(24)
                keys[new_key] = {
                    "label": label,
                    "created": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "revoked": False,
                }
                ok = save_embed_keys(keys)
                self._send(200 if ok else 500, json.dumps({
                    "saved": ok, "key": new_key, "label": label,
                }).encode(), "application/json")
                return
            if action == "revoke":
                given = (body.get("key") or "").strip()
                if given not in keys:
                    self._send(404, json.dumps({"error": "unknown key"}).encode(), "application/json")
                    return
                keys[given]["revoked"] = True
                ok = save_embed_keys(keys)
                self._send(200 if ok else 500, json.dumps({"saved": ok}).encode(), "application/json")
                return
            self._send(400, json.dumps({"error": "action must be create or revoke"}).encode(),
                       "application/json")
            return

        if path == "/api/links":
            if not self._auth_key():
                self._send(401, json.dumps({"error": "unauthorized"}).encode(), "application/json")
                return
            body = self._read_json()
            try:
                stored = json.loads(kv_get(LINKS_KEY) or "{}")
            except Exception:
                stored = {}
            for k in ("discord", "tradingview", "donate"):
                if k not in body:
                    continue
                v = (body.get(k) or "").strip()
                if v == "":
                    stored.pop(k, None)  # retour à la valeur par défaut
                elif v.startswith(LINK_PREFIXES[k]):
                    stored[k] = v
                else:
                    self._send(400, json.dumps(
                        {"error": f"{k}: URL invalide (préfixe attendu : {' ou '.join(LINK_PREFIXES[k])})"}
                    ).encode(), "application/json")
                    return
            ok = kv_set(LINKS_KEY, json.dumps(stored))
            self._send(200 if ok else 500,
                       json.dumps({"saved": ok, "links": _links()}).encode(), "application/json")
            return

        # ---- admin: gestion des indicateurs auto-level (TradingView =
        # lien Chrome Web Store ; Quantower/MotiveWave = fichier) ----
        if path == "/api/autolevel":
            if not self._auth_key():
                self._send(401, json.dumps({"error": "unauthorized"}).encode(), "application/json")
                return
            # Content-Length verifie AVANT de lire rfile : un fichier depasse
            # AUTOLEVEL_MAX_FILE_BYTES une fois decode, mais le corps JSON+b64
            # qui le porte est deja ~1.4x plus gros -- rejeter tot evite de
            # bloquer le worker sur une lecture couteuse pour rien.
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
            except Exception:
                n = 0
            if n > int(AUTOLEVEL_MAX_FILE_BYTES * 1.5):
                self._send(413, json.dumps(
                    {"error": f"fichier trop volumineux (max {AUTOLEVEL_MAX_FILE_BYTES // (1024*1024)} Mo)"}
                ).encode(), "application/json")
                return
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                body = {}
            platform = (body.get("platform") or "").strip()
            action = (body.get("action") or "").strip()
            stored = _autolevel()

            if platform == "tradingview":
                if action == "set_url":
                    url = (body.get("url") or "").strip()
                    if not url.startswith("https://chromewebstore.google.com/"):
                        self._send(400, json.dumps(
                            {"error": "URL invalide (Chrome Web Store attendu)"}
                        ).encode(), "application/json")
                        return
                    stored["tradingview"] = {"url": url}
                elif action == "delete":
                    stored.pop("tradingview", None)
                else:
                    self._send(400, json.dumps({"error": "action invalide"}).encode(), "application/json")
                    return
            elif platform in AUTOLEVEL_PLATFORMS:
                if action == "set_file":
                    filename = (body.get("filename") or "").strip()
                    data_b64 = body.get("data_b64") or ""
                    if not filename or not data_b64:
                        self._send(400, json.dumps(
                            {"error": "filename et data_b64 requis"}
                        ).encode(), "application/json")
                        return
                    try:
                        raw = base64.b64decode(data_b64, validate=True)
                    except Exception:
                        self._send(400, json.dumps({"error": "data_b64 invalide"}).encode(), "application/json")
                        return
                    if len(raw) > AUTOLEVEL_MAX_FILE_BYTES:
                        self._send(413, json.dumps(
                            {"error": f"fichier trop volumineux (max {AUTOLEVEL_MAX_FILE_BYTES // (1024*1024)} Mo)"}
                        ).encode(), "application/json")
                        return
                    stored[platform] = {
                        "filename": filename, "size": len(raw), "data_b64": data_b64,
                        "uploaded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    }
                elif action == "delete":
                    stored.pop(platform, None)
                else:
                    self._send(400, json.dumps({"error": "action invalide"}).encode(), "application/json")
                    return
            else:
                self._send(400, json.dumps({"error": "platform invalide"}).encode(), "application/json")
                return

            ok = kv_set(AUTOLEVEL_KEY, json.dumps(stored))
            self._send(200 if ok else 500,
                       json.dumps({"saved": ok, "autolevel": _autolevel_meta()}).encode(), "application/json")
            return

        if path == "/api/webhooks/test":
            if not self._auth_key():
                self._send(401, json.dumps({"error": "unauthorized"}).encode(), "application/json")
                return
            tgt = (self._read_json().get("target") or "NQ").upper()
            if tgt == "NEWS":
                ok = discord_news("🧪 Test du canal News — GEX Terminal")
                self._send(200, json.dumps({"sent": ok, "target": "NEWS"}).encode(),
                           "application/json")
                return
            if tgt == "HORIZON":
                cfg = fetch_webhooks()
                if not cfg.get("horizon"):
                    self._send(200, json.dumps(
                        {"sent": False, "target": "HORIZON",
                         "error": "aucun webhook configuré sur cette ligne"}
                    ).encode(), "application/json")
                    return
                ok = discord_news("🧪 Test du canal Horizon — GEX Terminal", key="horizon")
                self._send(200, json.dumps({"sent": ok, "target": "HORIZON"}).encode(),
                           "application/json")
                return
            if tgt not in TARGETS and tgt != "DEFAULT":
                self._send(400, json.dumps({"error": "target invalide"}).encode(), "application/json")
                return
            # Test = envoi à l'URL EXACTE de la ligne testée. Aucun routage,
            # aucun fallback : si la ligne n'a pas de webhook, on le dit.
            cfg = fetch_webhooks()
            key = "default" if tgt == "DEFAULT" else tgt
            url = cfg.get(key)
            if key == "default" and not url:
                url = os.environ.get("DISCORD_WEBHOOK_URL")
            if not url:
                self._send(200, json.dumps(
                    {"sent": False, "target": key,
                     "error": "aucun webhook configuré sur cette ligne"}
                ).encode(), "application/json")
                return
            fake = {"target": tgt if tgt != "DEFAULT" else "NQ",
                    "mode": "snapshot", "date": et_today().isoformat(),
                    "generated_utc": "", "regime": "positive",
                    "levels": [], "pine": "",
                    "expected_move": None, "net_gex_bn": 0, "pc_oi": None,
                    "nq_price": None, "basis": 0, "basis_source": "test"}
            ok = discord_send(url, fake, note=f"🧪 Test webhook — ligne {key}")
            self._send(200, json.dumps({"sent": ok, "target": key}).encode(), "application/json")
            return

        if path == "/api/cron":
            self._cron(parsed)
            return

        self._send(404, json.dumps({"error": "not found"}).encode(), "application/json")

    def _send(self, code, body, ctype, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cron(self, parsed):
        """Shared by GET (browser / Vercel cron) and POST (QStash schedules).
        EVERY hit is journaled to Redis (gex:cron:log) so failures are never
        silent. Auth: x-gex-key header/param, CRON_SECRET bearer, or Vercel's
        own cron user-agent. Computes+publishes stale targets; ?force=1
        recomputes all, and any hit between 00:00-03:00 Paris auto-forces
        (Globex-open anchor refresh). Market Discord ping: ?notify=1, or a
        non-intraday Vercel-cron hit between 15:20 and 18:00 Paris (backup
        notifier) — ?intraday=1 NEVER pings Discord, whatever the time. The
        kv guard is only set once this same request actually published
        fresh canonical levels (`computed` non-empty); a hit that merely
        re-sends the last published payload (freshness-guard skip branch)
        never locks out a genuine canonical publish that follows. At most
        ONCE per day either way. The 'news' webhook receives a short note
        on every run that actually recomputed data."""
        qs = parse_qs(parsed.query)
        ua = self.headers.get("user-agent", "") or ""
        entry = {"utc": _utc_now_iso(), "paris": paris_hhmm(),
                 "q": parsed.query or "", "ua": ua[:60], "outcome": "?"}

        def journal(outcome):
            entry["outcome"] = outcome
            try:
                log = json.loads(kv_get(CRON_LOG_KEY) or "[]")
                if not isinstance(log, list):
                    log = []
            except Exception:
                log = []
            log.insert(0, entry)
            kv_set(CRON_LOG_KEY, json.dumps(log[:15]), ex=14 * 86400)

        def journal_flow(outcome):
            """Meme forme que journal() ci-dessus, cle dediee FLOW_CRON_LOG_KEY
            -- reserve au bruit routine des tirs intrajournaliers (cf. note sur
            FLOW_CRON_LOG_KEY plus haut), jamais aux events rares/importants."""
            entry["outcome"] = outcome
            try:
                log = json.loads(kv_get(FLOW_CRON_LOG_KEY) or "[]")
                if not isinstance(log, list):
                    log = []
            except Exception:
                log = []
            log.insert(0, entry)
            kv_set(FLOW_CRON_LOG_KEY, json.dumps(log[:FLOW_CRON_LOG_MAX]), ex=14 * 86400)

        cron_secret = os.environ.get("CRON_SECRET")
        gex_key = os.environ.get("GEX_REFRESH_KEY")
        auth = self.headers.get("Authorization", "")
        given_key = self.headers.get("x-gex-key") or (qs.get("key", [None])[0] or "")
        ok_cron = cron_secret and hmac.compare_digest(auth, f"Bearer {cron_secret}")
        ok_key = gex_key and hmac.compare_digest(given_key, gex_key)
        ok_vercel = ua.startswith("vercel-cron")
        if (cron_secret or gex_key) and not (ok_cron or ok_key or ok_vercel):
            journal("401 unauthorized")
            self._send(401, json.dumps({"error": "unauthorized"}).encode(), "application/json")
            return
        try:
            today = et_today().isoformat()
            now_p = paris_hhmm().replace(":", "")
            results, computed, cache = {}, [], {}
            # ---- XR : snapshot du profil GEX par strike, toutes les 15 min
            #      (schedule dédié). N'écrit QUE l'historique du jour ; les
            #      niveaux publiés (walls/EM de 15h25) ne bougent pas. ----
            if "xr" in qs:
                snaps = {}
                for target in TARGETS:
                    try:
                        p = build_payload(target=target, mode="snapshot",
                                          chain_cache=cache)
                        prof = p.get("gex_by_strike") or []
                        if not prof:
                            snaps[target] = 0
                            continue
                        key = f"gex:xr:{target}:{today}"
                        try:
                            hist = json.loads(kv_get(key) or "[]")
                        except Exception:
                            hist = []
                        hist.append({"t": int(time.time()),
                                     "px": p.get("nq_price"),
                                     "prof": prof})
                        hist = hist[-60:]
                        kv_set(key, json.dumps(hist), ex=3 * 86400)
                        snaps[target] = len(hist)
                    except Exception as e:
                        journal(f"xr {target} KO: {e}")
                        snaps[target] = -1
                kv_set("gex:xr:last", json.dumps(
                    {"utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                     "paris": paris_hhmm(), "snaps": snaps}), ex=86400)
                self._send(200, json.dumps({"xr": True, "date": today,
                                            "snaps": snaps}).encode(),
                           "application/json")
                return
            # ---- Filet de securite macro.json : verifie que le cron GitHub
            #      Actions "Macro snapshot" a bien tourne recemment (cf.
            #      MACRO_STALE_HOURS ci-dessus). N'ecrit rien dans les cles
            #      gelees par _freeze_levels -- redeclenche uniquement le
            #      workflow GitHub via son API, jamais de calcul local. ----
            if "macrocheck" in qs:
                stale, age_h, gen = True, None, None
                try:
                    blob = json.loads((ROOT / "macro.json").read_text())
                    gen = blob.get("generated")
                    gen_dt = dt.datetime.strptime(gen, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=dt.timezone.utc)
                    age_h = (dt.datetime.now(dt.timezone.utc) - gen_dt).total_seconds() / 3600
                    stale = age_h > MACRO_STALE_HOURS
                except Exception:
                    pass
                outcome = "fresh"
                if stale:
                    if kv_get(MACRO_DISPATCH_GUARD_KEY):
                        outcome = "stale but already redeclenche recemment"
                    else:
                        token = os.environ.get("GITHUB_DISPATCH_TOKEN")
                        if not token:
                            outcome = "stale mais GITHUB_DISPATCH_TOKEN absent"
                        else:
                            import requests
                            try:
                                r = requests.post(
                                    f"https://api.github.com/repos/{MACRO_GH_REPO}"
                                    "/actions/workflows/macro.yml/dispatches",
                                    json={"ref": "main"},
                                    headers={"Authorization": f"Bearer {token}",
                                             "Accept": "application/vnd.github+json",
                                             "X-GitHub-Api-Version": "2022-11-28"},
                                    timeout=8)
                                if r.status_code == 204:
                                    kv_set(MACRO_DISPATCH_GUARD_KEY, "1", ex=3600)
                                    outcome = "redeclenche"
                                else:
                                    outcome = f"echec dispatch HTTP {r.status_code}"
                            except Exception as e:
                                outcome = f"echec dispatch {e}"
                # Journal seulement si non-trivial : un hit "fresh" toutes les
                # 30 min pendant 9h noierait vite les 15 entrees du journal
                # principal (meme raison que FLOW_CRON_LOG_KEY plus haut).
                if outcome != "fresh":
                    journal(f"macrocheck generated={gen} age_h={age_h} -> {outcome}")
                self._send(200, json.dumps({
                    "macrocheck": True, "generated": gen, "age_h": age_h,
                    "stale": stale, "outcome": outcome,
                }).encode(), "application/json")
                return
            # ---- NUIT / open Globex : aucune info options nouvelle. On ne
            #      recale QUE la partie daily (Daily Open + grille sigma), le
            #      gamma/EM/IV de 15h25 restent intouchés. Publication et
            #      annonce News UNIQUEMENT si l'ancre a réellement bougé
            #      (auto-dédupliquant : schedules en double et backups
            #      redeviennent muets une fois l'ancre à jour). ----
            if ("daily" in qs) or now_p < "0300":
                changed_any = []
                for target in TARGETS:
                    latest = _latest_payload(target)
                    if latest is None:   # premier démarrage : calcul complet
                        payload = build_payload(target=target, mode="snapshot",
                                                chain_cache=cache)
                        payload["iv_ref"] = payload.get("iv_atm")
                        payload["iv_source"] = "session"
                        ok, why = _upstash_set(payload)
                        results[target] = {"daily_only": True, "bootstrap": True,
                                           "published": ok}
                        if ok:
                            changed_any.append(payload)
                        continue
                    # IV FRAÎCHE pour dimensionner la grille : le snapshot de
                    # clôture de la chaîne (dispo à 00h01) reflète l'IV réelle
                    # post-séance, bien plus juste que l'IV de la veille 15h25
                    # (qui peut être gonflée un jour de selloff -> grille trop
                    # large toute la nuit et la matinée). Repli silencieux sur
                    # l'IV stockée si la chaîne est indisponible.
                    iv_note = "kept"
                    try:
                        ch = TARGETS[target]["chain"]
                        data = cache.get(ch)
                        if data is None:
                            data = fetch_cboe(ch)
                            cache[ch] = data
                        spot_c, opts_c, _e = parse_chain(data, 8, today=et_today())
                        iv_fresh = atm_iv(spot_c, opts_c)
                        if iv_fresh and 0.05 < iv_fresh < 1.5:
                            latest["iv_atm"] = round(float(iv_fresh), 4)
                            iv_note = f"fresh {iv_fresh:.3f}"
                    except Exception:
                        pass
                    if refresh_daily_anchor(latest):
                        ok, why = _upstash_set(latest)
                        results[target] = {"daily_only": True, "changed": True,
                                           "published": ok, "iv": iv_note,
                                           "anchor": latest["open_grid"]["anchor"]}
                        if ok:
                            changed_any.append(latest)
                    else:
                        results[target] = {"daily_only": True, "changed": False}
                news = False
                if changed_any:
                    px = " · ".join(
                        "{} {:,}".format(p["target"], round(p["open_grid"]["anchor"]))
                        .replace(",", " ")
                        for p in changed_any if p.get("open_grid"))
                    news = discord_news(
                        "🔄 **GEX Terminal** — Daily Open recalé ("
                        + paris_hhmm() + " Paris · open Globex)"
                        + ("\n" + px if px else "")
                        + "\nhttps://www.dash.gexdash.app")
                journal("ok daily-only changed=%d news=%s" % (len(changed_any), news))
                self._send(200, json.dumps({
                    "date": today, "daily_only": True,
                    "changed": [p["target"] for p in changed_any],
                    "news": news, "targets": results,
                }).encode(), "application/json")
                return
            # ---- Annonce de lancement Flux/Horizon : une fois par jour, au
            # tout premier tir intrajournalier qui passe (peu importe l'heure
            # reelle -- pas de dependance a un horaire fige). Message COURT
            # et distinct du "niveaux mis a jour" plus bas : ce dernier
            # parle de gamma/niveaux, celui-ci annonce juste le demarrage du
            # module pour la seance. _refresh_flow et _refresh_horizon
            # tournent tous les deux dans le meme bloc "intraday" plus bas,
            # au meme tir -- Horizon demarre donc toujours EXACTEMENT en
            # meme temps que Flux, jamais decale.
            if "intraday" in qs:
                start_guard = f"gex:fluxstart:{today}"
                if not kv_get(start_guard):
                    kv_set(start_guard, "1", ex=16 * 3600)
                    flux_ping = discord_news(
                        "🚀 **FLUX lancée** — " + paris_hhmm() + " Paris"
                        "\nhttps://www.dash.gexdash.app/flux")
                    horizon_ping = discord_news(
                        "🚀 **Horizon lancé** — " + paris_hhmm() + " Paris"
                        "\nhttps://www.dash.gexdash.app/horizon",
                        key="horizon")
                    journal_flow(f"start ping flux={flux_ping} horizon={horizon_ping}")
            force = "force" in qs
            for target in TARGETS:
                latest = _latest_payload(target)
                fresh = (latest is not None
                         and latest.get("date") == today
                         and latest.get("generated_utc", "") >= f"{today}T11:30:00")
                if fresh and not force:
                    # Niveaux deja publies aujourd'hui : cette branche ne les
                    # retouche JAMAIS -- _freeze_levels est inconditionnel ici,
                    # etre dans cette branche EST la definition de "deja
                    # canonique aujourd'hui", donc la string Pine ne peut pas
                    # bouger. Mais prix/basis/net_gex/regime/pc_oi/iv doivent
                    # continuer a se rafraichir a chaque tir intrajournalier
                    # -- sinon _track_intraday et le bandeau restent figes
                    # toute la journee des la publication canonique (bug vu
                    # en prod le 2026-08-13 : plus aucune metrique n'avait
                    # bouge depuis la publication de 13h25 UTC, faute de ce
                    # republishing). Le fetch CBOE est deja necessaire pour
                    # recalculer le flux (comme avant ce correctif) :
                    # republier ne coute qu'un SET Redis de plus par cible
                    # et par tir, aucun appel reseau supplementaire.
                    #
                    # AUCUN payload de cette branche n'alimente `computed` :
                    # c'est la seule liste qui declenche le ping Discord
                    # "canal News" plus bas (un message par tir de 5-10 min
                    # rendrait le canal inutilisable). L'embed Discord
                    # (want_notify) reste lui gate uniquement par ?notify=1
                    # ou le creneau de secours Vercel -- jamais atteint sur
                    # ce chemin, quelle que soit la valeur du payload.
                    try:
                        pre_open_iv = (latest.get("iv_atm")
                                       if _et_now_minutes() < OPTIONS_OPEN_ET_MIN
                                       else None)
                        flow_capture = {} if "intraday" in qs else None
                        payload = build_payload(target=target, mode="snapshot",
                                                chain_cache=cache,
                                                iv_override=pre_open_iv,
                                                capture=flow_capture)
                        payload["iv_source"] = "close" if pre_open_iv else "session"
                        self._preserve_daily(payload, latest)
                        self._freeze_levels(payload, latest)
                        self._stamp_iv_ref(payload, latest, False)
                        flow_check, flow_skip_reason = None, None
                        if flow_capture:
                            try:
                                flow_check, flow_skip_reason = _refresh_flow(
                                    target, payload, flow_capture)
                                if flow_skip_reason:
                                    journal_flow(f"flow {target} : rien calcule "
                                                 f"({flow_skip_reason})")
                                elif (flow_check and flow_check["deviation_pct"]
                                      > FLOW_CHECK_TOL_PCT):
                                    journal_flow(f"flow {target} controle de "
                                                 f"justesse KO : cboe_gross="
                                                 f"{flow_check['cboe_gross_bn']}Bn vs "
                                                 f"flow={flow_check['flow_gross_bn']}Bn "
                                                 f"(ecart {flow_check['deviation_pct']}%)")
                            except Exception as e:
                                flow_skip_reason = f"{type(e).__name__}: {e}"
                                journal_flow(f"flow {target} KO: {e}")
                        if "intraday" in qs:
                            try:
                                _refresh_horizon(target, payload, flow_capture)
                            except Exception:
                                pass
                        try:
                            _track_intraday(payload)
                        except Exception:
                            pass
                        ok, why = _upstash_set(payload)
                        # flow_check/flow_skip_reason : seul moyen de lire l'ecart
                        # de controle de justesse (vs net_gex_bn) depuis un appel
                        # ?intraday=1&key=... manuel, sans fouiller les journaux --
                        # a garder dans la reponse.
                        results[target] = {"skipped": True, "metrics_refreshed": ok,
                                           "publish_info": why, "flow_check": flow_check,
                                           "flow_skip_reason": flow_skip_reason}
                    except Exception as e:
                        results[target] = {"skipped": True, "metrics_refreshed": False,
                                           "metrics_error": str(e)}
                        journal_flow(f"metrics refresh {target} KO: {e}")
                    continue
                # verrou GEX : hors chemin CANONIQUE, les niveaux publiés
                # restent ceux d'avant tant que c'est verrouillé. Canonique =
                # ?notify=1 (QStash 15h25) OU le cron de secours Vercel dans
                # son créneau 15h20-18h00 — les deux doivent pouvoir publier
                # des niveaux FRAIS même verrouillé, sinon une panne QStash
                # figerait les niveaux de la veille.
                # Un tir INTRAJOURNALIER n'est jamais canonique : il tombe
                # parfois dans la fenetre de secours du 15h25, et sans cette
                # exclusion il republierait les niveaux — donc la string Pine
                # changerait en pleine seance, ce qu'on veut precisement eviter.
                # Decide AVANT le fetch CBOE (build_payload plus bas) : c'est
                # tout l'interet du verrou Redis ci-dessous, qui doit couvrir
                # la partie la plus lente du traitement, pas seulement le gel.
                canonical = ("notify" in qs) or (
                    ok_vercel and "1520" <= now_p <= "1800"
                    and "intraday" not in qs)
                # Garde anti-course (regression constatee le 2026-08-18,
                # 15h25 Paris) : en heure d'ete, le tick intraday
                # `0,5,10,15,20,25 13 * * 1-5` de vercel.json tombe sur la
                # MEME minute UTC que le publish canonique QStash "15h25
                # Paris" (13:25 UTC = Paris UTC+2). Un simple re-lu de `latest`
                # juste avant de figer (premiere version de ce correctif)
                # ne fait que RETRECIR la fenetre de course, sans l'eliminer :
                # rien n'empechait un tir intraday de la re-lire "pas encore
                # fraiche" puis d'ecrire APRES le canonique quelques instants
                # plus tard. Verrou Redis (SET NX EX) a la place : le tir
                # canonique le tient pendant TOUT son traitement (fetch CBOE
                # inclus, la partie lente) ; un tir intraday qui le trouve
                # pose attend sa liberation (borne, tres inferieur au
                # maxDuration=60s de la fonction) avant d'ecrire quoi que ce
                # soit, garantissant que son ecriture ne peut plus jamais
                # precede -- ni ecraser -- celle du canonique en cours.
                # Avant ce correctif, l'ecriture intraday pouvait arriver
                # APRES celle du canonique et l'ecraser silencieusement avec
                # les niveaux d'HIER (generated_utc restampe a aujourd'hui,
                # donc invisible a la garde de fraicheur) : Discord notifiait
                # deja les bons niveaux depuis son payload en memoire, seul
                # Redis -- donc le site -- restait fige sur la veille.
                pub_lock_key = f"gex:pub:lock:{target}"
                if canonical:
                    kv_set_nx(pub_lock_key, "1", ex=25)
                elif kv_get(pub_lock_key):
                    waited = 0.0
                    while kv_get(pub_lock_key) and waited < 3.0:
                        time.sleep(0.25)
                        waited += 0.25
                # Pre-ouverture (< 9h30 ET) : la publication canonique de
                # 15h25 Paris tombe 5 minutes AVANT l'ouverture des cotations
                # d'options. A cet instant, CBOE ne renvoie que des cotations
                # pre-ouverture aux fourchettes bid/ask tres larges -> IV
                # bruitee (mesure : 30.96% vs VXN 24.18, +28% sur l'EM). Le
                # run nocturne (00h11 UTC, marche ferme depuis 16h15 ET) a
                # deja mesure une IV de CLOTURE propre et fiable pour cette
                # meme journee : on la reprend telle quelle plutot que de
                # l'ecraser par une valeur pre-ouverture degradee.
                pre_open_iv = None
                if _et_now_minutes() < OPTIONS_OPEN_ET_MIN and latest and latest.get("iv_atm"):
                    pre_open_iv = latest["iv_atm"]
                # capture : rempli par build_payload uniquement sur un tir
                # intrajournalier (le module Flux en a besoin, cf. plus bas) —
                # None sur les autres chemins, cout nul.
                flow_capture = {} if "intraday" in qs else None
                payload = build_payload(target=target, mode="snapshot",
                                        chain_cache=cache, iv_override=pre_open_iv,
                                        capture=flow_capture)
                payload["iv_source"] = "close" if pre_open_iv else "session"
                if not canonical:
                    # Relu apres l'attente ci-dessus : si le canonique a
                    # publie entretemps, on gele desormais sur SES niveaux
                    # frais plutot que sur ceux lus en haut de boucle.
                    latest = _upstash_get(target) or latest
                # le daily vient TOUJOURS du nocturne, même sur le chemin 15h25
                if latest:
                    self._preserve_daily(payload, latest)
                # gex:lock est une bascule pensee pour /api/gex (refresh manuel
                # admin) : quand elle est a "0", un tir intrajournalier tombant
                # ICI (branche "not fresh" plus haut -- ex. GitHub Actions a
                # publie les niveaux du jour AVANT le seuil 11:30 UTC de la
                # garde de fraicheur, donc `fresh` reste faux jusqu'au publish
                # canonique ~13h25 UTC) recalculait et republiait des niveaux
                # differents a chaque tir de 5-10 min -- exactement ce que le
                # commentaire ci-dessus dit vouloir eviter. Un tir intraday ne
                # doit donc JAMAIS deverrouiller les niveaux, meme gex:lock=0
                # (regression constatee le 2026-08-18).
                if (not canonical) and latest and (
                        self._gex_locked() or "intraday" in qs):
                    self._freeze_levels(payload, latest)
                self._stamp_iv_ref(payload, latest, canonical)
                if flow_capture:
                    try:
                        chk, reason = _refresh_flow(target, payload, flow_capture)
                        # tir intrajournalier = toujours cense tomber en
                        # seance ; "hors seance" ici signale un probleme de
                        # planification cron, pas un etat normal -- on le
                        # journalise comme le reste.
                        if reason:
                            journal_flow(f"flow {target} : rien calcule ({reason})")
                        elif chk and chk["deviation_pct"] > FLOW_CHECK_TOL_PCT:
                            journal_flow(f"flow {target} controle de justesse KO : "
                                         f"cboe_gross={chk['cboe_gross_bn']}Bn vs "
                                         f"flow={chk['flow_gross_bn']}Bn "
                                         f"(ecart {chk['deviation_pct']}%)")
                    except Exception as e:
                        journal_flow(f"flow {target} KO: {e}")
                if "intraday" in qs:
                    try:
                        _refresh_horizon(target, payload, flow_capture)
                    except Exception:
                        pass
                # Trace intrajournaliere : l'open interest ne bouge pas en
                # seance, donc les MURS sont figes — mais les gammas unitaires
                # evoluent avec le spot, l'IV et la decroissance temporelle.
                # Le flip et le Net GEX peuvent donc reellement migrer, et
                # c'est cette evolution qu'on enregistre.
                try:
                    _track_intraday(payload)
                except Exception:
                    pass
                try:                       # calibration de l'horloge, 1x/jour
                    vk = json.loads(kv_get(VOLPROF_KEY.format(t=target)) or "null")
                    if not vk or vk.get("d") != et_today().isoformat():
                        _refresh_vol_profile(target)
                except Exception:
                    pass
                ok, why = _upstash_set(payload)
                if canonical:
                    # Liberation immediate (best-effort) : un tir intraday en
                    # attente ci-dessus n'a pas a patienter l'EX=25s entiere.
                    # Si ce kv_del echoue, le verrou s'auto-purge via son EX.
                    kv_del(pub_lock_key)
                results[target] = {"skipped": False, "published": ok,
                                   "locked": payload.get("levels_locked", False),
                                   "publish_info": why,
                                   "generated_utc": payload["generated_utc"]}
                if ok:
                    computed.append(payload)
            # ---- ping Discord marchés : au plus une fois par jour ----
            # backup_slot exclut ?intraday=1, comme `canonical` plus haut --
            # sans quoi un tir intrajournalier tombant dans la fenêtre de
            # secours 15h20-18h00 declenche Discord avec des niveaux gelés
            # AVANT le calcul canonique de 15h25 (bug prod 2026-08-14 :
            # message parti à 15h20, valeurs différentes du site publié à
            # 15h25 -- le verrou posé à 15h20 faisait "sauter" le vrai
            # notify=1 cinq minutes plus tard via le `elif kv_get(guard)`
            # ci-dessous).
            backup_slot = (ok_vercel and "1520" <= now_p <= "1800"
                           and "intraday" not in qs)
            want_notify = ("notify" in qs) or backup_slot
            guard = f"gex:notified:{today}"
            if not want_notify:
                notified = False
            elif kv_get(guard):
                notified = "skipped (déjà notifié aujourd'hui)"
            elif not computed:
                # Cette requête n'a publié aucun niveau frais (branche
                # "fresh and not force" plus haut) : le verrou ne doit
                # JAMAIS être posé sur cette base, sinon une publication
                # canonique arrivant juste après se ferait "sauter" alors
                # qu'elle est la seule à avoir réellement calculé les
                # niveaux du jour. On notifie quand même avec le dernier
                # payload publié -- secours si QStash est totalement en
                # panne -- mais SANS poser le verrou ni le lock.
                plist = [p for p in (_latest_payload(t) for t in TARGETS)
                         if p and p.get("date") == today]
                notified = discord_notify(plist) if plist else False
            else:
                # `computed` non vide ici <=> want_notify implique canonical
                # pour cette requête (mêmes conditions qs/ok_vercel/now_p) :
                # publication canonique déjà faite plus haut, dans CETTE
                # même requête, avant tout envoi Discord.
                notified = discord_notify(computed)
                if notified is True:
                    kv_set(guard, "1", ex=172800)
                    # verrouillage AUTOMATIQUE des niveaux après le 15h25 :
                    # les refresh intraday suivants ne les bougeront plus
                    try:
                        kv_set("gex:lock", "1")
                    except Exception:
                        pass
            # ---- canal News : trace publique de chaque refresh effectif ----
            # Le libellé distingue un vrai changement de niveaux d'un refresh
            # verrouillé (cf. _freeze_levels) : hors fenetre canonique 15h25,
            # tant que gex:lock=1 depuis la veille, les payloads publiés ici
            # ne font que rafraîchir prix/IV/flux -- levels/gex_by_strike/
            # open_grid/expected_move/pine restent ceux d'hier (payload.
            # levels_locked = True). Sans cette distinction le message disait
            # "niveaux mis à jour" même sur ces tirs verrouillés, laissant
            # croire à tort que le gamma avait bougé.
            news = False
            if computed:
                px = " · ".join(
                    "{} {:,}".format(p["target"], round(p["nq_price"])).replace(",", " ")
                    for p in computed if p.get("nq_price"))
                slot = ("open Globex" if now_p < "0300"
                        else "pré-open US" if "1500" <= now_p <= "1800"
                        else "refresh")
                locked = all(p.get("levels_locked") for p in computed)
                title = ("🔒 **GEX Terminal** — refresh (niveaux gamma inchangés, verrouillés) ("
                          if locked else
                          "🔄 **GEX Terminal** — niveaux mis à jour (")
                news = discord_news(
                    title
                    + paris_hhmm() + " Paris · " + slot + ")"
                    + ("\n" + px if px else "")
                    + "\nhttps://www.dash.gexdash.app")
            # Tir intrajournalier (?intraday=1) : resume routine, cadence 5-10
            # min toute la seance -- vers le journal Flux dedie (cf.
            # FLOW_CRON_LOG_KEY), jamais le journal principal.
            (journal_flow if "intraday" in qs else journal)(
                "ok computed=%d notify=%s news=%s" % (len(computed), notified, news))
            self._send(200, json.dumps({
                "date": today, "discord": notified, "news": news,
                "targets": results,
            }).encode(), "application/json")
        except Exception as e:
            traceback.print_exc()
            journal("error %s" % e)
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/status":
            if not self._auth_key(parse_qs(parsed.query)):
                self._send(401, json.dumps({"error": "unauthorized"}).encode(), "application/json")
                return
            targets = {}
            for t in TARGETS:
                p = _latest_payload(t)
                targets[t] = ({"date": p.get("date"),
                               "generated_utc": p.get("generated_utc"),
                               "px": p.get("nq_price"), "iv": p.get("iv_atm")}
                              if p else None)
            today = et_today().isoformat()
            try:
                log = json.loads(kv_get(CRON_LOG_KEY) or "[]")
            except Exception:
                log = []
            try:
                flow_log = json.loads(kv_get(FLOW_CRON_LOG_KEY) or "[]")
            except Exception:
                flow_log = []
            self._send(200, json.dumps({
                "paris_now": paris_hhmm(), "date_et": today,
                "notified_today": bool(kv_get(f"gex:notified:{today}")),
                "targets": targets,
                "cron_log": log[:10] if isinstance(log, list) else [],
                "flow_log": flow_log[:15] if isinstance(flow_log, list) else [],
                "webhooks": sorted(fetch_webhooks().keys()),
            }).encode(), "application/json")
            return


        # ---- official levels: newest of committed snapshot vs published refresh ----
        if path in ("/levels.json", "/nq_levels.json"):
            qs0 = parse_qs(parsed.query)
            target = "NQ" if path == "/nq_levels.json" else _q_target(qs0)
            if target is None:
                self._send(400, json.dumps({"error": "target must be " + ", ".join(TARGETS)}).encode(),
                           "application/json")
                return
            payload = _latest_payload(target)
            if payload is None:
                self._send(404, json.dumps(
                    {"error": "no levels yet - run the GitHub Action or a refresh"}).encode(),
                    "application/json")
            else:
                # l'historique du jour vit dans sa propre cle : on l'attache a
                # la lecture pour que le terminal affiche l'evolution sans
                # requete supplementaire
                payload["intraday"] = _read_intraday(target)
                self._send(200, json.dumps(payload).encode(), "application/json")
            return

        # ---- static: dashboard + committed daily files ----
        # ── état de la session (consulté par le bouton de connexion) ──
        if path == "/api/auth":
            u = self._current_user()
            # `scale` voyage ici plutôt que dans un appel dédié : le terminal
            # appelle DÉJÀ /api/auth au chargement, donc zéro requête en plus.
            # Lecture mise en cache mémoire (cf. _user_scale) ; un visiteur
            # anonyme repart en 401 sans toucher Redis du tout.
            self._send(200 if u else 401,
                       json.dumps({"user": u, "scale": _user_scale(self, u)} if u
                                  else {"error": "anonyme"}).encode(),
                       "application/json")
            return

        # ── page profil : lecture des infos + clé API en clair (le seul
        # endroit qui la montre en entier — l'admin ne voit qu'une version
        # masquée, cf. /api/users) ──
        if path == "/api/profile":
            me = self._current_user()
            if not me:
                self._send(401, json.dumps({"error": "connexion requise"}).encode(),
                           "application/json")
                return
            u = self._users().get(me)
            if not u:
                self._send(401, json.dumps({"error": "compte introuvable"}).encode(),
                           "application/json")
                return
            apikey = u.get("apikey")
            state = fetch_api_keys().get(apikey, {}).get("state", "active") if apikey else None
            self._send(200, json.dumps({
                "user": me, "email": u.get("email", ""), "created": u.get("created", ""),
                "apikey": apikey, "apikey_state": state,
                "scale": u.get("scale") or "fut",
            }).encode(), "application/json")
            return

        if path == "/api/goldbasis":
            if not self._auth_key():
                self._send(401, json.dumps({"error": "unauthorized"}).encode(),
                           "application/json")
                return
            try:
                cur = json.loads(kv_get("gex:goldbasis") or "null")
            except Exception:
                cur = None
            live = None
            try:
                gc = yahoo_spot("GC=F")
                xau = yahoo_spot("XAUUSD=X|XAU=X|XAUUSD")
                if gc and xau:
                    live = round(gc - xau, 2)
            except Exception:
                pass
            self._send(200, json.dumps({"basis": cur, "measured": live}).encode(),
                       "application/json")
            return

        # ── FLUX : matrice gamma prix x temps (ETAPE 1, cf. docs/BRIEF-flux.md) ──
        # Lecture seule : le calcul vit dans le cron intrajournalier
        # (_refresh_flow), jamais sur ce chemin. Absence de donnee : 200
        # {"ready": false}, pas une 503 -- l'absence (avant le premier tir
        # intraday du jour, hors seance) est un etat normal, pas une panne
        # de source.
        if path == "/api/flow":
            qs = parse_qs(parsed.query)
            target = (qs.get("target", ["NQ"])[0] or "NQ").upper()
            if target not in TARGETS:
                self._send(400, json.dumps({"error": "target inconnu"}).encode(),
                           "application/json")
                return
            # ?hist=1 : serie du jour (colonne "maintenant" de chaque tir,
            # cf. _refresh_flow) pour reconstituer l'expo dealer reelle sur la
            # portion de seance ecoulee -- reponse separee et legere, jamais
            # mêlee au payload de projection normal (recupere une seule fois
            # par le panneau, pas a chaque poll de 2 min).
            if (qs.get("hist", ["0"])[0] or "0") == "1":
                hist = _flow_hist_cached(target)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "public, s-maxage=60, "
                                                  "stale-while-revalidate=60")
                self.end_headers()
                self.wfile.write(json.dumps({"ready": bool(hist), "history": hist or []}).encode())
                return
            cached = _flow_cached(target)
            if not cached:
                self._send(200, json.dumps({"ready": False}).encode(),
                           "application/json")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "public, s-maxage=60, "
                                              "stale-while-revalidate=60")
            self.end_headers()
            self.wfile.write(json.dumps(dict(cached, ready=True)).encode())
            return

        # ── /api/horizon : lecture seule de HORIZON_KEY, jamais de calcul ici
        # (meme regle que /api/flow) -- ecrit uniquement par _refresh_horizon
        # dans le cron intrajournalier. Contrairement a /api/flow, GARDE par
        # session (module classe "membres connectes" dans son ensemble, pas
        # seulement la page HTML /horizon -- docs/BRIEF-horizon.md). Reponse
        # jamais mise en cache public/edge : un cache partage sur une reponse
        # authentifiee servirait la donnee a un visiteur non connecte sans
        # jamais repasser par _current_user(). ──
        if path == "/api/horizon":
            if not self._current_user():
                self._send(401, json.dumps({"error": "login required"}).encode(),
                           "application/json")
                return
            qs = parse_qs(parsed.query)
            target = (qs.get("target", ["NQ"])[0] or "NQ").upper()
            if target not in TARGETS:
                self._send(400, json.dumps({"error": "target inconnu"}).encode(),
                           "application/json")
                return
            cached = _horizon_cached(target)
            if not cached:
                self._send(200, json.dumps({"ready": False}).encode(),
                           "application/json")
                return
            # Etape 5 (calibration) : stats cumulatives inter-seances, meme
            # reponse -- le client (horizon.html) affiche "calibration en
            # cours" tant que days < HORIZON_MIN_SAMPLE_DAYS pour un bucket.
            stats = _horizon_stats_cached(target) or {"buckets": {}}
            calibration = []
            for bucket_key, b in stats["buckets"].items():
                regime, hlabel = bucket_key.split(":", 1)
                n, bias_n = b.get("n", 0), b.get("bias_n", 0)
                days_n = len(b.get("dates") or {})
                calibration.append({
                    "regime": regime, "horizon_label": hlabel,
                    "n": n, "days": days_n,
                    "cov70_pct": round(100 * b.get("cov70", 0) / n, 1) if n else None,
                    "bias_hit_pct": round(100 * b.get("bias_hit", 0) / bias_n, 1) if bias_n else None,
                    "ready": days_n >= HORIZON_MIN_SAMPLE_DAYS,
                })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "private, no-store")
            self.end_headers()
            self.wfile.write(json.dumps(
                dict(cached, ready=True, calibration=calibration)).encode())
            return

        # ── widget Flux embarquable : meme cache FLOW_KEY que /api/flow,
        # lecture seule, jamais de calcul ici. Cle en query param (pas un
        # header custom type x-gex-key) : ce fichier n'a AUCUN handler
        # do_OPTIONS, un header custom cross-origin declencherait un
        # preflight OPTIONS que le serveur ne sait pas repondre aujourd'hui.
        # Un GET ?key=... reste une requete CORS "simple", sans preflight.
        # CORS pose sur CHAQUE branche (200/400/401/429), erreurs incluses --
        # sans ca un 401 sans le header est silencieusement illisible par le
        # JS de la page hote (le navigateur masque le corps), ce qui se
        # presenterait au site tiers comme "le widget reste bloque", pas
        # "cle invalide". ──
        if path == "/api/embed/flow":
            qs = parse_qs(parsed.query)
            given_key = qs.get("key", [None])[0] or ""
            ok, err = _check_embed_key(given_key)
            if not ok:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(json.dumps({"error": err}).encode())
                return
            if _embed_rate_limited(given_key):
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "rate limited"}).encode())
                return
            target = (qs.get("target", ["NQ"])[0] or "NQ").upper()
            if target not in TARGETS:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "target inconnu"}).encode())
                return
            cached = _flow_cached(target)
            body = json.dumps(dict(cached, ready=True) if cached else {"ready": False}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "public, s-maxage=60, "
                                              "stale-while-revalidate=60")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return

        # ── cle API personnelle : Pine strings des 5 marches en un seul
        # appel (extension Chrome qui remplit l'indicateur) -- lecture seule
        # stricte, AUCUN calcul declenche ici (meme regle que /api/embed/flow) :
        # on relit uniquement ce qui a deja ete publie, le verrou des niveaux
        # n'est jamais concerne. Cle en parametre d'URL (jamais un header
        # custom), meme raison CORS que le widget Flux -- pas de do_OPTIONS
        # sur ce fichier, un GET ?key=... reste une requete CORS "simple".
        # Cache-Control: no-store PARTOUT (succes compris) : un cache d'edge
        # court-circuiterait le journal d'appels (_apikey_log_call) sur
        # lequel repose toute la detection de partage de cle en admin. ──
        if path == "/api/mylevels":
            qs = parse_qs(parsed.query)
            given_key = qs.get("key", [None])[0] or ""
            meta, code, err = _check_api_key(given_key)
            if err:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(json.dumps({"error": err}).encode())
                return
            if _apikey_rate_limited(given_key):
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "rate limited"}).encode())
                return
            self._apikey_log_call(given_key)
            levels, parts = {}, []
            for t in ("NQ", "ES", "SPX", "GC", "XAU"):
                p = _latest_payload(t) or {}
                pine = p.get("pine") or ""
                levels[t] = {"pine": pine, "published_utc": p.get("generated_utc")}
                parts.append(pine)
            lot_hash = hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(json.dumps({"levels": levels, "hash": lot_hash}).encode())
            return

        # ── diagnostic des prix de reference (admin) ──
        # Un marche qui reste « en attente de refresh » vient presque toujours
        # d'un symbole de reference muet : ce point d'entree dit lequel repond.
        if path == "/api/symbols":
            if not self._auth_key():
                self._send(401, json.dumps({"error": "unauthorized"}).encode(),
                           "application/json")
                return
            out = {}
            for tgt, cfg in TARGETS.items():
                ref = cfg.get("scale_to")
                if not ref:
                    continue
                per = {}
                for s in ref.split("|"):
                    s = s.strip()
                    if not s:
                        continue
                    try:
                        px = yahoo_spot(s)
                        per["yahoo:" + s] = px if px else "muet"
                    except Exception as e:
                        per["yahoo:" + s] = type(e).__name__
                alt = ref.split("|")[0].replace("=X", "").replace("=F", "").lower()
                try:
                    px = _stooq_spot(alt)
                    per["stooq:" + alt] = px if px else "muet"
                except Exception as e:
                    per["stooq:" + alt] = type(e).__name__
                out[tgt] = per
            self._send(200, json.dumps(out, ensure_ascii=False).encode(),
                       "application/json")
            return

        # ── sources du flux direct (admin) ──
        if path == "/api/feeds":
            if not self._auth_key():
                self._send(401, json.dumps({"error": "unauthorized"}).encode(),
                           "application/json")
                return
            self._send(200, json.dumps({"feeds": _feed_sources(),
                                        "defaults": DEFAULT_FEEDS}).encode(),
                       "application/json")
            return

        # ── liste des comptes et invitations (admin) ──
        if path == "/api/users":
            if not self._auth_key():
                self._send(401, json.dumps({"error": "unauthorized"}).encode(),
                           "application/json")
                return
            qs = parse_qs(parsed.query)
            # ?reveal=<user> : la cle en clair n'est JAMAIS incluse dans le
            # listing groupe ci-dessous -- action explicite separee, un
            # appel par cle revelee, pour ne pas exposer toutes les cles en
            # clair d'un coup dans une seule reponse/log.
            reveal = (qs.get("reveal", [None])[0] or "").strip().lower()
            if reveal:
                self._send(200, json.dumps(
                    {"key": (self._users().get(reveal) or {}).get("apikey")}
                ).encode(), "application/json")
                return
            api_keys = fetch_api_keys()
            users = {}
            for k, v in self._users().items():
                entry = {"note": v.get("note", ""), "email": v.get("email", ""),
                         "created": v.get("created", "")}
                apikey = v.get("apikey")
                if apikey:
                    entry["apikey_masked"] = _mask(apikey)
                    entry["apikey_state"] = api_keys.get(apikey, {}).get("state", "active")
                    entry["apikey_stats"] = _apikey_stats(apikey)
                users[k] = entry
            self._send(200, json.dumps({"users": users,
                                        "invites": self._invites()}).encode(),
                       "application/json")
            return

        # ── données du terminal news : mêmes droits que la page ──
        if path == "/api/news":
            if not self._current_user():
                self._send(401, json.dumps({"error": "connexion requise"}).encode(),
                           "application/json")
                return
            qs = parse_qs(parsed.query)
            typ = (qs.get("type", ["news"])[0] or "news").lower()
            try:
                if typ == "calendar":
                    out = {"calendar": _news_calendar()}
                elif typ == "fj":
                    # Source PRINCIPALE : Finnhub, éprouvée en production.
                    # FinancialJuice n'est tenté qu'en complément — leur flux
                    # peut être refusé aux IP de datacenter, et une colonne
                    # doit s'afficher avec une source qui répond, pas avec la
                    # plus rapide en théorie.
                    # Diagnostic : ?diag=1 indique quelle source répond et
                    # pourquoi les autres échouent — évite de deviner.
                    if qs.get("diag"):
                        fj_rows, fj_err = _fetch_status(FJ_RSS, "FinancialJuice")
                        try:
                            fh = _news_headlines("general")
                            fh_err = "" if fh else "aucun élément"
                        except Exception as e:
                            fh, fh_err = [], type(e).__name__
                        inst = _news_feed()
                        self._send(200, json.dumps({
                            "financialjuice": {"n": len(fj_rows), "err": fj_err},
                            "finnhub": {"n": len(fh), "err": fh_err,
                                        "key": bool(os.environ.get("FINNHUB_API_KEY"))},
                            "institutionnel": {"n": len(inst)},
                        }, ensure_ascii=False).encode(), "application/json")
                        return

                    rows, srcname = [], "fj"
                    try:
                        rows = _news_fj_shared()
                    except Exception:
                        rows = []
                    if not rows:                       # repli : depeches Finnhub
                        try:
                            rows = _news_headlines("general")
                            srcname = "finnhub"
                        except Exception:
                            rows = []
                    if not rows:                       # dernier recours
                        rows = _news_feed()
                        srcname = "institutionnel"
                    out = {"news": rows, "src": srcname}
                elif typ == "feed":
                    out = {"feed": _news_feed()}
                elif typ == "mag7":
                    out = {"mag7": _news_mag7_shared()}
                else:
                    out = {"news": _news_headlines("general")}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "private, max-age=60")
                self.end_headers()
                self.wfile.write(json.dumps(out).encode())
            except Exception as e:
                self._send(502, json.dumps({"error": str(e)}).encode(),
                           "application/json")
            return

        # ── /news : réservé aux comptes autorisés ──
        if path == "/news":
            if not self._current_user():
                self.send_response(302)
                self.send_header("Location", "/?login=1&next=/news")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            fpath = ROOT / "news.html"
            if not fpath.is_file():
                self._send(404, json.dumps({"error": "news.html absent"}).encode(),
                           "application/json")
                return
            self._send(200, fpath.read_bytes(), "text/html; charset=utf-8")
            return

        # ── /profile : page profil, réservée aux comptes connectés (même
        # garde que /news) ──
        if path == "/profile":
            if not self._current_user():
                self.send_response(302)
                self.send_header("Location", "/?login=1&next=/profile")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            fpath = ROOT / "profile.html"
            if not fpath.is_file():
                self._send(404, json.dumps({"error": "profile.html absent"}).encode(),
                           "application/json")
                return
            self._send(200, fpath.read_bytes(), "text/html; charset=utf-8")
            return

        # ── /horizon : module Horizon (docs/BRIEF-horizon.md), page NON
        # REPERTORIEE -- pas de lien dans la nav, pas dans STATIC. Meme garde
        # de session que /profile ; volontairement pas dans STATIC pour ne
        # pas apparaitre au meme titre que les pages publiques. ──
        if path == "/horizon":
            if not self._current_user():
                self.send_response(302)
                self.send_header("Location", "/?login=1&next=/horizon")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            fpath = ROOT / "horizon.html"
            if not fpath.is_file():
                self._send(404, json.dumps({"error": "horizon.html absent"}).encode(),
                           "application/json")
                return
            self._send(200, fpath.read_bytes(), "text/html; charset=utf-8")
            return

        # ── /test : page NON REPERTORIEE (pas de lien dans la nav, pas dans
        # STATIC), sans garde de session -- juste non listee, cf. /horizon
        # pour le pendant garde-par-login. Bac a sable Lightweight Charts +
        # widget Flux public (widget/flux-widget.js), aucun calcul serveur
        # propre a cette page. ──
        if path == "/test":
            fpath = ROOT / "test.html"
            if not fpath.is_file():
                self._send(404, json.dumps({"error": "test.html absent"}).encode(),
                           "application/json")
                return
            self._send(200, fpath.read_bytes(), "text/html; charset=utf-8", cache="no-store")
            return

        # ── /demo : export statique Next.js (demo/out, cf. demo/next.config.mjs
        #    basePath:"/demo") -- volontairement non lie depuis la nav du site,
        #    juste un chemin direct pour travailler dessus "underground" avant
        #    de decider si/comment il rejoint le site public. Aucun rapport
        #    avec le reste de gexdash : simple mini-serveur de fichiers statiques
        #    prefixe, jamais de calcul ici. ──
        if path == "/demo" or path.startswith("/demo/"):
            rel = path[len("/demo"):].lstrip("/") or "index.html"
            demo_root = (ROOT / "demo" / "out").resolve()
            try:
                fpath = (demo_root / rel).resolve()
                fpath.relative_to(demo_root)   # leve ValueError si ca sort de demo/out
            except ValueError:
                self._send(404, json.dumps({"error": "not found"}).encode(), "application/json")
                return
            if fpath.is_dir():
                fpath = fpath / "index.html"
            if not fpath.is_file():
                self._send(404, json.dumps({"error": "not found"}).encode(), "application/json")
                return
            ctype = mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            self._send(200, fpath.read_bytes(), ctype)
            return

        if path in STATIC:
            fname, ctype = STATIC[path]
            fpath = ROOT / fname
            if not fpath.is_file():
                self._send(
                    404,
                    json.dumps({"error": f"{fname} not found - run the GitHub Action first"}).encode(),
                    "application/json",
                )
                return
            # Aucune de ces pages n'est gardee par session (news/profile/
            # horizon vivent dans des branches a part, cf. plus bas, et
            # gardent no-store) : le HTML est un gabarit statique, tout
            # contenu dynamique/par-utilisateur arrive apres coup via JS
            # (/api/auth, /api/quote, ...). Un court max-age rend le
            # prefetch au survol (shell.js, shellPrefetch) utile -- sans
            # ca le navigateur telecharge quand meme au survol mais
            # refuse de reutiliser la reponse au clic (no-store l'interdit
            # explicitement), prefetch pour rien.
            self._send(200, fpath.read_bytes(), ctype, cache="public, max-age=30, must-revalidate")
            return

        # ---- chart data: candles + last price, proxied (Yahoo blocks browser CORS) ----
        if path == "/api/matrix":
            qs = parse_qs(urlparse(self.path).query)
            tgt = (qs.get("target", ["NQ"])[0] or "NQ").upper()
            if tgt not in TARGETS:
                self._send(400, json.dumps({"error": "target invalide"}).encode(),
                           "application/json")
                return
            try:
                cfg = TARGETS[tgt]
                data = fetch_cboe(cfg["chain"])
                spot, opts, _exps = parse_chain(data, 8, today=et_today())
                # même remise à l'échelle que le moteur : sans elle, la
                # matrice de l'or resterait à l'échelle de l'ETF (~389 $)
                # au lieu de celle de l'once (~4200 $)
                if cfg.get("scale_to"):
                    tgt_spot = yahoo_spot(cfg["scale_to"])
                    if tgt_spot and tgt_spot > 0:
                        sc = spot / tgt_spot
                        for o in opts:
                            o.scale = sc
                        spot = tgt_spot
                bucket = cfg.get("bucket") or {"NQ": 10.0, "ES": 5.0, "SPX": 5.0}.get(tgt)
                pay = _latest_payload(tgt) or {}
                basis = float(pay.get("basis") or 0.0)
                dtes = sorted({o.dte for o in opts})[:6]
                cols = [{"dte": d, "label": f"{d}DTE"} for d in dtes] + [{"dte": -1, "label": "ALL"}]
                grids = []
                for d in dtes:
                    ks, net = per_strike_gex(spot, [o for o in opts if o.dte == d], bucket=bucket)
                    grids.append(dict(zip(ks.tolist(), net.tolist())))
                ks, net = per_strike_gex(spot, opts, bucket=bucket)
                grids.append(dict(zip(ks.tolist(), net.tolist())))
                win = spot * 0.03
                ladder = sorted({k for g in grids for k in g if abs(k - spot) <= win},
                                reverse=True)
                rows = [{"p": round(k + basis, 1),
                         "v": [round(g.get(k, 0.0)) for g in grids]} for k in ladder]
                body = json.dumps({
                    "target": tgt, "spot": round(spot + basis, 1),
                    "chain": TARGETS[tgt]["chain"], "basis": round(basis, 1),
                    "cols": cols, "rows": rows,
                    "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                }).encode()
                # Dernière matrice CONNUE BONNE, pour le repli ci-dessous.
                # Coût maîtrisé : cet endpoint est mis en cache 300 s côté
                # edge, donc la fonction ne s'exécute au plus qu'une fois
                # par cible et par tranche de 5 min -> un SET par calcul
                # réussi, pas un par visiteur.
                try:
                    kv_set(MATRIX_LAST_KEY.format(tgt=tgt), body.decode(), ex=86400)
                except Exception:
                    pass
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "public, s-maxage=300, max-age=0")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                # REPLI : la chaîne CBOE est refetchée en direct à chaque
                # calcul, donc un hoquet de la source renvoyait un 502 sec —
                # et la heatmap se vidait. Une matrice gamma de quelques
                # minutes reste parfaitement lisible (les niveaux du jour
                # sont de toute façon gelés après 15h25, cf. CLAUDE.md) :
                # bien meilleur qu'une page blanche. Marquée `stale` pour que
                # le client puisse le signaler, et cache edge court (30 s au
                # lieu de 300) pour ne pas figer le repli une fois la source
                # revenue.
                try:
                    last = kv_get(MATRIX_LAST_KEY.format(tgt=tgt))
                except Exception:
                    last = None
                if last:
                    try:
                        d0 = json.loads(last)
                        d0["stale"] = True
                        d0["stale_reason"] = str(e)[:200]
                        body = json.dumps(d0).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Cache-Control", "public, s-maxage=30, max-age=0")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    except Exception:
                        pass
                self._send(502, json.dumps({"error": str(e)}).encode(),
                           "application/json")
            return

        if path == "/api/dark":
            qs = parse_qs(urlparse(self.path).query)
            tgt = (qs.get("target", ["NQ"])[0] or "NQ").upper()
            sym = DP_SYMS.get(tgt)
            if not sym:
                self._send(400, json.dumps({"error": "target invalide"}).encode(),
                           "application/json")
                return
            try:
                hist = json.loads(kv_get("gex:dp:hist") or "{}")
            except Exception:
                hist = {}
            for s in ("QQQ", "SPY"):
                hist.setdefault(s, [])
            have = {x["d"] for x in hist["QQQ"]}
            days, cur = [], et_today()
            while len(days) < 30:                 # 30 jours ouvrés : de quoi
                if cur.weekday() < 5:             # tenir une moyenne 20 jours
                    days.append(cur.strftime("%Y%m%d"))
                cur -= dt.timedelta(days=1)
            missing = [d for d in days if d not in have]
            # Historique pauvre (première fois, ou purge du cache) : on le
            # reconstruit d'un coup en PARALLÈLE plutôt qu'en 4 jours par
            # appel — sinon il faut des dizaines de requêtes pour redevenir
            # exploitable. En régime établi, il ne manque qu'un jour ou deux
            # et on reste sur le mode incrémental.
            # Un bootstrap télécharge ~30 fichiers FINRA de plusieurs Mo. Si la
            # série ne peut pas atteindre 20 entrées (jours fériés, fichiers non
            # publiés), la condition resterait vraie indéfiniment et chaque
            # invocation relancerait le lot. On le limite donc à UNE fois par
            # jour : au pire on reste en mode incrémental, jamais en boucle.
            _bs_key = "gex:dp:bootstrap"
            _bs_today = et_today().isoformat()
            _bs_done = False
            try:
                _bs_done = (kv_get(_bs_key) or "") == _bs_today
            except Exception:
                pass
            bootstrap = (not _bs_done) and \
                len([x for x in hist["QQQ"] if x.get("r") is not None]) < 20
            if bootstrap:
                try:
                    kv_set(_bs_key, _bs_today, ex=7 * 86400)
                except Exception:
                    pass
            todo = missing if bootstrap else missing[:4]
            fetched = len(todo)
            results = {}
            if todo:
                try:
                    from concurrent.futures import ThreadPoolExecutor
                    with ThreadPoolExecutor(max_workers=8) as ex:
                        results = dict(zip(todo, ex.map(_finra_dp_day, todo)))
                except Exception:
                    results = {d: _finra_dp_day(d) for d in todo}
            for ymd, data in results.items():
                if not data:
                    continue                      # férié / fichier pas encore publié
                for s, (sv, tv) in data.items():
                    hist[s].append({"d": ymd, "sv": sv, "tv": tv,
                                    "r": round(100.0 * sv / tv, 2) if tv else None})
            for s in hist:
                hist[s] = sorted(hist[s], key=lambda x: x["d"])[-60:]
            if fetched:
                kv_set("gex:dp:hist", json.dumps(hist), ex=45 * 86400)
            rows = hist.get(sym, [])
            rs = [x["r"] for x in rows if x.get("r") is not None]
            body = json.dumps({
                "target": tgt, "sym": sym, "days": rows[-30:],
                "last": rows[-1] if rows else None,
                "avg20": round(sum(rs[-20:]) / len(rs[-20:]), 2) if rs else None,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "public, s-maxage=1800, max-age=0")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/xr":
            qs = parse_qs(urlparse(self.path).query)
            tgt = (qs.get("target", ["NQ"])[0] or "NQ").upper()
            if tgt not in TARGETS:
                self._send(400, json.dumps({"error": "target invalide"}).encode(),
                           "application/json")
                return
            today = et_today().isoformat()
            try:
                hist = json.loads(kv_get(f"gex:xr:{tgt}:{today}") or "[]")
            except Exception:
                hist = []
            body = json.dumps({"target": tgt, "date": today, "snaps": hist}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "public, s-maxage=120, max-age=0")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return

        # ── /api/nqlive : cf. _tv_live_quote ci-dessus. Route dediee a /test
        # uniquement (non repertoriee). GET seul, lecture au sens large --
        # ouvre/ferme un websocket TradingView a chaque appel. ──
        if path == "/api/nqlive":
            qsl = parse_qs(parsed.query)
            symbol = (qsl.get("symbol", ["CME_MINI:NQ1!"])[0] or "CME_MINI:NQ1!")
            try:
                v = _tv_live_quote(symbol)
            except Exception as e:
                self._send(503, json.dumps({"error": f"tv unavailable: {e}"}).encode(),
                           "application/json")
                return
            if not v or "lp" not in v:
                self._send(503, json.dumps({"error": "no quote (cookie env vars missing/expired?)"}).encode(),
                           "application/json")
                return
            body = json.dumps({
                "symbol": symbol, "price": v.get("lp"), "ch": v.get("ch"),
                "chp": v.get("chp"), "volume": v.get("volume"),
                "update_mode": v.get("update_mode"), "lp_time": v.get("lp_time"),
                "server_time": time.time(),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path in ("/api/chart", "/api/quote"):
            qs0 = parse_qs(parsed.query)
            target = _q_target(qs0)
            if target is None:
                self._send(400, json.dumps({"error": "target must be " + ", ".join(TARGETS)}).encode(),
                           "application/json")
                return
            interval = (qs0.get("interval", ["5m"])[0] or "5m")
            if interval not in CHART_INTERVALS:
                interval = "5m"
            # Échelle d'affichage : "fut" (défaut, inchangé) ou "idx" pour
            # l'indice cash (NAS100/US500). Opt-in strict : sans le paramètre,
            # la réponse est identique au caractère près à avant.
            want_idx = (qs0.get("scale", ["fut"])[0] or "fut").lower() == "idx"
            try:
                # /api/quote n'a besoin que de meta.regularMarketPrice : inutile
                # de télécharger 5 jours de bougies 5m (~1150 chandelles) à
                # chaque poll. Une seule bougie journalière porte le même meta
                # pour une fraction du coût de parsing.
                # L'or comptant n'a pas de serie chez Yahoo : on prend celle du
                # future et on la decale. Sans cela, la chart tombait en erreur
                # puis se rabattait silencieusement sur les prix du future.
                _sym = YCHART[target]
                _shift = 0.0
                if target == "XAU":
                    _o = _gold_offset()
                    if _o is not None:
                        _sym, _shift = "GC=F", _o
                if path == "/api/quote":
                    res = _yahoo_chart(_sym, "1d", "1d")
                else:
                    res = _yahoo_chart(_sym, interval, CHART_INTERVALS[interval])
                meta = res.get("meta", {})
                if _shift:
                    m = meta.get("regularMarketPrice")
                    if m:
                        meta["regularMarketPrice"] = round(float(m) - _shift, 2)
                    q0 = (res.get("indicators", {}).get("quote") or [{}])[0]
                    for _k in ("open", "high", "low", "close"):
                        arr = q0.get(_k)
                        if arr:
                            q0[_k] = [None if v is None else round(float(v) - _shift, 2)
                                      for v in arr]
                if path == "/api/quote":
                    price = meta.get("regularMarketPrice")
                    ptime = meta.get("regularMarketTime") or 0
                    source = "fut"
                    # l'ETF US cote en quasi temps réel là où le future est différé :
                    # converti à l'échelle target via le scale et la basis du snapshot
                    try:
                        _ctx = _quote_ctx(target)
                        scale = _ctx["scale"]
                        if scale:
                            # 1) Finnhub (temps réel actions US), 2) ETF Yahoo en repli
                            ep = et = None
                            src2 = None
                            fh = _finnhub_quote(YETF[target])
                            if fh:
                                ep, et, src2 = fh[0], fh[1], "finnhub"
                            if not ep:
                                emeta = _yahoo_chart(YETF[target], "1d", "1d").get("meta", {})
                                ep = emeta.get("regularMarketPrice")
                                et = emeta.get("regularMarketTime") or 0
                                src2 = "etf"
                            # L'ETF (Finnhub surtout) est la source la PLUS
                            # réactive : on la PRÉFÈRE dès qu'elle est récente
                            # dans l'absolu (< 90 s), sans exiger qu'elle batte
                            # l'horodatage du future. C'est ce qui évite de
                            # rester coincé sur un future périmé à l'open
                            # (ex : gexdash 29200 alors que NQ est à 29400).
                            import time as _tt
                            fresh = ep and et and (_tt.time() - et) < 90
                            if ep and (fresh or et > ptime):
                                derived = round(ep / scale + _ctx["basis"], 2)
                                # garde-fou anti-aberration : rejette un dérivé
                                # très loin du future SEULEMENT si le future est
                                # lui-même frais (< 60 s). Sur un future périmé
                                # (open, gap), on fait confiance à l'ETF récent.
                                fut_fresh = price and ptime and (_tt.time() - ptime) < 60
                                far = price and abs(derived / price - 1) > 0.015
                                if fut_fresh and far:
                                    source = "fut-guard"
                                else:
                                    # basis dynamique partagée (calibrée par
                                    # /api/chart sur le chevauchement fut/ETF)
                                    # correctif de basis : lu au plus une fois
                                    # par minute et mémorisé dans le process
                                    # (les instances restent chaudes), au lieu
                                    # d'une lecture Redis à chaque poll
                                    try:
                                        import time as _t
                                        _n = _t.time()
                                        _c = _BASIS_ADJ.get(target)
                                        if not _c or _n - _c[1] > 60:
                                            _a = kv_get(f"gex:basisadj:{target}")
                                            _v = (json.loads(_a).get("adj") or 0.0) if _a else 0.0
                                            _BASIS_ADJ[target] = (_v, _n)
                                            _c = _BASIS_ADJ[target]
                                        if _c[0]:
                                            derived = round(derived + _c[0], 2)
                                    except Exception:
                                        pass
                                    price, ptime, source = derived, et, src2
                    except Exception:
                        pass
                    # Garde-fou de continuité : un print isolé aberrant (Finnhub
                    # ou ETF Yahoo) crée une grosse mèche fantôme sur la bougie
                    # M1 en cours côté client (pollQuote étire high/low sur CE
                    # prix), mèche qui disparaît au refresh puisque le chart
                    # historique ne l'a jamais vue (bug vu en prod le
                    # 2026-08-19). Un saut implausible vs le dernier prix
                    # confirmé n'est accepté qu'une fois répété au tir suivant
                    # (~3s plus tard) -- un vrai mouvement rapide passe donc
                    # avec ~3s de retard, imperceptible ; un print isolé, lui,
                    # ne se répète pas et reste filtré.
                    try:
                        if price is not None:
                            prev = _QUOTE_GUARD.get(target)
                            next_pending = None
                            if prev and prev.get("price"):
                                delta = abs(price - prev["price"])
                                threshold = max(prev["price"] * 0.003, 20)
                                if delta > threshold:
                                    pend = prev.get("pending")
                                    if pend is not None and abs(price - pend) <= max(price * 0.001, 5):
                                        pass   # confirmé 2 fois d'affilée -> mouvement réel, on accepte
                                    else:
                                        next_pending = price   # 1re fois : on retient, sans l'appliquer
                                        price, ptime, source = prev["price"], prev["t"], prev.get("source", source)
                            _QUOTE_GUARD[target] = {"price": price, "t": ptime,
                                                     "source": source, "pending": next_pending}
                    except Exception:
                        pass
                    # Échelle indice demandée (NAS100/US500) : on convertit
                    # TOUT À LA FIN, une fois le prix future arrêté. Le garde
                    # de continuité ci-dessus et le correctif de basis
                    # travaillent donc toujours sur la même échelle qu'avant
                    # -- aucune de leur logique n'est touchée.
                    if want_idx and price is not None:
                        price = round(price - _calibrated_basis(target), 2)
                    body = json.dumps({
                        "target": target, "price": price,
                        "time": ptime, "source": source,
                        "scale": "idx" if want_idx else "fut",
                    }).encode()
                    max_age = 2
                else:
                    def _pb(rs):
                        tts = rs.get("timestamp") or []
                        qq = (rs.get("indicators", {}).get("quote") or [{}])[0]
                        out = []
                        for i, t in enumerate(tts):
                            o = (qq.get("open") or [None])[i]
                            h = (qq.get("high") or [None])[i]
                            l = (qq.get("low") or [None])[i]
                            c = (qq.get("close") or [None])[i]
                            if None in (o, h, l, c):
                                continue
                            out.append({"time": t, "open": round(o, 2),
                                        "high": round(h, 2), "low": round(l, 2),
                                        "close": round(c, 2)})
                        return out

                    bars = _pb(res)
                    src_flag = "fut"
                    # BOUGIES QUASI TEMPS RÉEL : le future Yahoo est différé
                    # ~10 min (politique CME), mais l'ETF (QQQ/SPY) est servi
                    # quasi temps réel par Yahoo. On reconstruit l'intraday
                    # depuis l'ETF converti (v/scale + basis) — y compris
                    # pré/post-marché — et on garde les bougies future
                    # UNIQUEMENT aux heures où l'ETF n'a pas coté (nuit
                    # Globex). Repli total sur le future si quoi que ce soit
                    # manque : jamais pire qu'avant.
                    try:
                        pay = _latest_payload(target) or {}
                        scale = next((s.get("scale") for s in pay.get("sources", [])
                                      if s.get("chain") == YETF[target]
                                      and s.get("scale")), None)
                        basis = pay.get("basis") or 0.0
                        if scale:
                            # RTH UNIQUEMENT (pas de pré/post : c'est la source
                            # des prints pourris). Hors séance US -> future pur.
                            rese = _yahoo_chart(YETF[target], interval,
                                                CHART_INTERVALS[interval],
                                                prepost=False)
                            ebars = [{"time": b["time"],
                                      "open": round(b["open"] / scale + basis, 2),
                                      "high": round(b["high"] / scale + basis, 2),
                                      "low": round(b["low"] / scale + basis, 2),
                                      "close": round(b["close"] / scale + basis, 2)}
                                     for b in _pb(rese)]
                            if ebars:
                                # BASIS DYNAMIQUE, PAR BOUGIE : le future différé
                                # est EXACT pour son propre horodatage. Dès qu'il
                                # est arrivé pour une minute donnée, son écart
                                # avec la bougie ETF dérivée de cette même minute
                                # EST la basis réelle à cet instant -- on
                                # l'applique directement, minute par minute (et
                                # on partage la plus récente avec /api/quote via
                                # Redis), plutôt qu'une médiane glissante étalée
                                # sur tout un segment.
                                # Un scalaire unique recalculé à chaque requête
                                # et appliqué à TOUT le segment RTH (parfois
                                # plusieurs heures de bougies déjà affichées)
                                # faisait sauter tout le bloc d'un coup à chaque
                                # refetch -- surtout visible au passage
                                # pré-marché -> RTH à 13h30 UTC, où la toute
                                # première bougie ETF récupérait le même
                                # ajustement "récent" que la bougie la plus
                                # actuelle, sans lien avec l'écart réel à cet
                                # instant (bug vu en prod le 2026-08-19, ~65-100
                                # pts de saut pile à l'ouverture cash). Avec la
                                # correction par bougie, celle de 13h30 utilise
                                # l'écart mesuré à 13h30 : plus de saut au
                                # raccord, et plus de re-décalage global d'un
                                # refetch à l'autre.
                                # Tant que le future n'est pas encore arrivé pour
                                # une minute (délai ~10 min), on garde le dernier
                                # écart connu (forward-fill) au lieu de 0 -- 0
                                # supposerait une basis nulle, ce qui n'est vrai
                                # qu'à l'ouverture, pas en cours de séance.
                                fmap = {b["time"]: b["close"] for b in bars}
                                # Garde-fou à 0,4 % (~118 pts sur NQ), pas 0,2 % :
                                # l'écart future/ETF à l'ouverture cash (13h30
                                # UTC) atteint couramment 65-100 pts -- un effet
                                # de liquidité/découverte de prix systématique,
                                # pas une aberration. À 0,2 % ce résidu légitime
                                # basculait de manière quasi aléatoire au-dessus
                                # ou en dessous du seuil d'un refresh à l'autre
                                # (la mesure est recalculée à chaque requête),
                                # ce qui faisait flotter le chart entre deux
                                # niveaux de prix différents (bug vu en prod le
                                # 2026-08-19). Un vrai print aberrant reste
                                # rejeté au-delà de ce seuil relevé.
                                guard = ebars[-1]["close"] * 0.004 if ebars else 0
                                last_diff = None
                                new_ebars = []
                                for e in ebars:
                                    t = e["time"]
                                    if t in fmap:
                                        d = fmap[t] - e["close"]
                                        # print aberrant ponctuel : on garde le
                                        # dernier écart valide plutôt que de
                                        # l'ignorer purement (ce qui créerait un
                                        # trou/notch local au lieu d'un saut).
                                        if abs(d) <= guard:
                                            last_diff = d
                                    d_use = last_diff if last_diff is not None else 0.0
                                    new_ebars.append({
                                        "time": t,
                                        "open": round(e["open"] + d_use, 2),
                                        "high": round(e["high"] + d_use, 2),
                                        "low": round(e["low"] + d_use, 2),
                                        "close": round(e["close"] + d_use, 2)})
                                ebars = new_ebars
                                adj = last_diff or 0.0   # partagé avec /api/quote (dernier écart connu)
                                try:
                                    kv_set(f"gex:basisadj:{target}",
                                           json.dumps({"adj": round(adj, 2)}),
                                           ex=900)
                                except Exception:
                                    pass
                                emap = {b["time"] for b in ebars}
                                bars = sorted(
                                    [b for b in bars if b["time"] not in emap]
                                    + ebars, key=lambda b: b["time"])
                                src_flag = "etf+fut"
                    except Exception:
                        pass
                    bars = _clean_bars(bars)
                    # Conversion en échelle indice APRÈS tout le reste : le
                    # montage future/ETF, la correction de basis par bougie et
                    # le nettoyage travaillent sur l'échelle historique, donc
                    # aucune de leur logique ne change. On retranche ensuite la
                    # basis calibrée, qui est par définition l'écart
                    # future -> indice.
                    px_meta = meta.get("regularMarketPrice")
                    if want_idx:
                        cb = _calibrated_basis(target)
                        bars = [{"time": b["time"],
                                 "open": round(b["open"] - cb, 2),
                                 "high": round(b["high"] - cb, 2),
                                 "low": round(b["low"] - cb, 2),
                                 "close": round(b["close"] - cb, 2)} for b in bars]
                        if px_meta is not None:
                            px_meta = round(float(px_meta) - cb, 2)
                    body = json.dumps({"target": target, "interval": interval,
                                       "bars": bars, "src": src_flag,
                                       "scale": "idx" if want_idx else "fut",
                                       "price": px_meta}).encode()
                    max_age = 12
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                # stale-while-revalidate volontairement COURT : sur un prix
                # live, autoriser une réponse périmée trop longtemps fige le
                # ticker à l'écran. Le gain CPU vient du payload allégé, pas
                # d'un cache long.
                self.send_header(
                    "Cache-Control",
                    f"public, s-maxage={max_age}, max-age=0, "
                    f"stale-while-revalidate={max_age}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                traceback.print_exc()
                self._send(502, json.dumps({"error": f"chart source: {e}"}).encode(),
                           "application/json")
            return

        # ---- public links (dashboard header) ----
        if path == "/api/links":
            body = json.dumps(_links()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "public, s-maxage=60, max-age=0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ---- auto-level : metadonnees (jamais le contenu du fichier).
        # Ouvert aux membres connectes (page /profile) ET a l'admin (panel
        # d'upload) -- les deux consomment la meme forme, cf. _autolevel_meta.
        if path == "/api/autolevel":
            qs0 = parse_qs(parsed.query)
            if not (self._current_user() or self._auth_key(qs0)):
                self._send(401, json.dumps({"error": "unauthorized"}).encode(), "application/json")
                return
            self._send(200, json.dumps({"autolevel": _autolevel_meta()}).encode(), "application/json")
            return

        # ---- auto-level : telechargement du fichier (Quantower/MotiveWave).
        # Meme garde que la metadonnee ci-dessus ; jamais de cache (fichier
        # remplacable a tout moment par l'admin, et reponse potentiellement
        # volumineuse -- un edge cache n'a rien a y gagner). ----
        if path == "/api/autolevel/download":
            qs0 = parse_qs(parsed.query)
            if not (self._current_user() or self._auth_key(qs0)):
                self._send(401, json.dumps({"error": "unauthorized"}).encode(), "application/json")
                return
            platform = (qs0.get("platform", [""])[0] or "").strip()
            if platform not in AUTOLEVEL_PLATFORMS:
                self._send(400, json.dumps({"error": "platform invalide"}).encode(), "application/json")
                return
            f = _autolevel().get(platform)
            if not f or not f.get("data_b64"):
                self._send(404, json.dumps({"error": "aucun fichier disponible"}).encode(), "application/json")
                return
            try:
                raw = base64.b64decode(f["data_b64"])
            except Exception:
                self._send(500, json.dumps({"error": "fichier corrompu"}).encode(), "application/json")
                return
            filename = f.get("filename") or f"{platform}.bin"
            # Assainissement minimal (pas d'injection d'en-tete via un nom de
            # fichier stocke) + variante UTF-8 pour les noms non-ASCII, cf.
            # RFC 6266 -- l'admin est un acteur de confiance mais un en-tete
            # HTTP mal forme casserait quand meme le telechargement.
            safe_ascii = "".join(c if (32 <= ord(c) < 127 and c != '"') else "_" for c in filename)
            from urllib.parse import quote as _urlquote
            disp = f'attachment; filename="{safe_ascii}"; filename*=UTF-8\'\'{_urlquote(filename)}'
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", disp)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        # ---- admin: current webhook config (masked) ----
        if path == "/api/webhooks":
            qs0 = parse_qs(parsed.query)
            if not self._auth_key(qs0):
                self._send(401, json.dumps({"error": "unauthorized"}).encode(), "application/json")
                return
            cfg = fetch_webhooks()
            self._send(200, json.dumps({
                "config": {k: _mask(v) for k, v in cfg.items()},
                "env_fallback": bool(os.environ.get("DISCORD_WEBHOOK_URL")),
            }).encode(), "application/json")
            return

        # ---- admin: liste des cles du widget Flux embarquable (masquees) ----
        if path == "/api/embed-keys":
            qs0 = parse_qs(parsed.query)
            if not self._auth_key(qs0):
                self._send(401, json.dumps({"error": "unauthorized"}).encode(), "application/json")
                return
            keys = fetch_embed_keys()
            out = [{"key": _mask(k), "label": m.get("label"), "created": m.get("created"),
                    "revoked": bool(m.get("revoked"))} for k, m in keys.items()]
            self._send(200, json.dumps({"keys": out}).encode(), "application/json")
            return

        # ---- CRON: QStash (POST) / navigateur / filet Vercel (GET) ----
        if path == "/api/cron":
            self._cron(parsed)
            return

        # ---- API: verrou des niveaux GEX ----
        if path == "/api/lock":
            qs = parse_qs(parsed.query)
            if not self._auth_key(qs):
                self._send(401, json.dumps({"error": "clé invalide"}).encode(),
                           "application/json")
                return
            state = (qs.get("state", ["status"])[0] or "status").lower()
            try:
                if state == "on":
                    kv_set("gex:lock", "1")
                elif state == "off":
                    kv_set("gex:lock", "0")
                locked = self._gex_locked()
                self._send(200, json.dumps({"locked": locked}).encode(),
                           "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(),
                           "application/json")
            return

        # ---- API: live recompute (protected by GEX_REFRESH_KEY if set) ----
        if path == "/api/gex":
            qs = parse_qs(parsed.query)

            def q(name, default=None):
                v = qs.get(name, [None])[0]
                return v if v not in (None, "") else default

            secret = os.environ.get("GEX_REFRESH_KEY")
            if secret:
                given = self.headers.get("x-gex-key") or q("key") or ""
                if not hmac.compare_digest(given, secret):
                    self._send(401, json.dumps({"error": "unauthorized"}).encode(),
                               "application/json")
                    return

            try:
                basis = q("basis")
                basis = float(basis) if basis is not None else None
                iv_ov = q("iv")
                iv_ov = float(iv_ov) / (100.0 if float(iv_ov) > 3 else 1.0) if iv_ov else None
                n = max(1, min(int(q("n", 10)), 16))
                target = (q("target", "NQ") or "NQ").upper()
                if target not in TARGETS:
                    raise ValueError("target must be " + ", ".join(TARGETS))
                bands = tuple(
                    float(x) for x in q("em_bands", "0.5,1.5").split(",") if x.strip()
                )

                payload = build_payload(
                    target=target, n_expiries=n, basis_override=basis, mode="live",
                    em_bands=bands, iv_override=iv_ov
                )
                payload["iv_source"] = "override" if iv_ov else "session"
                prev = _latest_payload(target)
                if prev:
                    self._preserve_daily(payload, prev)
                    if q("notify") != "1" and self._gex_locked():
                        self._freeze_levels(payload, prev)
                self._stamp_iv_ref(payload, prev, q("notify") == "1")
                ok, why = _upstash_set(payload)
                payload["published"] = ok
                payload["publish_info"] = why
                # Silencieux par défaut (le run planifié de 15h25 reste la seule
                # notification automatique). ?notify=1 = envoi Discord explicite
                # (bouton "Refresh" + case "notify" de /admin). Une fois des
                # niveaux annoncés publiquement, ils ne doivent plus dériver en
                # arrière-plan sans nouvelle annonce -- même verrou que pose
                # le chemin cron canonique après un notify réussi (cf. _cron
                # ci-dessus). Sans lui, un notify=1 manuel publie un
                # instantané puis les tirs intrajournaliers suivants
                # continuent de faire flotter les niveaux : le terminal
                # s'écarte silencieusement de ce qui vient d'être posté sur
                # Discord (observé en prod le 2026-08-13 : flip Discord
                # 29707 vs terminal 29673 quelques minutes plus tard).
                if q("notify") == "1" and ok:
                    notified = discord_notify([payload])
                    payload["notified"] = bool(notified)
                    if notified:
                        try:
                            kv_set("gex:lock", "1")
                        except Exception:
                            pass
                self._send(200, json.dumps(payload).encode(), "application/json")
            except ValueError as e:
                # Donnee manquante ou chaine inexploitable : ce n'est PAS une
                # panne du serveur. On repond 503 avec le motif, ce qui evite
                # de declencher les alertes d'anomalie 5xx tout en affichant
                # un message clair dans l'admin.
                self._send(503, json.dumps({"error": str(e)}).encode(),
                           "application/json")
            except Exception as e:
                traceback.print_exc()
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            return

        self._send(404, json.dumps({"error": "not found"}).encode(), "application/json")
