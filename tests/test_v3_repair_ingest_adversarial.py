"""Adversarial review of the two repair-ingest cores (READ-ONLY).

Scope under review:
  * ``scripts/v3_missing_day_repair.py`` -- appends exactly one absent day;
  * ``scripts/v3_rpc_transfer_proof.py`` -- derives legs from finalized
    ``jsonParsed`` transaction captures.

The suite attacks the properties the rollout depends on:

* the missing-day admission boundary is pinned to query 8894038 v4, the
  approved SQL fingerprint, one execution and the 2026-10-14 artifact;
* a day is accepted only when absent, complete (198 rows / totalRowCount),
  in scope and free of invented/missing legs, owners, fields or sides;
* the trusted receipt is an external boundary, never self-authorisation or a
  default inside either module;
* RPC rows come only from decoded relevant SPL instructions whose per-account
  flow reconciles with finalized pre/post token balances; inner instructions
  are mandatory and balance deltas alone can never create legs.

The high-priority Token-2022 gap found here (real XNET accounts/instructions
use ``TokenzQd...`` while the parser initially scanned only ``Tokenkeg...``) is
fixed in the reviewed tree; ``test_real_xnet_token2022_burn_must_be_derived``
is its green regression.

The six medium defects found by this review are now fixed and their former
characterisations are ordinary green regression gates (never
``expectedFailure``):

* malformed RPC captures and empty/blank signatures raise ``ProofRejected``
  instead of leaking Python errors or an empty ``tx_id``;
* missing-day rejects a null execution id, whitespace/unpadded identity fields
  and a predecessor row whose ``block_time`` falls on the repaired day, and
  raises ``MissingDayRejected`` for malformed source shapes.

No fixture is modified; all captures are deep copies. No Dune, HTTP, GitHub,
commits or production edits.
"""
from collections import Counter, defaultdict
from copy import deepcopy
import csv
from decimal import Decimal
import io
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v3_missing_day_repair import (  # noqa: E402
    MissingDayRejected,
    accept_missing_day,
    digest,
)
from v3_reduce_chain import TRANSFER_FIELDS, normalise_transfer  # noqa: E402
from v3_rpc_transfer_proof import (  # noqa: E402
    MINT,
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    ProofRejected,
    derive_transaction,
)

FIXTURES = Path(__file__).parent / "fixtures"
RESULT_BYTES = (FIXTURES / "dune_repair_20261014.json").read_bytes()
RECEIPT = json.loads((FIXTURES / "dune_repair_20261014_receipt.json").read_text())
APPROVED_SQL = (FIXTURES / "approved_repair_20261014.sql").read_bytes()
RPC_FIXTURE = json.loads((FIXTURES / "bbb_rpc_repair_20261014_16.json").read_text())
APPROVED_EXECUTION = "01M5B0JH2659VTCAGS9EBB1T4P"
APPROVED_DAY = "2026-10-14"

EXPECTED_SOURCE_FIELDS = set(TRANSFER_FIELDS) - {"event_id"}


# ---------------------------------------------------------------------------
# missing-day helpers
# ---------------------------------------------------------------------------
def render(rows):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=TRANSFER_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode()


def prior_row():
    row = normalise_transfer(json.loads(RESULT_BYTES)["data"]["rows"][0])
    row.update(block_date="2026-10-13", block_time="2026-10-13T00:00:00Z",
               event_id="prior")
    return row


PREDECESSOR = render([prior_row()])


def trusted_for(canonical=PREDECESSOR, result=RESULT_BYTES, **overrides):
    trusted = dict(RECEIPT)
    trusted.update(canonical_sha256=digest(canonical),
                   result_sha256=digest(result))
    trusted.update(overrides)
    return trusted


def accept(canonical=PREDECESSOR, result=RESULT_BYTES, **overrides):
    return accept_missing_day(canonical, result,
                              trusted_for(canonical, result, **overrides))


def mutated_result(mutator):
    data = json.loads(RESULT_BYTES)
    mutator(data["data"]["rows"])
    return json.dumps(data).encode()


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------
def balance(index, owner, raw, *, mint=MINT, decimals=8, program=TOKEN_PROGRAM):
    return {"accountIndex": index, "mint": mint, "owner": owner,
            "programId": program,
            "uiTokenAmount": {"amount": raw, "decimals": decimals}}


