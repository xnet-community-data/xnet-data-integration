#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

BBB_WALLET = (
    "5QsyByFVJcg7oN76Ma26KEDFQdHt1tsiVExK94zURzfd"
)

PROTOCOL_CONFIG = json.loads(
    (
        ROOT
        / "config/xnet_protocol_config.json"
    ).read_text()
)

BBB_POLICY_CONFIG = json.loads(
    (
        ROOT
        / "config/xnet_bbb_execution_policy.json"
    ).read_text()
)

MAX_SUPPLY = Decimal(
    str(
        PROTOCOL_CONFIG[
            "published_max_supply_xnet"
        ]
    )
)

USDC_MINT = (
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
)

SOLANA_RPC = (
    "https://api.mainnet-beta.solana.com"
)


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


def previous_snapshot():
    p = (
        ROOT
        / "data/current/xnet_chain_snapshot.json"
    )

    if not p.exists():
        return {}

    try:
        return json.loads(
            p.read_text()
        )
    except Exception:
        return {}


def bbb_policy():
    direct = Decimal(
        str(
            BBB_POLICY_CONFIG[
                "direct_bbb_share_of_received_revenue"
            ]
        )
    )

    liquidity = Decimal(
        str(
            BBB_POLICY_CONFIG[
                "liquidity_share_of_received_revenue"
            ]
        )
    )

    liquidity_xnet_fraction = Decimal(
        str(
            BBB_POLICY_CONFIG[
                "liquidity_xnet_market_buy_fraction"
            ]
        )
    )

    execution_days = int(
        BBB_POLICY_CONFIG[
            "execution_days"
        ]
    )

    for name, value in {
        "direct":
            direct,

        "liquidity":
            liquidity,

        "liquidity_xnet_fraction":
            liquidity_xnet_fraction,
    }.items():

        if (
            value < 0
            or value > 1
        ):
            raise RuntimeError(
                f"Invalid BBB policy {name}: {value}"
            )

    if execution_days <= 0:
        raise RuntimeError(
            "BBB execution_days must be positive"
        )

    effective = (
        direct
        + liquidity
        * liquidity_xnet_fraction
    )

    if effective > 1:
        raise RuntimeError(
            "BBB effective market-buy share "
            "exceeds 100%"
        )

    return {
        "basis":
            BBB_POLICY_CONFIG.get(
                "basis",
                "latest_wifi_payment_received_usd",
            ),

        "direct_bbb_share_of_received_revenue":
            float(direct),

        "liquidity_share_of_received_revenue":
            float(liquidity),

        "liquidity_xnet_market_buy_fraction":
            float(
                liquidity_xnet_fraction
            ),

        "effective_xnet_market_buy_share":
            float(effective),

        "execution_days":
            execution_days,
    }


def bbb_usdc_balance():
    previous = previous_snapshot()

    payload = json.dumps({
        "jsonrpc":
            "2.0",

        "id":
            1,

        "method":
            "getTokenAccountsByOwner",

        "params": [
            BBB_WALLET,

            {
                "mint":
                    USDC_MINT,
            },

            {
                "encoding":
                    "jsonParsed",

                "commitment":
                    "confirmed",
            },
        ],
    }).encode(
        "utf-8"
    )

    req = urllib.request.Request(
        SOLANA_RPC,

        data=payload,

        headers={
            "Content-Type":
                "application/json",

            "User-Agent":
                "xnet-v3-dashboard/1.0",
        },

        method="POST",
    )

    try:
        with urllib.request.urlopen(
            req,
            timeout=30,
        ) as resp:

            body = json.loads(
                resp.read().decode(
                    "utf-8"
                )
            )

        if body.get("error"):
            raise RuntimeError(
                json.dumps(
                    body["error"]
                )
            )

        accounts = (
            (
                body.get(
                    "result"
                )
                or {}
            )
            .get(
                "value"
            )
            or []
        )

        total = Decimal("0")

        for item in accounts:
            amount = (
                item[
                    "account"
                ][
                    "data"
                ][
                    "parsed"
                ][
                    "info"
                ][
                    "tokenAmount"
                ][
                    "uiAmountString"
                ]
            )

            total += Decimal(
                str(amount)
            )

        observed = (
            datetime.now(
                timezone.utc
            )
            .replace(
                microsecond=0
            )
            .isoformat()
            .replace(
                "+00:00",
                "Z",
            )
        )

        return (
            total,
            observed,
            "current",
        )

    except Exception as e:
        old_balance = previous.get(
            "bbb_wallet_usdc_balance"
        )

        old_observed = previous.get(
            "bbb_wallet_usdc_observed_at_utc"
        )

        if (
            old_balance is not None
            and old_observed
        ):
            print(
                "WARNING: BBB USDC RPC failed; "
                "preserving last-good value:",
                e,
            )

            return (
                Decimal(
                    str(
                        old_balance
                    )
                ),
                old_observed,
                "last_good",
            )

        raise RuntimeError(
            "BBB USDC RPC failed and no "
            "last-good value exists"
        ) from e


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

circulating = D(
    supply["latest_circulating_supply_xnet"]
)

burned = D(
    burns["verified_bbb_burned_xnet"]
)

bbb_usdc, bbb_usdc_observed_at, bbb_usdc_status = (
    bbb_usdc_balance()
)

policy = bbb_policy()


snapshot = {
    "schema_version": 1,

    "chain_collected_at_utc": load("xnet_chain_health.json").get("last_refresh_completed_utc"),
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


    "bbb_wallet_usdc_balance":
        str(bbb_usdc),

    "bbb_wallet_usdc_observed_at_utc":
        bbb_usdc_observed_at,

    "bbb_wallet_usdc_status":
        bbb_usdc_status,

    "bbb_execution_policy":
        policy,

    "latest_transfer_event_utc":
        supply["latest_transfer_event_utc"],

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
