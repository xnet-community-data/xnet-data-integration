#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
import urllib.request
from datetime import date, datetime, timedelta, timezone
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


def bbb_observed_spend():
    """Observed BBB buy spend and VWAP across up to seven complete UTC days."""
    path = ROOT / "data/derived/bbb_trades_daily.csv"

    empty = {
        "avg_daily_usd": None,
        "avg_buy_price_usd_per_xnet": None,
        "window_days": 0,
        "window_start": None,
        "window_end": None,
        "window_total_usd": None,
        "window_total_xnet_bought": None,
        "trade_days": 0,
    }

    if not path.exists():
        return empty

    health = load("xnet_chain_health.json")
    source = (
        health.get("sources", {})
        .get("bbb_dex", {})
    )

    completed_at = source.get("completed_at_utc")
    lookback_hours = (
        source.get("query_parameters", {})
        .get("lookback_hours")
    )

    if not completed_at or not lookback_hours:
        return empty

    completed_dt = datetime.fromisoformat(
        str(completed_at).replace("Z", "+00:00")
    )

    coverage_start_dt = (
        completed_dt
        - timedelta(hours=int(lookback_hours))
    )

    # Use only complete UTC calendar days that are fully inside the proven
    # query coverage window. The current/open UTC day is always excluded.
    first_full_day = coverage_start_dt.date()

    if coverage_start_dt.time() != datetime.min.time():
        first_full_day += timedelta(days=1)

    window_end = (
        completed_dt.date()
        - timedelta(days=1)
    )

    if window_end < first_full_day:
        return empty

    window_start = max(
        first_full_day,
        window_end - timedelta(days=6),
    )

    buy_value_by_day = {}
    xnet_bought_by_day = {}

    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            day_s = str(row.get("day") or "")[:10]

            if not day_s:
                continue

            day = date.fromisoformat(day_s)

            if not (
                window_start
                <= day
                <= window_end
            ):
                continue

            buy_value = row.get(
                "bbb_buy_value_usd"
            )

            # Migration fallback for pre-split rows is safe only where no
            # sells occurred on that day.
            if buy_value in (None, ""):
                if int(row.get("bbb_sell_count") or 0) != 0:
                    raise RuntimeError(
                        "BBB daily history needs buy/sell USD split before "
                        "observed spend can be calculated."
                    )

                buy_value = row.get(
                    "trade_value_usd"
                )

            buy_value_by_day[day] = D(
                buy_value
            )

            xnet_bought_by_day[day] = D(
                row.get("gross_xnet_bought")
            )

    window_days = (
        window_end - window_start
    ).days + 1

    total_usd = Decimal("0")
    total_xnet_bought = Decimal("0")
    trade_days = 0

    day = window_start

    while day <= window_end:
        buy_usd = buy_value_by_day.get(
            day,
            Decimal("0"),
        )

        xnet_bought = xnet_bought_by_day.get(
            day,
            Decimal("0"),
        )

        total_usd += buy_usd
        total_xnet_bought += xnet_bought

        if buy_usd > 0:
            trade_days += 1

        day += timedelta(days=1)

    avg_daily_usd = (
        total_usd
        / Decimal(window_days)
    )

    avg_buy_price = (
        total_usd
        / total_xnet_bought
        if total_xnet_bought > 0
        else None
    )

    return {
        "avg_daily_usd": avg_daily_usd,
        "avg_buy_price_usd_per_xnet": avg_buy_price,
        "window_days": window_days,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "window_total_usd": total_usd,
        "window_total_xnet_bought": total_xnet_bought,
        "trade_days": trade_days,
    }


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
observed_bbb_spend = bbb_observed_spend()


snapshot = {
    "schema_version": 3,

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

    "bbb_wallet_usdc_balance":
        str(bbb_usdc),

    "bbb_wallet_usdc_observed_at_utc":
        bbb_usdc_observed_at,

    "bbb_wallet_usdc_status":
        bbb_usdc_status,

    "bbb_execution_policy":
        policy,

    "bbb_observed_avg_daily_spend_usd":
        (
            str(
                observed_bbb_spend[
                    "avg_daily_usd"
                ]
            )
            if observed_bbb_spend[
                "avg_daily_usd"
            ] is not None
            else None
        ),

    "bbb_observed_spend_window_days":
        observed_bbb_spend[
            "window_days"
        ],

    "bbb_observed_spend_window_start":
        observed_bbb_spend[
            "window_start"
        ],

    "bbb_observed_spend_window_end":
        observed_bbb_spend[
            "window_end"
        ],

    "bbb_observed_spend_window_total_usd":
        (
            str(
                observed_bbb_spend[
                    "window_total_usd"
                ]
            )
            if observed_bbb_spend[
                "window_total_usd"
            ] is not None
            else None
        ),

    "bbb_observed_window_xnet_bought":
        (
            str(
                observed_bbb_spend[
                    "window_total_xnet_bought"
                ]
            )
            if observed_bbb_spend[
                "window_total_xnet_bought"
            ] is not None
            else None
        ),

    "bbb_observed_avg_buy_price_usd_per_xnet":
        (
            str(
                observed_bbb_spend[
                    "avg_buy_price_usd_per_xnet"
                ]
            )
            if observed_bbb_spend[
                "avg_buy_price_usd_per_xnet"
            ] is not None
            else None
        ),

    "bbb_observed_spend_trade_days":
        observed_bbb_spend[
            "trade_days"
        ],

    "bbb_observed_spend_methodology":
        (
            "actual_bbb_xnet_buy_value_and_volume_weighted_price_"
            "across_up_to_7_fully_covered_completed_utc_days"
        ),

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
