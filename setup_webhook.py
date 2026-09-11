"""
Registers a Helius webhook for pump.fun graduations.

Every bonding curve that fills ends with a pool being created on the
pump.fun AMM, so that is what we subscribe to. Roughly 4,000 events a day,
of which about a quarter are real graduations — the rest are unrelated
pools and get filtered in app.py for free.

    python setup_webhook.py            create
    python setup_webhook.py list       show existing
    python setup_webhook.py delete     remove the one we created
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text())
KEY = CFG["helius_key"]
HOST = "https://mainnet.helius-rpc.com"
STORE = BASE / "webhook_id.txt"

PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"


def call(method, path, body=None):
    sep = "&" if "?" in path else "?"
    req = urllib.request.Request(
        f"{HOST}{path}{sep}api-key={KEY}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
    )
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}")
        return None


cmd = sys.argv[1] if len(sys.argv) > 1 else "create"

if cmd == "list":
    print(json.dumps(call("GET", "/v0/webhooks"), indent=1))

elif cmd == "delete":
    if not STORE.exists():
        print("nothing to delete")
        sys.exit()
    call("DELETE", f"/v0/webhooks/{STORE.read_text().strip()}")
    STORE.unlink()
    print("deleted")

else:
    url = CFG["webhook_url"]
    if "your-domain" in url:
        print("set webhook_url in config.json first")
        sys.exit(1)

    r = call("POST", "/v0/webhooks", {
        "webhookURL": url,
        "transactionTypes": ["CREATE_POOL"],
        "accountAddresses": [PUMP_AMM],
        "webhookType": "enhanced",
    })
    if r and r.get("webhookID"):
        STORE.write_text(r["webhookID"])
        print("created:", r["webhookID"])
        print("expect ~4,000 events a day, ~1,000 of them real graduations")
    else:
        print("failed:", r)
