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
    # OR — pas d'indice sous-jacent chez CBOE : la chaîne est celle de l'ETF
    # GLD, dont les strikes sont ramenées à l'échelle du future GC (une part
    # de GLD vaut ~1/10 d'once). "scale_to" indique le symbole dont le prix
    # devient l'échelle de référence ; la basis est alors nulle puisque le
    # spot EST déjà celui du future.
    "GC":  {"chain": "GLD", "future": None, "etf": None, "ychart": "GC=F",
            "scale_to": "GC=F", "bucket": 10.0, "min_oi": 100000},
    # OR COMPTANT — meme chaine d'options, mais ramenee a l'echelle du spot
    # XAUUSD au lieu du future. Le future cote au-dessus du comptant (portage),
    # d'ou un ecart de plusieurs dizaines de dollars : calculer les deux
    # echelles a la source evite de le corriger a la main dans l'indicateur.
    # ychart = GC=F : Yahoo n'a pas de serie pour l'or comptant. L'ancre Open
    # est donc celle du future ; l'API applique l'ecart de portage.
    "XAU": {"chain": "GLD", "future": None, "etf": None, "ychart": "GC=F",
            "open_shift": True,
            "scale_to": "XAUUSD=X|XAU=X|XAUUSD", "derive_from": "GC=F",
            "bucket": 10.0, "min_oi": 100000},
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
    __slots__ = ("K", "is_call", "OI", "gamma", "iv", "dte", "vol", "scale",
                 "bid", "ask")

    def __init__(self, K, is_call, OI, gamma, iv, dte, vol=0.0, scale=1.0,
                 bid=0.0, ask=0.0):
        self.K, self.is_call, self.OI = K, is_call, OI
        self.gamma, self.iv, self.dte = gamma, iv, dte
        self.vol, self.scale = vol, scale
        self.bid, self.ask = bid, ask
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
        bid = _finite_float(o.get("bid"))
        ask = _finite_float(o.get("ask"))
        dte = max((exp - today).days, 0)
        opts.append(Opt(K, m.group(3) == "C", oi, gamma, iv, dte, vol=vol,
                        bid=bid, ask=ask))
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
STRADDLE_EM = 0.8        # facteur EM affiché (validé contre référence externe)


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


