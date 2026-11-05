#!/usr/bin/env python3
"""Find observed early buyers in one Pump.fun curve or PumpSwap pool."""
import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
IDL_URLS = (
    "https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/idl/pump.json",
    "https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/idl/pump_amm.json",
)
FIELDS = ("wallet", "first_observed_buy_utc", "seconds_after_start",
          "buy_transactions", "net_tokens_received", "first_signature", "solscan_url")


def b58decode(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("Invalid base58 data")
    number = 0
    for char in value:
        number = number * 58 + B58.index(char)
    zeros = len(value) - len(value.lstrip("1"))
    return b"\0" * zeros + number.to_bytes((number.bit_length() + 7) // 8, "big")


def address(value):
    try:
        valid = len(b58decode(value)) == 32
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError("Expected a Solana public address")
    return value


def utc(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return int(parsed.timestamp())
    except ValueError:
        raise argparse.ArgumentTypeError("Use a timezone, e.g. 2026-11-04T18:01:00Z") from None


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def flatten_accounts(items):
    output = []
    for item in items:
        if "accounts" in item:
            output.extend(flatten_accounts(item["accounts"]))
        else:
            output.append(item["name"])
    return output


def rules_from_idl(doc):
    """Use public instruction discriminators and account roles."""
    output = {}
    for instruction in doc.get("instructions", []):
        name = instruction["name"]
        if name != "buy" and not name.startswith("buy_"):
            continue
        names = flatten_accounts(instruction.get("accounts", []))
        mint = next((n for n in ("base_mint", "mint") if n in names), None)
        market = next((n for n in ("pool", "bonding_curve") if n in names), None)
        if mint and market and "user" in names:
            discriminator = bytes(instruction["discriminator"])
            if len(discriminator) == 8:
                output[(doc["address"], discriminator)] = (
                    names.index(mint), names.index("user"), names.index(market))
    if not output:
        raise ValueError("IDL has no supported buy instructions")
    return output


def load_rules(paths):
    rules = {}
    for source in paths or IDL_URLS:
        if paths:
            doc = json.loads(Path(source).read_text(encoding="utf-8"))
        else:
            request = Request(source, headers={"User-Agent": "qlo-early-buyers-starter/0.1"})
            with urlopen(request, timeout=30) as response:
                doc = json.load(response)
        rules.update(rules_from_idl(doc))
    return rules


def token_receipts(meta, mint):
    """Transaction-wide net received units, grouped by token-account owner."""
    before, after = meta.get("preTokenBalances"), meta.get("postTokenBalances")
    if before is None or after is None:
        raise ValueError("Missing token balances")
    owners, totals = {}, defaultdict(Decimal)
    for row in before + after:
        if row["mint"] == mint and row.get("owner"):
            owners[row["accountIndex"]] = row["owner"]
    with localcontext() as context:
        context.prec = 80
        for sign, rows in ((-1, before), (1, after)):
            for row in rows:
                if row["mint"] != mint:
                    continue
                owner = row.get("owner") or owners.get(row["accountIndex"])
                if owner is None:
                    raise ValueError("Missing target token-account owner")
                amount = row["uiTokenAmount"]
                totals[owner] += sign * Decimal(amount["amount"]).scaleb(-int(amount["decimals"]))
    return totals


def buyers_in_transaction(tx, mint, market, rules):
    """Match a buy for this mint/market, then require signer-owned net receipts."""
    meta = tx.get("meta")
    if not meta or meta.get("err") is not None:
        return []
    message = tx["transaction"]["message"]
    raw_keys = message["accountKeys"]
    keys = [key["pubkey"] if isinstance(key, dict) else key for key in raw_keys]
    signers = {key["pubkey"] for key in raw_keys
               if isinstance(key, dict) and key.get("signer")}
    if raw_keys and isinstance(raw_keys[0], str):
        signers = set(keys[:message.get("header", {}).get("numRequiredSignatures", 0)])

    def resolve(value):
        return keys[value] if isinstance(value, int) else value

    instructions = list(message.get("instructions", []))
    for group in meta.get("innerInstructions") or []:
        instructions.extend(group.get("instructions", []))
    matched = set()
    for instruction in instructions:
        if "data" not in instruction:
            continue
        try:
            program = instruction.get("programId")
            if program is None:
                program = resolve(instruction["programIdIndex"])
            role = rules.get((program, b58decode(instruction["data"])[:8]))
            if role is None:
                continue
            accounts = [resolve(value) for value in instruction["accounts"]]
            mint_index, user_index, market_index = role
            wallet = accounts[user_index]
            if (accounts[mint_index] == mint and accounts[market_index] == market
                    and wallet in signers):
                matched.add(wallet)
        except (KeyError, IndexError, ValueError, TypeError):
            continue
    if not matched:
        return []
    receipts = token_receipts(meta, mint)
    return [(wallet, receipts[wallet]) for wallet in sorted(matched)
            if receipts[wallet] > 0]


class RPC:
    def __init__(self, endpoint, budget, delay):
        self.endpoint, self.budget, self.delay = endpoint, budget, delay
        self.calls = 0

    def call(self, method, params):
        for attempt in range(4):
            if self.calls >= self.budget:
                raise RuntimeError("RPC request budget reached")
            self.calls += 1
            body = json.dumps({"jsonrpc": "2.0", "id": self.calls,
                               "method": method, "params": params}).encode()
            try:
                request = Request(self.endpoint, data=body,
                                  headers={"Content-Type": "application/json"})
                if self.delay:
                    time.sleep(self.delay)
                with urlopen(request, timeout=30) as response:
                    data = json.load(response)
                if "error" in data:
                    code = data["error"].get("code")
                    if code == -32005 and attempt < 3:
                        time.sleep(2 ** attempt)
                        continue
                    raise RuntimeError("RPC error code %s" % code)
                return data["result"]
            except HTTPError as error:
                if error.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError("RPC HTTP error %s" % error.code) from None
            except (URLError, TimeoutError, OSError):
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError("RPC connection failed") from None
            except (ValueError, KeyError, TypeError):
                raise RuntimeError("RPC returned an invalid response") from None


def scan(rpc, args, rules):
    start, end = args.start, args.start + args.minutes * 60
    report = {"market": args.market, "mint": args.mint,
              "window_start": iso(start), "window_end_exclusive": iso(end),
              "history_reached_before_start": False, "provider_history_exhausted": False,
              "pages": 0, "unknown_signature_times": 0, "missing_transactions": 0,
              "unread_transactions": 0, "unparsed_transactions": 0, "errors": [],
              "coverage": "one supplied pool or bonding-curve account"}
    signatures, seen, cursor = {}, set(), None
    try:
        for _ in range(args.max_pages):
            config = {"limit": args.page_size, "commitment": "finalized"}
            if cursor:
                config["before"] = cursor
            page = rpc.call("getSignaturesForAddress", [args.market, config])
            report["pages"] += 1
            if not isinstance(page, list):
                raise RuntimeError("Invalid signature page")
            if not page:
                report["provider_history_exhausted"] = True
                break
            times = []
            for item in page:
                signature = item["signature"]
                if signature in seen:
                    continue
                seen.add(signature)
                timestamp = item.get("blockTime")
                if timestamp is None:
                    report["unknown_signature_times"] += 1
                    continue
                times.append(timestamp)
                if start <= timestamp < end and item.get("err") is None:
                    signatures[signature] = timestamp
            # Strict inequality: more transactions at the start second may be on the next page.
            if times and min(times) < start:
                report["history_reached_before_start"] = True
                break
            next_cursor = page[-1]["signature"]
            if next_cursor == cursor:
                raise RuntimeError("Pagination cursor did not advance")
            cursor = next_cursor
    except (RuntimeError, KeyError, TypeError) as error:
        report["errors"].append(str(error))

    ordered = sorted(signatures, key=lambda signature: (signatures[signature], signature))
    chosen = ordered[:args.max_transactions]
    report["unread_transactions"] = len(ordered) - len(chosen)
    wallets = {}
    for index, signature in enumerate(chosen):
        try:
            tx = rpc.call("getTransaction", [signature, {
                "encoding": "jsonParsed", "commitment": "finalized",
                "maxSupportedTransactionVersion": 0}])
        except RuntimeError as error:
            report["errors"].append(str(error))
            report["unread_transactions"] += len(chosen) - index
            break
        if tx is None or tx.get("blockTime") is None:
            report["missing_transactions"] += 1
            continue
        timestamp = tx["blockTime"]
        if not start <= timestamp < end:
            report["unparsed_transactions"] += 1
            continue
        try:
            matches = buyers_in_transaction(tx, args.mint, args.market, rules)
        except (ValueError, KeyError, TypeError, IndexError):
            report["unparsed_transactions"] += 1
            continue
        for wallet, amount in matches:
            if wallet not in wallets:
                wallets[wallet] = {
                    "wallet": wallet, "first_observed_buy_utc": iso(timestamp),
                    "seconds_after_start": timestamp - start, "buy_transactions": 0,
                    "net_tokens_received": Decimal(0), "first_signature": signature,
                    "solscan_url": "https://solscan.io/tx/" + signature}
            row = wallets[wallet]
            row["buy_transactions"] += 1
            with localcontext() as context:
                context.prec = 80
                row["net_tokens_received"] += amount
            if timestamp - start < row["seconds_after_start"]:
                row.update(first_observed_buy_utc=iso(timestamp),
                           seconds_after_start=timestamp - start,
                           first_signature=signature,
                           solscan_url="https://solscan.io/tx/" + signature)
    report["rpc_requests"] = rpc.calls
    report["matched_wallets"] = len(wallets)
    report["incomplete"] = bool(
        not report["history_reached_before_start"] or report["errors"]
        or report["unknown_signature_times"] or report["missing_transactions"]
        or report["unread_transactions"] or report["unparsed_transactions"])
    rows = sorted(wallets.values(), key=lambda row: (row["seconds_after_start"], row["wallet"]))
    for row in rows:
        row["net_tokens_received"] = format(row["net_tokens_received"], "f")
    return rows, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mint", type=address, required=True)
    parser.add_argument("--market", type=address, required=True,
                        help="Pool or bonding-curve account, NOT the mint")
    parser.add_argument("--start", type=utc, required=True, help="ISO-8601 time with timezone")
    parser.add_argument("--minutes", type=int, default=25)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--max-transactions", type=int, default=200)
    parser.add_argument("--max-requests", type=int, default=250)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--idl", action="append", help="Local public IDL JSON; repeat as needed")
    parser.add_argument("--output", type=Path, default=Path("buyers.csv"))
    args = parser.parse_args()
    if (not 1 <= args.page_size <= 1000 or args.minutes <= 0 or args.max_pages <= 0
            or args.max_transactions <= 0 or args.max_requests <= 0 or args.delay < 0):
        parser.error("Positive limits required; page size must be 1..1000")
    if args.market == args.mint:
        parser.error("--market must be the pool or curve account, not the mint")
    endpoint = os.getenv("RPC_URL", "")
    if not endpoint.startswith(("https://", "http://")):
        parser.error("Set RPC_URL to your Solana RPC endpoint")
    try:
        rules = load_rules(args.idl)
        rows, report = scan(RPC(endpoint, args.max_requests, args.delay), args, rules)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        metadata = args.output.with_suffix(args.output.suffix + ".metadata.json")
        metadata.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except (ValueError, OSError, RuntimeError):
        # Never include an RPC URL or credential in error output.
        print("Scan failed. Check RPC access, IDL availability and output permissions.",
              file=sys.stderr)
        return 1
    print("Wallets: %s | CSV: %s | Report: %s" % (len(rows), args.output, metadata))
    if report["incomplete"]:
        print("Incomplete coverage: inspect the metadata before using this list.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
