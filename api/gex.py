"""Vercel Python serverless function: GET /api/gex

On-demand live recompute of the full GEX pipeline (CBOE delayed chain + basis).
Called by the dashboard's "Refresh live" button — nothing is persisted here;
the daily history is maintained by the GitHub Actions snapshot.

Query params:
  ?basis=145.5   manual NQ-NDX basis override (skips Yahoo)
  ?symbol=_NDX   _NDX (default) or QQQ
  ?n=4           number of nearest expiries (1-8)
"""

import json
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from api._gex_core import build_payload


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)

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
            body = json.dumps(payload).encode()
            self.send_response(200)
        except Exception as e:
            traceback.print_exc()
            body = json.dumps({"error": str(e)}).encode()
            self.send_response(500)

        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
