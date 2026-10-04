#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


TOKEN_DECIMALS = Decimal("100000000")

BBB_WALLET = (
    "5QsyByFVJcg7oN76Ma26KEDFQdHt1tsiVExK94zURzfd"
)

TRANSFER_FIELDS = [
    "event_id",
    "block_date",
    "block_time",
    "block_slot",
    "tx_id",
    "action",
    "from_owner",
    "to_owner",
    "from_token_account",
    "to_token_account",
    "amount_raw",
    "amount_xnet",
    "event_multiplicity",
]

BBB_FIELDS = [
    "tx_id",
    "block_date",
    "block_time",
    "block_slot",
    "trader_id",
    "economic_side",
    "gross_xnet_bought",
    "gross_xnet_sold",
    "net_xnet_change",
    "trade_value_usd",
    "primary_project",
    "trade_source",
    "project_main_id",
    "quote_symbol",
    "quote_mint",
    "decoded_xnet_leg_count",
]


def dec(v) -> Decimal:
    if v in (None, ""):
        return Decimal("0")
    return Decimal(str(v))


def decstr(v: Decimal) -> str:
    return format(v, "f")


def parse_ts(v: str) -> datetime:
    s = v.strip()

    if s.endswith(" UTC"):
        s = s[:-4] + "+00:00"

    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)

    dt = datetime.fromisoformat(s)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def read_dune_rows(path: Path) -> list[dict]:
    d = json.loads(path.read_text())

    if d.get("state") != "QUERY_STATE_COMPLETED":
        raise RuntimeError(
            f"Dune result not completed: {path}"
        )

    return d["result"].get("rows", [])


def transfer_event_id(r: dict) -> str:
    parts = [
        str(r.get("tx_id") or ""),
        str(r.get("block_slot") or ""),
        str(r.get("action") or ""),
        str(r.get("from_token_account") or ""),
        str(r.get("to_token_account") or ""),
        str(r.get("amount_raw") or ""),
    ]

    return hashlib.sha256(
        "|".join(parts).encode()
    ).hexdigest()[:32]


def normalise_transfer(r: dict) -> dict:
    out = {
        "event_id": transfer_event_id(r),
        "block_date": str(r.get("block_date") or ""),
        "block_time": str(r.get("block_time") or ""),
        "block_slot": str(r.get("block_slot") or ""),
        "tx_id": str(r.get("tx_id") or ""),
        "action": str(r.get("action") or ""),
        "from_owner": str(r.get("from_owner") or ""),
        "to_owner": str(r.get("to_owner") or ""),
        "from_token_account":
            str(r.get("from_token_account") or ""),
        "to_token_account":
            str(r.get("to_token_account") or ""),
        "amount_raw": str(r.get("amount_raw") or "0"),
        "amount_xnet": str(r.get("amount_xnet") or "0"),
        "event_multiplicity":
            str(r.get("event_multiplicity") or "1"),
    }

    return out


def normalise_bbb(r: dict) -> dict:
    return {
        k: "" if r.get(k) is None else str(r.get(k))
        for k in BBB_FIELDS
    }


def merge_csv(
    path: Path,
    new_rows: list[dict],
    key_field: str,
    fields: list[str],
    sort_fields: tuple[str, ...],
) -> list[dict]:

    merged = {}

    if path.exists():
        with path.open(newline="") as f:
            for r in csv.DictReader(f):
                merged[r[key_field]] = r

    for r in new_rows:
        merged[r[key_field]] = r

    rows = list(merged.values())

    rows.sort(
        key=lambda r: tuple(
            str(r.get(k, ""))
            for k in sort_fields
        )
    )

    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=fields,
            extrasaction="ignore",
        )
        w.writeheader()
        w.writerows(rows)

    return rows


def transfer_amount(r: dict) -> Decimal:
    raw = dec(r["amount_raw"])
    mult = dec(r.get("event_multiplicity", "1"))

    return (raw / TOKEN_DECIMALS) * mult


