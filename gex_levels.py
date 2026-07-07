#!/usr/bin/env python3
"""
NQ GEX Levels — daily snapshot CLI (GitHub Actions / local).

The computation lives in api/_gex_core.py (shared with the Vercel serverless
function /api/gex used by the dashboard's live-refresh button). This wrapper
adds: file outputs (nq_levels.txt/json), rolling history.json, output
validation, optional Supabase push, and an offline self-test.

Run live:   python gex_levels.py --symbol _NDX --n-expiries 4 --out .
Self-test:  python gex_levels.py --selftest
"""

import argparse
import datetime as dt
import json
import math
import sys

from api._gex_core import (
    Opt,
    atm_straddle,
    bs_gamma,
    build_payload,
    et_today,
    extract_levels,
    per_strike_gex,
    to_pine_string,
    zero_gamma_flip,
)

HISTORY_MAX = 120  # ~6 mois de séances


def validate_output(nq_price, levels_out):
    """Guard-rail: refuse to write/push obviously broken levels.
    Walls are mandatory; the gamma flip is OPTIONAL (no zero-crossing exists
    in a deeply positive-gamma regime and that is a legitimate state)."""
    kinds = {L["kind"]: L["price_nq"] for L in levels_out}
    missing = [k for k in ("res", "sup") if k not in kinds]
    if missing:
        sys.exit(f"[error] missing required level kind(s) {missing}; refusing to write/push")
    if "flip" not in kinds:
        print("[warn] no gamma flip in range (positive-gamma regime?)", file=sys.stderr)

    if not nq_price or not math.isfinite(nq_price):
        sys.exit(f"[error] nq_price is invalid ({nq_price}); refusing to write/push")

    for kind, price in kinds.items():
        if kind not in ("res", "sup", "flip"):
            continue
        if not math.isfinite(price) or abs(price - nq_price) / nq_price > 0.20:
            sys.exit(
                f"[error] {kind}={price} is implausible vs nq_price={nq_price}; "
                "refusing to write/push"
            )


def append_history(out_dir, record):
    """Append/replace today's record in history.json (keyed by date)."""
    path = f"{out_dir}/history.json"
    try:
        with open(path) as f:
            hist = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        hist = []
    hist = [h for h in hist if h.get("date") != record["date"]]
    hist.append(record)
    hist.sort(key=lambda h: h["date"])
    hist = hist[-HISTORY_MAX:]
    with open(path, "w") as f:
        json.dump(hist, f, indent=1)
    return len(hist)


def push_supabase(date_str, nq_price, levels_out):
    """Upsert today's key levels into the Supabase `gex_levels` table.
    Requires env SUPABASE_URL and SUPABASE_SERVICE_KEY (set as GitHub secrets).
    NOTE: `Prefer: merge-duplicates` only deduplicates if the table has a
    UNIQUE constraint on `date` — create it, otherwise rows pile up."""
    import os

    import requests

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("[warn] SUPABASE_URL/SUPABASE_SERVICE_KEY not set; skipping push", file=sys.stderr)
        return

    def find(kind):
        for L in levels_out:
            if L["kind"] == kind:
                return L["price_nq"]
        return None

    row = {
        "date": date_str,
        "nq_price": nq_price,
        "call_wall": find("res"),
        "put_wall": find("sup"),
        "gamma_flip": find("flip"),
        "levels": levels_out,
    }
    r = requests.post(
        f"{url.rstrip('/')}/rest/v1/gex_levels",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        json=row,
        timeout=30,
    )
    r.raise_for_status()
    print("[ok] pushed levels to Supabase")


