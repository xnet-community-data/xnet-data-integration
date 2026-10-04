#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

MAX_SUPPLY_XNET = Decimal("1307098713")

BBB_WALLET = (
    "5QsyByFVJcg7oN76Ma26KEDFQdHt1tsiVExK94zURzfd"
)


def D(v):
    if v in (None, ""):
        return Decimal("0")
    return Decimal(str(v))


def decstr(v):
    return format(D(v), "f")


def load(name):
    return json.loads(
        (
            ROOT
            / "data/current"
            / name
        ).read_text()
    )


def find_bbb_balance():
    p = (
        ROOT
        / "data/derived/xnet_owner_balances.csv"
    )

    with p.open(newline="") as f:
        for r in csv.DictReader(f):
            if r["owner"] == BBB_WALLET:
                return D(r["xnet_balance"])

    return Decimal("0")


def bbb_scope():
    p = ROOT / "data/canonical/bbb_trades.csv"

    if not p.exists():
        return None

    with p.open(newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        return None

    return min(
        r["block_time"]
        for r in rows
        if r.get("block_time")
    )


market = load("xnet_market_state.json")
holders = load("xnet_holder_state.json")
supply = load("xnet_supply_state.json")
burns = load("xnet_bbb_burn_state.json")
bbb = load("xnet_bbb_trade_state.json")

price = D(market["xnet_price_usd"])
circulating = D(
    supply["latest_circulating_supply_xnet"]
)
burned = D(
    burns["verified_bbb_burned_xnet"]
)

reconstructed_market_cap = (
    price * circulating
)

reconstructed_fdv = (
    price * MAX_SUPPLY_XNET
)

bbb_balance = find_bbb_balance()

snapshot = {
    "schema_version": 1,

    "snapshot_utc":
        market["snapshot_utc"],

    "market_fetched_at_utc":
        market["fetched_at_utc"],

    # MARKET
    "xnet_price_usd":
        decstr(price),

    "market_cap_usd":
        decstr(reconstructed_market_cap),

    "fdv_usd":
        decstr(reconstructed_fdv),

    "dex_liquidity_usd":
        market["total_dex_liquidity_usd"],

    "dex_volume_h24_usd":
        market["total_dex_volume_h24_usd"],

    # These are pool-level observations from DexScreener,
    # not deduplicated unique economic transactions.
    "dex_pool_buys_h24":
        market["observed_buys_h24"],

    "dex_pool_sells_h24":
        market["observed_sells_h24"],

    "liquidity_pool_count":
        market["liquidity_pool_count"],

    "primary_pool_address":
        market["primary_pool_address"],

    "primary_pool_dex":
        market["primary_pool_dex"],

    "primary_pool_quote_symbol":
        market["primary_pool_quote_symbol"],

    "primary_pool_liquidity_usd":
        market["primary_pool_liquidity_usd"],

    "primary_pool_liquidity_share":
        market["primary_pool_liquidity_share"],

    "price_change_h1_pct":
        market["price_change_h1_pct"],

    "price_change_h6_pct":
        market["price_change_h6_pct"],

    "price_change_h24_pct":
        market["price_change_h24_pct"],

    # TOKEN STATE
    "circulating_supply_xnet":
        decstr(circulating),

    "published_max_supply_xnet":
        decstr(MAX_SUPPLY_XNET),

    "circulating_supply_pct_max":
        decstr(
            circulating
            / MAX_SUPPLY_XNET
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

    # BBB
    "verified_bbb_burned_xnet":
        decstr(burned),

    "verified_bbb_burned_pct_max":
        decstr(
            burned
            / MAX_SUPPLY_XNET
            * Decimal("100")
        ),

    "bbb_wallet_xnet_balance":
        decstr(bbb_balance),

    # IMPORTANT:
    # Current canonical BBB trade history begins only
    # with the Oct-2026 catch-up. Do not display these
    # as all-time BBB purchases until historical V2
    # trade backfill has been imported.
    "bbb_recent_trade_scope_start_utc":
        bbb_scope(),

    "bbb_recent_transaction_count":
        bbb["canonical_transaction_count"],

    "bbb_recent_gross_xnet_bought":
        bbb["gross_xnet_bought"],

    "bbb_recent_gross_xnet_sold":
        bbb["gross_xnet_sold"],

    "bbb_recent_trade_value_usd":
        bbb["trade_value_usd"],

    # FRESHNESS
    "latest_transfer_event_utc":
        supply["latest_transfer_event_utc"],

    "latest_bbb_trade_utc":
        bbb["latest_trade_utc"],

    "generated_at_utc":
        (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        ),

    # QA-only provider references
    "provider_market_cap_usd":
        market["provider_market_cap_usd"],

    "provider_fdv_usd":
        market["provider_fdv_usd"],
}


out_json = (
    ROOT
    / "data/current/xnet_current_snapshot.json"
)

out_json.write_text(
    json.dumps(snapshot, indent=2)
    + "\n"
)


out_csv = (
    ROOT
    / "data/current/xnet_current_snapshot.csv"
)

with out_csv.open("w", newline="") as f:
    w = csv.DictWriter(
        f,
        fieldnames=list(snapshot),
    )
    w.writeheader()
    w.writerow(snapshot)


print(
    "=== XNET V3 UNIFIED CURRENT SNAPSHOT ==="
)

for key, value in snapshot.items():
    print(f"{key}: {value}")
