"""Domain-scoped CLI behaviour for the owned reducer ``scripts/v3_reduce_chain.py``.

Covers the three decoupled reduction domains and their guards:

* **BBB-only** — ``--bbb-result`` without fresh transfers must merge the BBB
  canonical and rebuild only the BBB trade domain: no transfer-ledger,
  holder, supply or burn-clock writes, and untouched unrelated mtimes.
* **Rebuild-canonical** — ``--rebuild-canonical`` must rebuild the derived
  domains from the already-validated canonical CSV while leaving stored
  stable ``event_id`` values untouched (never normalised or rehashed) and the
  13-field legacy schema intact.
* **Preserve-derived** — ``--preserve-derived holders,supply,burns`` must skip
  the named domain entirely, preserving its published bytes with no negative
  clamping and unchanged accounting.
* **Ordinary full path** — ``--transfer-result`` (+ optional ``--bbb-result``)
  keeps its legacy default semantics and exact values.
* **Fail closed** — no/invalid input is rejected before any write.

Every test runs against throwaway temp trees (subprocess with ``cwd`` at the
temp root, or in-process) so the repository ``data/`` is never touched. All
arithmetic is asserted exactly with ``Decimal``. No network, Dune, or commits.

Critical invariant carried over into the reducer helpers: an absent value stays
empty/nil and is never silently turned into a zero.
"""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REDUCER = ROOT / "scripts" / "v3_reduce_chain.py"

BBB_WALLET = "5QsyByFVJcg7oN76Ma26KEDFQdHt1tsiVExK94zURzfd"
CUTOFF = "2026-10-03 12:46:12.000 UTC"
TRANSFER_FIELDS = [
    "event_id", "block_date", "block_time", "block_slot", "tx_id", "action",
    "from_owner", "to_owner", "from_token_account", "to_token_account",
    "amount_raw", "amount_xnet", "event_multiplicity",
]
# Derived / ledger files grouped by the domain that owns them.
TRANSFER_CANONICAL = "data/canonical/xnet_transfers.csv"
BBB_CANONICAL = "data/canonical/bbb_trades.csv"
HOLDER_FILES = (
    "data/derived/xnet_owner_balances.csv",
    "data/current/xnet_holder_state.json",
    "data/current/xnet_holder_integrity.json",
)
SUPPLY_FILES = (
    "data/xnet_supply_daily.csv",
    "data/xnet_supply_history.json",
    "data/current/xnet_supply_state.json",
)
BURN_FILES = (
    "data/derived/bbb_burn_daily.csv",
    "data/current/xnet_bbb_burn_state.json",
)
BBB_TRADE_FILES = (
    "data/derived/bbb_trades_daily.csv",
    "data/current/xnet_bbb_trade_state.json",
)


