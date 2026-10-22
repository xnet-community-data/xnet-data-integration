"""Adversarial offline tests for additive-day validation; fake rows only."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from v3_additive_day import AdditiveDayRejected, validate_additive_day
from v3_reduce_chain import normalise_transfer


DAY = "2026-10-14"
SQL = "a" * 64
BASE = {
    "block_date": DAY,
    "block_time": "2026-10-14 00:14:25.000 UTC",
    "block_slot": 456635786,
    "tx_id": "tx-1",
    "action": "transfer",
    "from_owner": "owner-a",
    "to_owner": "owner-b",
    "from_token_account": "account-a",
    "to_token_account": "account-b",
    "amount_raw": "100000000",
    "amount_xnet": 1.0,
    "event_multiplicity": 1,
}


def provider(**overrides):
    row = deepcopy(BASE)
    row.update(overrides)
    return row


def old(**overrides):
    row = provider(**overrides)
    if "amount_xnet" not in overrides:
        row["amount_xnet"] = format(
            Decimal(str(row["amount_raw"])) / Decimal(100000000), "f"
        )
    return normalise_transfer(row)


def call(canonical, rows, day=DAY, **overrides):
    return validate_additive_day(
        canonical, rows, day,
        execution_id=overrides.pop("execution_id", "execution-1"),
        sql_sha256=overrides.pop("sql_sha256", SQL),
        **overrides,
    )


class AdditiveDayTests(unittest.TestCase):
    def test_happy_path_returns_only_sorted_additions_and_auditable_receipt(self):
        existing = old()
        other_day = old(
            block_date="2026-10-13",
            block_time="2026-10-13 23:59:59.000 UTC",
            tx_id="tx-prior",
        )
        later = provider(
            block_time="2026-10-14 00:30:00.000 UTC",
            tx_id="tx-later",
            from_token_account="account-c",
            to_token_account="account-d",
            amount_raw="250000000",
        )
        earlier = provider(
            block_time="2026-10-14 00:05:00.000 UTC",
            tx_id="tx-earlier",
            from_token_account="account-e",
            to_token_account="account-f",
            amount_raw="200000000",
        )
        canonical = [other_day, existing]
        rows = [later, provider(), earlier]
        canonical_before = deepcopy(canonical)
        rows_before = deepcopy(rows)

        result = call(canonical, rows)
        additions = result["additions"]
        receipt = result["receipt"]

        self.assertEqual([row["tx_id"] for row in additions], ["tx-earlier", "tx-later"])
        self.assertNotIn(existing["event_id"], [row["event_id"] for row in additions])
        self.assertEqual(additions[0]["amount_xnet"], "2")
        self.assertEqual(additions[1]["amount_xnet"], "2.5")
        self.assertEqual(receipt["canonical_day_row_count"], 1)
        self.assertEqual(receipt["provider_row_count"], 3)
        self.assertEqual(receipt["matched_old_rows"], 1)
        self.assertEqual(receipt["added_rows"], 2)
        self.assertEqual(receipt["deleted_rows"], 0)
        self.assertEqual(receipt["absolute_day"], DAY)
        self.assertEqual(receipt["scope_start_utc"], DAY + "T00:00:00Z")
        self.assertEqual(receipt["scope_end_utc"], "2026-10-15T00:00:00Z")
        self.assertEqual(receipt["execution_id"], "execution-1")
        self.assertEqual(receipt["sql_sha256"], SQL)
        self.assertIs(receipt["financial_authority"], False)
        self.assertEqual(
            receipt["addition_event_ids"],
            [row["event_id"] for row in additions],
        )
        for field in (
            "canonical_day_semantics_sha256",
            "normalized_provider_semantics_sha256",
            "addition_event_ids_sha256",
        ):
            self.assertEqual(len(receipt[field]), 64)
        self.assertEqual(canonical, canonical_before)
        self.assertEqual(rows, rows_before)

    def test_receipt_hashes_are_order_independent(self):
        existing = old()
        first = provider(
            block_time="2026-10-14 00:05:00.000 UTC",
            tx_id="tx-new-1",
            from_token_account="account-c",
            to_token_account="account-d",
        )
        second = provider(
            block_time="2026-10-14 00:06:00.000 UTC",
            tx_id="tx-new-2",
            from_token_account="account-e",
            to_token_account="account-f",
        )
        one = call([existing], [provider(), first, second])["receipt"]
        two = call([existing], [second, first, provider()])["receipt"]
        self.assertEqual(one["canonical_day_semantics_sha256"],
                         two["canonical_day_semantics_sha256"])
        self.assertEqual(one["normalized_provider_semantics_sha256"],
                         two["normalized_provider_semantics_sha256"])
        self.assertEqual(one["addition_event_ids_sha256"],
                         two["addition_event_ids_sha256"])

    def test_zero_provider_rows_only_accepted_when_existing_day_is_empty(self):
        result = call([old(block_date="2026-10-13", block_time="2026-10-13 00:00:00.000 UTC")],
                      [])
        self.assertEqual(result["additions"], [])
        self.assertEqual(result["receipt"]["provider_row_count"], 0)
        self.assertEqual(result["receipt"]["added_rows"], 0)
        self.assertEqual(result["receipt"]["deleted_rows"], 0)

        with self.assertRaisesRegex(AdditiveDayRejected, "omits or changes"):
            call([old()], [])

    def test_partial_provider_that_omits_an_old_row_is_rejected(self):
        new = provider(
            block_time="2026-10-14 00:20:00.000 UTC",
            tx_id="tx-new",
            from_token_account="account-c",
            to_token_account="account-d",
        )
        with self.assertRaisesRegex(AdditiveDayRejected, "omits or changes"):
            call([old()], [new])

    def test_changed_owner_with_same_event_id_is_rejected(self):
        existing = old()
        changed = provider(from_owner="owner-other")
        self.assertEqual(normalise_transfer(changed)["event_id"], existing["event_id"])
        with self.assertRaisesRegex(AdditiveDayRejected, "omits or changes"):
            call([existing], [changed])

    def test_changed_amount_is_rejected_even_though_the_old_identity_disappears(self):
        existing = old()
        changed = provider(amount_raw="200000000")
        self.assertNotEqual(normalise_transfer(changed)["event_id"], existing["event_id"])
        with self.assertRaisesRegex(AdditiveDayRejected, "omits or changes"):
            call([existing], [changed])

    def test_changed_time_slot_or_multiplicity_is_rejected(self):
        existing = old()
        for field, value in (
            ("block_time", "2026-10-14 00:14:26.000 UTC"),
            ("block_slot", 456635787),
            ("event_multiplicity", 2),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                    AdditiveDayRejected, "omits or changes"):
                call([existing], [provider(**{field: value})])

    def test_day_and_timestamp_scope_are_strict(self):
        existing = old()
        bad_rows = [
            provider(block_date="2026-10-13"),
            provider(block_time="2026-10-13 23:59:59.000 UTC"),
            provider(block_time="2026-10-14T00:14:25"),
            provider(block_time="2026-10-14T00:14:25+01:00"),
            provider(block_time="2026-10-14"),
            provider(block_time=" 2026-10-14 00:14:25.000 UTC"),
        ]
        for row in bad_rows:
            with self.subTest(row=row), self.assertRaises(AdditiveDayRejected):
                call([existing], [row])
            with self.subTest(row=row, empty_day=True), self.assertRaises(
                    AdditiveDayRejected):
                call([], [row])

    def test_integer_fields_reject_bool_float_exponent_leading_zero_and_sign(self):
        existing = old()
        for field in ("amount_raw", "block_slot", "event_multiplicity"):
            for value in (True, 1.0, "1e3", "01", " 1", "-1", "+1", ""):
                with self.subTest(field=field, value=value), self.assertRaises(
                        AdditiveDayRejected):
                    call([existing], [provider(**{field: value})])
        self.assertEqual(
            call([], [provider(amount_raw="0")])["receipt"]["added_rows"],
            1,
        )
        with self.assertRaises(AdditiveDayRejected):
            call([], [provider(event_multiplicity=0)])

    def test_provider_amount_xnet_is_derived_and_never_trusted(self):
        existing = old(amount_xnet="1.00000000")
        new = provider(
            block_time="2026-10-14 00:20:00.000 UTC",
            tx_id="tx-new",
            from_token_account="account-c",
            to_token_account="account-d",
            amount_raw="123456789",
            amount_xnet="999.0",
        )
        result = call([existing], [provider(amount_xnet="999.0"), new])
        self.assertEqual(result["additions"][0]["amount_xnet"], "1.23456789")

    def test_wrong_canonical_amount_value_is_not_a_rendering_difference(self):
        existing = old(amount_xnet="2")
        with self.assertRaisesRegex(AdditiveDayRejected, "does not match amount_raw"):
            call([existing], [provider()])
        nonfinite = old(amount_xnet=float("nan"))
        with self.assertRaisesRegex(AdditiveDayRejected, "does not match amount_raw"):
            call([nonfinite], [provider()])

    def test_duplicate_old_event_ids_are_rejected(self):
        existing = old()
        with self.assertRaisesRegex(AdditiveDayRejected, "duplicate event_id"):
            call([existing, deepcopy(existing)], [provider(), provider()])

    def test_new_event_id_must_not_collide_with_another_canonical_day(self):
        other_day = old(
            block_date="2026-10-13",
            block_time="2026-10-13 00:00:00.000 UTC",
        )
        with self.assertRaisesRegex(AdditiveDayRejected, "collides"):
            call([other_day], [provider()])

    def test_existing_multi_variant_locator_is_preserved_unchanged(self):
        first = old(amount_raw="100000000")
        second = old(amount_raw="200000000")
        result = call(
            [first, second],
            [provider(amount_raw="200000000"), provider(amount_raw="100000000")],
        )
        self.assertEqual(result["additions"], [])
        self.assertEqual(result["receipt"]["matched_old_rows"], 2)
        self.assertEqual(result["receipt"]["deleted_rows"], 0)

    def test_new_multi_variant_locator_requires_instruction_proof(self):
        first = provider(amount_raw="100000000")
        second = provider(amount_raw="200000000")
        with self.assertRaisesRegex(AdditiveDayRejected, "instruction proof"):
            call([], [first, second])

    def test_new_row_reusing_an_existing_locator_requires_instruction_proof(self):
        existing = old()
        new_variant = provider(amount_raw="200000000")
        with self.assertRaisesRegex(AdditiveDayRejected, "instruction proof"):
            call([existing], [provider(), new_variant])

    def test_new_single_locator_is_additive(self):
        result = call([], [provider()])
        self.assertEqual(result["receipt"]["added_rows"], 1)
        self.assertEqual(result["additions"][0]["event_id"],
                         normalise_transfer(provider())["event_id"])

    def test_seed_cutoff_rejects_the_seeded_day(self):
        with self.assertRaisesRegex(AdditiveDayRejected, "seeded day"):
            call([], [], seed_cutoff=DAY)
        later = provider(
            block_date="2026-10-15",
            block_time="2026-10-15 00:00:00.000 UTC",
            tx_id="tx-later-day",
        )
        result = call([], [later], day="2026-10-15", seed_cutoff=DAY)
        self.assertEqual(result["receipt"]["added_rows"], 1)

    def test_execution_and_sql_identity_are_required(self):
        for kwargs in (
            {"execution_id": ""},
            {"execution_id": " execution-1"},
            {"sql_sha256": "bad"},
            {"sql_sha256": "A" * 64},
            {"sql_sha256": "a" * 63},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(AdditiveDayRejected):
                call([], [], **kwargs)

    def test_action_owner_and_account_requirements(self):
        for row in (
            provider(action="burn", from_owner=""),
            provider(action="burn", from_token_account=""),
            provider(action="mint", to_owner=""),
            provider(action="mint", to_token_account=""),
            provider(from_owner=""),
            provider(to_token_account=""),
            provider(action="unknown"),
        ):
            with self.subTest(row=row), self.assertRaises(AdditiveDayRejected):
                call([], [row])
        self.assertEqual(
            call([], [provider(action="burn", to_owner="", to_token_account="")])
            ["receipt"]["added_rows"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
