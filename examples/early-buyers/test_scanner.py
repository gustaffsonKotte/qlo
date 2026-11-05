"""Synthetic, offline regression cases; no keys and no RPC requests."""
import copy
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

import scanner

PROGRAM, MINT, MARKET, WALLET = "test_program", "test_mint", "test_pool", "test_wallet"
BUY = bytes([102, 6, 61, 18, 1, 218, 235, 234])
IDL = {"address": PROGRAM, "instructions": [{
    "name": "buy", "discriminator": list(BUY),
    "accounts": [{"name": n} for n in ("pool", "user", "global_config", "base_mint")]}]}
RULES = scanner.rules_from_idl(IDL)


def b58encode(raw):
    number = int.from_bytes(raw, "big")
    out = ""
    while number:
        number, digit = divmod(number, 58)
        out = scanner.B58[digit] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def balance(index, amount, owner=WALLET):
    row = {"accountIndex": index, "mint": MINT,
           "uiTokenAmount": {"amount": str(amount), "decimals": 6}}
    if owner is not None:
        row["owner"] = owner
    return row


def transaction(timestamp=101):
    return {
        "blockTime": timestamp,
        "transaction": {"message": {
            "accountKeys": [{"pubkey": n, "signer": n == WALLET}
                            for n in (WALLET, MARKET, MINT, PROGRAM)],
            "instructions": [{"programId": PROGRAM,
                              "accounts": [MARKET, WALLET, "global", MINT],
                              "data": b58encode(BUY + b"\0" * 16)}]}},
        "meta": {"err": None, "innerInstructions": [],
                 "preTokenBalances": [], "postTokenBalances": [balance(4, 1250000)]}}


class FakeRPC:
    def __init__(self, pages, transactions):
        self.pages, self.transactions = iter(pages), transactions
        self.calls, self.params = 0, []

    def call(self, method, params):
        self.calls += 1
        self.params.append((method, params))
        if method == "getSignaturesForAddress":
            return next(self.pages, [])
        value = self.transactions.get(params[0])
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def signature(name, timestamp):
    return {"signature": name, "blockTime": timestamp, "err": None}


def options(**updates):
    values = dict(start=100, minutes=1, market=MARKET, mint=MINT,
                  max_pages=10, page_size=1000, max_transactions=200)
    values.update(updates)
    return SimpleNamespace(**values)


class ParserTests(unittest.TestCase):
    def matches(self, tx):
        return scanner.buyers_in_transaction(tx, MINT, MARKET, RULES)

    def test_direct_buy(self):
        self.assertEqual(self.matches(transaction()), [(WALLET, Decimal("1.25"))])

    def test_token_transfer_is_not_a_buy(self):
        tx = transaction()
        tx["transaction"]["message"]["instructions"] = [
            {"program": "spl-token", "parsed": {"type": "transfer"}}]
        self.assertEqual(self.matches(tx), [])

    def test_sell_is_not_a_buy(self):
        tx = transaction()
        tx["transaction"]["message"]["instructions"][0]["data"] = b58encode(b"sell0000")
        self.assertEqual(self.matches(tx), [])

    def test_other_market_and_mint_are_excluded(self):
        for index in (0, 3):
            with self.subTest(index=index):
                tx = transaction()
                tx["transaction"]["message"]["instructions"][0]["accounts"][index] = "other"
                self.assertEqual(self.matches(tx), [])

    def test_failed_transaction(self):
        tx = transaction()
        tx["meta"]["err"] = {"InstructionError": [0, "Custom"]}
        self.assertEqual(self.matches(tx), [])

    def test_user_must_sign(self):
        tx = transaction()
        tx["transaction"]["message"]["accountKeys"][0]["signer"] = False
        self.assertEqual(self.matches(tx), [])

    def test_unrelated_receiver_does_not_become_buyer(self):
        tx = transaction()
        tx["meta"]["postTokenBalances"] = [balance(4, 1250000, owner="someone_else")]
        self.assertEqual(self.matches(tx), [])

    def test_net_zero_receipt(self):
        tx = transaction()
        tx["meta"]["preTokenBalances"] = [balance(4, 1250000)]
        self.assertEqual(self.matches(tx), [])

    def test_all_user_token_accounts_are_aggregated(self):
        tx = transaction()
        tx["meta"]["preTokenBalances"] = [balance(4, 500000)]
        tx["meta"]["postTokenBalances"] = [balance(4, 1250000), balance(5, 1000000)]
        self.assertEqual(self.matches(tx), [(WALLET, Decimal("1.75"))])

    def test_owner_can_be_resolved_from_other_side_of_same_account(self):
        tx = transaction()
        tx["meta"]["preTokenBalances"] = [balance(4, 500000, owner=None)]
        self.assertEqual(self.matches(tx), [(WALLET, Decimal("0.75"))])

    def test_missing_token_balance_owner_is_explicit_error(self):
        tx = transaction()
        tx["meta"]["postTokenBalances"] = [balance(4, 100, owner=None)]
        with self.assertRaises(ValueError):
            self.matches(tx)

    def test_inner_instruction_with_indices_and_resolved_lookup_key(self):
        tx = transaction()
        message = tx["transaction"]["message"]
        message["accountKeys"][1]["source"] = "lookupTable"
        message["instructions"] = []
        tx["meta"]["innerInstructions"] = [{"index": 0, "instructions": [{
            "programIdIndex": 3, "accounts": [1, 0, 3, 2],
            "data": b58encode(BUY)}]}]
        self.assertEqual(self.matches(tx), [(WALLET, Decimal("1.25"))])

    def test_legacy_raw_account_keys(self):
        tx = transaction()
        message = tx["transaction"]["message"]
        message["accountKeys"] = [k["pubkey"] for k in message["accountKeys"]]
        message["header"] = {"numRequiredSignatures": 1}
        self.assertEqual(self.matches(tx), [(WALLET, Decimal("1.25"))])

    def test_large_raw_amounts_do_not_lose_integer_precision(self):
        tx = transaction()
        tx["meta"]["postTokenBalances"] = [balance(4, "900719925474099312345678")]
        self.assertEqual(self.matches(tx),
                         [(WALLET, Decimal("900719925474099312.345678"))])

    def test_buy_versions_derive_roles_from_idl(self):
        doc = {"address": PROGRAM, "instructions": [{
            "name": "buy_exact_quote_in_v3", "discriminator": list(BUY),
            "accounts": [{"name": n} for n in ("user", "base_mint", "bonding_curve")]}]}
        self.assertEqual(scanner.rules_from_idl(doc)[(PROGRAM, BUY)], (1, 0, 2))

    def test_base58_preserves_leading_zeros(self):
        raw = b"\0\0abc"
        self.assertEqual(scanner.b58decode(b58encode(raw)), raw)


