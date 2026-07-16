"""Shared GEX computation core.

Used by:
  - api/gex.py        (Vercel Python serverless function, on-demand refresh)
  - gex_levels.py     (CLI for the daily GitHub Actions snapshot + history)

Dealer convention (SpotGamma "naive"): long calls, short puts
  -> per option signed GEX = (+1 call / -1 put) * gamma * OI * 100 * S^2 * 0.01
Walls: call wall = strike of max positive net GEX (ALL strikes),
       put wall  = strike of most negative net GEX (ALL strikes).
"""

import datetime as dt
import json
import math
import re
from zoneinfo import ZoneInfo

import numpy as np

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1m&range=1d"

# target -> (CBOE option chain, Yahoo future for the basis; None = index scale)
TARGETS = {
    "NQ":  {"chain": "_NDX", "future": "NQ=F", "etf": "QQQ", "ychart": "NQ=F"},
    "ES":  {"chain": "_SPX", "future": "ES=F", "etf": "SPY", "ychart": "ES=F"},
    "SPX": {"chain": "_SPX", "future": None,   "etf": "SPY", "ychart": "^GSPC"},
}
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=2mo"
CONTRACT_MULT = 100
RISK_FREE = 0.04
OCC_RE = re.compile(r"^([A-Z\^_]+?)(\d{6})([CP])(\d{8})$")
ET = ZoneInfo("America/New_York")


def et_today():
    """Trading date anchored to US/Eastern, not the runner's UTC clock."""
    return dt.datetime.now(ET).date()