def token_instruction(kind="transferChecked", amount="123", *,
                      source="a", destination="b", mint=MINT, decimals=8,
                      program=TOKEN_PROGRAM):
    if kind in {"burn", "burnChecked"}:
        info = {"account": source, "mint": mint,
                "tokenAmount": {"amount": amount, "decimals": decimals}}
    elif kind in {"mintTo", "mintToChecked"}:
        info = {"account": destination, "mint": mint,
                "tokenAmount": {"amount": amount, "decimals": decimals}}
    else:
        info = {"source": source, "destination": destination, "mint": mint,
                "tokenAmount": {"amount": amount, "decimals": decimals}}
    return {"programId": program, "parsed": {"type": kind, "info": info}}


def capture(*, instruction=None, pre=None, post=None, signature="tx",
            slot=100, block_time=1791975600, commitment="finalized",
            encoding="jsonParsed"):
    return {
        "signature": signature,
        "slot": slot,
        "request": {"method": "getTransaction", "params": [signature, {
            "encoding": encoding, "commitment": commitment,
            "maxSupportedTransactionVersion": 0}]},
        "result": {
            "slot": slot,
            "blockTime": block_time,
            "transaction": {
                "signatures": [signature],
                "message": {
                    "accountKeys": ["a", "b"],
                    "instructions": [{"programId": "router"}],
                },
            },
            "meta": {
                "err": None,
                "innerInstructions": [{
                    "index": 0,
                    "instructions": [instruction or token_instruction()],
                }],
                "preTokenBalances": pre if pre is not None else [
                    balance(0, "owner-a", "200"),
                    balance(1, "owner-b", "0"),
                ],
                "postTokenBalances": post if post is not None else [
                    balance(0, "owner-a", "77"),
                    balance(1, "owner-b", "123"),
                ],
            },
        },
    }


def side_capture(action):
    if action == "burn":
        return capture(
            instruction=token_instruction("burnChecked"),
            pre=[balance(0, "owner-a", "200"), balance(1, "owner-b", "0")],
            post=[balance(0, "owner-a", "77"), balance(1, "owner-b", "0")],
        )
    if action == "mint":
        return capture(
            instruction=token_instruction("mintToChecked", destination="b"),
            pre=[balance(0, "owner-a", "0"), balance(1, "owner-b", "0")],
            post=[balance(0, "owner-a", "0"), balance(1, "owner-b", "123")],
        )
    raise AssertionError(action)


class PinnedMissingDayArtifactTests(unittest.TestCase):
    def test_receipt_and_artifact_are_the_single_approved_repair(self):
        self.assertEqual(RECEIPT["query_id"], 8894038)
        self.assertEqual(RECEIPT["query_version"], 4)
        self.assertEqual(RECEIPT["saved_sql_sha256"],
                         digest(APPROVED_SQL.rstrip(b"\n")))
        self.assertEqual(RECEIPT["execution_id"], APPROVED_EXECUTION)
        self.assertEqual(RECEIPT["absolute_day"], APPROVED_DAY)
        self.assertEqual(RECEIPT["query_parameters"],
                         {"oldest_days_ago": 5, "newest_days_ago": 5})
        self.assertEqual(RECEIPT["result_sha256"], digest(RESULT_BYTES))
        self.assertEqual(RECEIPT["retrieval"]["offset"], 0)
        self.assertEqual(RECEIPT["retrieval"]["rows_received"], 198)

        result = json.loads(RESULT_BYTES)
        rows = result["data"]["rows"]
        self.assertEqual(result["executionId"], APPROVED_EXECUTION)
        self.assertEqual(result["state"], "COMPLETED")
        self.assertEqual(len(rows), result["resultMetadata"]["totalRowCount"])
        self.assertEqual(len(rows), 198)
        self.assertEqual({row["block_date"] for row in rows}, {APPROVED_DAY})
        columns = [column["name"] for column in
                   result["resultMetadata"]["columns"]]
        self.assertEqual(len(columns), len(set(columns)))
        self.assertEqual(set(columns), EXPECTED_SOURCE_FIELDS)

    def test_approved_day_is_appended_with_a_bound_receipt(self):
        canonical = PREDECESSOR
        candidate, receipt = accept(canonical)
        self.assertTrue(candidate.startswith(canonical))
        records = list(csv.DictReader(io.StringIO(candidate.decode())))
        self.assertEqual(len(records), 199)
        self.assertEqual(receipt["added_rows"], 198)
        self.assertEqual(receipt["deleted_rows"], 0)
        self.assertEqual(receipt["day"], APPROVED_DAY)
        self.assertEqual(receipt["execution_id"], APPROVED_EXECUTION)
        self.assertEqual(receipt["source_sha256"], digest(RESULT_BYTES))
        self.assertEqual(receipt["canonical_before_sha256"], digest(canonical))
        self.assertEqual(receipt["canonical_after_sha256"], digest(candidate))
        self.assertEqual(receipt["scope_start_utc"],
                         "2026-10-14T00:00:00Z")
        self.assertEqual(receipt["scope_end_utc"],
                         "2026-10-15T00:00:00Z")

    def test_approved_query_versions_and_sql_are_pinned(self):
        for field, bad in (("query_id", 8894039), ("query_version", 3),
                           ("saved_sql_sha256", "0" * 64)):
            with self.subTest(field=field), self.assertRaises(MissingDayRejected):
                accept(**{field: bad})