def load_excluded_wallets() -> set[str]:
    path = Path("config/legacy_wallet_registry.csv")

    with path.open(newline="") as f:
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            raise RuntimeError("Empty legacy wallet registry")

        candidates = {
            "address",
            "wallet",
            "wallet_address",
            "owner",
        }

        addr_col = next(
            (
                c for c in reader.fieldnames
                if c.lower().strip() in candidates
            ),
            None,
        )

        if addr_col is None:
            raise RuntimeError(
                "Could not identify wallet address column in "
                "legacy_wallet_registry.csv"
            )

        wallets = {
            str(r[addr_col]).strip()
            for r in reader
            if str(r.get(addr_col) or "").strip()
        }

    if len(wallets) != 61:
        raise RuntimeError(
            f"Expected 61 excluded wallets, got {len(wallets)}"
        )

    return wallets


def derive_holders(transfers: list[dict]) -> None:
    seed_path = Path("data/xnet_owner_balances_seed.csv")
    meta_path = Path(
        "data/xnet_owner_balances_seed_metadata.json"
    )

    meta = json.loads(meta_path.read_text())
    cutoff = parse_ts(meta["latest_balance_event_time"])

    balances: dict[str, Decimal] = {}

    with seed_path.open(newline="") as f:
        for r in csv.DictReader(f):
            balances[r["owner"]] = dec(r["xnet_balance"])

    seed_positive_owners = {owner for owner, balance in balances.items() if balance > 0}
    applied = 0

    for r in transfers:
        if parse_ts(r["block_time"]) <= cutoff:
            continue

        action = r["action"].lower()
        amount = transfer_amount(r)

        frm = r["from_owner"].strip()
        to = r["to_owner"].strip()

        if action == "transfer":
            if not frm or not to:
                raise RuntimeError(
                    f"Unresolved owner in transfer {r['event_id']}"
                )

            balances[frm] = balances.get(
                frm, Decimal("0")
            ) - amount

            balances[to] = balances.get(
                to, Decimal("0")
            ) + amount

        elif action == "burn":
            if not frm:
                raise RuntimeError(
                    f"Missing burn owner {r['event_id']}"
                )

            balances[frm] = balances.get(
                frm, Decimal("0")
            ) - amount

        elif action == "mint":
            if not to:
                raise RuntimeError(
                    f"Missing mint owner {r['event_id']}"
                )

            balances[to] = balances.get(
                to, Decimal("0")
            ) + amount

        else:
            raise RuntimeError(
                f"Unknown transfer action: {action}"
            )

        applied += 1

    # Preserve the original holder methodology:
    # every genuinely positive owner balance counts as a holder.
    #
    # The seed comes from Dune's positive-balance owner state, so we must
    # not erase tiny positive balances merely because they are below one
    # raw-token unit after aggregation/representation.
    #
    # Only a tiny NEGATIVE numerical residual may be clamped to zero.
    negative_tolerance = Decimal("0.00000001")

    for owner, bal in list(balances.items()):
        if bal < -negative_tolerance:
            raise RuntimeError(
                f"Negative holder balance after reduction: "
                f"{owner} = {bal}"
            )

        if bal < 0:
            balances[owner] = Decimal("0")

    current_rows = [
        {
            "owner": owner,
            "xnet_balance": decstr(bal),
        }
        for owner, bal in balances.items()
        if bal > 0
    ]

    current_rows.sort(
        key=lambda r: Decimal(r["xnet_balance"]),
        reverse=True,
    )

    out = Path("data/derived/xnet_owner_balances.csv")

    with out.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["owner", "xnet_balance"],
        )
        w.writeheader()
        w.writerows(current_rows)

    positive = [
        Decimal(r["xnet_balance"])
        for r in current_rows
    ]

    total = sum(positive, Decimal("0"))

    current_positive_owners = {
        r["owner"]
        for r in current_rows
    }

    holders_created = (
        current_positive_owners - seed_positive_owners
    )

    holders_zeroed = (
        seed_positive_owners - current_positive_owners
    )

    # One applied transfer/burn/mint event cannot remove more than one
    # positive holder. This catches accidental holder-state corruption.
    if len(holders_zeroed) > applied:
        raise RuntimeError(
            "Holder QA failed: more seeded holders disappeared "
            f"({len(holders_zeroed)}) than transfer events applied "
            f"({applied})."
        )

    holder_state = {
        "schema_version": 2,
        "seed_cutoff_utc": meta["latest_balance_event_time"],
        "seed_positive_holder_count": len(seed_positive_owners),
        "transfer_events_applied": applied,
        "holders_created_since_seed": len(holders_created),
        "holders_zeroed_since_seed": len(holders_zeroed),
        "net_holder_change_since_seed":
            len(current_positive_owners) - len(seed_positive_owners),
        "positive_holder_count": len(positive),
        "holders_ge_1_xnet":
            sum(v >= 1 for v in positive),
        "holders_ge_100_xnet":
            sum(v >= 100 for v in positive),
        "holders_ge_1000_xnet":
            sum(v >= 1000 for v in positive),
        "outstanding_balance_sum_xnet": decstr(total),
        "latest_transfer_event_utc":
            max(
                (
                    r["block_time"]
                    for r in transfers
                    if parse_ts(r["block_time"]) > cutoff
                ),
                default=meta["latest_balance_event_time"],
            ),
    }

    Path(
        "data/current/xnet_holder_state.json"
    ).write_text(
        json.dumps(holder_state, indent=2) + "\n"
    )