# ═══════════════════ FLUX — projection prix x temps (ETAPE 1 : gamma) ═══════
#
# Meme recalcul Black-Scholes que zero_gamma_flip ci-dessus (deja valide en
# production) : c'est ce qui rend le controle de justesse possible. On y
# reprend a l'identique la convention de signe de per_strike_gex (+1 call /
# -1 put) et le T = dte/365 (calendaire) de zero_gamma_flip -- PAS le
# sqrt(252) du module EM, qui sert a une chose differente (convertir une IV
# annuelle en sigma 1 jour pour le dimensionnement de l'EM, sans rapport avec
# le T d'une formule BS). sigma = IV par option, sticky strike : chaque
# strike garde l'IV que CBOE lui a mesuree, inchangee sur toute la grille.
def flow_gamma_matrix(opts, price_grid_idx, hours_grid):
    """Matrice gamma dealer $, agregee sur toute la chaine, pour chaque
    couple (heures depuis maintenant, prix candidat en echelle INDICE --
    meme convention que zero_gamma_flip/per_strike_gex : S_own = S * o.scale
    pour les options ramenees d'une autre chaine, ex. le blend ETF).

    Retourne une liste de lignes (une par element de hours_grid), chaque
    ligne etant une liste de valeurs $ (une par element de price_grid_idx).
    Liste vide si la chaine ne contient aucune option a IV exploitable."""
    valid = [o for o in opts if o.iv > 0]
    if not valid or not price_grid_idx or not hours_grid:
        return []
    K = np.array([o.K for o in valid])
    dte = np.array([o.dte for o in valid], dtype=float)
    iv = np.array([o.iv for o in valid])
    OI = np.array([o.OI for o in valid])
    sign = np.array([1.0 if o.is_call else -1.0 for o in valid])
    scale = np.array([o.scale for o in valid])

    rows = []
    for h in hours_grid:
        # jours calendaires ecoules entre maintenant et ce point de grille :
        # une option 0DTE arrivant a echeance EXACTEMENT a la cloture, sa
        # colonne de cloture retombe sur le meme T~0 que zero_gamma_flip
        # traite deja pour "maintenant" -- limitation connue, pas nouvelle.
        T = (dte - h / 24.0) / 365.0
        col = []
        for S in price_grid_idx:
            S_own = S * scale
            g = bs_gamma(S_own, K, T, iv)
            col.append(float(np.sum(sign * g * OI * CONTRACT_MULT * S_own * S_own * 0.01)))
        rows.append(col)
    return rows


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
    excluded: mixing payout scales is not meaningful).
    Quand la chaîne EST l'ETF (or : GLD ramené à l'once), aucune option n'a
    scale 1.0 — on prend alors toute la chaîne, homogène par construction."""
    prim = [o for o in opts if o.scale == 1.0]
    opts = prim if prim else opts
    if not opts:
        return None
    min_dte = min(o.dte for o in opts)
    sub = [o for o in opts if o.dte == min_dte]
    # strikes ramenées à l'échelle du produit (cf. per_strike_gex)
    ks = np.array(sorted({o.K / (o.scale or 1.0) for o in sub}))
    if not len(ks):
        return None
    K = np.array([o.K / (o.scale or 1.0) for o in sub])
    OI = np.array([o.OI for o in sub])
    is_call = np.array([o.is_call for o in sub])
    pay = np.array([
        np.sum(np.where(is_call, OI * np.maximum(0.0, S - K), OI * np.maximum(0.0, K - S)))
        for S in ks
    ])
    return float(ks[int(np.argmin(pay))])


MAX_QUOTE_SPREAD_PCT = 0.35   # fourchette bid/ask au-dela de laquelle une
                              # cotation est jugee peu fiable (pre-ouverture,
                              # illiquide) et ecartee du calcul d'IV ATM


def _quote_reliable(o):
    """Fourchette bid/ask raisonnable par rapport au mid. Sans cotation
    exploitable (marché fermé, champs absents), on ne filtre PAS — mieux
    vaut une IV moins filtrée qu'aucune IV du tout."""
    if o.bid <= 0 or o.ask <= 0 or o.ask < o.bid:
        return True
    mid = (o.bid + o.ask) / 2.0
    if mid <= 0:
        return True
    return (o.ask - o.bid) / mid <= MAX_QUOTE_SPREAD_PCT