class MissingDayAuthorityTests(unittest.TestCase):
    def test_no_receipt_means_no_authority(self):
        for name, value in (("none", None), ("empty", {}),
                            ("list", []), ("string", "approved")):
            with self.subTest(name=name), self.assertRaises(MissingDayRejected):
                accept_missing_day(PREDECESSOR, RESULT_BYTES, value)

    def test_self_authorisation_flags_are_not_authority(self):
        self_authorised = {
            "authenticated": True,
            "authorization": "User explicitly approved one query",
            "authenticated_boundary": "official dune MCP",
            "canonical_sha256": digest(PREDECESSOR),
            "result_sha256": digest(RESULT_BYTES),
        }
        with self.assertRaises(MissingDayRejected):
            accept_missing_day(PREDECESSOR, RESULT_BYTES, self_authorised)

    def test_every_pinned_binding_is_load_bearing(self):
        for field in ("canonical_sha256", "result_sha256", "query_id",
                      "query_version", "saved_sql_sha256", "execution_id",
                      "submitted_at_utc", "completed_received_at_utc",
                      "query_parameters", "absolute_day"):
            stripped = trusted_for()
            stripped.pop(field)
            with self.subTest(field=field), self.assertRaises(MissingDayRejected):
                accept_missing_day(PREDECESSOR, RESULT_BYTES, stripped)

    def test_stale_predecessor_and_changed_source_artifact_are_refused(self):
        other = render([prior_row(), {
            **prior_row(), "event_id": "second", "tx_id": "another",
        }])
        for args in ((PREDECESSOR, RESULT_BYTES, {"canonical_sha256": digest(other)}),
                     (PREDECESSOR, RESULT_BYTES, {"result_sha256": digest(b"{}")})):
            with self.subTest(args=args), self.assertRaises(MissingDayRejected):
                canonical, result, override = args
                accept_missing_day(canonical, result, trusted_for(**override))

    def test_relative_scope_must_be_one_complete_utc_day(self):
        cases = {
            "cross-midnight": {"submitted_at_utc": "2026-10-19T23:59:59Z",
                               "completed_received_at_utc": "2026-10-20T00:00:00Z"},
            "reversed": {"submitted_at_utc": "2026-10-19T21:21:00Z",
                         "completed_received_at_utc": "2026-10-19T21:20:00Z"},
            "naive-submitted": {"submitted_at_utc": "2026-10-19T21:20:00"},
            "naive-received": {"completed_received_at_utc": "2026-10-19T21:21:00"},
            "wrong-day": {"absolute_day": "2026-10-15"},
            "seed-day": {"submitted_at_utc": "2026-10-08T12:00:00Z",
                         "completed_received_at_utc": "2026-10-08T12:01:00Z",
                         "absolute_day": "2026-10-03"},
        }
        for name, override in cases.items():
            with self.subTest(name=name), self.assertRaises(MissingDayRejected):
                accept(**override)

    def test_parameters_must_be_exactly_one_relative_day(self):
        base = dict(RECEIPT["query_parameters"])
        cases = [
            {**base, "oldest_days_ago": 0},
            {**base, "oldest_days_ago": 4},
            {**base, "newest_days_ago": 4},
            {**base, "extra": 1},
            {"oldest_days_ago": 5},
        ]
        for params in cases:
            with self.subTest(params=params), self.assertRaises(MissingDayRejected):
                accept(query_parameters=params)


