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
import os
import time
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from api._gex_core import (TARGETS, build_payload, discord_news,
                           discord_notify, discord_send, et_today,
                           fetch_webhooks, kv_get, kv_set,
                           refresh_daily_anchor, save_webhooks, parse_chain, per_strike_gex, fetch_cboe, atm_iv)

CRON_LOG_KEY = "gex:cron:log"
FINNHUB_CACHE_S = 2.5
DP_SYMS = {"NQ": "QQQ", "ES": "SPY", "SPX": "SPY"}


def _finra_dp_day(ymd):
    """Volume off-exchange FINRA (fichier CNMS quotidien) pour QQQ et SPY.
    C'est le volume exécuté hors bourses (dark pools + internalisation),
    avec sa part shortée — la matière première du ratio type DIX.
    Retourne {"QQQ": (short, total), "SPY": (...)} ou None. Jamais d'exception."""
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
                    out[p[1]] = (int(p[2]), int(p[4]))
                except ValueError:
                    pass
                if len(out) == 2:
                    break
        return out or None
    except Exception:
        return None


def _finnhub_quote(sym):
    """Cote actions US quasi temps réel via Finnhub (env FINNHUB_API_KEY).
    Micro-cache Redis de quelques secondes : quel que soit le trafic du site,
    l'API tierce reste loin sous la limite du palier gratuit (60 req/min).
    Retourne (prix, ts_dernier_trade) ou None — jamais d'exception."""
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return None
    import time as _t
    now = _t.time()
    ck = f"gex:fh:{sym}"
    try:
        cached = kv_get(ck)
        if cached:
            d = json.loads(cached)
            if now - d.get("at", 0) < FINNHUB_CACHE_S and d.get("p"):
                return d["p"], d.get("t") or int(now)
    except Exception:
        pass
    try:
        import requests
        r = requests.get("https://finnhub.io/api/v1/quote",
                         params={"symbol": sym, "token": key}, timeout=4)
        j = r.json()
        p, t = j.get("c"), j.get("t")
        if not p:
            return None
        kv_set(ck, json.dumps({"p": p, "t": t, "at": now}), ex=30)
        return p, t or int(now)
    except Exception:
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
    "/ui.js": ("ui.js", "application/javascript; charset=utf-8"),
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


YCHART = {"NQ": "NQ=F", "ES": "ES=F", "SPX": "^GSPC"}
YETF = {"NQ": "QQQ", "ES": "SPY", "SPX": "SPY"}
CHART_INTERVALS = {"1m": "1d", "5m": "5d", "15m": "5d"}  # interval -> range


