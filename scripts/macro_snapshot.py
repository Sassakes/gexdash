#!/usr/bin/env python3
"""Generate macro.json for the demo's Macro Synthesis banner.

FRED is blocked from Vercel's edge but reachable from GitHub Actions runners (and
locally), so we compute the macro snapshot HERE and commit a static macro.json the
frontend reads. Primary source = FRED (free, unlimited, gives true Core CPI);
fallback = Alpha Vantage (ALPHAVANTAGE_API_KEY, headline CPI, ~25 req/day / 1 req/s).
"""

import datetime as dt
import json
import os
import sys
import time

import requests
from concurrent.futures import ThreadPoolExecutor

# Ce script est AUTONOME : la logique FRED est intégrée ici plutôt qu'importée
# depuis api/news.py, car le serveur gexdash regroupe tout dans api/gex.py.
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; gexdash/1.0)"}
_CACHE = {}

def _fred_raw(series):
    for _ in range(2):                       # one retry — FRED can be slow from the edge
        try:
            r = requests.get(FRED.format(series), headers=UA, timeout=14)
            out = []
            for line in r.text.splitlines()[1:]:
                p = line.split(",")
                if len(p) >= 2 and p[1] not in (".", ""):
                    try:
                        out.append(float(p[1]))
                    except ValueError:
                        pass
            if out:
                return out
        except Exception:
            pass
    return []


def _fred(series):
    """6h cache, but only cache SUCCESSFUL (non-empty) fetches so a transient
    FRED timeout doesn't pin an empty result for hours."""
    now = time.time()
    hit = _CACHE.get(f"fred:{series}")
    if hit and hit[0] and now - hit[1] < 6 * 3600:
        return hit[0]
    data = _fred_raw(series)
    if data:
        _CACHE[f"fred:{series}"] = (data, now)
    return data


def _trend(a, b, thr):
    if a is None or b is None:
        return "stable"
    if a > b + thr:
        return "rising"
    if a < b - thr:
        return "falling"
    return "stable"


def _scenario(gdp, cpi, unemp, fed, curve, tr):
    scen = {
        "goldilocks":  ("bullish", [gdp >= 1.5, cpi < 3.5, tr["cpi"] != "rising", unemp < 4.5, tr["fed"] != "rising", curve > 0]),
        "overheating": ("bearish", [gdp > 3.0, cpi >= 3.0, tr["cpi"] == "rising", tr["fed"] == "rising", unemp < 4.0]),
        "stagflation": ("bearish", [gdp < 1.5, cpi >= 3.5, tr["unemp"] == "rising"]),
        "recession":   ("bearish", [gdp < 1.0 or tr["gdp"] == "falling", tr["unemp"] == "rising", curve < 0.2, tr["fed"] != "rising"]),
        "reflation":   ("bullish", [tr["gdp"] == "rising", gdp >= 1.0, cpi < 4.0, tr["fed"] == "falling"]),
    }
    scored = []
    for name, (bias, cond) in scen.items():
        met = sum(1 for c in cond if c)
        scored.append((name, bias, met / len(cond), met, len(cond)))
    scored.sort(key=lambda x: -x[2])
    win = scored[0]
    second = scored[1][2] if len(scored) > 1 else 0
    return {"scenario": win[0], "bias": win[1],
            "conditionsMet": round(win[2] * 100),
            "confidence": round(min(95, 55 + 100 * (win[2] - second)))}


def fred_macro():
    ser = ["A191RL1Q225SBEA", "UNRATE", "FEDFUNDS", "T10Y2Y", "CPILFESL"]
    with ThreadPoolExecutor(max_workers=5) as ex:   # parallel so 5 FRED reads fit the budget
        res = dict(zip(ser, ex.map(_fred, ser)))
    gdp, unemp, fed, curve, cpiix = (res[s] for s in ser)

    gdp_v   = gdp[-1] if gdp else None
    unemp_v = unemp[-1] if unemp else None
    fed_v   = fed[-1] if fed else None
    curve_v = curve[-1] if curve else None
    cpi_yoy  = (cpiix[-1] / cpiix[-13] - 1) * 100 if len(cpiix) >= 13 else None
    cpi_prev = (cpiix[-2] / cpiix[-14] - 1) * 100 if len(cpiix) >= 14 else None

    tr = {
        "gdp":   _trend(gdp[-1], gdp[-2], 0.2) if len(gdp) >= 2 else "stable",
        "cpi":   _trend(cpi_yoy, cpi_prev, 0.1),
        "unemp": _trend(unemp[-1], unemp[-2], 0.1) if len(unemp) >= 2 else "stable",
        "fed":   _trend(fed[-1], fed[-2], 0.06) if len(fed) >= 2 else "stable",
        "curve": _trend(curve[-1], curve[-21], 0.05) if len(curve) >= 21 else "stable",
    }
    metrics = [
        {"key": "gdp",   "value": gdp_v,   "trend": tr["gdp"]},
        {"key": "cpi",   "value": round(cpi_yoy, 1) if cpi_yoy is not None else None, "trend": tr["cpi"]},
        {"key": "unemp", "value": unemp_v, "trend": tr["unemp"]},
        {"key": "fed",   "value": fed_v,   "trend": tr["fed"]},
        {"key": "curve", "value": curve_v, "trend": tr["curve"]},
    ]
    # don't fabricate a scenario when the data source is unreachable
    if sum(1 for v in (gdp_v, cpi_yoy, unemp_v, fed_v, curve_v) if v is not None) < 3:
        return {"metrics": metrics, "scenario": None, "bias": "neutral",
                "conditionsMet": 0, "confidence": 0, "insufficient": True}
    sc = _scenario(gdp_v or 0, cpi_yoy or 0, unemp_v or 99, fed_v or 0, curve_v or 0, tr)
    return {"metrics": metrics, **sc}




