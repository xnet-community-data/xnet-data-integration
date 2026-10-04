#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

BBB_WALLET = (
    "5QsyByFVJcg7oN76Ma26KEDFQdHt1tsiVExK94zURzfd"
)

MAX_SUPPLY = Decimal("1307098713")


def D(v):
    if v in (None, ""):
        return Decimal("0")
    return Decimal(str(v))


def load(name):
    return json.loads(
        (
            ROOT
            / "data/current"
            / name
        ).read_text()
    )


def bbb_balance():
    p = (
        ROOT
        / "data/derived/xnet_owner_balances.csv"
    )

    with p.open(newline="") as f:
        for r in csv.DictReader(f):
            if r["owner"] == BBB_WALLET:
                return D(r["xnet_balance"])

    return Decimal("0")


def bbb_scope_start():
    p = ROOT / "data/canonical/bbb_trades.csv"

    if not p.exists():
        return None

    with p.open(newline="") as f:
        rows = [
            r for r in csv.DictReader(f)
            if r.get("block_time")
        ]

    if not rows:
        return None

    return min(r["block_time"] for r in rows)


holders = load("xnet_holder_state.json")
supply = load("xnet_supply_state.json")
burns = load("xnet_bbb_burn_state.json")
bbb = load("xnet_bbb_trade_state.json")

circulating = D(
    supply["latest_circulating_supply_xnet"]
)

burned = D(
    burns["verified_bbb_burned_xnet"]
)

snapshot = {
    "schema_version": 1,

    "generated_at_utc":
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),

    "circulating_supply_xnet":
        str(circulating),

    "published_max_supply_xnet":
        str(MAX_SUPPLY),

    "circulating_supply_pct_max":
        str(
            circulating
            / MAX_SUPPLY
            * Decimal("100")
        ),

    "holder_count_positive":
        holders["positive_holder_count"],

    "holders_ge_1_xnet":
        holders["holders_ge_1_xnet"],

    "holders_ge_100_xnet":
        holders["holders_ge_100_xnet"],

    "holders_ge_1000_xnet":
        holders["holders_ge_1000_xnet"],

    "verified_bbb_burned_xnet":
        str(burned),

    "verified_bbb_burned_pct_max":
        str(
            burned
            / MAX_SUPPLY
            * Decimal("100")
        ),

    "bbb_wallet_xnet_balance":
        str(bbb_balance()),

    "bbb_recent_trade_scope_start_utc":
        bbb_scope_start(),

    "bbb_recent_transaction_count":
        bbb["canonical_transaction_count"],

    "bbb_recent_gross_xnet_bought":
        bbb["gross_xnet_bought"],

    "bbb_recent_gross_xnet_sold":
        bbb["gross_xnet_sold"],

    "bbb_recent_trade_value_usd":
        bbb["trade_value_usd"],

    "latest_transfer_event_utc":
        supply["latest_transfer_event_utc"],

    "latest_bbb_trade_utc":
        bbb["latest_trade_utc"],

    "methodology":
        "official_excluded_wallet_flow_plus_burn"
}

out = (
    ROOT
    / "data/current/xnet_chain_snapshot.json"
)

tmp = out.with_suffix(".json.tmp")

tmp.write_text(
    json.dumps(snapshot, indent=2)
    + "\n"
)

tmp.replace(out)

print("=== XNET CHAIN SNAPSHOT ===")

for k, v in snapshot.items():
    print(f"{k}: {v}")