def derive_supply(transfers: list[dict]) -> None:
    checkpoint = json.loads(
        Path(
            "config/xnet_supply_checkpoint.json"
        ).read_text()
    )

    checkpoint_date = date.fromisoformat(
        checkpoint["checkpoint_date"]
    )

    checkpoint_supply = dec(
        checkpoint[
            "checkpoint_circulating_supply_xnet"
        ]
    )

    excluded = load_excluded_wallets()

    supply_path = Path("data/xnet_supply_daily.csv")

    previous: dict[str, dict] = {}

    if supply_path.exists():
        with supply_path.open(newline="") as f:
            for r in csv.DictReader(f):
                previous[r["day"]] = r

    rebuild_from = date(2026, 10, 2)

    per_day = defaultdict(
        lambda: {
            "flow": Decimal("0"),
            "burn": Decimal("0"),
        }
    )

    for r in transfers:
        day = date.fromisoformat(r["block_date"])

        if day < rebuild_from:
            continue

        amount = transfer_amount(r)
        action = r["action"].lower()

        if action == "transfer":
            if r["to_owner"] in excluded:
                per_day[day]["flow"] += amount

            if r["from_owner"] in excluded:
                per_day[day]["flow"] -= amount

        elif action == "burn":
            per_day[day]["burn"] += amount
            # Burning a non-circulating balance also reduces the exclusion.
            if r["from_owner"] in excluded:
                per_day[day]["flow"] -= amount

    today = datetime.now(timezone.utc).date()

    replacement: dict[str, dict] = {}

    d = rebuild_from

    while d <= today:
        vals = per_day[d]

        change = -vals["flow"] - vals["burn"]

        replacement[d.isoformat()] = {
            "day": d.isoformat(),
            "net_flow_to_excluded_xnet":
                decstr(vals["flow"]),
            "burn_xnet":
                decstr(vals["burn"]),
            "circulation_change_xnet":
                decstr(change),
        }

        d += timedelta(days=1)

    # Newly indexed events legitimately revise earlier daily totals.
    # Record revisions instead of requiring yesterday's partial data to match.
    revisions = []
    qa_days = sorted(set(previous).intersection(replacement))

    for day_s in qa_days:
        if day_s not in previous:
            continue

        old = dec(
            previous[day_s][
                "circulation_change_xnet"
            ]
        )

        new = dec(
            replacement[day_s][
                "circulation_change_xnet"
            ]
        )

        if abs(old - new) > Decimal("0.000001"):
            revisions.append({"day": day_s, "previous_change_xnet": decstr(old), "updated_change_xnet": decstr(new)})

    combined = {
        day_s: row
        for day_s, row in previous.items()
        if date.fromisoformat(day_s) < rebuild_from
    }

    combined.update(replacement)

    rows = [
        combined[k]
        for k in sorted(combined)
    ]

    with supply_path.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "day",
                "net_flow_to_excluded_xnet",
                "burn_xnet",
                "circulation_change_xnet",
            ],
        )
        w.writeheader()
        w.writerows(rows)

    circulating = checkpoint_supply
    supply_history = []

    for row in rows:
        row_day = date.fromisoformat(row["day"])

        if row_day > checkpoint_date:
            circulating += dec(
                row["circulation_change_xnet"]
            )
            supply_history.append({**row, "circulating_supply_xnet": decstr(circulating)})

    latest_event = max(
        (r["block_time"] for r in transfers),
        default=None,
    )

    max_supply = dec(json.loads(Path("config/xnet_protocol_config.json").read_text())["published_max_supply_xnet"])
    if circulating < 0 or circulating > max_supply:
        raise RuntimeError(f"Circulating supply outside token supply bounds: {circulating}")
    Path("data/xnet_supply_history.json").write_text(json.dumps(supply_history, indent=2) + "\n")

    state = {
        "schema_version": 2,
        "daily_revisions": revisions,
        "checkpoint_date":
            checkpoint["checkpoint_date"],
        "checkpoint_circulating_supply_xnet":
            decstr(checkpoint_supply),
        "latest_circulating_supply_xnet":
            decstr(circulating),
        "latest_transfer_event_utc":
            latest_event,
        "methodology":
            "official_excluded_wallet_flow_plus_burn",
        "derived_from":
            "canonical_xnet_transfer_events",
    }

    Path(
        "data/current/xnet_supply_state.json"
    ).write_text(
        json.dumps(state, indent=2) + "\n"
    )