AV = os.environ.get("ALPHAVANTAGE_API_KEY", "")


def _av_series(fn, **kw):
    time.sleep(1.4)                       # free tier ~1 req/sec
    p = {"function": fn, "apikey": AV, **kw}
    d = requests.get("https://www.alphavantage.co/query", params=p, timeout=25).json().get("data", [])
    out = []
    for x in reversed(d):                 # oldest -> newest
        try:
            out.append(float(x["value"]))
        except (KeyError, ValueError, TypeError):
            pass
    return out


def av_macro():
    gdp = _av_series("REAL_GDP", interval="quarterly")
    cpi = _av_series("CPI", interval="monthly")
    unemp = _av_series("UNEMPLOYMENT")
    fed = _av_series("FEDERAL_FUNDS_RATE", interval="monthly")
    y10 = _av_series("TREASURY_YIELD", interval="daily", maturity="10year")
    y2 = _av_series("TREASURY_YIELD", interval="daily", maturity="2year")

    # AV GDP levels are noisy QoQ -> use YoY (4 quarters), which matches FRED's rate
    gdp_g = (gdp[-1] / gdp[-5] - 1) * 100 if len(gdp) >= 5 else None
    gdp_gp = (gdp[-2] / gdp[-6] - 1) * 100 if len(gdp) >= 6 else None
    cpi_y = (cpi[-1] / cpi[-13] - 1) * 100 if len(cpi) >= 13 else None
    cpi_p = (cpi[-2] / cpi[-14] - 1) * 100 if len(cpi) >= 14 else None
    curve = (y10[-1] - y2[-1]) if (y10 and y2) else None
    curve_p = (y10[-21] - y2[-21]) if (len(y10) >= 21 and len(y2) >= 21) else None

    tr = {
        "gdp": _trend(gdp_g, gdp_gp, 0.2),
        "cpi": _trend(cpi_y, cpi_p, 0.1),
        "unemp": _trend(unemp[-1], unemp[-2], 0.1) if len(unemp) >= 2 else "stable",
        "fed": _trend(fed[-1], fed[-2], 0.06) if len(fed) >= 2 else "stable",
        "curve": _trend(curve, curve_p, 0.05),
    }
    metrics = [
        {"key": "gdp",   "value": round(gdp_g, 1) if gdp_g is not None else None, "trend": tr["gdp"]},
        {"key": "cpi",   "value": round(cpi_y, 1) if cpi_y is not None else None, "trend": tr["cpi"], "headline": True},
        {"key": "unemp", "value": unemp[-1] if unemp else None, "trend": tr["unemp"]},
        {"key": "fed",   "value": fed[-1] if fed else None, "trend": tr["fed"]},
        {"key": "curve", "value": round(curve, 2) if curve is not None else None, "trend": tr["curve"]},
    ]
    if sum(1 for m in metrics if m["value"] is not None) < 3:
        return None
    sc = _scenario(gdp_g or 0, cpi_y or 0, unemp[-1] if unemp else 99, fed[-1] if fed else 0, curve or 0, tr)
    return {"metrics": metrics, **sc, "source": "alphavantage"}


def main():
    data = fred_macro()                   # FRED (Core CPI)
    src = "fred"
    if data.get("insufficient"):
        av = av_macro() if AV else None
        if av:
            data, src = av, "alphavantage"
    data.setdefault("source", src)
    data["generated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open("macro.json", "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print("wrote macro.json:", data.get("scenario"), "| source:", data.get("source"),
          "| conditions:", data.get("conditionsMet"))


if __name__ == "__main__":
    main()


# ─────────────────────────────────────────────────────────────────────────────
# Calendrier economique : la source refuse les IP de datacenter (Vercel), mais
# repond aux runners GitHub. On l'enregistre donc ici, comme macro.json.
# ─────────────────────────────────────────────────────────────────────────────
def save_calendar():
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36",
          "Accept": "application/json,text/plain,*/*"}
    try:
        r = requests.get(url, headers=ua, timeout=25)
        data = r.json() if r.status_code == 200 else None
        if isinstance(data, list) and data:
            with open("calendar.json", "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            print(f"calendar.json : {len(data)} evenements")
            return True
        print(f"calendrier indisponible (HTTP {r.status_code})")
    except Exception as exc:
        print(f"calendrier : echec ({exc})")
    return False


try:
    save_calendar()
except Exception as exc:      # ne doit jamais faire echouer le job macro
    print(f"calendrier ignore : {exc}")