def load_reducer():
    spec = importlib.util.spec_from_file_location("v3_reduce_chain_under_test", REDUCER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dune_result(rows):
    return json.dumps({"state": "QUERY_STATE_COMPLETED", "result": {"rows": rows}})


def transfer_result_rows():
    return [
        {"tx_id": "TxTransferA", "block_date": "2026-10-04",
         "block_time": "2026-10-04 01:00:00.000 UTC", "block_slot": 452700001,
         "action": "transfer", "from_owner": "OwnerAlpha", "to_owner": "OwnerBeta",
         "from_token_account": "FromTA1", "to_token_account": "ToTA1",
         "amount_raw": "1000000001", "amount_xnet": "10.00000001",
         "event_multiplicity": "1"},
        {"tx_id": "TxMintD", "block_date": "2026-10-04",
         "block_time": "2026-10-04 02:00:00.000 UTC", "block_slot": 452700002,
         "action": "mint", "from_owner": "", "to_owner": "OwnerDelta",
         "from_token_account": "", "to_token_account": "ToTA2",
         "amount_raw": "500000000", "amount_xnet": "5",
         "event_multiplicity": "1"},
        {"tx_id": "TxBurnBBB", "block_date": "2026-10-04",
         "block_time": "2026-10-04 03:00:00.000 UTC", "block_slot": 452700003,
         "action": "burn", "from_owner": BBB_WALLET, "to_owner": "",
         "from_token_account": "FromTA3", "to_token_account": "",
         "amount_raw": "300000000", "amount_xnet": "3",
         "event_multiplicity": "1"},
    ]


def bbb_result_rows(extra=False):
    rows = [
        {"tx_id": "TxBbb1", "block_date": "2026-10-04",
         "block_time": "2026-10-04 04:00:00.000 UTC", "block_slot": 452700004,
         "trader_id": BBB_WALLET, "economic_side": "BUY_XNET",
         "gross_xnet_bought": "20", "gross_xnet_sold": "0",
         "net_xnet_change": "20", "trade_value_usd": "5",
         "primary_project": "whirlpool", "trade_source": "jupiter",
         "project_main_id": "main", "quote_symbol": "WSOL",
         "quote_mint": "mint-wsol", "decoded_xnet_leg_count": "1"},
        {"tx_id": "TxBbb2", "block_date": "2026-10-04",
         "block_time": "2026-10-04 05:00:00.000 UTC", "block_slot": 452700005,
         "trader_id": BBB_WALLET, "economic_side": "SELL_XNET",
         "gross_xnet_bought": "0", "gross_xnet_sold": "8",
         "net_xnet_change": "-8", "trade_value_usd": "2",
         "primary_project": "whirlpool", "trade_source": "jupiter",
         "project_main_id": "main", "quote_symbol": "WSOL",
         "quote_mint": "mint-wsol", "decoded_xnet_leg_count": "1"},
    ]
    if extra:
        rows.append({
            "tx_id": "TxBbb3", "block_date": "2026-10-05",
            "block_time": "2026-10-05 06:00:00.000 UTC", "block_slot": 452800006,
            "trader_id": BBB_WALLET, "economic_side": "BUY_XNET",
            "gross_xnet_bought": "4", "gross_xnet_sold": "0",
            "net_xnet_change": "4", "trade_value_usd": "1",
            "primary_project": "whirlpool", "trade_source": "jupiter",
            "project_main_id": "main", "quote_symbol": "WSOL",
            "quote_mint": "mint-wsol", "decoded_xnet_leg_count": "1",
        })
    return rows


def canonical_row(event_id, *, day="2026-10-04", time="2026-10-04 01:00:00.000 UTC",
                  slot=452700001, tx="TxTransferA", action="transfer",
                  frm="OwnerAlpha", to="OwnerBeta", amount_raw="1000000001",
                  amount_xnet="10.00000001", mult="1", fta="FromTA1", tta="ToTA1"):
    return {"event_id": event_id, "block_date": day, "block_time": time,
            "block_slot": str(slot), "tx_id": tx, "action": action,
            "from_owner": frm, "to_owner": to, "from_token_account": fta,
            "to_token_account": tta, "amount_raw": amount_raw,
            "amount_xnet": amount_xnet, "event_multiplicity": mult}


def canonical_rows():
    return [
        canonical_row("a" * 32, time="2026-10-04 01:00:00.000 UTC", slot=452700001,
                      tx="TxTransferA", action="transfer", frm="OwnerAlpha",
                      to="OwnerBeta", amount_raw="1000000001",
                      amount_xnet="10.00000001"),
        canonical_row("b" * 32, time="2026-10-04 02:00:00.000 UTC", slot=452700002,
                      tx="TxMintD", action="mint", frm="", to="OwnerDelta",
                      amount_raw="500000000", amount_xnet="5", fta="", tta="ToTA2"),
        canonical_row("c" * 32, time="2026-10-04 03:00:00.000 UTC", slot=452700003,
                      tx="TxBurnBBB", action="burn", frm=BBB_WALLET, to="",
                      amount_raw="300000000", amount_xnet="3", fta="FromTA3", tta=""),
    ]


def canonical_csv(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=TRANSFER_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return out.getvalue()


def legacy_registry():
    addresses = ["OwnerBeta"] + [f"ExcludedWallet{i:03d}" for i in range(1, 61)]
    assert len(addresses) == 61
    lines = ["address,legacy_classification,legacy_comment,v3_supply_treatment,verification_status"]
    lines += [
        f"{address},insider_lock_or_ops_pool,ops,DO_NOT_USE_UNTIL_VERIFIED,legacy_reference_only"
        for address in addresses
    ]
    return "\n".join(lines) + "\n"


def read_rows(text):
    return list(csv.DictReader(io.StringIO(text)))


class ReducerModuleTest(unittest.TestCase):
    """Loads the reducer under test; no fixture tree."""

    def setUp(self):
        self.reducer = load_reducer()


class TempTreeTest(ReducerModuleTest):
    """Adds a throwaway working tree and the config/seed fixture."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        self.old_cwd = Path.cwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, self.old_cwd)

    # --- tree helpers -----------------------------------------------------
    def write(self, rel, text):
        path = self.cwd / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def read(self, rel):
        return (self.cwd / rel).read_text()

    def exists(self, rel):
        return (self.cwd / rel).exists()

    def snapshot(self, rels):
        return {
            rel: ((self.cwd / rel).read_bytes(), (self.cwd / rel).stat().st_mtime_ns)
            if (self.cwd / rel).exists() else None
            for rel in rels
        }

    def tree_snapshot(self):
        state = {}
        for path in sorted(self.cwd.rglob("*")):
            if path.is_file():
                state[str(path.relative_to(self.cwd))] = path.read_bytes()
        return state

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(REDUCER), *args],
                              cwd=self.cwd, capture_output=True, text=True)

    # --- fixture ----------------------------------------------------------
    def build_config(self):
        # The real repository ships these output directories; the derive
        # writers open into them without creating them.
        for rel in ("data/derived", "data/current", "data/canonical"):
            (self.cwd / rel).mkdir(parents=True, exist_ok=True)
        self.write("config/legacy_wallet_registry.csv", legacy_registry())
        self.write("config/xnet_supply_checkpoint.json", json.dumps({
            "schema_version": 1, "token": "XNET", "checkpoint_date": "2026-09-24",
            "checkpoint_circulating_supply_xnet": "443533995",
        }))
        self.write("config/xnet_bbb_burn_checkpoint.json", json.dumps({
            "schema_version": 1, "checkpoint_date": "2026-10-01",
            "verified_bbb_burned_xnet": "7744133.611149261",
        }))
        self.write("config/xnet_protocol_config.json", json.dumps({
            "schema_version": 1, "published_max_supply_xnet": 1307098713,
            "primary_bbb_wallet": BBB_WALLET,
        }))
        self.write("data/xnet_owner_balances_seed_metadata.json", json.dumps({
            "schema_version": 1, "latest_balance_event_time": CUTOFF, "owner_count": 3,
        }))
        self.write("data/xnet_owner_balances_seed.csv",
                   "owner,xnet_balance\n"
                   "OwnerAlpha,100\nOwnerBeta,50\nOwnerGamma,0\n"
                   f"{BBB_WALLET},1000\n")

    def seed_supply_history(self):
        self.write("data/xnet_supply_daily.csv",
                   "day,net_flow_to_excluded_xnet,burn_xnet,circulation_change_xnet\n"
                   "2026-10-01,0,0,0\n")

    def write_transfer_result(self, rows=None):
        return self.write("transfer-result.json", dune_result(rows or transfer_result_rows()))

    def write_bbb_result(self, extra=False):
        return self.write("bbb-result.json", dune_result(bbb_result_rows(extra=extra)))


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
class PreserveDerivedParsingTests(ReducerModuleTest):
    def test_empty_choice_preserves_nothing(self):
        self.assertEqual(self.reducer.parse_preserve_derived(""), frozenset())
        self.assertEqual(self.reducer.parse_preserve_derived(None), frozenset())
        self.assertEqual(self.reducer.parse_preserve_derived(" , "), frozenset())

    def test_each_supported_domain_parses(self):
        for domain in ("holders", "supply", "burns"):
            self.assertEqual(self.reducer.parse_preserve_derived(domain), frozenset({domain}))

    def test_combined_choice_parses_as_a_set(self):
        chosen = self.reducer.parse_preserve_derived("holders, supply ,burns")
        self.assertEqual(chosen, frozenset({"holders", "supply", "burns"}))

    def test_unknown_domain_fails_closed_with_the_choices(self):
        with self.assertRaisesRegex(RuntimeError, "holders, supply, burns"):
            self.reducer.parse_preserve_derived("holders,international")


class RequestResolutionTests(ReducerModuleTest):
    def resolve(self, transfer=None, bbb=None, rebuild=False):
        return self.reducer.resolve_reduction_request(
            transfer_result=transfer, bbb_result=bbb, rebuild_canonical=rebuild)

    def test_no_input_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "At least one of"):
            self.resolve()

    def test_bbb_only_is_a_first_class_request(self):
        self.assertEqual(self.resolve(bbb="b.json"),
                         {"transfer": False, "bbb": True, "rebuild": False})

    def test_transfer_only_and_rebuild_only_are_valid(self):
        self.assertTrue(self.resolve(transfer="t.json")["transfer"])
        self.assertTrue(self.resolve(rebuild=True)["rebuild"])

    def test_transfer_with_rebuild_is_mutually_exclusive(self):
        with self.assertRaisesRegex(RuntimeError, "mutually"):
            self.resolve(transfer="t.json", rebuild=True)

    def test_rebuild_may_be_combined_with_a_bbb_result(self):
        self.assertEqual(self.resolve(bbb="b.json", rebuild=True),
                         {"transfer": False, "bbb": True, "rebuild": True})


class CanonicalReadTests(TempTreeTest):
    def test_stored_event_ids_are_returned_verbatim(self):
        rows = canonical_rows()
        self.write(TRANSFER_CANONICAL, canonical_csv(rows))
        loaded = self.reducer.read_canonical_transfers(Path(TRANSFER_CANONICAL))
        self.assertEqual([r["event_id"] for r in loaded], ["a" * 32, "b" * 32, "c" * 32])
        for stored, original in zip(loaded, rows):
            self.assertEqual(stored, {field: str(original[field]) for field in TRANSFER_FIELDS})

    def test_instruction_ids_are_not_rehashed_to_legacy_ids(self):
        # The scoped repair module mints instruction-based ids; the legacy
        # amount-based identity must not be applied to them.
        rows = canonical_rows()
        self.write(TRANSFER_CANONICAL, canonical_csv(rows))
        loaded = self.reducer.read_canonical_transfers(Path(TRANSFER_CANONICAL))
        for stored, original in zip(loaded, rows):
            self.assertNotEqual(stored["event_id"],
                                self.reducer.transfer_event_id(original))
            self.assertEqual(stored["event_id"], original["event_id"])

    def test_absent_values_stay_nil_and_are_never_zeroed(self):
        rows = [canonical_row("d" * 32, amount_raw="", amount_xnet="")]
        self.write(TRANSFER_CANONICAL, canonical_csv(rows))
        loaded = self.reducer.read_canonical_transfers(Path(TRANSFER_CANONICAL))
        self.assertEqual(loaded[0]["amount_raw"], "")
        self.assertEqual(loaded[0]["amount_xnet"], "")

    def test_non_legacy_schema_is_refused(self):
        short = canonical_csv(canonical_rows()).splitlines()
        self.write(TRANSFER_CANONICAL, "\n".join(
            [",".join(TRANSFER_FIELDS[:-1])] + [",".join(line.split(",")[:-1]) for line in short[1:]]
        ) + "\n")
        with self.assertRaisesRegex(RuntimeError, "13-field legacy schema"):
            self.reducer.read_canonical_transfers(Path(TRANSFER_CANONICAL))

    def test_missing_event_id_is_refused(self):
        rows = [canonical_row("", time="2026-10-04 01:00:00.000 UTC")]
        self.write(TRANSFER_CANONICAL, canonical_csv(rows))
        with self.assertRaisesRegex(RuntimeError, "missing a stable event_id"):
            self.reducer.read_canonical_transfers(Path(TRANSFER_CANONICAL))

    def test_duplicate_event_id_is_refused(self):
        rows = [canonical_row("e" * 32), canonical_row("e" * 32)]
        self.write(TRANSFER_CANONICAL, canonical_csv(rows))
        with self.assertRaisesRegex(RuntimeError, "Duplicate canonical event_id"):
            self.reducer.read_canonical_transfers(Path(TRANSFER_CANONICAL))

    def test_missing_file_and_empty_file_are_refused(self):
        with self.assertRaisesRegex(RuntimeError, "not found"):
            self.reducer.read_canonical_transfers(Path(TRANSFER_CANONICAL))
        self.write(TRANSFER_CANONICAL, ",".join(TRANSFER_FIELDS) + "\n")
        with self.assertRaisesRegex(RuntimeError, "no rows"):
            self.reducer.read_canonical_transfers(Path(TRANSFER_CANONICAL))


# ---------------------------------------------------------------------------
# BBB-only domain
# ---------------------------------------------------------------------------
class BbbOnlyDomainTests(TempTreeTest):
    def setUp(self):
        super().setUp()
        self.build_config()

    def test_bbb_only_does_not_require_a_transfer_result(self):
        self.write_bbb_result()
        result = self.run_cli("--bbb-result", "bbb-result.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Canonical BBB trade rows: 2", result.stdout)
        self.assertNotIn("Canonical transfer rows", result.stdout)

    def test_bbb_only_never_creates_transfer_owned_files(self):
        self.write_bbb_result()
        result = self.run_cli("--bbb-result", "bbb-result.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        for rel in (TRANSFER_CANONICAL, *HOLDER_FILES, *SUPPLY_FILES, *BURN_FILES):
            self.assertFalse(self.exists(rel), f"BBB-only run touched {rel}")
        for rel in (BBB_CANONICAL, *BBB_TRADE_FILES):
            self.assertTrue(self.exists(rel), f"BBB-only run missed {rel}")

    def test_bbb_only_leaves_all_unrelated_bytes_and_mtimes_untouched(self):
        self.write_transfer_result()
        self.write_bbb_result()
        full = self.run_cli("--transfer-result", "transfer-result.json",
                            "--bbb-result", "bbb-result.json")
        self.assertEqual(full.returncode, 0, full.stderr)

        untouched = (TRANSFER_CANONICAL, *HOLDER_FILES, *SUPPLY_FILES, *BURN_FILES)
        before = self.snapshot(untouched)
        canonical_before = self.read(BBB_CANONICAL)

        # A later BBB-only run with one more trade must not disturb anything else.
        self.write_bbb_result(extra=True)
        result = self.run_cli("--bbb-result", "bbb-result.json")
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertEqual(self.snapshot(untouched), before,
                         "BBB-only run changed an unrelated file or its mtime")
        self.assertNotEqual(self.read(BBB_CANONICAL), canonical_before,
                            "BBB-only run did not merge the BBB canonical")
        self.assertEqual(len(read_rows(self.read(BBB_CANONICAL))), 3)

    def test_bbb_only_math(self):
        self.write_bbb_result()
        result = self.run_cli("--bbb-result", "bbb-result.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        daily = {r["day"]: r for r in read_rows(self.read("data/derived/bbb_trades_daily.csv"))}
        self.assertEqual(daily["2026-10-04"], {
            "day": "2026-10-04", "bbb_buy_count": "1", "bbb_sell_count": "1",
            "gross_xnet_bought": "20", "gross_xnet_sold": "8", "net_xnet_change": "12",
            "bbb_buy_value_usd": "5", "bbb_sell_value_usd": "2", "trade_value_usd": "7",
        })
        state = json.loads(self.read("data/current/xnet_bbb_trade_state.json"))
        self.assertEqual(state["canonical_transaction_count"], 2)
        self.assertEqual(state["net_xnet_change"], "12")
        self.assertEqual(state["trade_value_usd"], "7")
        self.assertEqual(state["latest_trade_utc"], "2026-10-04 05:00:00.000 UTC")


# ---------------------------------------------------------------------------
# rebuild-canonical
# ---------------------------------------------------------------------------
class RebuildCanonicalTests(TempTreeTest):
    def setUp(self):
        super().setUp()
        self.build_config()

    def test_rebuild_preserves_canonical_bytes_and_stored_ids(self):
        original = canonical_csv(canonical_rows())
        self.write(TRANSFER_CANONICAL, original)
        digest = hashlib.sha256(original.encode()).hexdigest()

        result = self.run_cli("--rebuild-canonical")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read(TRANSFER_CANONICAL), original,
                         "rebuild rewrote the canonical transfer file")
        self.assertEqual(hashlib.sha256(self.read(TRANSFER_CANONICAL).encode()).hexdigest(), digest)

    def test_rebuild_keeps_the_thirteen_field_legacy_schema(self):
        self.write(TRANSFER_CANONICAL, canonical_csv(canonical_rows()))
        result = self.run_cli("--rebuild-canonical")
        self.assertEqual(result.returncode, 0, result.stderr)
        header = self.read(TRANSFER_CANONICAL).splitlines()[0].split(",")
        self.assertEqual(len(header), 13)
        self.assertEqual(header, TRANSFER_FIELDS)

    def test_rebuild_derives_the_same_values_as_the_fresh_transfer_path(self):
        # Establish the world with the ordinary transfer path.
        self.write_transfer_result()
        full = self.run_cli("--transfer-result", "transfer-result.json")
        self.assertEqual(full.returncode, 0, full.stderr)
        fresh = {rel: self.read(rel) for rel in (HOLDER_FILES[0], HOLDER_FILES[1],
                                                 *SUPPLY_FILES, *BURN_FILES)}

        # Now rebuild from the already-validated canonical CSV only.
        result = self.run_cli("--rebuild-canonical")
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertEqual(self.read(HOLDER_FILES[0]), fresh[HOLDER_FILES[0]])
        self.assertEqual(self.read(HOLDER_FILES[1]), fresh[HOLDER_FILES[1]])
        for rel in SUPPLY_FILES:
            self.assertEqual(self.read(rel), fresh[rel], rel)
        for rel in BURN_FILES:
            self.assertEqual(self.read(rel), fresh[rel], rel)

    def test_rebuild_math_is_exact(self):
        self.seed_supply_history()
        self.write(TRANSFER_CANONICAL, canonical_csv(canonical_rows()))
        result = self.run_cli("--rebuild-canonical")
        self.assertEqual(result.returncode, 0, result.stderr)

        amount = Decimal(1000000001) / Decimal(100000000)
        balances = {r["owner"]: r["xnet_balance"]
                    for r in read_rows(self.read(HOLDER_FILES[0]))}
        self.assertEqual(balances["OwnerAlpha"], format(Decimal(100) - amount, "f"))
        self.assertEqual(balances["OwnerBeta"], format(Decimal(50) + amount, "f"))
        self.assertEqual(balances["OwnerDelta"], "5")
        self.assertEqual(balances[BBB_WALLET], format(Decimal(1000) - Decimal(3), "f"))

        state = json.loads(self.read(HOLDER_FILES[1]))
        self.assertEqual(state["positive_holder_count"], 4)
        self.assertEqual(state["holders_created_since_seed"], 1)
        self.assertEqual(state["holders_zeroed_since_seed"], 0)
        self.assertEqual(state["transfer_events_applied"], 3)
        self.assertEqual(
            state["outstanding_balance_sum_xnet"],
            format((Decimal(100) - amount) + (Decimal(50) + amount) + Decimal(5)
                   + (Decimal(1000) - Decimal(3)), "f"))

        supply = {r["day"]: r for r in read_rows(self.read(SUPPLY_FILES[0]))}
        self.assertEqual(supply["2026-10-04"]["net_flow_to_excluded_xnet"], format(amount, "f"))
        self.assertEqual(supply["2026-10-04"]["burn_xnet"], "3")
        self.assertEqual(supply["2026-10-04"]["circulation_change_xnet"], format(-(amount + 3), "f"))
        self.assertIn("2026-10-01", supply, "pre-rebuild supply history must be preserved")
        supply_state = json.loads(self.read(SUPPLY_FILES[2]))
        self.assertEqual(supply_state["latest_circulating_supply_xnet"],
                         format(Decimal(443533995) - (amount + 3), "f"))

        burns = read_rows(self.read(BURN_FILES[0]))
        self.assertEqual(burns, [{"day": "2026-10-04", "verified_bbb_burn_xnet": "3"}])
        burn_state = json.loads(self.read(BURN_FILES[1]))
        self.assertEqual(burn_state["verified_bbb_burned_xnet"],
                         format(Decimal("7744133.611149261") + 3, "f"))

    def test_rebuild_does_not_write_the_bbb_domain(self):
        self.write(TRANSFER_CANONICAL, canonical_csv(canonical_rows()))
        result = self.run_cli("--rebuild-canonical")
        self.assertEqual(result.returncode, 0, result.stderr)
        for rel in (BBB_CANONICAL, *BBB_TRADE_FILES):
            self.assertFalse(self.exists(rel), f"transfer-only rebuild wrote {rel}")


# ---------------------------------------------------------------------------
# preserve-derived
# ---------------------------------------------------------------------------
class PreserveDerivedTests(TempTreeTest):
    def setUp(self):
        super().setUp()
        self.build_config()

    def establish_healthy_state(self):
        self.write_transfer_result()
        result = self.run_cli("--transfer-result", "transfer-result.json")
        self.assertEqual(result.returncode, 0, result.stderr)

    def negative_burn_csv(self):
        return canonical_csv([
            canonical_row("f" * 32, time="2026-10-04 06:00:00.000 UTC",
                          slot=452700009, tx="TxBigBurn", action="burn",
                          frm="OwnerAlpha", to="", amount_raw="20000000000",
                          amount_xnet="200", fta="FromTA9", tta=""),
        ])

    def test_preserve_holders_keeps_bytes_and_skips_clamping(self):
        self.establish_healthy_state()
        before = self.snapshot(HOLDER_FILES)

        # A material negative burn would otherwise raise / be clamped.
        self.write(TRANSFER_CANONICAL, self.negative_burn_csv())
        result = self.run_cli("--rebuild-canonical", "--preserve-derived", "holders")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(HOLDER_FILES), before,
                         "preserved holder domain changed bytes or mtime")

        # Supply and burns are still recomputed with the new burn.
        supply_state = json.loads(self.read(SUPPLY_FILES[2]))
        self.assertEqual(supply_state["latest_circulating_supply_xnet"],
                         format(Decimal(443533995) - 200, "f"))
        burn_state = json.loads(self.read(BURN_FILES[1]))
        self.assertEqual(burn_state["verified_bbb_burned_xnet"],
                         format(Decimal("7744133.611149261")))

    def test_material_negative_is_never_clamped_when_holders_are_rebuilt(self):
        self.establish_healthy_state()
        healthy = self.read(HOLDER_FILES[0])
        self.write(TRANSFER_CANONICAL, self.negative_burn_csv())
        result = self.run_cli("--rebuild-canonical")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Negative holder balance", result.stderr)
        # Fail closed rather than publish a clamped balance.
        self.assertEqual(self.read(HOLDER_FILES[0]), healthy,
                         "a material negative must not be clamped or published")

    def test_preserve_supply_keeps_supply_bytes_only(self):
        self.establish_healthy_state()
        before = self.snapshot(SUPPLY_FILES)
        holder_before = self.snapshot(HOLDER_FILES)

        self.write(TRANSFER_CANONICAL, self.negative_burn_csv())
        result = self.run_cli("--rebuild-canonical", "--preserve-derived", "supply,holders")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(SUPPLY_FILES), before)
        self.assertEqual(self.snapshot(HOLDER_FILES), holder_before)
        burn_state = json.loads(self.read(BURN_FILES[1]))
        self.assertEqual(burn_state["verified_bbb_burned_xnet"],
                         format(Decimal("7744133.611149261")))

    def test_preserve_burns_keeps_burn_bytes_and_rebuilds_the_rest(self):
        self.establish_healthy_state()
        self.write(TRANSFER_CANONICAL, canonical_csv(canonical_rows()))
        burn_before = self.snapshot(BURN_FILES)

        result = self.run_cli("--rebuild-canonical", "--preserve-derived", "burns")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(BURN_FILES), burn_before,
                         "preserved burn domain changed")
        # Supply was still rebuilt.
        self.assertTrue(self.exists(SUPPLY_FILES[2]))

    def test_preserve_all_domains_writes_nothing_on_rebuild(self):
        self.establish_healthy_state()
        tracked = (TRANSFER_CANONICAL, *HOLDER_FILES, *SUPPLY_FILES, *BURN_FILES)
        before = self.snapshot(tracked)
        result = self.run_cli("--rebuild-canonical", "--preserve-derived",
                              "holders,supply,burns")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(tracked), before)


# ---------------------------------------------------------------------------
# invalid requests reject before any write
# ---------------------------------------------------------------------------
class InvalidRequestTests(TempTreeTest):
    def setUp(self):
        super().setUp()
        self.build_config()

    def assert_no_writes(self, before, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.tree_snapshot(), before,
                         "an invalid request wrote or changed files")

    def test_no_input_is_rejected_before_writes(self):
        before = self.tree_snapshot()
        result = self.run_cli()
        self.assert_no_writes(before, result)
        self.assertIn("At least one of", result.stderr)

    def test_unknown_preserve_domain_is_rejected_before_writes(self):
        self.write_transfer_result()
        before = self.tree_snapshot()
        result = self.run_cli("--transfer-result", "transfer-result.json",
                              "--preserve-derived", "holders,international")
        self.assert_no_writes(before, result)
        self.assertIn("Unknown --preserve-derived domain", result.stderr)

    def test_rebuild_without_canonical_csv_is_rejected_before_writes(self):
        before = self.tree_snapshot()
        result = self.run_cli("--rebuild-canonical")
        self.assert_no_writes(before, result)
        self.assertIn("not found", result.stderr)

    def test_incomplete_transfer_result_is_rejected_before_writes(self):
        self.write("pending.json", json.dumps({"state": "QUERY_STATE_RUNNING",
                                               "result": {"rows": []}}))
        before = self.tree_snapshot()
        result = self.run_cli("--transfer-result", "pending.json")
        self.assert_no_writes(before, result)
        self.assertIn("not completed", result.stderr)

    def test_transfer_and_rebuild_together_are_rejected_before_writes(self):
        self.write_transfer_result()
        self.write(TRANSFER_CANONICAL, canonical_csv(canonical_rows()))
        before = self.tree_snapshot()
        result = self.run_cli("--transfer-result", "transfer-result.json",
                              "--rebuild-canonical")
        self.assert_no_writes(before, result)
        self.assertIn("mutually exclusive", result.stderr)


# ---------------------------------------------------------------------------
# ordinary full path
# ---------------------------------------------------------------------------
class FullPathRegressionTests(TempTreeTest):
    def setUp(self):
        super().setUp()
        self.build_config()
        # October 1 precedes the canonical rebuild boundary (October 2).
        # Its existing published row must be preserved, not manufactured.
        self.seed_supply_history()

    def full_run(self):
        self.write_transfer_result()
        self.write_bbb_result()
        return self.run_cli("--transfer-result", "transfer-result.json",
                            "--bbb-result", "bbb-result.json",
                            "--allow-stale-holders")

    def test_legacy_invocation_is_accepted_and_writes_the_legacy_output_set(self):
        result = self.full_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        for rel in (TRANSFER_CANONICAL, BBB_CANONICAL, *HOLDER_FILES, *SUPPLY_FILES,
                    *BURN_FILES, *BBB_TRADE_FILES):
            self.assertTrue(self.exists(rel), f"full run missed {rel}")
        self.assertIn("Canonical transfer rows: 3", result.stdout)
        self.assertIn("Canonical BBB trade rows: 2", result.stdout)

    def test_full_path_values_are_exact(self):
        self.assertEqual(self.full_run().returncode, 0)
        amount = Decimal(1000000001) / Decimal(100000000)

        balances = [(r["owner"], r["xnet_balance"])
                    for r in read_rows(self.read(HOLDER_FILES[0]))]
        self.assertEqual(balances, [
            (BBB_WALLET, format(Decimal(1000) - Decimal(3), "f")),
            ("OwnerAlpha", format(Decimal(100) - amount, "f")),
            ("OwnerBeta", format(Decimal(50) + amount, "f")),
            ("OwnerDelta", "5"),
        ])
        self.assertNotIn("OwnerGamma", dict(balances))

        holder_state = json.loads(self.read(HOLDER_FILES[1]))
        self.assertEqual(holder_state["transfer_events_applied"], 3)
        self.assertEqual(holder_state["latest_transfer_event_utc"],
                         "2026-10-04 03:00:00.000 UTC")

        supply = {r["day"]: r for r in read_rows(self.read(SUPPLY_FILES[0]))}
        self.assertEqual(supply["2026-10-04"]["circulation_change_xnet"],
                         format(-(amount + 3), "f"))
        self.assertEqual(supply["2026-10-01"],
                         {"day": "2026-10-01", "net_flow_to_excluded_xnet": "0",
                          "burn_xnet": "0", "circulation_change_xnet": "0"})

        burns = read_rows(self.read(BURN_FILES[0]))
        self.assertEqual(burns, [{"day": "2026-10-04", "verified_bbb_burn_xnet": "3"}])

        trades = {r["day"]: r for r in read_rows(self.read(BBB_TRADE_FILES[0]))}
        self.assertEqual(trades["2026-10-04"]["net_xnet_change"], "12")
        self.assertEqual(trades["2026-10-04"]["trade_value_usd"], "7")

    def test_full_path_is_deterministic_across_runs(self):
        self.assertEqual(self.full_run().returncode, 0)
        first = {rel: self.read(rel) for rel in
                 (TRANSFER_CANONICAL, BBB_CANONICAL, HOLDER_FILES[0], HOLDER_FILES[1],
                  BURN_FILES[0], BURN_FILES[1], *BBB_TRADE_FILES)}

        self.assertEqual(self.full_run().returncode, 0)
        for rel, text in first.items():
            self.assertEqual(self.read(rel), text, f"{rel} was not deterministic")
        # The holder-integrity clock is the one non-deterministic field; all others must match.
        self.assertNotIn("checked_at_utc", first)
        integrity = json.loads(self.read(HOLDER_FILES[2]))
        self.assertIn("checked_at_utc", integrity)
        # Supply is today-bounded; its event-day content must still be stable.
        supply = {r["day"]: r for r in read_rows(self.read(SUPPLY_FILES[0]))}
        self.assertEqual(supply["2026-10-04"]["circulation_change_xnet"],
                         format(-(Decimal(1000000001) / Decimal(100000000) + 3), "f"))


if __name__ == "__main__":
    unittest.main()
