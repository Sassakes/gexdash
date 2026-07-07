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


def extract_levels(spot, strikes, net, flip, em=None, top_n=4):
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


def build_payload(symbol="_NDX", n_expiries=4, top_n=4, basis_override=None, mode="snapshot"):
    """Full pipeline: fetch -> compute -> JSON-ready payload dict.
    Raises on fetch/parse failure; caller handles errors."""
    today = et_today()
    data = fetch_cboe(symbol)
    spot, opts, exps = parse_chain(data, n_expiries, today=today)
    if not opts:
        raise ValueError("no options parsed from CBOE chain")
    strikes, net = per_strike_gex(spot, opts)
    flip = zero_gamma_flip(opts, spot * 0.92, spot * 1.08)
    em = atm_straddle(data, spot, today=today)
    levels = extract_levels(spot, strikes, net, flip, em=em, top_n=top_n)
    basis, nq_price, basis_source = nq_basis(spot, override=basis_override)
    net_total_bn = float(net.sum()) / 1e9

    levels_out = [
        {"price_nq": round(p + basis, 1), "label": l, "kind": k}
        for p, l, k in sorted(levels, key=lambda x: -x[0])
    ]
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
        "expected_move": em,
        "expiries": [str(e) for e in exps],
        "levels": levels_out,
        "profile": gex_profile(spot, strikes, net, basis),
        "pine": to_pine_string(levels, basis),
    }