class MissingDaySourceShapeTests(unittest.TestCase):
    def refuse(self, mutator):
        raw = mutated_result(mutator)
        with self.assertRaises(MissingDayRejected):
            accept(result=raw)

    def test_missing_or_extra_fields_are_refused(self):
        for field in ("from_owner", "to_owner", "from_token_account",
                      "to_token_account", "amount_raw", "amount_xnet",
                      "block_time", "tx_id", "event_multiplicity"):
            with self.subTest(missing=field):
                self.refuse(lambda rows, field=field: rows[0].pop(field))
        with self.subTest(extra="event_id"):
            self.refuse(lambda rows: rows[0].__setitem__("event_id", "x"))

    def test_missing_owner_or_side_is_refused_for_every_action(self):
        def transfer_from(rows):
            rows[0]["from_owner"] = None

        def transfer_to(rows):
            rows[0]["to_owner"] = ""

        def burn_owner(rows):
            rows[0].update(action="burn", from_owner=None)

        def burn_account(rows):
            rows[0].update(action="burn", from_token_account="")

        def mint_owner(rows):
            rows[0].update(action="mint", to_owner=None)

        def mint_account(rows):
            rows[0].update(action="mint", to_token_account="")

        for mutation in (transfer_from, transfer_to, burn_owner, burn_account,
                         mint_owner, mint_account):
            with self.subTest(mutation=mutation.__name__):
                self.refuse(mutation)

    def test_unknown_action_and_non_exact_integers_are_refused(self):
        mutations = [
            lambda rows: rows[0].__setitem__("action", "approve"),
            lambda rows: rows[0].__setitem__("event_multiplicity", "0"),
            lambda rows: rows[0].__setitem__("event_multiplicity", "1.0"),
            lambda rows: rows[0].__setitem__("amount_raw", "-1"),
            lambda rows: rows[0].__setitem__("amount_raw", "01"),
            lambda rows: rows[0].__setitem__("amount_raw", 123.5),
            lambda rows: rows[0].__setitem__("block_slot", 1.5),
            lambda rows: rows[0].__setitem__("tx_id", ""),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.refuse(mutation)

    def test_out_of_scope_rows_are_refused_from_both_date_fields(self):
        self.refuse(lambda rows: rows[0].__setitem__("block_date", "2026-10-15"))
        self.refuse(lambda rows: rows[0].__setitem__("block_time",
                                                     "2026-10-15T00:00:00Z"))
        self.refuse(lambda rows: rows[0].__setitem__("block_time",
                                                     "2026-10-14T00:00:00+01:00"))

    def test_duplicate_leg_is_refused_not_unioned(self):
        def duplicate(rows):
            rows[1] = deepcopy(rows[0])

        self.refuse(duplicate)

    def test_day_candidate_plus_rpc_legs_union_only_new_event_ids(self):
        """HIGH integration: the 198 Dune rows are not chain-complete alone.

        The five transactions absent from the Dune result contribute ten RPC
        legs.  The other seventeen captured legs already exist and must match
        exactly; a concatenation would duplicate them.  This is the merge the
        production wiring still has to own.
        """
        candidate, _ = accept(PREDECESSOR)
        day = list(csv.DictReader(io.StringIO(candidate.decode())))[1:]
        self.assertEqual(len(day), 198)
        merged = {row["event_id"]: row for row in day}
        self.assertEqual(len(merged), 198)

        new = 0
        for capture_row in RPC_FIXTURE["repair_rows"]:
            rows, _ = derive_transaction(capture_row)
            for row in rows:
                if row["event_id"] in merged:
                    self.assertEqual(merged[row["event_id"]], row)
                else:
                    merged[row["event_id"]] = row
                    new += 1

        self.assertEqual(new, 10)
        self.assertEqual(len(merged), 208)

    def test_identical_locator_with_distinct_amount_is_conservatively_refused(self):
        """The approved SQL groups by amount, so this shape can be legitimate.

        The refusal invents nothing but can block an otherwise valid day.  It
        is recorded here as a fail-closed availability limit, not a union.
        """
        def distinct_amount(rows):
            rows[1] = {**rows[0], "amount_raw": "999"}

        raw = mutated_result(distinct_amount)
        with self.assertRaisesRegex(MissingDayRejected,
                                    "Ambiguous legacy event locator"):
            accept(result=raw)


class MissingDayCompletenessTests(unittest.TestCase):
    def test_empty_short_or_overcounted_results_are_refused(self):
        cases = {
            "empty": lambda data: data["data"].__setitem__("rows", []),
            "short": lambda data: data["data"]["rows"].pop(),
            "total-197": lambda data: data["resultMetadata"].__setitem__(
                "totalRowCount", 197),
            "total-199": lambda data: data["resultMetadata"].__setitem__(
                "totalRowCount", 199),
            "no-total": lambda data: data["resultMetadata"].pop(
                "totalRowCount"),
        }
        for name, mutate in cases.items():
            data = json.loads(RESULT_BYTES)
            mutate(data)
            raw = json.dumps(data).encode()
            with self.subTest(name=name), self.assertRaises(MissingDayRejected):
                accept(result=raw)

    def test_projected_duplicate_or_missing_columns_are_refused(self):
        def duplicate_column(data):
            data["resultMetadata"]["columns"].append(
                dict(data["resultMetadata"]["columns"][0]))

        def extra_column(data):
            data["resultMetadata"]["columns"].append(
                {"name": "event_id", "type": "varchar"})

        def missing_column(data):
            data["resultMetadata"]["columns"].pop()

        for name, mutate in (("duplicate", duplicate_column),
                             ("extra", extra_column),
                             ("missing", missing_column)):
            data = json.loads(RESULT_BYTES)
            mutate(data)
            raw = json.dumps(data).encode()
            with self.subTest(name=name), self.assertRaises(MissingDayRejected):
                accept(result=raw)

    def test_wrong_execution_or_state_is_refused(self):
        for field, value in (("executionId", "other"),
                             ("state", "QUERY_STATE_COMPLETED_PARTIAL"),
                             ("state", "RUNNING")):
            data = json.loads(RESULT_BYTES)
            data[field] = value
            raw = json.dumps(data).encode()
            with self.subTest(field=field, value=value), \
                    self.assertRaises(MissingDayRejected):
                accept(result=raw)

    def test_existing_day_is_never_replaced(self):
        day_row = normalise_transfer(json.loads(RESULT_BYTES)["data"]["rows"][0])
        day_row.update(block_date=APPROVED_DAY,
                       block_time="2026-10-14T00:00:00Z",
                       event_id="existing-day")
        canonical = render([day_row])
        with self.assertRaises(MissingDayRejected):
            accept(canonical=canonical)


class MissingDayAdversarialFindingsTests(unittest.TestCase):
    def test_execution_id_cannot_be_absent(self):
        """Regression: a null execution id must not count as one execution."""
        data = json.loads(RESULT_BYTES)
        data["executionId"] = None
        raw = json.dumps(data).encode()
        with self.assertRaises(MissingDayRejected):
            accept(result=raw, execution_id=None)

    def test_whitespace_identity_fields_are_not_resolved(self):
        """Regression: whitespace must not pass as a resolved identity.

        A padded BBB owner is especially risky: holder reduction strips it but
        burn attribution compares the padded string exactly.
        """
        cases = {
            "blank-owner": lambda rows: rows[0].__setitem__("from_owner", "   "),
            "padded-owner": lambda rows: rows[0].__setitem__(
                "from_owner", f" {rows[0]['from_owner']} "),
            "blank-tx": lambda rows: rows[0].__setitem__("tx_id", " "),
            "blank-account": lambda rows: rows[0].__setitem__(
                "from_token_account", " "),
        }
        for name, mutate in cases.items():
            raw = mutated_result(mutate)
            with self.subTest(name=name), self.assertRaises(MissingDayRejected):
                accept(result=raw)

    def test_malformed_missing_day_shapes_raise_missing_day_rejection(self):
        """Regression: malformed shapes raise MissingDayRejected, not Python."""
        def data_none(raw):
            value = json.loads(raw)
            value["data"] = None
            return json.dumps(value).encode()

        def null_column(raw):
            value = json.loads(raw)
            value["resultMetadata"]["columns"][0] = None
            return json.dumps(value).encode()

        for name, raw in (("data-none", data_none(RESULT_BYTES)),
                          ("null-column", null_column(RESULT_BYTES))):
            with self.subTest(name=name), self.assertRaises(MissingDayRejected):
                accept(result=raw)
        with self.subTest(name="timestamp-z"), \
                self.assertRaises(MissingDayRejected):
            accept(submitted_at_utc="Z")

    def test_day_absence_checks_block_time_not_only_block_date(self):
        """Regression: a 10-14 event mislabelled 10-13 must still be refused."""
        mislabelled = normalise_transfer(
            json.loads(RESULT_BYTES)["data"]["rows"][0])
        mislabelled.update(block_date="2026-10-13",
                           block_time="2026-10-14T01:00:00Z",
                           tx_id="legacy-mislabelled-day",
                           event_id="mislabelled-day")
        canonical = render([mislabelled])
        with self.assertRaises(MissingDayRejected):
            accept(canonical=canonical)


# ---------------------------------------------------------------------------
# RPC proof: positive controls
# ---------------------------------------------------------------------------
class RpcProofPositiveTests(unittest.TestCase):
    def test_decoded_instruction_and_flow_receipt_are_complete(self):
        rows, proof = derive_transaction(capture())
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["action"], row["amount_raw"], row["event_multiplicity"]),
                         ("transfer", "123", "1"))
        self.assertEqual((row["from_owner"], row["to_owner"]),
                         ("owner-a", "owner-b"))
        self.assertEqual((row["from_token_account"], row["to_token_account"]),
                         ("a", "b"))
        self.assertEqual(row["amount_xnet"],
                         format(Decimal(123) / Decimal(10**8), "f"))
        self.assertEqual(row["block_time"], "2026-10-14 11:00:00.000 UTC")
        self.assertEqual(proof["signature"], "tx")
        self.assertEqual(proof["slot"], 100)
        self.assertEqual(proof["source"], "finalized_solana_rpc")
        self.assertTrue(proof["balance_flow_verified"])
        self.assertEqual(proof["instructions"][0]["outer"], 0)
        self.assertEqual(proof["instructions"][0]["inner"], 0)

    def test_full_fixture_legs_match_every_shared_dune_row(self):
        """The complete captures must agree with Dune on every shared leg.

        The 198-row day contains 17 of the 27 captured legs; the other 10 are
        legs of transactions absent from the Dune result.  Existing legs must
        match field-for-field (including identity), so a union can be keyed by
        ``event_id`` instead of concatenating duplicates.
        """
        dune_by_tx = defaultdict(list)
        for source in json.loads(RESULT_BYTES)["data"]["rows"]:
            dune_by_tx[source["tx_id"]].append(normalise_transfer(source))

        total = shared = new = 0
        seen_ids = set()
        for capture_row in RPC_FIXTURE["repair_rows"]:
            rows, proof = derive_transaction(capture_row)
            total += len(rows)
            self.assertTrue(proof["balance_flow_verified"])
            self.assertTrue(rows)
            for row in rows:
                self.assertNotIn(row["event_id"], seen_ids)
                seen_ids.add(row["event_id"])
            expected = dune_by_tx.get(capture_row["signature"], [])
            if expected:
                fields = tuple(field for field in TRANSFER_FIELDS
                               if field != "event_id")
                self.assertEqual(
                    Counter(tuple(row[field] for field in fields) for row in rows),
                    Counter(tuple(row[field] for field in fields)
                            for row in expected),
                    capture_row["signature"][:8],
                )
                self.assertEqual({row["event_id"] for row in rows},
                                 {row["event_id"] for row in expected},
                                 capture_row["signature"][:8])
                shared += len(rows)
            else:
                new += len(rows)

        self.assertEqual((total, shared, new), (27, 17, 10))

    def test_burn_and_mint_have_no_invented_counterparty(self):
        for action in ("burn", "mint"):
            with self.subTest(action=action):
                rows, _ = derive_transaction(side_capture(action))
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row["action"], action)
                if action == "burn":
                    self.assertTrue(row["from_owner"])
                    self.assertTrue(row["from_token_account"])
                    self.assertEqual((row["to_owner"], row["to_token_account"]),
                                     ("", ""))
                else:
                    self.assertTrue(row["to_owner"])
                    self.assertTrue(row["to_token_account"])
                    self.assertEqual((row["from_owner"], row["from_token_account"]),
                                     ("", ""))