def _finite_float(value, default=0.0):
    """float() coercion that treats None/NaN/Inf as `default`.
    CBOE occasionally emits literal NaN for degenerate 0DTE/extreme-moneyness
    greeks; `value or default` does NOT catch this because NaN is truthy."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


# --------------------------------------------------------------------------- #
# Black-Scholes gamma                                                          #
# --------------------------------------------------------------------------- #
def bs_gamma(S, K, T, sigma, r=RISK_FREE):
    """Vectorized BS gamma. S may be a scalar or array; K,T,sigma are arrays."""
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    T = np.maximum(np.asarray(T, dtype=float), 1e-6)
    sigma = np.maximum(np.asarray(sigma, dtype=float), 1e-6)
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    pdf = np.exp(-0.5 * d1**2) / math.sqrt(2 * math.pi)
    return pdf / (S * sigma * np.sqrt(T))


class Opt:
    __slots__ = ("K", "is_call", "OI", "gamma", "iv", "dte", "vol", "scale")

    def __init__(self, K, is_call, OI, gamma, iv, dte, vol=0.0, scale=1.0):
        self.K, self.is_call, self.OI = K, is_call, OI
        self.gamma, self.iv, self.dte = gamma, iv, dte
        self.vol, self.scale = vol, scale
        # scale = spot_du_produit / spot_indice (1.0 pour la chaîne indice).
        # K/scale ramène la strike à l'échelle indice ; le dollar-gamma de
        # chaque option reste calculé avec SON spot (spot_indice * scale).


# --------------------------------------------------------------------------- #
# Fetch + parse                                                                #
# --------------------------------------------------------------------------- #
def fetch_cboe(symbol):
    import requests

    url = CBOE_URL.format(sym=symbol)
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()["data"]


def _occ_expiry(m):
    yymmdd = m.group(2)
    return dt.date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))


def parse_chain(data, n_expiries, today=None):
    today = today or et_today()
    spot = float(data["current_price"])
    raw = data["options"]

    def _is_monthly(exp):
        # 3e vendredi : là où vit l'OI institutionnel (monthlies + quarterlies)
        return exp.weekday() == 4 and 15 <= exp.day <= 21

    exps = set()
    for o in raw:
        m = OCC_RE.match(o["option"])
        if not m:
            continue
        exp = _occ_expiry(m)
        if exp >= today:
            exps.add(exp)
    nearest = sorted(exps)[:n_expiries]
    monthlies = [e for e in sorted(exps)
                 if _is_monthly(e) and (e - today).days <= 60]
    keep = sorted(set(nearest) | set(monthlies))
    keep_set = set(keep)

    opts = []
    for o in raw:
        m = OCC_RE.match(o["option"])
        if not m:
            continue
        exp = _occ_expiry(m)
        if exp not in keep_set:
            continue
        oi = _finite_float(o.get("open_interest"))
        if oi <= 0:
            continue
        K = int(m.group(4)) / 1000.0
        gamma = _finite_float(o.get("gamma"))
        iv = _finite_float(o.get("iv"))
        vol = _finite_float(o.get("volume"))
        dte = max((exp - today).days, 0)
        opts.append(Opt(K, m.group(3) == "C", oi, gamma, iv, dte, vol=vol))
    return spot, opts, keep


# --------------------------------------------------------------------------- #
# ATM straddle -> expected move                                                #
# --------------------------------------------------------------------------- #
def _mid(rec):
    """Returns (mid, from_quotes). Falls back to last trade (stale) when the
    book is empty — flagged so the EM quality can be surfaced downstream."""
    bid = _finite_float(rec.get("bid"))
    ask = _finite_float(rec.get("ask"))
    if bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2.0, True
    return _finite_float(rec.get("last_trade_price")), False


def atm_straddle(data, spot, today=None):
    """Daily expected move from the nearest-expiry ATM straddle.
    Returns dict {expiry, strike, call_mid, put_mid, straddle, em_pct} or None."""
    today = today or et_today()
    by_exp = {}
    for o in data["options"]:
        m = OCC_RE.match(o["option"])
        if not m:
            continue
        exp = _occ_expiry(m)
        if exp < today:
            continue
        K = int(m.group(4)) / 1000.0
        mid, live = _mid(o)
        if mid <= 0:
            continue
        by_exp.setdefault(exp, {}).setdefault(K, {})[m.group(3)] = (mid, live)

    for exp in sorted(by_exp):  # nearest expiry with a usable straddle
        pairs = {K: v for K, v in by_exp[exp].items() if "C" in v and "P" in v}
        if not pairs:
            continue
        K = min(pairs, key=lambda k: abs(k - spot))
        (c, c_live), (p, p_live) = pairs[K]["C"], pairs[K]["P"]
        straddle = c + p
        return {
            "expiry": str(exp),
            "strike": K,
            "call_mid": round(c, 2),
            "put_mid": round(p, 2),
            "straddle": round(straddle, 2),
            "em_pct": round(100.0 * straddle / spot, 3),
            "quality": "live" if (c_live and p_live) else "indicative",
        }
    return None


STRADDLE_SIGMA = 0.7979  # ATM straddle ~= 0.8 * sigma_daily * spot (BS)


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def em_band_stats(fraction):
    """Theoretical stats for a +/- fraction*straddle band (normal, no drift).
    prob_inside: close inside the band; prob_touch: touch of ONE side."""
    z = fraction * STRADDLE_SIGMA
    inside = 2.0 * _phi(z) - 1.0
    touch = 2.0 * (1.0 - _phi(z))
    return round(100 * inside, 1), round(100 * min(touch, 1.0), 1)


def em_bands_levels(spot, straddle, fractions):
    """Extra levels for fractional straddle bands, kind 'emb'.
    Returns (levels, bands_meta)."""
    levels, meta = [], []
    for f in fractions:
        if f <= 0 or abs(f - 1.0) < 1e-9:  # 1.0 = the main EM, already plotted
            continue
        d = straddle * f
        pct = int(round(f * 100))
        levels.append((spot + d, f"EM +{pct}%", "emb"))
        levels.append((spot - d, f"EM -{pct}%", "emb"))
        inside, touch = em_band_stats(f)
        meta.append({"pct": pct, "high": None, "low": None,
                     "prob_inside": inside, "prob_touch_side": touch})
    return levels, meta


# --------------------------------------------------------------------------- #
# GEX computation                                                              #
# --------------------------------------------------------------------------- #
def per_strike_gex(spot, opts, bucket=None):
    """Net signed dollar GEX aggregated by INDEX-scale strike (calls +, puts -).
    Handles blended products: each option's dollar gamma uses its own spot
    (spot * o.scale); its strike is mapped to index scale (K / o.scale) and
    optionally bucketed (e.g. 10 pts NDX, 5 pts SPX) so index and ETF strikes
    aggregate into the same levels."""
    agg = {}
    for o in opts:
        sign = 1.0 if o.is_call else -1.0
        S = spot * o.scale
        gex = sign * o.gamma * o.OI * CONTRACT_MULT * S * S * 0.01
        k_idx = o.K / o.scale
        if bucket:
            k_idx = round(k_idx / bucket) * bucket
        agg[k_idx] = agg.get(k_idx, 0.0) + gex
    strikes = np.array(sorted(agg))
    net = np.array([agg[k] for k in strikes])
    return strikes, net


def zero_gamma_flip(opts, lo, hi, n=300):
    """Find spot where total BS-recomputed net GEX crosses zero.
    Returns None when there is no crossing in [lo, hi] (e.g. deeply positive
    gamma regime) — callers must treat the flip as optional."""
    valid = [o for o in opts if o.iv > 0]
    if not valid:
        return None
    K = np.array([o.K for o in valid])
    T = np.array([o.dte for o in valid]) / 365.0
    iv = np.array([o.iv for o in valid])
    OI = np.array([o.OI for o in valid])
    sign = np.array([1.0 if o.is_call else -1.0 for o in valid])
    scale = np.array([o.scale for o in valid])

    spots = np.linspace(lo, hi, n)
    totals = np.empty(n)
    for i, S in enumerate(spots):
        S_own = S * scale
        g = bs_gamma(S_own, K, T, iv)
        totals[i] = np.sum(sign * g * OI * CONTRACT_MULT * S_own * S_own * 0.01)

    sgn = np.sign(totals)
    cross = np.where(np.diff(sgn) != 0)[0]
    if len(cross) == 0:
        return None
    mid = (lo + hi) / 2
    j = cross[np.argmin(np.abs(spots[cross] - mid))]
    x0, x1, y0, y1 = spots[j], spots[j + 1], totals[j], totals[j + 1]
    return float(x0 - y0 * (x1 - x0) / (y1 - y0))


def zero_dte_walls(spot, opts, bucket=None):
    """Call/Put walls on the nearest expiry only, weighted by max(OI, volume):
    OI is yesterday's settled positioning, volume captures today's 0DTE flow.
    Returns (cw, pw, expiry_dte) — any element may be None."""
    if not opts:
        return None, None, None
    min_dte = min(o.dte for o in opts)
    sub = [o for o in opts if o.dte == min_dte]
    agg = {}
    for o in sub:
        sign = 1.0 if o.is_call else -1.0
        w = max(o.OI, o.vol)
        S = spot * o.scale
        gex = sign * o.gamma * w * CONTRACT_MULT * S * S * 0.01
        k_idx = o.K / o.scale
        if bucket:
            k_idx = round(k_idx / bucket) * bucket
        agg[k_idx] = agg.get(k_idx, 0.0) + gex
    strikes = np.array(sorted(agg))
    net = np.array([agg[k] for k in strikes])
    cw = float(strikes[int(np.argmax(net))]) if len(net) and net.max() > 0 else None
    pw = float(strikes[int(np.argmin(net))]) if len(net) and net.min() < 0 else None
    return cw, pw, min_dte


def max_pain(opts):
    """Classic max pain on the nearest expiry of the INDEX chain (ETF legs
    excluded: mixing payout scales is not meaningful)."""
    opts = [o for o in opts if o.scale == 1.0]
    if not opts:
        return None
    min_dte = min(o.dte for o in opts)
    sub = [o for o in opts if o.dte == min_dte]
    ks = np.array(sorted({o.K for o in sub}))
    if not len(ks):
        return None
    K = np.array([o.K for o in sub])
    OI = np.array([o.OI for o in sub])
    is_call = np.array([o.is_call for o in sub])
    pay = np.array([
        np.sum(np.where(is_call, OI * np.maximum(0.0, S - K), OI * np.maximum(0.0, K - S)))
        for S in ks
    ])
    return float(ks[int(np.argmin(pay))])


def atm_iv(spot, opts, now_et=None):
    """Robust ATM implied vol: MEDIAN of the ~8 options closest to spot on the
    front expiry. The front includes today's 0DTE while the session still has
    most of the day ahead (its IV is literally the market's price of TODAY's
    move — what daily sigma grids are built on). After 13:00 ET the 0DTE IV
    becomes a decaying-intraday artefact, so we roll to the next expiry."""
    opts = [o for o in opts if o.scale == 1.0]
    if now_et is None:
        now_et = dt.datetime.now(ET)
    min_ok = 0 if now_et.hour < 13 else 1
    cands = ([o for o in opts if o.dte >= min_ok and o.iv > 0]
             or [o for o in opts if o.iv > 0])
    if not cands:
        return None
    min_dte = min(o.dte for o in cands)
    sub = sorted((o for o in cands if o.dte == min_dte), key=lambda o: abs(o.K - spot))
    ivs = sorted(o.iv for o in sub[:8])
    n = len(ivs)
    return ivs[n // 2] if n % 2 else (ivs[n // 2 - 1] + ivs[n // 2]) / 2.0


def extract_levels(spot, strikes, net, flip, em=None, extras=None, top_n=4):
    """Return ordered list of (price, label, kind) on the INDEX scale."""
    levels = []

    # SpotGamma convention: walls picked across ALL strikes, so a call wall
    # sitting at/below spot (end-of-squeeze magnet) is not missed.
    cw = pw = None
    if len(net) and net.max() > 0:
        cw = float(strikes[int(np.argmax(net))])
        levels.append((cw, "Call Wall", "res"))
    if len(net) and net.min() < 0:
        pw = float(strikes[int(np.argmin(net))])
        levels.append((pw, "Put Wall", "sup"))

    # HGEX : strike au gamma absolu dominant — l'aimant principal de la séance
    if len(net):
        hg = float(strikes[int(np.argmax(np.abs(net)))])
        if hg != cw and hg != pw:
            levels.append((hg, "HGEX", "hgex"))

    if flip is not None:
        levels.append((flip, "Gamma Flip", "flip"))

    if em is not None:
        a = em.get("anchor_idx", spot)
        levels.append((a + em["straddle"], "EM High", "emh"))
        levels.append((a - em["straddle"], "EM Low", "eml"))

    if extras:
        levels.extend(extras)

    chosen = {round(p, 2) for p, _, _ in levels}
    order = np.argsort(-np.abs(net))
    added = 0
    for idx in order:
        k = float(strikes[idx])
        if round(k, 2) in chosen:
            continue
        kind = "gpos" if net[idx] > 0 else "gneg"
        tag = "G+" if net[idx] > 0 else "G-"
        levels.append((k, tag, kind))
        chosen.add(round(k, 2))
        added += 1
        if added >= top_n:
            break

    return levels


# --------------------------------------------------------------------------- #
# NQ basis (direct Yahoo HTTP, no yfinance dependency)                         #
# --------------------------------------------------------------------------- #
def future_basis(index_spot, yahoo_future, override=None):
    """basis = front future - index spot. yahoo_future=None -> index scale (0).
    Returns (basis, target_price_or_None, source)."""
    if override is not None:
        return float(override), float(index_spot) + float(override), "manual"
    if yahoo_future is None:
        return 0.0, float(index_spot), "index"
    try:
        import requests

        r = requests.get(YAHOO_URL.format(sym=yahoo_future), timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        fut = float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
        return fut - float(index_spot), fut, "yahoo"
    except Exception:
        return 0.0, None, "none"


# --------------------------------------------------------------------------- #
# Upstash KV helpers (env UPSTASH_REDIS_REST_* or KV_REST_API_*)               #
# --------------------------------------------------------------------------- #
def _kv_conf():
    import os

    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
    return (url.rstrip("/"), token) if url and token else (None, None)


def kv_get(key):
    """GET a key from Upstash REST. Returns string or None. Never raises."""
    url, token = _kv_conf()
    if not url:
        return None
    try:
        import requests

        r = requests.get(f"{url}/get/{key}",
                         headers={"Authorization": f"Bearer {token}"}, timeout=5)
        r.raise_for_status()
        return r.json().get("result")
    except Exception:
        return None


def kv_set(key, value, ex=None):
    """SET a key in Upstash REST. Returns bool. Never raises."""
    url, token = _kv_conf()
    if not url:
        return False
    try:
        import requests

        q = f"?EX={int(ex)}" if ex else ""
        r = requests.post(f"{url}/set/{key}{q}",
                          headers={"Authorization": f"Bearer {token}"},
                          data=value, timeout=5)
        r.raise_for_status()
        return True
    except Exception:
        return False


WEBHOOKS_KEY = "gex:webhooks"


def fetch_webhooks():
    """Per-target webhook config from Upstash: {"NQ": url, "ES": url, "SPX": url,
    "default": url}. Empty dict when unset/unavailable."""
    v = kv_get(WEBHOOKS_KEY)
    if not v:
        return {}
    try:
        d = json.loads(v)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_webhooks(cfg):
    return kv_set(WEBHOOKS_KEY, json.dumps(cfg))


# --------------------------------------------------------------------------- #
# Discord notification                                                         #
# --------------------------------------------------------------------------- #
def discord_notify(payloads, dashboard_url="https://gexdash.wealthbuilders.group"):
    """Post published levels to a Discord webhook (env DISCORD_WEBHOOK_URL).
    Accepts one payload dict or a list (one embed per target, single message).
    No-op when unset. Never raises. Returns True on success."""
    import os
    import traceback as tb

    if isinstance(payloads, dict):
        payloads = [payloads]
    env_url = os.environ.get("DISCORD_WEBHOOK_URL")
    cfg = fetch_webhooks()
    groups = {}
    for payload in payloads:
        tgt = payload.get("target", "NQ")
        url = cfg.get(tgt) or cfg.get("default") or env_url
        if url:
            groups.setdefault(url, []).append(payload)
    if not groups:
        return False
    try:
        import requests

        ok = True
        for url, plist in groups.items():
            embeds = [_discord_embed(p, dashboard_url) for p in plist[:10]]
            r = requests.post(url, json={"embeds": embeds}, timeout=10)
            if r.status_code >= 300:
                ok = False
        return ok
    except Exception:
        tb.print_exc()
        return False


def _discord_embed(payload, dashboard_url):
    def find(kind):
        for L in payload.get("levels", []):
            if L["kind"] == kind:
                return L["price_nq"]
        return None

    def f(v):
        return f"{v:,.1f}".replace(",", " ") if v is not None else "—"

    em = payload.get("expected_move") or {}
    live = payload.get("mode") == "live"
    regime = payload.get("regime")
    if True:
        fields = [
            {"name": "Call Wall", "value": f(find("res")), "inline": True},
            {"name": "Put Wall", "value": f(find("sup")), "inline": True},
            {"name": "Gamma Flip", "value": f(find("flip")), "inline": True},
            {"name": "CW 0DTE", "value": f(find("res0")), "inline": True},
            {"name": "PW 0DTE", "value": f(find("sup0")), "inline": True},
            {"name": "Max Pain", "value": f(find("mpain")), "inline": True},
            {"name": "EM ±", "value": f"{em.get('straddle', '—')} pts ({em.get('em_pct', '—')}%)", "inline": True},
            {"name": "Net GEX", "value": f"{payload.get('net_gex_bn', '—')} $Bn/1%", "inline": True},
            {"name": "P/C OI", "value": str(payload.get("pc_oi", "—")), "inline": True},
        ]
        pine = payload.get("pine", "")
        tgt = payload.get("target", "NQ")
        desc = f"**String Pine — coller dans la zone {tgt} de l'indicateur :**\n```{pine}```" if pine else ""
        return {
            "title": f"GEX {tgt} — {payload.get('date')} · {'LIVE (publié)' if live else 'SNAPSHOT auto'}",
            "description": desc[:1800],
            "url": dashboard_url,
            "color": 0x26A69A if regime == "positive" else 0xEF5350,
            "fields": fields,
            "footer": {"text": f"{tgt} {f(payload.get('nq_price'))} · basis {payload.get('basis')} ({payload.get('basis_source')}) · régime GAMMA {'+' if regime == 'positive' else '−'}"},
        }


def daily_bars(yahoo_sym):
    """(today_open, atr14) on the TARGET scale. The futures daily bar starts
    18:00 ET the prior evening, so the open is fixed well before a pre-open
    run. ATR14 = mean true range of the last 14 COMPLETED daily bars.
    Either element may be None on failure."""
    try:
        import requests
        from urllib.parse import quote as _q

        r = requests.get(YAHOO_CHART.format(sym=_q(yahoo_sym)),
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        q = (res.get("indicators", {}).get("quote") or [{}])[0]
        rows = [(o, h, l, c) for o, h, l, c in
                zip(q.get("open") or [], q.get("high") or [],
                    q.get("low") or [], q.get("close") or [])
                if None not in (o, h, l, c)
                and all(math.isfinite(float(x)) for x in (o, h, l, c))]
        if not rows:
            return None, None
        today_open = float(rows[-1][0])
        atr = None
        done = rows[:-1]  # barres terminées uniquement
        if len(done) >= 5:
            trs = []
            for i in range(1, len(done)):
                _, h, l, _ = done[i]
                pc = done[i - 1][3]
                trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            trs = trs[-14:]
            atr = float(sum(trs) / len(trs))
        return today_open, atr
    except Exception:
        return None, None


def open_grid(anchor, iv=None, atr=None, n=6):
    """Daily-open grid in VOLATILITY multiples: anchor +/- 0.5..3.0 units.
    Preferred unit = the 1-day implied sigma (anchor x IV_ATM / sqrt(252)) —
    the same yardstick options desks quote the day in, which is why these
    levels hold so well. Falls back to ATR14, then to percent steps."""
    if anchor is None or anchor <= 0:
        return None
    if iv and iv > 0:
        mode, unit = "iv", anchor * float(iv) / math.sqrt(252)
    elif atr and atr > 0:
        mode, unit = "atr", float(atr)
    else:
        mode, unit = "pct", anchor * 0.01  # 1% en points
    levels = []
    for i in range(1, n + 1):
        m = round(i * 0.5, 2)
        levels.append({"mult": m,
                       "up": round(anchor + m * unit, 1),
                       "down": round(anchor - m * unit, 1)})
    return {"anchor": round(anchor, 1), "mode": mode,
            "unit": round(unit, 2), "n": n, "levels": levels}


# --------------------------------------------------------------------------- #
# Output helpers                                                               #
# --------------------------------------------------------------------------- #
def to_pine_string(levels, basis):
    rows = []
    for price, label, kind in sorted(levels, key=lambda x: -x[0]):
        rows.append(f"{price + basis:.1f},{label},{kind}")
    return ";".join(rows)


def gex_profile(spot, strikes, net, basis, band=0.045):
    """Per-strike net GEX around spot, NQ scale, $Bn — feeds the dashboard chart."""
    mask = (strikes >= spot * (1 - band)) & (strikes <= spot * (1 + band))
    return [
        {"k_nq": round(float(k) + basis, 1), "gex_bn": round(float(g) / 1e9, 3)}
        for k, g in zip(strikes[mask], net[mask])
    ]


def build_payload(target="NQ", n_expiries=10, top_n=4, basis_override=None,
                  mode="snapshot", em_bands=(0.5, 1.5), chain_cache=None,
                  iv_override=None):
    """Full pipeline: fetch -> compute -> JSON-ready payload dict.
    Raises on fetch/parse failure; caller handles errors.
    n_expiries=10 by default: wide enough that main walls approach the
    aggregate (MenthorQ-style) view while 0DTE walls give the intraday one."""
    if target not in TARGETS:
        raise ValueError(f"target must be one of {sorted(TARGETS)}")
    cfg = TARGETS[target]
    symbol = cfg["chain"]
    today = et_today()

    def _chain(sym):
        if chain_cache is not None and sym in chain_cache:
            return chain_cache[sym]
        d = fetch_cboe(sym)
        if chain_cache is not None:
            chain_cache[sym] = d
        return d

    data = _chain(symbol)
    spot, opts, exps = parse_chain(data, n_expiries, today=today)
    # garde-fous : refuser une chaîne dégénérée plutôt que publier du bruit
    idx_oi = sum(o.OI for o in opts)
    if len(opts) < 50 or idx_oi < 1000:
        raise ValueError(
            f"index chain too thin ({len(opts)} opts, OI {idx_oi:.0f}) — refusing")
    sources = [{"chain": symbol, "opts": len(opts), "oi": round(idx_oi)}]

    # blend ETF (QQQ/SPY) : le gros du positionnement gamma vit là.
    # Strikes ramenées à l'échelle indice, dollar-gamma agrégé par bucket.
    etf_sym = cfg.get("etf")
    if etf_sym:
        try:
            etf_data = _chain(etf_sym)
            etf_spot, etf_opts, _ = parse_chain(etf_data, n_expiries, today=today)
            scale = etf_spot / spot
            for o in etf_opts:
                o.scale = scale
            opts = opts + etf_opts
            sources.append({"chain": etf_sym, "opts": len(etf_opts),
                            "oi": round(sum(o.OI for o in etf_opts)),
                            "scale": round(scale, 5)})
        except Exception as e:
            sources.append({"chain": etf_sym, "error": str(e)[:120]})

    bucket = 10.0 if spot >= 10000 else 5.0
    strikes, net = per_strike_gex(spot, opts, bucket=bucket)
    flip = zero_gamma_flip(opts, spot * 0.92, spot * 1.08)
    if flip is None:  # régime très déséquilibré : élargir avant d'abandonner
        flip = zero_gamma_flip(opts, spot * 0.85, spot * 1.15)
    iv = float(iv_override) if iv_override else atm_iv(spot, opts)

    # basis et open daily calculés tôt : l'EM daily s'ancre au Daily Open
    basis, nq_price, basis_source = future_basis(spot, cfg["future"], override=basis_override)
    if basis_source == "none":  # Yahoo KO : dernière basis connue plutôt que 0
        last = kv_get(f"gex:basis:{target}")
        if last is not None:
            try:
                basis = float(last)
                nq_price = spot + basis
                basis_source = "last-known"
            except ValueError:
                pass
    elif basis_source in ("yahoo", "manual"):
        kv_set(f"gex:basis:{target}", str(round(basis, 2)), ex=7 * 86400)
    d_open, atr14 = daily_bars(cfg["ychart"])

    # EM DAILY : straddle théorique plein-jour = 0.8 x sigma implicite,
    # ancré au Daily Open — stable toute la séance (le straddle de marché,
    # lui, mesure le move RESTANT et fond au fil de la journée : il est
    # conservé en information secondaire).
    market = atm_straddle(data, spot, today=today)
    em = None
    if iv is not None:
        size = 0.8 * spot * iv / math.sqrt(252)
        anchor_idx = (d_open - basis) if d_open is not None else spot
        em = {"straddle": round(size, 2),
              "em_pct": round(100.0 * size / spot, 3),
              "anchor_idx": round(anchor_idx, 2),
              "anchor": round(anchor_idx + basis, 1),
              "sigma1d": round(spot * iv / math.sqrt(252), 2),
              "iv_source": "override" if iv_override else "chain-front",
              "source": "0.8σ daily", "quality": "model",
              "expiry": market["expiry"] if market else None,
              "market_straddle": market["straddle"] if market else None,
              "market_quality": market["quality"] if market else None}
    elif market is not None:  # pas d'IV exploitable : straddle brut en secours
        em = dict(market, source="straddle", anchor_idx=spot,
                  anchor=None, market_straddle=market["straddle"],
                  market_quality=market["quality"])

    # ---- extra levels: 0DTE walls, max pain, IV-based 1D range ----
    extras = []
    cw0, pw0, dte0 = zero_dte_walls(spot, opts, bucket=bucket)
    if cw0 is not None:
        extras.append((cw0, "CW 0DTE", "res0"))
    if pw0 is not None:
        extras.append((pw0, "PW 0DTE", "sup0"))
    mp = max_pain(opts)
    if mp is not None:
        extras.append((mp, "Max Pain", "mpain"))
    if iv is not None:
        rng = spot * iv / math.sqrt(252)
        extras.append((spot + rng, "1D Max", "ivh"))
        extras.append((spot - rng, "1D Min", "ivl"))
    bands_meta = []
    if em is not None and em_bands:
        band_lv, bands_meta = em_bands_levels(em.get("anchor_idx", spot),
                                              em["straddle"], em_bands)
        extras.extend(band_lv)

    levels = extract_levels(spot, strikes, net, flip, em=em, extras=extras, top_n=top_n)
    net_total_bn = float(net.sum()) / 1e9
    call_oi = sum(o.OI for o in opts if o.is_call)
    put_oi = sum(o.OI for o in opts if not o.is_call)
    pc_oi = round(put_oi / call_oi, 2) if call_oi > 0 else None

    levels_out = [
        {"price_nq": round(p + basis, 1), "label": l, "kind": k}
        for p, l, k in sorted(levels, key=lambda x: -x[0])
    ]
    if em is not None:
        inside100, touch100 = em_band_stats(1.0)
        em["prob_inside"] = inside100
        em["prob_touch_side"] = touch100
        for b in bands_meta:
            b["high"] = round(spot + em["straddle"] * b["pct"] / 100 + basis, 1)
            b["low"] = round(spot - em["straddle"] * b["pct"] / 100 + basis, 1)
        em["bands"] = bands_meta
    grid = open_grid(d_open, iv=iv, atr=atr14)

    pine_rows = [(p + basis, l, k) for p, l, k in levels]
    if grid:
        suf = {"iv": "σ", "atr": " ATR"}.get(grid["mode"], "%")
        pine_rows.append((grid["anchor"], "Daily O", "opo"))
        for g in grid["levels"]:
            pine_rows.append((g["up"], f"+{g['mult']:g}{suf}", "opu"))
            pine_rows.append((g["down"], f"-{g['mult']:g}{suf}", "opd"))
    pine = ";".join(f"{p:.1f},{l},{k}" for p, l, k in sorted(pine_rows, key=lambda x: -x[0]))

    return {
        "date": today.isoformat(),
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": mode,
        "target": target,
        "symbol": symbol,
        "index_spot": spot,
        "nq_price": nq_price,
        "basis": round(basis, 2),
        "basis_source": basis_source,
        "net_gex_bn": round(net_total_bn, 2),
        "regime": "positive" if net_total_bn > 0 else "negative",
        "pc_oi": pc_oi,
        "iv_atm": round(iv, 4) if iv is not None else None,
        "zero_dte": {"dte": dte0, "call_wall": cw0, "put_wall": pw0},
        "max_pain_index": mp,
        "expected_move": em,
        "expiries": [str(e) for e in exps],
        "sources": sources,
        "bucket": bucket,
        "levels": levels_out,
        "open_grid": grid,
        "profile": gex_profile(spot, strikes, net, basis),
        "pine": pine,
    }