def _clean_bars(bars):
    """Écrête les mèches aberrantes : le pré/post-marché ETF de Yahoo contient
    des prints isolés loin du marché (odd lots) qui, convertis, donnent des
    barres géantes. On borne high/low à ~10x l'amplitude médiane des bougies
    (plancher 0.35% du prix) — les vrais mouvements passent, les prints non."""
    if len(bars) < 10:
        return bars
    rngs = sorted(b["high"] - b["low"] for b in bars)
    med = rngs[len(rngs) // 2] or 1.0
    for b in bars:
        px = max(abs(b["close"]), 1.0)
        lim = max(10.0 * med, px * 0.0035)
        top = max(b["open"], b["close"])
        bot = min(b["open"], b["close"])
        if b["high"] - top > lim:
            b["high"] = round(top + lim, 2)
        if bot - b["low"] > lim:
            b["low"] = round(bot - lim, 2)
    return bars


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
    def _auth_key(self, qs=None):
        """True if the request carries a valid GEX_REFRESH_KEY."""
        secret = os.environ.get("GEX_REFRESH_KEY")
        if not secret:
            return True
        given = self.headers.get("x-gex-key") or ((qs or {}).get("key", [None])[0] or "")
        return bool(given) and hmac.compare_digest(given, secret)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

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
            for k in ("discord", "tradingview"):
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
                ok, why = _upstash_set(payload)
                results[target] = {"skipped": False, "published": ok,
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
                self._send(400, json.dumps({"error": "target must be NQ, ES or SPX"}).encode(),
                           "application/json")
                return
            payload = _latest_payload(target)
            if payload is None:
                self._send(404, json.dumps(
                    {"error": "no levels yet - run the GitHub Action or a refresh"}).encode(),
                    "application/json")
            else:
                self._send(200, json.dumps(payload).encode(), "application/json")
            return

        # ---- static: dashboard + committed daily files ----
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
                data = fetch_cboe(TARGETS[tgt]["chain"])
                spot, opts, _exps = parse_chain(data, 8, today=et_today())
                bucket = {"NQ": 10.0, "ES": 5.0, "SPX": 5.0}.get(tgt)
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
            while len(days) < 14:                 # 14 derniers jours ouvrés
                if cur.weekday() < 5:
                    days.append(cur.strftime("%Y%m%d"))
                cur -= dt.timedelta(days=1)
            fetched = 0
            for ymd in days:                      # récent -> ancien, max 4 fetchs
                if ymd in have or fetched >= 4:
                    continue
                data = _finra_dp_day(ymd)
                fetched += 1
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
                self._send(400, json.dumps({"error": "target must be NQ, ES or SPX"}).encode(),
                           "application/json")
                return
            interval = (qs0.get("interval", ["5m"])[0] or "5m")
            if interval not in CHART_INTERVALS:
                interval = "5m"
            try:
                res = _yahoo_chart(YCHART[target], interval, CHART_INTERVALS[interval])
                meta = res.get("meta", {})
                if path == "/api/quote":
                    price = meta.get("regularMarketPrice")
                    ptime = meta.get("regularMarketTime") or 0
                    source = "fut"
                    # l'ETF US cote en quasi temps réel là où le future est différé :
                    # converti à l'échelle target via le scale et la basis du snapshot
                    try:
                        pay = _latest_payload(target) or {}
                        scale = next((s.get("scale") for s in pay.get("sources", [])
                                      if s.get("chain") == YETF[target] and s.get("scale")), None)
                        if scale:
                            # 1) Finnhub (temps réel actions US), 2) ETF Yahoo en repli
                            ep = et = None
                            src2 = None
                            fh = _finnhub_quote(YETF[target])
                            if fh:
                                ep, et, src2 = fh[0], fh[1], "finnhub"
                            if not ep:
                                emeta = _yahoo_chart(YETF[target], "1m", "1d").get("meta", {})
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
                                derived = round(ep / scale + (pay.get("basis") or 0), 2)
                                # garde-fou anti-aberration : rejette un dérivé
                                # très loin du future SEULEMENT si le future est
                                # lui-même frais (< 60 s). Sur un future périmé
                                # (open, gap), on fait confiance à l'ETF récent.
                                fut_fresh = price and ptime and (_tt.time() - ptime) < 60
                                far = price and abs(derived / price - 1) > 0.015
                                if fut_fresh and far:
                                    source = "fut-guard"
                                else:
                                    price, ptime, source = derived, et, src2
                    except Exception:
                        pass
                    body = json.dumps({
                        "target": target, "price": price,
                        "time": ptime, "source": source,
                    }).encode()
                    max_age = 1
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
                            rese = _yahoo_chart(YETF[target], interval,
                                                CHART_INTERVALS[interval],
                                                prepost=True)
                            ebars = [{"time": b["time"],
                                      "open": round(b["open"] / scale + basis, 2),
                                      "high": round(b["high"] / scale + basis, 2),
                                      "low": round(b["low"] / scale + basis, 2),
                                      "close": round(b["close"] / scale + basis, 2)}
                                     for b in _pb(rese)]
                            if ebars:
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
                self.send_header("Cache-Control", f"public, s-maxage={max_age}, max-age=0")
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
                    raise ValueError("target must be NQ, ES or SPX")
                bands = tuple(
                    float(x) for x in q("em_bands", "0.5,1.5").split(",") if x.strip()
                )

                payload = build_payload(
                    target=target, n_expiries=n, basis_override=basis, mode="live",
                    em_bands=bands, iv_override=iv_ov
                )
                ok, why = _upstash_set(payload)
                payload["published"] = ok
                payload["publish_info"] = why
                # Silencieux par défaut (le run planifié de 15h25 reste la seule
                # notification automatique). ?notify=1 = envoi Discord explicite.
                if q("notify") == "1" and ok:
                    payload["notified"] = bool(discord_notify([payload]))
                self._send(200, json.dumps(payload).encode(), "application/json")
            except Exception as e:
                traceback.print_exc()
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            return

        self._send(404, json.dumps({"error": "not found"}).encode(), "application/json")
