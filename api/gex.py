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
import json
import os
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from api._gex_core import build_payload, discord_notify, et_today

ROOT = Path(__file__).resolve().parent.parent

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/history.json": ("history.json", "application/json"),
    "/nq_levels.txt": ("nq_levels.txt", "text/plain; charset=utf-8"),
}

UPSTASH_KEY = "gex:latest"


def _upstash_conf():
    """Accept both naming schemes: direct Upstash vars and Vercel
    Marketplace/KV aliases (KV_REST_API_*)."""
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
    return (url.rstrip("/"), token) if url and token else (None, None)


def _upstash_get():
    """Latest published payload from Redis, or None (never raises)."""
    url, token = _upstash_conf()
    if not url:
        return None
    try:
        import requests

        r = requests.get(f"{url}/get/{UPSTASH_KEY}",
                         headers={"Authorization": f"Bearer {token}"}, timeout=5)
        r.raise_for_status()
        v = r.json().get("result")
        return json.loads(v) if v else None
    except Exception:
        traceback.print_exc()
        return None


def _upstash_set(payload):
    """Publish payload to Redis. Returns (ok, reason) — never raises."""
    url, token = _upstash_conf()
    if not url:
        return False, "no-credentials (variables KV_/UPSTASH_ absentes du déploiement)"
    try:
        import requests

        r = requests.post(f"{url}/set/{UPSTASH_KEY}",
                          headers={"Authorization": f"Bearer {token}"},
                          data=json.dumps(payload), timeout=5)
        r.raise_for_status()
        return True, "ok"
    except Exception as e:
        traceback.print_exc()
        return False, f"{type(e).__name__}: {e}"


def _load_file_payload():
    p = ROOT / "nq_levels.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _latest_payload():
    """Newest of: committed daily snapshot vs last published live refresh.
    ISO timestamps compare correctly as strings."""
    file_p = _load_file_payload()
    up_p = _upstash_get()
    if file_p and up_p:
        return up_p if up_p.get("generated_utc", "") >= file_p.get("generated_utc", "") else file_p
    return up_p or file_p


class handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        # ---- official levels: newest of committed snapshot vs published refresh ----
        if path == "/nq_levels.json":
            payload = _latest_payload()
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

        # ---- CRON fallback: compute+publish only if today's snapshot is missing ----
        if path == "/api/cron":
            qs = parse_qs(parsed.query)
            cron_secret = os.environ.get("CRON_SECRET")
            gex_key = os.environ.get("GEX_REFRESH_KEY")
            auth = self.headers.get("Authorization", "")
            given_key = self.headers.get("x-gex-key") or (qs.get("key", [None])[0] or "")
            ok_cron = cron_secret and hmac.compare_digest(auth, f"Bearer {cron_secret}")
            ok_key = gex_key and hmac.compare_digest(given_key, gex_key)
            if (cron_secret or gex_key) and not (ok_cron or ok_key):
                self._send(401, json.dumps({"error": "unauthorized"}).encode(),
                           "application/json")
                return
            try:
                today = et_today().isoformat()
                latest = _latest_payload()
                fresh = (latest is not None
                         and latest.get("date") == today
                         and latest.get("generated_utc", "") >= f"{today}T11:30:00")
                if fresh and "force" not in qs:
                    self._send(200, json.dumps({
                        "skipped": True,
                        "reason": "snapshot du jour déjà présent",
                        "latest_generated_utc": latest.get("generated_utc"),
                    }).encode(), "application/json")
                    return
                payload = build_payload(mode="snapshot")
                ok, why = _upstash_set(payload)
                notified = discord_notify(payload) if ok else False
                self._send(200, json.dumps({
                    "skipped": False, "published": ok, "publish_info": why,
                    "discord": notified, "date": payload["date"],
                    "generated_utc": payload["generated_utc"],
                }).encode(), "application/json")
            except Exception as e:
                traceback.print_exc()
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
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
                n = max(1, min(int(q("n", 10)), 16))
                symbol = q("symbol", "_NDX")
                if symbol not in ("_NDX", "QQQ"):
                    raise ValueError("symbol must be _NDX or QQQ")
                bands = tuple(
                    float(x) for x in q("em_bands", "0.5,1.5").split(",") if x.strip()
                )

                payload = build_payload(
                    symbol=symbol, n_expiries=n, basis_override=basis, mode="live",
                    em_bands=bands
                )
                ok, why = _upstash_set(payload)
                payload["published"] = ok
                payload["publish_info"] = why
                if ok:
                    payload["discord"] = discord_notify(payload)
                self._send(200, json.dumps(payload).encode(), "application/json")
            except Exception as e:
                traceback.print_exc()
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            return

        self._send(404, json.dumps({"error": "not found"}).encode(), "application/json")