def run_live(args):
    payload = build_payload(
        symbol=args.symbol,
        n_expiries=args.n_expiries,
        top_n=args.top_n,
        basis_override=args.basis,
        mode="snapshot",
    )
    levels_out = payload["levels"]
    nq_price = payload["nq_price"]
    em = payload["expected_move"]

    print(
        f"# {payload['symbol']} spot={payload['index_spot']:.1f}  NQ={nq_price}  "
        f"basis={payload['basis']:+.1f} ({payload['basis_source']})"
    )
    print(f"# expiries: {', '.join(payload['expiries'])}")
    print(f"# net GEX total: {payload['net_gex_bn']:+.2f} $Bn/1%  regime={payload['regime']}")
    if em:
        print(
            f"# EM (straddle {em['expiry']} K={em['strike']:.0f}): "
            f"+/-{em['straddle']:.1f} pts ({em['em_pct']:.2f}%)"
        )
    for L in levels_out:
        print(f"  {L['price_nq']:>9.1f}  {L['label']:<10} ({L['kind']})")
    print("\nPINE:\n" + payload["pine"])

    validate_output(
        nq_price if nq_price is not None else payload["index_spot"] + payload["basis"],
        levels_out,
    )

    out = args.out.rstrip("/")
    with open(f"{out}/nq_levels.txt", "w") as f:
        f.write(payload["pine"] + "\n")
    with open(f"{out}/nq_levels.json", "w") as f:
        json.dump(payload, f, indent=2)

    def find(kind):
        for L in levels_out:
            if L["kind"] == kind:
                return L["price_nq"]
        return None

    n_hist = append_history(
        out,
        {
            "date": payload["date"],
            "nq_price": nq_price,
            "call_wall": find("res"),
            "put_wall": find("sup"),
            "gamma_flip": find("flip"),
            "em_high": find("emh"),
            "em_low": find("eml"),
            "net_gex_bn": payload["net_gex_bn"],
            "basis": payload["basis"],
        },
    )
    print(f"\nWrote {out}/nq_levels.txt, nq_levels.json, history.json ({n_hist} days)")

    if args.push_supabase:
        push_supabase(payload["date"], nq_price, levels_out)


# --------------------------------------------------------------------------- #
# Self-test (no network)                                                       #
# --------------------------------------------------------------------------- #
def selftest():
    """Synthetic chain: heavy call OI at 22000, heavy put OI at 21000, spot 21500.
    Expect call wall ~22000, put wall ~21000, flip between them, EM from straddle."""
    spot = 21500.0
    opts = []
    raw = {"options": [], "current_price": spot}
    today = et_today()
    exp = today + dt.timedelta(days=7)
    yymmdd = exp.strftime("%y%m%d")
    for K in range(20500, 22600, 100):
        for is_call in (True, False):
            oi = 100.0
            if is_call and K == 22000:
                oi = 5000.0
            if (not is_call) and K == 21000:
                oi = 5000.0
            g = float(bs_gamma(spot, K, 7 / 365, 0.16))
            opts.append(Opt(float(K), is_call, oi, g, 0.16, 7))
            raw["options"].append(
                {
                    "option": f"_NDX{yymmdd}{'C' if is_call else 'P'}{K * 1000:08d}",
                    "bid": 99.0,
                    "ask": 101.0,
                    "open_interest": oi,
                }
            )

    strikes, net = per_strike_gex(spot, opts)
    flip = zero_gamma_flip(opts, spot * 0.92, spot * 1.08)
    em = atm_straddle(raw, spot, today=today)
    levels = extract_levels(spot, strikes, net, flip, em=em, top_n=3)

    cw = next(p for p, t, k in levels if k == "res")
    pw = next(p for p, t, k in levels if k == "sup")
    print(f"spot={spot}  call_wall={cw}  put_wall={pw}  flip={flip:.1f}  em={em}")
    assert cw == 22000.0, f"call wall expected 22000, got {cw}"
    assert pw == 21000.0, f"put wall expected 21000, got {pw}"
    assert 21000 < flip < 22000, f"flip out of range: {flip}"
    assert em is not None and em["strike"] == 21500.0, f"bad ATM strike: {em}"
    assert abs(em["straddle"] - 200.0) < 1e-6, f"bad straddle: {em}"
    emh = next(p for p, t, k in levels if k == "emh")
    eml = next(p for p, t, k in levels if k == "eml")
    assert emh == spot + 200 and eml == spot - 200

    pine = to_pine_string(levels, basis=12.5)  # fake basis
    print("PINE:", pine)
    assert pine.count(";") == len(levels) - 1
    assert all(len(r.split(",")) == 3 for r in pine.split(";"))
    print("OK — self-test passed.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="_NDX", help="_NDX (index) or QQQ")
    ap.add_argument("--n-expiries", type=int, default=4)
    ap.add_argument("--top-n", type=int, default=4, help="extra gamma strikes")
    ap.add_argument("--basis", type=float, default=None,
                    help="manual NQ-NDX basis override (skips Yahoo)")
    ap.add_argument("--out", default=".")
    ap.add_argument("--push-supabase", action="store_true", help="upsert levels into Supabase")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
    else:
        run_live(args)


if __name__ == "__main__":
    main()