class RpcFlowAndCompletenessTests(unittest.TestCase):
    def test_balance_delta_alone_never_creates_a_leg(self):
        c = capture()
        c["result"]["meta"]["innerInstructions"] = []
        with self.assertRaises(ProofRejected):
            derive_transaction(c)

    def test_inner_instructions_must_be_present_and_a_list(self):
        for value in ("missing", None, {}, "not-a-list"):
            c = capture()
            if value == "missing":
                c["result"]["meta"].pop("innerInstructions")
            else:
                c["result"]["meta"]["innerInstructions"] = value
            with self.subTest(value=value), self.assertRaises(ProofRejected):
                derive_transaction(c)

    def test_inflated_instruction_amount_fails_balance_flow(self):
        c = capture(instruction=token_instruction(amount="124"))
        with self.assertRaises(ProofRejected):
            derive_transaction(c)

    def test_duplicate_or_out_of_range_inner_groups_are_refused(self):
        duplicate = capture()
        duplicate["result"]["meta"]["innerInstructions"].append(
            deepcopy(duplicate["result"]["meta"]["innerInstructions"][0]))
        out_of_range = capture()
        out_of_range["result"]["meta"]["innerInstructions"][0]["index"] = 1
        not_a_list = capture()
        not_a_list["result"]["meta"]["innerInstructions"][0]["instructions"] = None
        for name, c in (("duplicate", duplicate),
                        ("out-of-range", out_of_range),
                        ("not-a-list", not_a_list)):
            with self.subTest(name=name), self.assertRaises(ProofRejected):
                derive_transaction(c)

    def test_non_token_instruction_cannot_explain_xnet_delta(self):
        c = capture()
        c["result"]["meta"]["innerInstructions"][0]["instructions"][0][
            "programId"] = "SomeOtherProgram"
        with self.assertRaises(ProofRejected):
            derive_transaction(c)

    def test_relevant_unsupported_spl_instruction_is_refused(self):
        c = side_capture("burn")
        c["result"]["meta"]["innerInstructions"][0]["instructions"][0][
            "parsed"]["type"] = "approve"
        with self.assertRaises(ProofRejected):
            derive_transaction(c)


