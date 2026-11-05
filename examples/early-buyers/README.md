# QLO Early Buyers Starter

Find the wallets that bought a Solana token during a chosen time window.

**Start with a token. Leave with a CSV of wallets to investigate.**

[Русская инструкция](README.ru.md) · [Use QLO](https://t.me/kotte_memescan_bot)

## What this example does

1. Reads transaction history for one Pump.fun bonding curve or PumpSwap pool.
2. Selects transactions in your chosen interval.
3. Matches public buy instructions for the supplied token and market.
4. Requires a positive net token receipt owned by the buying signer.
5. Exports the observed buyers, earliest observed purchase times and transaction links.

It is a starting point for wallet research. Buying early does not establish that a
wallet is profitable, independent, human-controlled or suitable to copy.

This is an independently written educational example, not QLO's production
Trencher source. QLO's internal wallet selection, grouping, ranking and trading
logic are outside this project.

## Requirements

- Python 3.11 or newer. This example uses the Python standard library.
- A Solana JSON-RPC endpoint supporting historical getSignaturesForAddress and
  getTransaction requests for the period you want to inspect.
- Internet access to download the two official Pump public IDLs at startup.
  Alternatively, supply local IDL files with --idl.

RPC providers can charge for requests. Default limits are 250 RPC attempts,
10 history pages and 200 transaction reads. Retries count toward the RPC budget.

## 1. Get the example

```bash
git clone https://github.com/gustaffsonKotte/qlo.git
cd qlo/examples/early-buyers
python3 scanner.py --help
```

## 2. Choose the token, market and starting time

You need three inputs:

| Input | Meaning |
|---|---|
| --mint | The token contract address (mint) |
| --market | Its Pump.fun bonding_curve account or PumpSwap pool account |
| --start | Start of your research window, with a timezone |
| --minutes | Window length; default 25 minutes |

The pool/curve account and token mint are different addresses.

To find the market account, inspect a buy transaction for your token in a Solana
explorer. Use the bonding_curve account from the Pump.fun instruction, or the pool
account from the PumpSwap instruction. The public IDLs linked below list the
account roles when an explorer does not label them.

Choose the actual launch time if you want seconds after launch.
The script calculates seconds after your supplied --start; it does not discover
or verify launch time.

After a token migrates, the curve and AMM pool are separate markets. Run a
separate scan for each relevant market to investigate both periods.

## 3. Set your RPC endpoint and run

Replace ALL_CAPS placeholders with your own values.

Linux / macOS:

```bash
export RPC_URL='https://YOUR_SOLANA_RPC_ENDPOINT'
python3 scanner.py \
  --mint YOUR_TOKEN_MINT \
  --market YOUR_POOL_OR_CURVE_ACCOUNT \
  --start 2026-11-04T18:01:00Z \
  --minutes 25 \
  --output buyers.csv
```

Windows PowerShell:

```powershell
$env:RPC_URL = "https://YOUR_SOLANA_RPC_ENDPOINT"
python scanner.py --mint YOUR_TOKEN_MINT --market YOUR_POOL_OR_CURVE_ACCOUNT --start 2026-11-04T18:01:00Z --minutes 25 --output buyers.csv
```

The date is an example, not a preconfigured historical trade.

The script reads RPC_URL from the environment. The .env.example file documents
the setting; .env files are not loaded automatically. Wallet private keys are
not used, and the script does not submit transactions.

## 4. Read the output

The script creates buyers.csv and buyers.csv.metadata.json.

| CSV column | Meaning |
|---|---|
| wallet | Buying signer who had a positive net receipt of the target token |
| first_observed_buy_utc | Earliest matching purchase found in the scanned history |
| seconds_after_start | Seconds after your supplied start time |
| buy_transactions | Number of matching transactions, not instruction count |
| net_tokens_received | Sum of transaction-wide net target-token receipts in matching transactions |
| first_signature | Signature of the earliest observed purchase |
| solscan_url | Link to that transaction |

net_tokens_received is denominated in token units. A transaction can also
contain other transfers, so this is not a decoded swap fill or a position
balance. It is not SOL spent, profit, ROI or current holdings.

Rows are ordered by earliest observed purchase time.
Transactions sharing one block timestamp are not ordered within that second.

## 5. Check coverage before using the list

The metadata file reports page counts, RPC attempts, missing transactions,
unknown timestamps, unread transactions and parsing failures.

Exit codes:

- 0: The scan reached a timestamp before the start and encountered no recorded gaps or caps.
- 2: A CSV was produced, but coverage is partial or cannot be established.
- 1: Setup, network or file-output failure.

A provider reporting no older signatures does not prove it has the complete
history. The example conservatively returns exit code 2 when it cannot reach
a timestamp before your start, including cases where the market was only just
created. Read provider_history_exhausted and history_reached_before_start
in the report.

Even exit code 0 applies only to this supplied account, the provider's returned
data and supported buy instructions. It is not proof of complete on-chain
coverage. Providers may prune data without exposing the gap.

For a busy market, scanning backwards from its latest transactions may reach the
page limit before it reaches your window. Increase limits deliberately, or choose
a provider with the historical data you need.

Example with larger limits:

```bash
python3 scanner.py --mint YOUR_TOKEN_MINT --market YOUR_POOL_OR_CURVE_ACCOUNT --start 2026-11-04T18:01:00Z --minutes 25 --max-pages 30 --max-transactions 500 --max-requests 550 --output buyers.csv
```

## 6. Investigate the wallets

Open the transaction links and examine each address's wider trading history:

- Did it actually sell, or is the apparent gain still an open position?
- Are losses included in its record?
- Is most of its result explained by one winning token?
- How quickly does it exit?
- Is it still trading?

Use the output as a research shortlist. This script does not calculate wallet
profitability, identify bundles or certify smart wallets.

## Supported scope

- Public Pump.fun and PumpSwap buy instructions containing identifiable
  mint/base_mint, user and bonding_curve/pool account roles.
- Top-level and inner instructions, with a signer-owned positive net receipt.
- Parsed legacy and v0 transaction responses.
- One supplied market account per run.

Other DEXs, PDA-owned buyers, arbitrary routers, all multi-hop swaps, quote-side
purchases and every historical instruction version are not covered. Transfers
alone and failed transactions are excluded.

By default the script reads current official IDLs at startup. Supported account
layouts may change upstream. For repeatable research, save the public IDLs you
used and pass both explicitly:

```bash
python3 scanner.py --mint YOUR_TOKEN_MINT --market YOUR_POOL_OR_CURVE_ACCOUNT --start 2026-11-04T18:01:00Z --idl pump.json --idl pump_amm.json
```

## Tests

```bash
python3 -m unittest -v
```

Tests use clearly synthetic transaction fixtures. They exercise buy/transfer
classification, owners and signers, exact token arithmetic, time boundaries,
pagination, caps and missing history. They do not certify live RPC results.
The repository's Early buyers tests workflow runs the checks on GitHub.

## Public references

- [Solana signature history](https://solana.com/docs/rpc/http/getsignaturesforaddress)
- [Solana transaction responses](https://solana.com/docs/rpc/http/gettransaction)
- [Solana JSON structures](https://solana.com/docs/rpc/json-structures)
- [Official Pump.fun IDL](https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump.json)
- [Official PumpSwap IDL](https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump_amm.json)

## Continue in QLO

In QLO: Smart money → Trencher → GMGN search.

Search for wallet candidates, inspect their history and add selected addresses
to tracking for future buy alerts.

[Open QLO](https://t.me/kotte_memescan_bot)

License: the repository's MIT license.
