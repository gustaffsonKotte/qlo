# QLO

Pump.fun graduation scanner.

Watches every bonding curve that fills on pump.fun and tells you about the
slow ones.

## Why slow

Roughly 35,000 tokens are created on pump.fun every day. About 2% of them
ever fill their bonding curve. The median one that does takes around
twenty minutes.

Three minutes means bots pushed the price with nobody real behind it.
Six hours means people were buying, with their own money, for hours.

Measured across six months of graduations, the slow ones were about
**1.5× more likely to double** after graduation and roughly **2.4× more
likely to do 5×** than the fast ones. That held on a holdout period the
analysis had never seen.

It is a filter, not a crystal ball. Most of these tokens still go to zero.

## What it does

- subscribes to pool creations on the pump.fun AMM via a Helius webhook
- discards pools that are not pump.fun graduations (about three quarters
  of the stream, filtered locally with no API calls)
- measures how long each token sat on the curve
- sends the ones above your threshold to Telegram
- optionally gates access on membership of a Telegram channel

## Setup

```bash
git clone https://github.com/gustaffsonKotte/qlo.git
cd qlo
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp config.example.json config.json
```

Fill in `config.json`:

| field | where to get it |
|---|---|
| `tg_token` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `helius_key` | [helius.dev](https://helius.dev) → API keys (free tier is enough) |
| `webhook_url` | your public https endpoint, ending in `/hook` |
| `channel` | `@yourchannel` to gate access, or `""` for open |
| `min_age_hours` | how slow is slow. `6` is a reasonable start |

Then:

```bash
./venv/bin/python setup_webhook.py
./venv/bin/uvicorn app:app --host 127.0.0.1 --port 8091
```

Point Telegram at the bot endpoint:

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://your-domain.com/tg"
```

Your endpoint must be reachable over https. A reverse proxy in front of
port 8091 is the usual arrangement.

### Channel gating

If `channel` is set, the bot has to be an **admin of that channel** —
Telegram will not answer membership questions otherwise. It needs no
permissions beyond the admin status itself, and it never posts anything
there.

## Cost

Helius free tier gives a million credits a month. This uses roughly
270,000 of them: about 120,000 for the webhook stream and the rest for
looking up token ages. No paid services anywhere.

## Files it writes

`users.json`, `webhook_id.txt` and any logs stay local and are gitignored
along with `config.json`. Nothing is sent anywhere except Telegram.

## License

MIT