class RpcCaptureShapeTests(unittest.TestCase):
    def test_request_must_be_finalized_json_parsed_get_transaction(self):
        cases = {
            "confirmed": {"commitment": "confirmed"},
            "json": {"encoding": "json"},
        }
        for name, change in cases.items():
            c = capture()
            c["request"]["params"][1].update(change)
            with self.subTest(name=name), self.assertRaises(ProofRejected):
                derive_transaction(c)
        for params in ([["tx"]], [["tx"], {"encoding": "jsonParsed",
                                            "commitment": "finalized"}, "x"]):
            c = capture()
            c["request"]["params"] = params
            with self.subTest(params=params), self.assertRaises(ProofRejected):
                derive_transaction(c)

    def test_signature_slot_and_failure_must_match(self):
        cases = [
            lambda c: c.__setitem__("signature", "other"),
            lambda c: c["result"]["transaction"].__setitem__(
                "signatures", ["other"]),
            lambda c: c["result"]["meta"].__setitem__("err", {"InstructionError": 1}),
            lambda c: c["result"].__setitem__("slot", 101),
            lambda c: c.__setitem__("slot", 101),
            lambda c: c["result"]["transaction"]["message"].__setitem__(
                "accountKeys", ["a", "a"]),
        ]
        for change in cases:
            c = capture()
            change(c)
            with self.subTest(change=change), self.assertRaises(ProofRejected):
                derive_transaction(c)

    def test_token_balances_must_be_complete_and_consistent(self):
        cases = {
            "missing-post": lambda c: c["result"]["meta"].pop("postTokenBalances"),
            "duplicate-pre": lambda c: c["result"]["meta"]["preTokenBalances"].append(
                balance(0, "owner-a", "200")),
            "index-outside-keys": lambda c: c["result"]["meta"]["preTokenBalances"][0].update(
                accountIndex=2),
            "owner-changed": lambda c: c["result"]["meta"]["postTokenBalances"][0].update(
                owner="someone-else"),
            "mint-changed": lambda c: c["result"]["meta"]["postTokenBalances"][0].update(
                mint="OtherMint"),
            "decimals-changed": lambda c: c["result"]["meta"]["postTokenBalances"][0][
                "uiTokenAmount"].update(decimals=6),
            "missing-owner": lambda c: c["result"]["meta"]["preTokenBalances"][1].update(
                owner=None),
            "no-xnet": lambda c: [entry.update(mint="OtherMint") for entry in
                                  c["result"]["meta"]["preTokenBalances"] +
                                  c["result"]["meta"]["postTokenBalances"]],
        }
        for name, change in cases.items():
            c = capture()
            change(c)
            with self.subTest(name=name), self.assertRaises(ProofRejected):
                derive_transaction(c)

    def test_transfer_requires_both_xnet_counterparties(self):
        c = capture()
        c["result"]["meta"]["preTokenBalances"][1]["mint"] = "OtherMint"
        c["result"]["meta"]["postTokenBalances"][1]["mint"] = "OtherMint"
        with self.assertRaises(ProofRejected):
            derive_transaction(c)

    def test_instruction_mint_and_decimals_are_checked(self):
        for instruction in (
            token_instruction(mint="OtherMint"),
            token_instruction(decimals=6),
        ):
            c = capture(instruction=instruction)
            with self.subTest(instruction=instruction), \
                    self.assertRaises(ProofRejected):
                derive_transaction(c)