def _expiry_iv(spot, opts, dte):
    """Median IV of the ~8 strikes closest to spot on one expiry (noise-proof).
    La distance est mesurée à l'ÉCHELLE DU PRODUIT (K / scale) : sans cela, une
    chaîne ETF ramenée à une autre échelle — GLD vers l'once d'or — verrait
    tous ses strikes à égale distance du spot, et le tri retiendrait les plus
    éloignés au lieu des ATM, faussant l'IV puis toute la grille sigma.
    Les cotations à fourchette bid/ask anormalement large (pré-ouverture,
    illiquide) sont écartées en priorité — sauf si ça viderait le pool
    entièrement, auquel cas on retombe sur l'ensemble non filtré plutôt que
    de perdre l'IV."""
    cand = [o for o in opts if o.dte == dte and o.iv > 0]
    reliable = [o for o in cand if _quote_reliable(o)]
    pool = reliable if reliable else cand
    sub = sorted(pool, key=lambda o: abs(o.K / (o.scale or 1.0) - spot))
    ivs = sorted(o.iv for o in sub[:8])
    if not ivs:
        return None
    n = len(ivs)
    return ivs[n // 2] if n % 2 else (ivs[n // 2 - 1] + ivs[n // 2]) / 2.0


def atm_iv_detail(spot, opts, now_et=None):
    """1-day ATM implied vol as a BLEND (mean) of the two front expiries'
    median IVs: the 0DTE carries today's exact pricing, the next expiry
    anchors the term structure — averaging them approximates a constant
    1-day maturity (VIX1D spirit) and is far more stable day-to-day than
    either leg alone. Before 13:00 ET the 0DTE is eligible as front; after,
    its IV is a decaying-intraday artefact and the front rolls to dte>=1.
    Returns {iv, mode, front:{dte,iv}, next:{dte,iv}} or None."""
    # On privilégie les options de la chaîne de référence (scale 1.0) pour
    # écarter l'ETF mélangé — mais quand la chaîne EST l'ETF (or : GLD ramené
    # à l'échelle de l'once), aucune option n'a scale 1.0 et ce filtre les
    # supprimait toutes, renvoyant None : plus d'IV, donc plus d'Expected
    # Move et une grille repliée sur l'ATR.
    prim = [o for o in opts if o.scale == 1.0]
    opts = prim if prim else opts
    if now_et is None:
        now_et = dt.datetime.now(ET)
    min_ok = 0 if now_et.hour < 13 else 1
    dtes = sorted({o.dte for o in opts if o.dte >= min_ok and o.iv > 0})
    if not dtes:
        dtes = sorted({o.dte for o in opts if o.iv > 0})
    if not dtes:
        return None
    front_dte = dtes[0]
    next_dte = dtes[1] if len(dtes) > 1 else None
    iv_f = _expiry_iv(spot, opts, front_dte)
    iv_n = _expiry_iv(spot, opts, next_dte) if next_dte is not None else None
    if iv_f is None and iv_n is None:
        return None
    if iv_f is not None and iv_n is not None:
        iv, mode = (iv_f + iv_n) / 2.0, "blend"
    elif iv_f is not None:
        iv, mode = iv_f, "front-only"
    else:
        iv, mode = iv_n, "next-only"
    return {"iv": iv, "mode": mode,
            "front": {"dte": front_dte, "iv": round(iv_f, 4) if iv_f else None},
            "next": {"dte": next_dte, "iv": round(iv_n, 4) if iv_n else None}}


def atm_iv(spot, opts, now_et=None):
    d = atm_iv_detail(spot, opts, now_et=now_et)
    return d["iv"] if d else None


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
def _goldapi_spot(metal="XAU"):
    """Prix comptant via gold-api.com : gratuit, sans cle, sans limite de
    debit, CORS ouvert et bascule interne entre fournisseurs. C'est la source
    la plus adaptee ici — Yahoo ne publie pas de facon fiable les paires
    metaux depuis un datacenter."""
    try:
        import requests
        r = requests.get(f"https://api.gold-api.com/price/{metal}", timeout=8,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; gexdash/1.0)"})
        if r.status_code != 200:
            return None
        d = r.json()
        # le champ de prix a varie selon les versions : on accepte les alias
        for k in ("price", "Price", "value", "rate"):
            v = d.get(k)
            if v:
                px = float(v)
                # garde-fou : une once d'or vaut des milliers de dollars, pas
                # des fractions (certaines API renvoient l'inverse du taux)
                if 100 < px < 100000:
                    return px
                if 0 < px < 0.1:
                    return 1.0 / px
        return None
    except Exception:
        return None


def _stooq_spot(sym):
    """Prix comptant via Stooq (CSV public, sans cle). Sert de source de
    secours quand Yahoo ne publie pas le symbole demande — c'est le cas de
    certaines paires metaux selon les regions."""
    try:
        import requests
        r = requests.get("https://stooq.com/q/l/",
                         params={"s": sym.lower(), "f": "sd2t2ohlcv", "h": "", "e": "csv"},
                         timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        lines = [l for l in r.text.strip().splitlines() if l]
        if len(lines) < 2:
            return None
        cols = lines[0].split(",")
        vals = lines[1].split(",")
        row = dict(zip(cols, vals))
        px = float(row.get("Close") or row.get("close") or 0)
        return px if px > 0 else None
    except Exception:
        return None


def yahoo_spot(sym):
    """Dernier prix Yahoo pour un symbole. Accepte plusieurs symboles separes
    par « | » et retourne le premier qui repond : les cotations de l'or
    comptant ne portent pas le meme code partout, et un symbole muet bloquait
    toute la publication du marche."""
    # Or : source dediee EN PREMIER — gratuite, sans limite, et concue pour
    # ca. Interroger Yahoo d'abord ne ferait qu'ajouter trois appels perdus.
    if "XAU" in str(sym).upper():
        px = _goldapi_spot("XAU")
        if px:
            return px

    for s in str(sym).split("|"):
        s = s.strip()
        if not s:
            continue
        try:
            import requests
            r = requests.get(YAHOO_URL.format(sym=s), timeout=15,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            px = r.json()["chart"]["result"][0]["meta"].get("regularMarketPrice")
            if px and float(px) > 0:
                return float(px)
        except Exception:
            continue
    # Yahoo muet sur tous les codes : on tente Stooq, dont la nomenclature
    # differe (xauusd au lieu de XAUUSD=X)
    for s in str(sym).split("|"):
        alt = s.strip().replace("=X", "").replace("=F", "").lower()
        if not alt:
            continue
        px = _stooq_spot(alt)
        if px:
            return px
    return None


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
def discord_send(url, payload, note=None,
                 dashboard_url="https://gexdash.wealthbuilders.group"):
    """Post ONE payload's embed to a SPECIFIC webhook URL — no routing, no
    fallback. Used by the admin per-row Test button. Never raises."""
    try:
        import requests
        body = {"embeds": [_discord_embed(payload, dashboard_url)]}
        if note:
            body["content"] = note
        r = requests.post(url, json=body, timeout=10)
        return r.status_code in (200, 204)
    except Exception:
        return False


def discord_news(text):
    """Short plain message to the 'news' webhook (refresh announcements).
    No-op when unset. Never raises."""
    url = fetch_webhooks().get("news")
    if not url:
        return False
    try:
        import requests
        r = requests.post(url, json={"content": text}, timeout=10)
        return r.status_code in (200, 204)
    except Exception:
        return False


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


def open_anchor(cfg, spot):
    """Ouverture du jour a l'echelle du produit affiche.

    Certains produits n'ont pas de serie propre chez Yahoo (l'or comptant
    renvoie 404) : on lit alors celle du future et on retire l'ecart de
    portage. Sans ce recalage, la grille Open et l'Expected Move seraient
    decales de plusieurs dizaines de dollars."""
    d_open, atr = daily_bars(cfg["ychart"])
    if cfg.get("open_shift") and d_open and spot:
        # L'ecart de portage est MEMORISE pour la seance. Le recalculer a
        # chaque passage faisait deriver l'ancre du comptant d'un tir a
        # l'autre (GC et XAU bougent chacun de leur cote) : une ouverture est
        # un point FIXE, elle ne doit pas osciller au fil de la nuit.
        day = et_today().isoformat()
        key = f"gex:goldoff:{day}"
        off = None
        try:
            v = kv_get(key)
            off = float(v) if v is not None else None
        except Exception:
            off = None
        if off is None:
            ref = yahoo_spot(cfg["ychart"])
            if ref and ref > 0:
                off = ref - spot
                try:
                    kv_set(key, str(round(off, 3)), ex=3 * 86400)
                except Exception:
                    pass
        if off is not None:
            d_open = d_open - off
    return d_open, atr


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


def _strike_profile(strikes, net, spot, basis, window=0.04, cap=120):
    rows = [(float(k) + basis, float(g) / 1e9)
            for k, g in zip(strikes, net)
            if abs(float(k) - spot) <= spot * window and abs(float(g)) > 0]
    if len(rows) > cap:
        rows = sorted(rows, key=lambda r: -abs(r[1]))[:cap]
    rows.sort()
    return [[round(k, 1), round(g, 3)] for k, g in rows]


def build_pine(rows, grid):
    """Pine string from target-scale rows [(price, label, kind)] + open grid."""
    pine_rows = list(rows)
    if grid:
        suf = {"iv": "σ", "atr": " ATR"}.get(grid["mode"], "%")
        pine_rows.append((grid["anchor"], "Daily O", "opo"))
        for g in grid["levels"]:
            pine_rows.append((g["up"], f"+{g['mult']:g}{suf}", "opu"))
            pine_rows.append((g["down"], f"-{g['mult']:g}{suf}", "opd"))
    return ";".join(f"{p:.1f},{l},{k}" for p, l, k in sorted(pine_rows, key=lambda x: -x[0]))


def refresh_daily_anchor(payload):
    """Recalage nocturne de l'open Globex : recalcule UNIQUEMENT la partie
    daily (Daily Open + grille sigma, avec l'IV DEJA stockée du run de 15h25).
    Gamma, EM, straddle, IV, prix : intouchés — il n'existe aucune information
    options nouvelle pendant la nuit. Retourne True seulement si l'ancre a
    réellement bougé (Yahoo a créé la bougie de la nouvelle séance)."""
    cfg = TARGETS[payload["target"]]
    # Le prix de reference vient du payload : cette fonction tourne la nuit,
    # sans recalcul de chaine, donc il n'y a pas de variable `spot` locale.
    # (Regression introduite en branchant open_anchor ici — le nom existait
    # dans build_payload, pas ici.)
    ref_spot = payload.get("nq_price")
    d_open, atr14 = open_anchor(cfg, ref_spot)
    if d_open is None:
        return False
    grid = open_grid(d_open, iv=payload.get("iv_atm"), atr=atr14)
    if grid is None:
        return False
    old_anchor = (payload.get("open_grid") or {}).get("anchor")
    if old_anchor is not None and abs(grid["anchor"] - old_anchor) < 0.6:
        return False
    payload["open_grid"] = grid
    payload["daily_refresh_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()

    # EM DAILY recalé sur le MÊME open que la grille. Il ne dépend que de
    # (ancre, IV) — deux valeurs déjà rafraîchies ici — donc aucune donnée
    # options nouvelle n'est requise. Sans ça, les bandes EM resteraient
    # centrées sur l'open de la VEILLE pendant toute la session suivante.
    iv = payload.get("iv_atm")
    basis = payload.get("basis") or 0.0
    if iv and float(iv) > 0:
        anchor = grid["anchor"]
        anchor_idx = anchor - basis
        size = STRADDLE_EM * anchor_idx * float(iv) / math.sqrt(252)
        em = dict(payload.get("expected_move") or {})
        em.update({
            "straddle": round(size, 2),
            "em_pct": round(100.0 * size / anchor_idx, 3),
            "anchor_idx": round(anchor_idx, 2),
            "anchor": round(anchor, 1),
            "sigma1d": round(anchor_idx * float(iv) / math.sqrt(252), 2),
            "source": "0.8σ daily", "quality": "model",
            "daily_refresh": True,
        })
        # le straddle de marché date de la clôture précédente : il ne décrit
        # plus la séance qui commence, on ne le propage pas
        em.pop("market_straddle", None)
        em.pop("market_quality", None)
        for b in (em.get("bands") or []):
            b["high"] = round(anchor + size * b["pct"] / 100, 1)
            b["low"] = round(anchor - size * b["pct"] / 100, 1)
        payload["expected_move"] = em

        # niveaux tracés : EM High/Low + bandes fractionnaires
        for L in payload.get("levels", []):
            k = L.get("kind")
            if k == "emh":
                L["price_nq"] = round(anchor + size, 1)
            elif k == "eml":
                L["price_nq"] = round(anchor - size, 1)
            elif k == "emb":
                m = re.match(r"EM ([+-])(\d+)%", L.get("label", ""))
                if m:
                    d = size * int(m.group(2)) / 100.0
                    L["price_nq"] = round(anchor + (d if m.group(1) == "+" else -d), 1)

    rows = [(L["price_nq"], L["label"], L["kind"]) for L in payload.get("levels", [])]
    payload["pine"] = build_pine(rows, grid)
    return True


def build_payload(target="NQ", n_expiries=10, top_n=4, basis_override=None,
                  mode="snapshot", em_bands=(0.5, 1.5), chain_cache=None,
                  iv_override=None, capture=None):
    """Full pipeline: fetch -> compute -> JSON-ready payload dict.
    Raises on fetch/parse failure; caller handles errors.
    n_expiries=10 by default: wide enough that main walls approach the
    aggregate (MenthorQ-style) view while 0DTE walls give the intraday one.

    capture: dict optionnel, rempli en sortie avec les objets internes
    (opts post-blend/scale, spot, basis, net_total_bn) dont le module Flux a
    besoin pour recalculer les grecques sur la MEME chaine sans refaire le
    fetch+blend ETF ni en dupliquer la logique ailleurs. No-op si None :
    n'affecte aucun appelant existant."""
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

    # Remise à l'échelle : quand la chaîne n'est pas celle du produit tracé
    # (or : chaîne GLD, produit GC), on prend le spot du produit comme
    # référence et on marque chaque option de son facteur d'échelle. Le
    # dollar-gamma reste calculé avec le spot propre à l'option ; seules les
    # strikes sont ramenées, exactement comme pour le mélange QQQ/NDX.
    if cfg.get("scale_to"):
        tgt_spot = yahoo_spot(cfg["scale_to"])
        # DERNIER RECOURS : deduire le comptant du future moins un ecart saisi
        # dans l'admin. Les sources gratuites de prix or comptant sont peu
        # fiables depuis un datacenter ; l'ecart de portage, lui, evolue
        # lentement — une valeur revue de temps en temps suffit et ne depend
        # de personne.
        if (not tgt_spot or tgt_spot <= 0) and cfg.get("derive_from"):
            base = yahoo_spot(cfg["derive_from"])
            try:
                off = float(json.loads(kv_get("gex:goldbasis") or "null"))
            except Exception:
                off = None
            if base and off is not None:
                tgt_spot = base - off
        if not tgt_spot or tgt_spot <= 0:
            raise ValueError(
                f"prix {cfg['scale_to']} indisponible — renseigne l'écart "
                f"GC − XAUUSD dans l'admin pour publier quand même")
        sc = spot / tgt_spot
        for o in opts:
            o.scale = sc
        spot = tgt_spot

    # garde-fous : refuser une chaîne dégénérée plutôt que publier du bruit
    idx_oi = sum(o.OI for o in opts)
    if len(opts) < 50 or idx_oi < cfg.get("min_oi", 1000):
        raise ValueError(
            f"index chain too thin ({len(opts)} opts, OI {idx_oi:.0f}) — refusing")
    sources = [{"chain": symbol, "opts": len(opts), "oi": round(idx_oi)}]
    if cfg.get("scale_to") and opts:
        # indispensable : /api/quote et /api/chart lisent ce facteur pour
        # convertir le prix et les bougies de l'ETF vers l'échelle du produit
        sources[0]["scale"] = round(opts[0].scale, 6)

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

    bucket = cfg.get("bucket") or (10.0 if spot >= 10000 else 5.0)
    strikes, net = per_strike_gex(spot, opts, bucket=bucket)
    flip = zero_gamma_flip(opts, spot * 0.92, spot * 1.08)
    if flip is None:  # régime très déséquilibré : élargir avant d'abandonner
        flip = zero_gamma_flip(opts, spot * 0.85, spot * 1.15)
    iv_detail = atm_iv_detail(spot, opts)
    iv = float(iv_override) if iv_override else (iv_detail["iv"] if iv_detail else None)

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
    d_open, atr14 = open_anchor(cfg, spot)

    # EM DAILY : straddle théorique plein-jour = 0.8 x sigma implicite,
    # ancré au Daily Open — stable toute la séance (le straddle de marché,
    # lui, mesure le move RESTANT et fond au fil de la journée : il est
    # conservé en information secondaire).
    market = atm_straddle(data, spot, today=today)
    em = None
    if iv is not None:
        # Ancré ET dimensionné sur le Daily Open (et non sur le spot courant) :
        # l'EM vaut alors exactement 0.8 x l'unité sigma de la grille Open, et
        # il ne dépend plus de l'INSTANT du refresh — seule l'IV le fait varier.
        anchor_idx = (d_open - basis) if d_open is not None else spot
        size = STRADDLE_EM * anchor_idx * iv / math.sqrt(252)
        em = {"straddle": round(size, 2),
              "em_pct": round(100.0 * size / anchor_idx, 3),
              "anchor_idx": round(anchor_idx, 2),
              "anchor": round(anchor_idx + basis, 1),
              "sigma1d": round(anchor_idx * iv / math.sqrt(252), 2),
              "iv_source": "override" if iv_override else
                            (iv_detail["mode"] if iv_detail else "none"),
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
        _a = em.get("anchor_idx", spot)
        for b in bands_meta:
            b["high"] = round(_a + em["straddle"] * b["pct"] / 100 + basis, 1)
            b["low"] = round(_a - em["straddle"] * b["pct"] / 100 + basis, 1)
        em["bands"] = bands_meta
    grid = open_grid(d_open, iv=iv, atr=atr14)

    pine = build_pine([(p + basis, l, k) for p, l, k in levels], grid)

    if capture is not None:
        capture["opts"] = opts
        capture["spot"] = spot
        capture["basis"] = basis
        capture["net_total_bn"] = net_total_bn

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
        # profil par strike (échelle cible, $Bn) : fenêtre ±4% du spot ;
        # si trop dense, on garde les 120 plus fortes expositions (jamais
        # une troncature aveugle par prix), puis re-tri par strike
        "gex_by_strike": _strike_profile(strikes, net, spot, basis),
        "regime": "positive" if net_total_bn > 0 else "negative",
        "pc_oi": pc_oi,
        "iv_atm": round(iv, 4) if iv is not None else None,
        "iv_diag": ({**iv_detail, "iv": round(iv_detail["iv"], 4),
                     "override": round(float(iv_override), 4) if iv_override else None}
                    if iv_detail else None),
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