def derive_bbb_burns(transfers: list[dict]) -> None:
    checkpoint = json.loads(
        Path(
            "config/xnet_bbb_burn_checkpoint.json"
        ).read_text()
    )

    checkpoint_date = date.fromisoformat(
        checkpoint["checkpoint_date"]
    )

    checkpoint_total = dec(
        checkpoint["verified_bbb_burned_xnet"]
    )

    daily = defaultdict(lambda: Decimal("0"))

    for r in transfers:
        day = date.fromisoformat(r["block_date"])

        if day <= checkpoint_date:
            continue

        if (
            r["action"].lower() == "burn"
            and r["from_owner"] == BBB_WALLET
        ):
            daily[day] += transfer_amount(r)

    out_rows = []

    for day in sorted(daily):
        out_rows.append({
            "day": day.isoformat(),
            "verified_bbb_burn_xnet":
                decstr(daily[day]),
        })

    out = Path("data/derived/bbb_burn_daily.csv")

    with out.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "day",
                "verified_bbb_burn_xnet",
            ],
        )
        w.writeheader()
        w.writerows(out_rows)

    recent = sum(daily.values(), Decimal("0"))
    total = checkpoint_total + recent

    state = {
        "schema_version": 1,
        "checkpoint_date":
            checkpoint["checkpoint_date"],
        "checkpoint_verified_bbb_burned_xnet":
            decstr(checkpoint_total),
        "post_checkpoint_bbb_burned_xnet":
            decstr(recent),
        "verified_bbb_burned_xnet":
            decstr(total),
        "derived_from":
            "canonical_xnet_transfer_events",
    }

    Path(
        "data/current/xnet_bbb_burn_state.json"
    ).write_text(
        json.dumps(state, indent=2) + "\n"
    )


