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
  ?n=4           number of nearest expiries (1-8)
"""

import json
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from api._gex_core import build_payload

ROOT = Path(__file__).resolve().parent.parent

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/nq_levels.json": ("nq_levels.json", "application/json"),
    "/history.json": ("history.json", "application/json"),
    "/nq_levels.txt": ("nq_levels.txt", "text/plain; charset=utf-8"),
}


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

        # ---- API: live recompute ----
        if path == "/api/gex":
            qs = parse_qs(parsed.query)

            def q(name, default=None):
                v = qs.get(name, [None])[0]
                return v if v not in (None, "") else default

            try:
                basis = q("basis")
                basis = float(basis) if basis is not None else None
                n = max(1, min(int(q("n", 4)), 8))
                symbol = q("symbol", "_NDX")
                if symbol not in ("_NDX", "QQQ"):
                    raise ValueError("symbol must be _NDX or QQQ")

                payload = build_payload(
                    symbol=symbol, n_expiries=n, basis_override=basis, mode="live"
                )
                self._send(200, json.dumps(payload).encode(), "application/json")
            except Exception as e:
                traceback.print_exc()
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            return

        self._send(404, json.dumps({"error": "not found"}).encode(), "application/json")
