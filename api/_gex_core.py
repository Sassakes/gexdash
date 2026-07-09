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
import math
import re
from zoneinfo import ZoneInfo

import numpy as np

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
YAHOO_NQ = "https://query1.finance.yahoo.com/v8/finance/chart/NQ=F?interval=1m&range=1d"
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
    __slots__ = ("K", "is_call", "OI", "gamma", "iv", "dte")

    def __init__(self, K, is_call, OI, gamma, iv, dte):
        self.K, self.is_call, self.OI = K, is_call, OI
        self.gamma, self.iv, self.dte = gamma, iv, dte


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

    exps = set()
    for o in raw:
        m = OCC_RE.match(o["option"])
        if not m:
            continue
        exp = _occ_expiry(m)
        if exp >= today:
            exps.add(exp)
    keep = sorted(exps)[:n_expiries]
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
        dte = max((exp - today).days, 0)
        opts.append(Opt(K, m.group(3) == "C", oi, gamma, iv, dte))
    return spot, opts, keep


# --------------------------------------------------------------------------- #
# ATM straddle -> expected move                                                #
# --------------------------------------------------------------------------- #
def _mid(rec):
    bid = _finite_float(rec.get("bid"))
    ask = _finite_float(rec.get("ask"))
    if bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2.0
    return _finite_float(rec.get("last_trade_price"))


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
        mid = _mid(o)
        if mid <= 0:
            continue
        by_exp.setdefault(exp, {}).setdefault(K, {})[m.group(3)] = mid

    for exp in sorted(by_exp):  # nearest expiry with a usable straddle
        pairs = {K: v for K, v in by_exp[exp].items() if "C" in v and "P" in v}
        if not pairs:
            continue
        K = min(pairs, key=lambda k: abs(k - spot))
        c, p = pairs[K]["C"], pairs[K]["P"]
        straddle = c + p
        return {
            "expiry": str(exp),
            "strike": K,
            "call_mid": round(c, 2),
            "put_mid": round(p, 2),
            "straddle": round(straddle, 2),
            "em_pct": round(100.0 * straddle / spot, 3),
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
def per_strike_gex(spot, opts):
    """Net signed GEX aggregated by strike (calls +, puts -), using chain gamma."""
    agg = {}
    for o in opts:
        sign = 1.0 if o.is_call else -1.0
        gex = sign * o.gamma * o.OI * CONTRACT_MULT * spot * spot * 0.01
        agg[o.K] = agg.get(o.K, 0.0) + gex
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

    spots = np.linspace(lo, hi, n)
    totals = np.empty(n)
    for i, S in enumerate(spots):
        g = bs_gamma(S, K, T, iv)
        totals[i] = np.sum(sign * g * OI * CONTRACT_MULT * S * S * 0.01)

    sgn = np.sign(totals)
    cross = np.where(np.diff(sgn) != 0)[0]
    if len(cross) == 0:
        return None
    mid = (lo + hi) / 2
    j = cross[np.argmin(np.abs(spots[cross] - mid))]
    x0, x1, y0, y1 = spots[j], spots[j + 1], totals[j], totals[j + 1]
    return float(x0 - y0 * (x1 - x0) / (y1 - y0))


def zero_dte_walls(spot, opts):
    """Call/Put walls computed on the nearest expiry only (0DTE on a trading
    day). Returns (cw, pw, expiry_dte) — any element may be None."""
    if not opts:
        return None, None, None
    min_dte = min(o.dte for o in opts)
    sub = [o for o in opts if o.dte == min_dte]
    strikes, net = per_strike_gex(spot, sub)
    cw = float(strikes[int(np.argmax(net))]) if len(net) and net.max() > 0 else None
    pw = float(strikes[int(np.argmin(net))]) if len(net) and net.min() < 0 else None
    return cw, pw, min_dte


def max_pain(opts):
    """Classic max pain on the nearest expiry: strike minimizing total
    intrinsic payout to option holders."""
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


def atm_iv(spot, opts):
    """ATM implied vol from the nearest expiry with dte >= 1 (0DTE IV decays
    intraday and is a poor daily-range proxy). Falls back to any expiry."""
    cands = [o for o in opts if o.dte >= 1 and o.iv > 0] or [o for o in opts if o.iv > 0]
    if not cands:
        return None
    min_dte = min(o.dte for o in cands)
    sub = [o for o in cands if o.dte == min_dte]
    K = min({o.K for o in sub}, key=lambda k: abs(k - spot))
    ivs = [o.iv for o in sub if o.K == K]
    return sum(ivs) / len(ivs)


def extract_levels(spot, strikes, net, flip, em=None, extras=None, top_n=4):
    """Return ordered list of (price, label, kind) on the INDEX scale."""
    levels = []

    # SpotGamma convention: walls picked across ALL strikes, so a call wall
    # sitting at/below spot (end-of-squeeze magnet) is not missed.
    if len(net) and net.max() > 0:
        cw = strikes[int(np.argmax(net))]
        levels.append((float(cw), "Call Wall", "res"))
    if len(net) and net.min() < 0:
        pw = strikes[int(np.argmin(net))]
        levels.append((float(pw), "Put Wall", "sup"))

    if flip is not None:
        levels.append((flip, "Gamma Flip", "flip"))

    if em is not None:
        levels.append((spot + em["straddle"], "EM High", "emh"))
        levels.append((spot - em["straddle"], "EM Low", "eml"))

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
def nq_basis(ndx_spot, override=None):
    """basis = NQ front future - NDX spot.
    Returns (basis, nq_price_or_None, source)."""
    if override is not None:
        return float(override), float(ndx_spot) + float(override), "manual"
    try:
        import requests

        r = requests.get(YAHOO_NQ, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        nq = float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
        return nq - float(ndx_spot), nq, "yahoo"
    except Exception:
        return 0.0, None, "none"


# --------------------------------------------------------------------------- #
# Discord notification                                                         #
# --------------------------------------------------------------------------- #
def discord_notify(payload, dashboard_url="https://gexdash.wealthbuilders.group"):
    """Post the published levels to a Discord webhook (env DISCORD_WEBHOOK_URL).
    No-op when unset. Never raises. Returns True on success."""
    import os
    import traceback as tb

    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return False
    try:
        import requests

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
        embed = {
            "title": f"GEX NQ — {payload.get('date')} · {'LIVE (publié)' if live else 'SNAPSHOT auto'}",
            "url": dashboard_url,
            "color": 0x26A69A if regime == "positive" else 0xEF5350,
            "fields": fields,
            "footer": {"text": f"NQ {f(payload.get('nq_price'))} · basis {payload.get('basis')} ({payload.get('basis_source')}) · régime GAMMA {'+' if regime == 'positive' else '−'}"},
        }
        r = requests.post(url, json={"embeds": [embed]}, timeout=10)
        r.raise_for_status()
        return True
    except Exception:
        tb.print_exc()
        return False


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


def build_payload(symbol="_NDX", n_expiries=10, top_n=4, basis_override=None,
                  mode="snapshot", em_bands=(0.5, 1.5)):
    """Full pipeline: fetch -> compute -> JSON-ready payload dict.
    Raises on fetch/parse failure; caller handles errors.
    n_expiries=10 by default: wide enough that main walls approach the
    aggregate (MenthorQ-style) view while 0DTE walls give the intraday one."""
    today = et_today()
    data = fetch_cboe(symbol)
    spot, opts, exps = parse_chain(data, n_expiries, today=today)
    if not opts:
        raise ValueError("no options parsed from CBOE chain")
    strikes, net = per_strike_gex(spot, opts)
    flip = zero_gamma_flip(opts, spot * 0.92, spot * 1.08)
    em = atm_straddle(data, spot, today=today)

    # ---- extra levels: 0DTE walls, max pain, IV-based 1D range ----
    extras = []
    cw0, pw0, dte0 = zero_dte_walls(spot, opts)
    if cw0 is not None:
        extras.append((cw0, "CW 0DTE", "res0"))
    if pw0 is not None:
        extras.append((pw0, "PW 0DTE", "sup0"))
    mp = max_pain(opts)
    if mp is not None:
        extras.append((mp, "Max Pain", "mpain"))
    iv = atm_iv(spot, opts)
    if iv is not None:
        rng = spot * iv / math.sqrt(252)
        extras.append((spot + rng, "1D Max", "ivh"))
        extras.append((spot - rng, "1D Min", "ivl"))
    bands_meta = []
    if em is not None and em_bands:
        band_lv, bands_meta = em_bands_levels(spot, em["straddle"], em_bands)
        extras.extend(band_lv)

    levels = extract_levels(spot, strikes, net, flip, em=em, extras=extras, top_n=top_n)
    basis, nq_price, basis_source = nq_basis(spot, override=basis_override)
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
    return {
        "date": today.isoformat(),
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": mode,
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
        "levels": levels_out,
        "profile": gex_profile(spot, strikes, net, basis),
        "pine": to_pine_string(levels, basis),
    }