def derive_bbb_trades(rows: list[dict]) -> None:
    daily = defaultdict(
        lambda: {
            "buy_count": 0,
            "sell_count": 0,
            "gross_bought": Decimal("0"),
            "gross_sold": Decimal("0"),
            "net": Decimal("0"),
            "usd": Decimal("0"),
        }
    )

    for r in rows:
        day = r["block_date"]
        side = r["economic_side"]

        d = daily[day]

        if side == "BUY_XNET":
            d["buy_count"] += 1

        elif side == "SELL_XNET":
            d["sell_count"] += 1

        d["gross_bought"] += dec(
            r["gross_xnet_bought"]
        )

        d["gross_sold"] += dec(
            r["gross_xnet_sold"]
        )

        d["net"] += dec(
            r["net_xnet_change"]
        )

        d["usd"] += dec(
            r["trade_value_usd"]
        )

    out_rows = []

    for day in sorted(daily):
        d = daily[day]

        out_rows.append({
            "day": day,
            "bbb_buy_count": d["buy_count"],
            "bbb_sell_count": d["sell_count"],
            "gross_xnet_bought":
                decstr(d["gross_bought"]),
            "gross_xnet_sold":
                decstr(d["gross_sold"]),
            "net_xnet_change":
                decstr(d["net"]),
            "trade_value_usd":
                decstr(d["usd"]),
        })

    out = Path("data/derived/bbb_trades_daily.csv")

    with out.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "day",
                "bbb_buy_count",
                "bbb_sell_count",
                "gross_xnet_bought",
                "gross_xnet_sold",
                "net_xnet_change",
                "trade_value_usd",
            ],
        )
        w.writeheader()
        w.writerows(out_rows)

    total_bought = sum(
        (
            dec(r["gross_xnet_bought"])
            for r in rows
        ),
        Decimal("0"),
    )

    total_sold = sum(
        (
            dec(r["gross_xnet_sold"])
            for r in rows
        ),
        Decimal("0"),
    )

    total_usd = sum(
        (
            dec(r["trade_value_usd"])
            for r in rows
        ),
        Decimal("0"),
    )

    state = {
        "schema_version": 1,
        "canonical_transaction_count": len(rows),
        "gross_xnet_bought": decstr(total_bought),
        "gross_xnet_sold": decstr(total_sold),
        "net_xnet_change":
            decstr(total_bought - total_sold),
        "trade_value_usd": decstr(total_usd),
        "latest_trade_utc":
            max(
                (r["block_time"] for r in rows),
                default=None,
            ),
        "note":
            "Current canonical BBB DEX store begins with "
            "the Oct 2026 catch-up. Historical V2 BBB "
            "trades will be imported separately.",
    }

    Path(
        "data/current/xnet_bbb_trade_state.json"
    ).write_text(
        json.dumps(state, indent=2) + "\n"
    )


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--transfer-result",
        type=Path,
        required=True,
    )

    ap.add_argument(
        "--bbb-result",
        type=Path,
    )

    args = ap.parse_args()

    transfer_rows = [
        normalise_transfer(r)
        for r in read_dune_rows(args.transfer_result)
    ]

    bbb_rows = [
        normalise_bbb(r)
        for r in (read_dune_rows(args.bbb_result) if args.bbb_result else [])
    ]

    transfers = merge_csv(
        Path("data/canonical/xnet_transfers.csv"),
        transfer_rows,
        "event_id",
        TRANSFER_FIELDS,
        ("block_time", "block_slot", "tx_id"),
    )

    bbb = merge_csv(
        Path("data/canonical/bbb_trades.csv"),
        bbb_rows,
        "tx_id",
        BBB_FIELDS,
        ("block_time", "tx_id"),
    ) if args.bbb_result else []

    derive_holders(transfers)
    derive_supply(transfers)
    derive_bbb_burns(transfers)
    if args.bbb_result:
        derive_bbb_trades(bbb)

    print("=== XNET V3 CHAIN REDUCTION COMPLETE ===")
    print("Canonical transfer rows:", len(transfers))
    if args.bbb_result:
        print("Canonical BBB trade rows:", len(bbb))

    print()
    print("Holder state:")
    print(
        Path(
            "data/current/xnet_holder_state.json"
        ).read_text()
    )

    print("Supply state:")
    print(
        Path(
            "data/current/xnet_supply_state.json"
        ).read_text()
    )

    print("BBB burn state:")
    print(
        Path(
            "data/current/xnet_bbb_burn_state.json"
        ).read_text()
    )

    print("BBB trade state:")
    print(
        Path(
            "data/current/xnet_bbb_trade_state.json"
        ).read_text()
    )


if __name__ == "__main__":
    main()
