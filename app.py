"""
Pump.fun graduation scanner.

Listens to every bonding-curve graduation on pump.fun, measures how long
the token spent on the curve, and sends the slow ones to Telegram.

A token that fills its curve in three minutes was filled by bots.
One that takes six hours was filled by people. That difference is the
whole idea.

Run:
    uvicorn app:app --host 127.0.0.1 --port 8091
"""

import csv
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Request

BASE = Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text())

TG_TOKEN = CFG["tg_token"]
CHANNEL = CFG.get("channel", "")
MIN_AGE_H = float(CFG.get("min_age_hours", 6))
RPC = f"https://mainnet.helius-rpc.com/?api-key={CFG['helius_key']}"

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

USERS = BASE / "users.json"
users = json.loads(USERS.read_text()) if USERS.exists() else {}

app = FastAPI()


# ----------------------------------------------------------------- telegram

def api(method: str, params: dict):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(url, data, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "error_code": e.code}
    except Exception as e:
        print("telegram:", e)
        return {"ok": False}


def send(chat_id, text: str, buttons=None):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": "true"}
    if buttons:
        p["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    return api("sendMessage", p)


def save_users():
    USERS.write_text(json.dumps(users, ensure_ascii=False, indent=1))


def is_member(user_id) -> bool:
    """Gate access on channel membership. Empty channel in config = open to all.
    The bot must be an admin of the channel for this call to work."""
    if not CHANNEL:
        return True
    r = api("getChatMember", {"chat_id": CHANNEL, "user_id": user_id})
    if not r.get("ok"):
        return False
    return r["result"].get("status") in ("creator", "administrator", "member")


def broadcast(text: str, buttons=None):
    now = time.time()
    sent = dropped = 0
    for uid, u in list(users.items()):
        if now - u.get("checked", 0) > 21600:
            u["member"] = is_member(uid)
            u["checked"] = now
        if not u.get("member"):
            continue
        r = send(uid, text, buttons)
        if not r.get("ok"):
            desc = (r.get("description") or "").lower()
            if r.get("error_code") == 403 or "blocked" in desc or "deactivated" in desc:
                users.pop(uid, None)
                dropped += 1
                continue
        else:
            sent += 1
        time.sleep(0.04)
    save_users()
    print(f"broadcast: sent={sent} dropped={dropped} total={len(users)}")


# ---------------------------------------------------------------------- rpc

stats = {"events": 0, "graduations": 0, "alerts": 0, "rpc": 0}


def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    stats["rpc"] += 1
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


def age_minutes(mint, grad_ts):
    """How long the token lived before graduating.

    Pages signatures backwards and stops as soon as it finds one older than
    the threshold, so old tokens cost one or two calls instead of twelve.
    """
    cutoff = grad_ts - MIN_AGE_H * 3600
    before = None
    oldest = grad_ts
    for _ in range(20):
        p = [mint, {"limit": 1000}]
        if before:
            p[1]["before"] = before
        try:
            res = rpc("getSignaturesForAddress", p).get("result") or []
        except Exception as e:
            print("age:", e)
            return None
        if not res:
            break
        bt = res[-1].get("blockTime")
        if bt:
            oldest = bt
            if bt < cutoff:
                return (grad_ts - bt) / 60
        before = res[-1]["signature"]
        if len(res) < 1000:
            break
    return (grad_ts - oldest) / 60


def token_meta(mint):
    try:
        a = rpc("getAsset", {"id": mint}).get("result") or {}
        md = (a.get("content") or {}).get("metadata") or {}
        return md.get("symbol", ""), md.get("name", "")
    except Exception:
        return "", ""


# ------------------------------------------------------------------ scanner

queue = []
qlock = threading.Lock()
seen = {}


def keyboard(mint):
    return [[
        {"text": "Pump.fun", "url": f"https://pump.fun/coin/{mint}"},
        {"text": "DexScreener", "url": f"https://dexscreener.com/solana/{mint}"},
    ], [
        {"text": "Axiom", "url": f"https://axiom.trade/t/{mint}"},
        {"text": "Solscan", "url": f"https://solscan.io/token/{mint}"},
    ]]


def build(mint, symbol, name, mins):
    hours = mins / 60
    age = f"{hours:.1f} h" if hours < 48 else f"{hours / 24:.1f} d"
    title = f"${symbol}" + (f" \u2014 {name}" if name and name != symbol else "")
    return "\n".join([
        "\U0001F40C <b>SLOW GRADUATION</b>",
        "",
        title if symbol else "<i>unnamed</i>",
        "",
        f"<code>Time on curve   {age}</code>",
        f"<code>Median token    0.3 h</code>",
        "",
        "<b>Contract</b>",
        f"<code>{mint}</code>",
    ])


def worker():
    while True:
        time.sleep(2)
        with qlock:
            batch, queue[:] = list(queue), []
        for mint, grad_ts in batch:
            try:
                mins = age_minutes(mint, grad_ts)
                if mins is None or mins < MIN_AGE_H * 60:
                    continue
                sym, name = token_meta(mint)
                stats["alerts"] += 1
                broadcast(build(mint, sym, name, mins), keyboard(mint))
                print(f"alert: {mint} {mins / 60:.1f}h")
            except Exception as e:
                print("worker:", e)


threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------- endpoints

@app.post("/hook")
async def hook(req: Request):
    """Helius webhook: CREATE_POOL events on the pump.fun AMM."""
    body = await req.json()
    events = body if isinstance(body, list) else [body]
    now = time.time()

    for e in events:
        stats["events"] += 1
        accs = {a.get("account") for a in (e.get("accountData") or [])}
        if PUMP_PROGRAM not in accs:
            continue                      # someone else's pool, not a graduation

        mint = ""
        for a in (e.get("accountData") or []):
            for c in (a.get("tokenBalanceChanges") or []):
                m = c.get("mint") or ""
                if m.endswith("pump"):
                    mint = m
                    break
            if mint:
                break
        if not mint:
            continue

        if mint in seen and now - seen[mint] < 3600:
            continue
        seen[mint] = now
        for k, v in list(seen.items()):
            if now - v > 7200:
                seen.pop(k, None)

        stats["graduations"] += 1
        with qlock:
            queue.append((mint, e.get("timestamp") or now))

    return {"ok": True}


WELCOME = (
    "\U0001F4E1 <b>Pump.fun graduation scanner</b>\n\n"
    "You will get an alert when a token takes hours to fill its bonding "
    "curve instead of minutes.\n\n"
    "A curve filled in three minutes was filled by bots. One that takes six "
    "hours was filled by people.\n\n"
    "Not financial advice. Most of these still go to zero.\n\n"
    "Send /stop to unsubscribe."
)


@app.post("/tg")
async def telegram(req: Request):
    upd = await req.json()
    msg = upd.get("message") or {}
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return {"ok": True}

    uid = str(chat.get("id"))
    text = (msg.get("text") or "").strip()

    if text.startswith("/start"):
        if not is_member(uid):
            ch = CHANNEL.lstrip("@")
            send(uid, "\U0001F512 Subscribe to the channel first, then send /start again.",
                 [[{"text": "Open channel", "url": f"https://t.me/{ch}"}]])
            return {"ok": True}
        first = uid not in users
        users[uid] = {
            "username": chat.get("username", ""),
            "joined": users.get(uid, {}).get("joined") or int(time.time()),
            "member": True,
            "checked": time.time(),
        }
        save_users()
        send(uid, WELCOME if first else "\u2705 You are back on the list.")

    elif text.startswith("/stop"):
        if users.pop(uid, None):
            save_users()
        send(uid, "Unsubscribed. Send /start to come back.")

    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True, "users": len(users), **stats,
            "min_age_hours": MIN_AGE_H, "queue": len(queue)}