class HistoryTests(unittest.TestCase):
    def test_interval_is_start_inclusive_end_exclusive(self):
        rpc = FakeRPC([[signature("end", 160), signature("inside", 101),
                        signature("start", 100), signature("older", 99)]],
                      {"inside": transaction(101), "start": transaction(100)})
        rows, report = scanner.scan(rpc, options(), RULES)
        self.assertFalse(report["incomplete"])
        self.assertEqual(rows[0]["seconds_after_start"], 0)
        self.assertEqual(rows[0]["buy_transactions"], 2)
        self.assertEqual(rows[0]["net_tokens_received"], "2.500000")
        fetched = [p[0] for method, p in rpc.params if method == "getTransaction"]
        self.assertEqual(fetched, ["start", "inside"])

    def test_start_second_continues_across_pages_and_deduplicates(self):
        rpc = FakeRPC([[signature("b", 100)],
                       [signature("b", 100), signature("a", 100), signature("old", 99)]],
                      {"a": transaction(100), "b": transaction(100)})
        rows, report = scanner.scan(rpc, options(), RULES)
        self.assertEqual(report["pages"], 2)
        self.assertFalse(report["incomplete"])
        self.assertEqual(rows[0]["buy_transactions"], 2)
        self.assertEqual(rpc.params[1][1][1]["before"], "b")

    def test_cap_keeps_oldest_observed_transactions_and_reports_partial(self):
        rpc = FakeRPC([[signature("late", 110), signature("early", 101),
                        signature("old", 99)]],
                      {"late": transaction(110), "early": transaction(101)})
        rows, report = scanner.scan(rpc, options(max_transactions=1), RULES)
        self.assertEqual(rows[0]["first_signature"], "early")
        self.assertEqual(report["unread_transactions"], 1)
        self.assertTrue(report["incomplete"])

    def test_provider_exhaustion_does_not_claim_complete_launch_history(self):
        rpc = FakeRPC([[signature("one", 101)], []], {"one": transaction(101)})
        rows, report = scanner.scan(rpc, options(), RULES)
        self.assertTrue(report["provider_history_exhausted"])
        self.assertFalse(report["history_reached_before_start"])
        self.assertTrue(report["incomplete"])
        self.assertEqual(len(rows), 1)

    def test_missing_transaction_is_not_silently_ignored(self):
        rpc = FakeRPC([[signature("missing", 101), signature("old", 99)]], {})
        rows, report = scanner.scan(rpc, options(), RULES)
        self.assertEqual(rows, [])
        self.assertEqual(report["missing_transactions"], 1)
        self.assertTrue(report["incomplete"])

    def test_request_budget_failure_preserves_partial_report(self):
        rpc = FakeRPC([[signature("one", 101), signature("old", 99)]],
                      {"one": RuntimeError("RPC request budget reached")})
        rows, report = scanner.scan(rpc, options(), RULES)
        self.assertEqual(rows, [])
        self.assertEqual(report["unread_transactions"], 1)
        self.assertTrue(report["incomplete"])

    def test_unknown_time_is_recorded(self):
        rpc = FakeRPC([[signature("unknown", None), signature("old", 99)]], {})
        _, report = scanner.scan(rpc, options(), RULES)
        self.assertEqual(report["unknown_signature_times"], 1)
        self.assertTrue(report["incomplete"])

    def test_rpc_error_does_not_expose_endpoint_credentials(self):
        endpoint = "https://example.invalid/?api-key=DO_NOT_EXPOSE"
        error = HTTPError(endpoint, 403, "Forbidden", {}, None)
        with patch.object(scanner, "urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "^RPC HTTP error 403$"):
                scanner.RPC(endpoint, budget=1, delay=0).call("getTransaction", ["test"])


if __name__ == "__main__":
    unittest.main()
