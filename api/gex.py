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
import os
import time
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from api._gex_core import (TARGETS, build_payload, discord_news,
                           discord_notify, discord_send, et_today,
                           fetch_webhooks, kv_get, kv_set,
                           refresh_daily_anchor, save_webhooks, parse_chain, per_strike_gex, fetch_cboe, atm_iv, build_pine, yahoo_spot, _stooq_spot, _goldapi_spot)

CRON_LOG_KEY = "gex:cron:log"
FINNHUB_CACHE_S = 2.5
_BASIS_ADJ = {}          # cache mémoire du correctif de basis (par marché)
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


def _news_finnhub(path_, params, key, ttl):
    def fetch():
        import requests
        p = dict(params)
        p["token"] = os.environ.get("FINNHUB_API_KEY", "")
        return requests.get(f"https://finnhub.io/api/v1/{path_}", params=p,
                            timeout=10).json()
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

    rows, err = _fetch_status(FJ_RSS, "FinancialJuice")
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
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_mag = ex.submit(lambda: [_news_mag_one(s) for s in _MAG7])
        f_mkt = ex.submit(_news_markets)
        rows, markets = f_mag.result(), f_mkt.result()

    def avg(k):
        vals = [r[k] for r in rows if r.get(k) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    # Lecture de régime : on ne se contente pas de la moyenne, qui masque une
    # hausse portée par une seule valeur. On mesure aussi la LARGEUR (combien
    # de titres montent) et on croise semis, small caps et volatilité — les
    # trois signaux qui distinguent une vraie prise de risque d'un rebond
    # étroit sur quelques méga-capitalisations.
    ups = [r for r in rows if (r.get("dp") or 0) > 0]
    breadth = round(100.0 * len(ups) / len(rows)) if rows else None
    mk = {m["sym"]: (m.get("dp") or 0) for m in markets}
    mag_d = avg("dp") or 0
    score = 0
    if mag_d > 0.15:
        score += 1
    elif mag_d < -0.15:
        score -= 1
    if (breadth or 0) >= 70:
        score += 1
    elif breadth is not None and breadth <= 30:
        score -= 1
    if mk.get("SOXX", 0) > 0.3:
        score += 1                      # semis en tête = appétit pour le risque
    elif mk.get("SOXX", 0) < -0.3:
        score -= 1
    if mk.get("IWM", 0) > 0.3:
        score += 1                      # small caps suivent = hausse large
    elif mk.get("IWM", 0) < -0.3:
        score -= 1
    if mk.get("VIXY", 0) < -1:
        score += 1                      # volatilité qui reflue
    elif mk.get("VIXY", 0) > 3:
        score -= 1
    bias = ("risk_on" if score >= 3 else "risk_off" if score <= -3
            else "lean_on" if score > 0 else "lean_off" if score < 0 else "neutral")
    return {"rows": rows, "markets": markets,
            "agg": {"day": avg("dp"), "week": avg("week"), "month": avg("month"),
                    "breadth": breadth, "score": score, "bias": bias,
                    "sox": mk.get("SOXX"), "iwm": mk.get("IWM"),
                    "vix": mk.get("VIXY")}}


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


def _vol_profile(target):
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


def _variance_remaining(prof, now_min):
    """Part de la variance du jour encore devant nous, selon l'horloge
    ponderee. Sans profil, on retombe sur le temps calendaire."""
    end = SESSION_END_ET[0] * 60 + SESSION_END_ET[1]
    if not prof:
        span = end - now_min
        total = end - (18 * 60 - 24 * 60)     # session Globex ~22h
        return max(0.0, min(1.0, span / total)) if total > 0 else 0.0
    tot = sum(prof.values())
    if tot <= 0:
        return 0.0
    rest = 0.0
    for k, w in prof.items():
        b = int(k)
        if b >= now_min:
            rest += w
        elif b + 30 > now_min:               # tranche en cours, au prorata
            rest += w * (b + 30 - now_min) / 30.0
    return max(0.0, min(1.0, rest / tot))


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
    try:
        return json.loads(kv_get(INTRA_KEY.format(
            t=tgt, d=et_today().isoformat())) or "[]")
    except Exception:
        return []


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
    "/ui.js": ("ui.js", "application/javascript; charset=utf-8"),
    "/favicon.png": ("favicon.png", "image/png"),
    "/favicon.ico": ("favicon.png", "image/png"),
    "/dash.html": ("dash.html", "text/html; charset=utf-8"),
    "/admin.html": ("admin.html", "text/html; charset=utf-8"),
    "/history.json": ("history.json", "application/json"),
    "/nq_levels.txt": ("nq_levels.txt", "text/plain; charset=utf-8"),
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

        key = _upstash_key(payload.get("target", "NQ"))
        r = requests.post(f"{url}/set/{key}",
                          headers={"Authorization": f"Bearer {token}"},
                          data=json.dumps(payload), timeout=5)
        r.raise_for_status()
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
    ISO timestamps compare correctly as strings."""
    file_p = _load_file_payload(target)
    up_p = _upstash_get(target)
    if file_p and up_p:
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
        continuent de se rafraîchir."""
        for k in ("levels", "gex_by_strike", "open_grid",
                  "expected_move", "pine"):
            if old_p.get(k) is not None:
                new_p[k] = old_p[k]
        new_p["levels_locked"] = True

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
                if not (3 <= len(user) <= 32) or not user.replace("_", "").replace("-", "").isalnum():
                    return fail(400, "Identifiant invalide (3-32 caractères, lettres/chiffres)")
                # validation volontairement simple : on veut une adresse
                # exploitable pour te recontacter, pas un filtrage exhaustif
                if ("@" not in email or "." not in email.split("@")[-1]
                        or len(email) < 6 or len(email) > 120 or " " in email):
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
            for tgt in list(TARGETS) + ["default", "news"]:
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

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
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
        Vercel-cron hit between 15:20 and 18:00 Paris (backup notifier) — in
        all cases at most ONCE per day via the kv guard. The 'news' webhook
        receives a short note on every run that actually recomputed data."""
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
                        + "\nhttps://gexdash.wealthbuilders.group")
                journal("ok daily-only changed=%d news=%s" % (len(changed_any), news))
                self._send(200, json.dumps({
                    "date": today, "daily_only": True,
                    "changed": [p["target"] for p in changed_any],
                    "news": news, "targets": results,
                }).encode(), "application/json")
                return
            force = "force" in qs
            for target in TARGETS:
                latest = _latest_payload(target)
                fresh = (latest is not None
                         and latest.get("date") == today
                         and latest.get("generated_utc", "") >= f"{today}T11:30:00")
                if fresh and not force:
                    results[target] = {"skipped": True}
                    continue
                payload = build_payload(target=target, mode="snapshot",
                                        chain_cache=cache)
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
                canonical = ("notify" in qs) or (
                    ok_vercel and "1520" <= now_p <= "1800"
                    and "intraday" not in qs)
                # le daily vient TOUJOURS du nocturne, même sur le chemin 15h25
                if latest:
                    self._preserve_daily(payload, latest)
                if (not canonical) and self._gex_locked() and latest:
                    self._freeze_levels(payload, latest)
                # Trace intrajournaliere : l'open interest ne bouge pas en
                # seance, donc les MURS sont figes — mais les gammas unitaires
                # evoluent avec le spot, l'IV et la decroissance temporelle.
                # Le flip et le Net GEX peuvent donc reellement migrer, et
                # c'est cette evolution qu'on enregistre.
                try:
                    _track_intraday(payload)
                except Exception:
                    pass
                ok, why = _upstash_set(payload)
                results[target] = {"skipped": False, "published": ok,
                                   "locked": payload.get("levels_locked", False),
                                   "publish_info": why,
                                   "generated_utc": payload["generated_utc"]}
                if ok:
                    computed.append(payload)
            # ---- ping Discord marchés : au plus une fois par jour ----
            backup_slot = ok_vercel and "1520" <= now_p <= "1800"
            want_notify = ("notify" in qs) or backup_slot
            guard = f"gex:notified:{today}"
            if not want_notify:
                notified = False
            elif kv_get(guard):
                notified = "skipped (déjà notifié aujourd'hui)"
            else:
                plist = computed or [p for p in (_latest_payload(t) for t in TARGETS)
                                     if p and p.get("date") == today]
                notified = discord_notify(plist) if plist else False
                if notified is True:
                    kv_set(guard, "1", ex=172800)
                    # verrouillage AUTOMATIQUE des niveaux après le 15h25 :
                    # les refresh intraday suivants ne les bougeront plus
                    try:
                        kv_set("gex:lock", "1")
                    except Exception:
                        pass
            # ---- canal News : trace publique de chaque refresh effectif ----
            news = False
            if computed:
                px = " · ".join(
                    "{} {:,}".format(p["target"], round(p["nq_price"])).replace(",", " ")
                    for p in computed if p.get("nq_price"))
                slot = ("open Globex" if now_p < "0300"
                        else "pré-open US" if "1500" <= now_p <= "1800"
                        else "refresh")
                news = discord_news(
                    "🔄 **GEX Terminal** — niveaux mis à jour ("
                    + paris_hhmm() + " Paris · " + slot + ")"
                    + ("\n" + px if px else "")
                    + "\nhttps://gexdash.wealthbuilders.group")
            journal("ok computed=%d notify=%s news=%s" % (len(computed), notified, news))
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
            self._send(200, json.dumps({
                "paris_now": paris_hhmm(), "date_et": today,
                "notified_today": bool(kv_get(f"gex:notified:{today}")),
                "targets": targets,
                "cron_log": log[:10] if isinstance(log, list) else [],
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
            self._send(200 if u else 401,
                       json.dumps({"user": u} if u else {"error": "anonyme"}).encode(),
                       "application/json")
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

        # ── EM restant : ce qu'il reste a parcourir d'ici la cloture ──
        if path == "/api/emlive":
            qs = parse_qs(parsed.query)
            target = (qs.get("target", ["NQ"])[0] or "NQ").upper()
            if target not in TARGETS:
                self._send(400, json.dumps({"error": "target inconnu"}).encode(),
                           "application/json")
                return
            pay = _latest_payload(target) or {}
            em = (pay.get("expected_move") or {}).get("straddle")
            anchor = (pay.get("open_grid") or {}).get("anchor")
            px = pay.get("nq_price")
            try:                                  # prix courant si disponible
                live = _quote_price(target)
                if live:
                    px = live
            except Exception:
                pass
            if not em or not px:
                self._send(200, json.dumps({"ready": False}).encode(),
                           "application/json")
                return
            prof = _vol_profile(target)
            nowm = _et_now_minutes()
            frac = _variance_remaining(prof, nowm)
            rem = em * math.sqrt(frac)
            travel = abs(px - anchor) if anchor else None
            out = {
                "ready": True, "target": target, "price": round(px, 2),
                "em_day": round(em, 2), "anchor": anchor,
                "var_remaining": round(frac, 4),
                "em_remaining": round(rem, 2),
                "cone_high": round(px + rem, 2),
                "cone_low": round(px - rem, 2),
                "band_high": round(anchor + em, 2) if anchor else None,
                "band_low": round(anchor - em, 2) if anchor else None,
                "travelled": round(travel, 2) if travel is not None else None,
                "used_pct": round(100.0 * travel / em, 1) if travel is not None else None,
                "calibrated": bool(prof),
                "et_minutes": nowm,
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "public, s-maxage=30, "
                                              "stale-while-revalidate=30")
            self.end_headers()
            self.wfile.write(json.dumps(out).encode())
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
            users = {k: {"note": v.get("note", ""), "email": v.get("email", ""),
                         "created": v.get("created", "")}
                     for k, v in self._users().items()}
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
                    out = {"mag7": _news_mag7()}
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
            self._send(200, fpath.read_bytes(), ctype)
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
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "public, s-maxage=300, max-age=0")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
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
                    body = json.dumps({
                        "target": target, "price": price,
                        "time": ptime, "source": source,
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
                                # BASIS DYNAMIQUE : le future différé est EXACT
                                # pour son horodatage. Sur la fenêtre où future
                                # et ETF se chevauchent, l'écart médian mesure
                                # la dérive réelle de la basis -> on recale tout
                                # le dérivé dessus (et on partage la correction
                                # avec /api/quote via Redis).
                                fmap = {b["time"]: b["close"] for b in bars}
                                diffs = sorted(fmap[e["time"]] - e["close"]
                                               for e in ebars
                                               if e["time"] in fmap)
                                adj = 0.0
                                if len(diffs) >= 5:
                                    adj = diffs[len(diffs) // 2]
                                    if abs(adj) > (ebars[-1]["close"] * 0.01):
                                        adj = 0.0        # garde-fou aberration
                                if adj:
                                    ebars = [{"time": e["time"],
                                              "open": round(e["open"] + adj, 2),
                                              "high": round(e["high"] + adj, 2),
                                              "low": round(e["low"] + adj, 2),
                                              "close": round(e["close"] + adj, 2)}
                                             for e in ebars]
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
                    body = json.dumps({"target": target, "interval": interval,
                                       "bars": bars, "src": src_flag,
                                       "price": meta.get("regularMarketPrice")}).encode()
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
                prev = _latest_payload(target)
                if prev:
                    self._preserve_daily(payload, prev)
                    if q("notify") != "1" and self._gex_locked():
                        self._freeze_levels(payload, prev)
                ok, why = _upstash_set(payload)
                payload["published"] = ok
                payload["publish_info"] = why
                # Silencieux par défaut (le run planifié de 15h25 reste la seule
                # notification automatique). ?notify=1 = envoi Discord explicite.
                if q("notify") == "1" and ok:
                    payload["notified"] = bool(discord_notify([payload]))
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