class RpcAdversarialFindingsTests(unittest.TestCase):
    def test_real_xnet_token2022_burn_must_be_derived(self):
        """Regression: real XNET instructions are Token-2022, not Tokenkeg.

        A complete outer ``burnChecked`` from the real 2026-10-14 capture set
        must produce one burn row.  Before the reviewed fix, the parser only
        scanned legacy ``TOKEN_PROGRAM``, saw no legs and raised
        ``No decoded XNET transfer instructions``; that blocked every real
        capture, including the three canonically required burns.
        """
        row = next(item for item in RPC_FIXTURE["repair_rows"]
                   if item["signature"].startswith("3g8MXb6V"))
        c = deepcopy(row)
        self.assertEqual(c["result"]["meta"]["preTokenBalances"][0]["programId"],
                         TOKEN_2022_PROGRAM)
        self.assertEqual(
            c["result"]["transaction"]["message"]["instructions"][0]["programId"],
            TOKEN_2022_PROGRAM)
        c["result"]["meta"]["innerInstructions"] = []
        rows, proof = derive_transaction(c)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "burn")
        self.assertEqual(rows[0]["amount_raw"], "20878301482286")
        self.assertEqual(rows[0]["from_owner"],
                         "5QsyByFVJcg7oN76Ma26KEDFQdHt1tsiVExK94zURzfd")
        self.assertEqual(rows[0]["from_token_account"],
                         "8joUsyKkRQzxUWHwuXjLAnnxdR8q4yy6VicKt2mJFCPC")
        self.assertEqual((rows[0]["to_owner"], rows[0]["to_token_account"]),
                         ("", ""))
        self.assertTrue(proof["balance_flow_verified"])

    def test_empty_signature_cannot_produce_an_unidentified_row(self):
        """Regression: empty/blank signatures must raise ProofRejected."""
        for signature in ("", " "):
            c = capture(signature=signature)
            with self.subTest(signature=repr(signature)), \
                    self.assertRaises(ProofRejected):
                derive_transaction(c)

    def test_malformed_captures_raise_proof_rejection_not_python_errors(self):
        """Regression: fail-closed must be ``ProofRejected``, not AttributeError."""
        def transaction_none(c):
            c["result"]["transaction"] = None

        def meta_none(c):
            c["result"]["meta"] = None

        def empty_signatures(c):
            c["result"]["transaction"]["signatures"] = []

        def options_none(c):
            c["request"]["params"][1] = None

        def null_balance(c):
            c["result"]["meta"]["postTokenBalances"][0] = None

        def null_parsed_info(c):
            c["result"]["meta"]["innerInstructions"][0]["instructions"][0][
                "parsed"]["info"] = None

        def mint_stray_source(c):
            instruction = c["result"]["meta"]["innerInstructions"][0][
                "instructions"][0]
            instruction["parsed"]["info"]["source"] = "not-a-balance"

        def burn_stray_destination(c):
            instruction = c["result"]["meta"]["innerInstructions"][0][
                "instructions"][0]
            instruction["parsed"]["info"]["destination"] = "not-a-balance"

        for name, change in (("transaction-none", transaction_none),
                             ("meta-none", meta_none),
                             ("empty-signatures", empty_signatures),
                             ("options-none", options_none),
                             ("null-balance", null_balance),
                             ("null-parsed-info", null_parsed_info),
                             ("mint-stray-source", mint_stray_source),
                             ("burn-stray-destination", burn_stray_destination)):
            c = capture()
            change(c)
            with self.subTest(name=name), self.assertRaises(ProofRejected):
                derive_transaction(c)


if __name__ == "__main__":
    unittest.main()
