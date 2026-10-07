#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

QUERY_ID = 8894185

STATE = (
    ROOT
    / "state/v3_headline_visuals.json"
)


SPECS = [
    # --------------------------------------------------
    # MARKET
    # --------------------------------------------------
    {
        "key": "price",
        "name": "XNET Price",
        "type": "counter",
        "description":
            "Current XNET USD price from the "
            "highest-liquidity XNET-base pool.",
        "options": {
            "counterColName":
                "xnet_price_usd",
            "rowNumber": 1,
            "stringDecimal": 6,
            "stringPrefix": "$",
            "counterLabel":
                "Live market price",
        },
    },

    {
        "key": "market_cap",
        "name": "XNET Market Cap",
        "type": "counter",
        "description":
            "Verified circulating supply multiplied "
            "by current XNET USD price.",
        "options": {
            "counterColName":
                "market_cap_usd",
            "rowNumber": 1,
            "stringDecimal": 0,
            "stringPrefix": "$",
            "counterLabel":
                "Verified circulation × live price",
        },
    },

    {
        "key": "dex_liquidity",
        "name": "DEX Liquidity",
        "type": "counter",
        "description":
            "Total observed XNET DEX liquidity "
            "across current DexScreener pools.",
        "options": {
            "counterColName":
                "total_dex_liquidity_usd",
            "rowNumber": 1,
            "stringDecimal": 0,
            "stringPrefix": "$",
            "counterLabel":
                "Across observed XNET pools",
        },
    },

    # --------------------------------------------------
    # NETWORK
    # --------------------------------------------------
    {
        "key": "latest_offload",
        "name": "Latest Daily Offload",
        "type": "counter",
        "description":
            "Latest verified daily XNET network "
            "offload reading.",
        "options": {
            "counterColName":
                "latest_daily_offload_gb",
            "rowNumber": 1,
            "stringDecimal": 0,
            "stringSuffix": " GB",
            "counterLabel":
                "Latest verified network day",
        },
    },

    {
        "key": "avg_offload_30d",
        "name": "30-Day Avg Daily Offload",
        "type": "counter",
        "description":
            "Average daily network offload over "
            "the latest available 30-day period.",
        "options": {
            "counterColName":
                "avg_daily_offload_gb_30d",
            "rowNumber": 1,
            "stringDecimal": 0,
            "stringSuffix": " GB/day",
            "counterLabel":
                "30-day daily average",
        },
    },

    {
        "key": "operational_devices",
        "name": "Operational Devices",
        "type": "counter",
        "description":
            "Latest reported count of operational "
            "XNET devices.",
        "options": {
            "counterColName":
                "operational_devices",
            "rowNumber": 1,
            "stringDecimal": 0,
            "counterLabel":
                "Latest verified device observation",
        },
    },

    # --------------------------------------------------
    # COMMERCIAL
    # --------------------------------------------------
    {
        "key": "revenue_arr",
        "name": "Annualized Revenue Run Rate",
        "type": "counter",
        "description":
            "Annualized revenue run rate derived "
            "from the latest service month.",
        "options": {
            "counterColName":
                "annualized_revenue_run_rate_usd",
            "rowNumber": 1,
            "stringDecimal": 0,
            "stringPrefix": "$",
            "counterLabel":
                "Annualized from latest service month",
        },
    },

    {
        "key": "annualized_fees_30d",
        "name": "Annualized Service Fees",
        "type": "counter",
        "description":
            "Trailing 30-day carrier service Fees "
            "annualized at 365/30. Run rate only, "
            "not guidance or a forecast.",
        "options": {
            "counterColName":
                "annualized_fees_30d_usd",
            "rowNumber": 1,
            "stringDecimal": 0,
            "stringPrefix": "$",
            "counterLabel":
                "Trailing 30d × 365/30",
        },
    },

    {
        "key": "annualized_retained_revenue_30d",
        "name": "Annualized Retained Revenue",
        "type": "counter",
        "description":
            "Trailing 30-day Revenue after fiat "
            "operator Supply-Side Revenue, annualized "
            "at 365/30. Run rate only.",
        "options": {
            "counterColName":
                "annualized_retained_revenue_30d_usd",
            "rowNumber": 1,
            "stringDecimal": 0,
            "stringPrefix": "$",
            "counterLabel":
                "Trailing 30d × 365/30",
        },
    },

    {
        "key": "annualized_holders_revenue_30d",
        "name": "Annualized Holders Revenue",
        "type": "counter",
        "description":
            "Trailing 30-day holder/BBB accrual "
            "annualized at 365/30. Run rate only.",
        "options": {
            "counterColName":
                "annualized_holders_revenue_30d_usd",
            "rowNumber": 1,
            "stringDecimal": 0,
            "stringPrefix": "$",
            "counterLabel":
                "Trailing 30d × 365/30",
        },
    },

    {
        "key": "annualized_protocol_revenue_30d",
        "name": "Annualized Protocol Revenue",
        "type": "counter",
        "description":
            "Trailing 30-day protocol-retained "
            "accrual annualized at 365/30. Run rate "
            "only.",
        "options": {
            "counterColName":
                "annualized_protocol_revenue_30d_usd",
            "rowNumber": 1,
            "stringDecimal": 0,
            "stringPrefix": "$",
            "counterLabel":
                "Trailing 30d × 365/30",
        },
    },

    {
        "key": "latest_payment",
        "name": "Latest Wi-Fi Payment",
        "type": "counter",
        "description":
            "Latest recorded Wi-Fi payment received "
            "in the XNET revenue source.",
        "options": {
            "counterColName":
                "latest_wifi_payment_received_usd",
            "rowNumber": 1,
            "stringDecimal": 2,
            "stringPrefix": "$",
            "counterLabel":
                "Latest recorded receipt",
        },
    },

    {
        "key": "outstanding",
        "name": "Balance Outstanding to Transfer",
        "type": "counter",
        "description":
            "Latest reported balance outstanding "
            "to transfer.",
        "options": {
            "counterColName":
                "balance_outstanding_to_transfer_usd",
            "rowNumber": 1,
            "stringDecimal": 2,
            "stringPrefix": "$",
            "counterLabel":
                "Source-reported outstanding balance",
        },
    },

    # --------------------------------------------------
    # TOKEN / BBB
    # --------------------------------------------------
    {
        "key": "circulating",
        "name": "Circulating Supply",
        "type": "counter",
        "description":
            "Reconstructed circulation using the "
            "official excluded-wallet flow and burn "
            "methodology.",
        "options": {
            "counterColName":
                "circulating_supply_xnet",
            "rowNumber": 1,
            "stringDecimal": 0,
            "stringSuffix": " XNET",
            "counterLabel":
                "XNET in circulation",
        },
    },

    {
        "key": "holders",
        "name": "XNET Holders",
        "type": "counter",
        "description":
            "Positive-balance XNET owner count.",
        "options": {
            "counterColName":
                "holder_count_positive",
            "rowNumber": 1,
            "stringDecimal": 0,
            "counterLabel":
                "XNET holders",
        },
    },

    {
        "key": "bbb_burned",
        "name": "Verified Buy & Burn",
        "type": "counter",
        "description":
            "Cumulative XNET burned from the "
            "verified primary BBB wallet flow.",
        "options": {
            "counterColName":
                "verified_bbb_burned_xnet",
            "rowNumber": 1,
            "stringDecimal": 0,
            "stringSuffix": " XNET",
            "counterLabel":
                "Verified cumulative BBB burns",
        },
    },

    # --------------------------------------------------
    # FRESHNESS
    # --------------------------------------------------
    {
        "key": "freshness",
        "name": "Data Freshness",
        "type": "table",
        "description":
            "Source-specific observation dates. "
            "Network readings remain visible when "
            "the upstream APIs are unavailable.",
        "options": {
            "itemsPerPage": 1,
            "columns": [
                {
                    "name":
                        "network_status",
                    "title":
                        "Network Status",
                    "type":
                        "normal",
                    "alignContent":
                        "left",
                    "isHidden":
                        False,
                },
                {
                    "name":
                        "offload_data_as_of",
                    "title":
                        "Offload As Of",
                    "type":
                        "normal",
                    "alignContent":
                        "left",
                    "isHidden":
                        False,
                },
                {
                    "name":
                        "device_data_as_of",
                    "title":
                        "Devices As Of",
                    "type":
                        "normal",
                    "alignContent":
                        "left",
                    "isHidden":
                        False,
                },
                {
                    "name":
                        "revenue_service_month",
                    "title":
                        "Revenue Service Month",
                    "type":
                        "normal",
                    "alignContent":
                        "left",
                    "isHidden":
                        False,
                },
                {
                    "name":
                        "latest_wifi_payment_date",
                    "title":
                        "Latest Payment",
                    "type":
                        "normal",
                    "alignContent":
                        "left",
                    "isHidden":
                        False,
                },
                {
                    "name":
                        "latest_transfer_event_utc",
                    "title":
                        "Chain Through",
                    "type":
                        "normal",
                    "alignContent":
                        "left",
                    "isHidden":
                        False,
                },
            ],
        },
    },
]


def write_state(obj):
    STATE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = STATE.with_suffix(
        ".json.tmp"
    )

    tmp.write_text(
        json.dumps(
            obj,
            indent=2,
        ) + "\n"
    )

    tmp.replace(STATE)


if STATE.exists():
    state = json.loads(
        STATE.read_text()
    )
else:
    state = {
        "schema_version": 1,
        "query_id": QUERY_ID,
        "visualizations": {},
        "order": [
            s["key"]
            for s in SPECS
        ],
    }


if int(state["query_id"]) != QUERY_ID:
    raise SystemExit(
        "FAIL: visualization state belongs "
        "to another query"
    )


for spec in SPECS:

    key = spec["key"]

    if key in state["visualizations"]:
        print(
            "SKIP existing:",
            key,
            state[
                "visualizations"
            ][key]["id"],
        )
        continue

    print()
    print(
        "Creating:",
        spec["name"],
    )

    cmd = [
        "dune",
        "viz",
        "create",
        "--query-id",
        str(QUERY_ID),
        "--name",
        spec["name"],
        "--type",
        spec["type"],
        "--description",
        spec["description"],
        "--options",
        json.dumps(
            spec["options"],
            separators=(",", ":"),
        ),
        "-o",
        "json",
    ]

    p = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    if p.returncode != 0:
        print(p.stdout)
        print(p.stderr)
        raise SystemExit(
            f"FAIL creating {spec['name']}"
        )

    try:
        result = json.loads(
            p.stdout
        )
    except Exception:
        print(p.stdout)
        raise SystemExit(
            "FAIL parsing visualization response"
        )

    viz_id = (
        result.get("id")
        or result.get("visualization_id")
    )

    if not viz_id:
        print(result)
        raise SystemExit(
            "FAIL: visualization ID missing"
        )

    state[
        "visualizations"
    ][key] = {
        "id": int(viz_id),
        "name": spec["name"],
        "type": spec["type"],
        "column":
            spec["options"].get(
                "counterColName"
            ),
    }

    # Persist immediately so an interruption
    # does not create duplicates on resume.
    write_state(state)

    print(
        "Created:",
        viz_id,
    )


print()
print(
    "=== V3 HEADLINE VISUALS COMPLETE ==="
)

for key in state["order"]:
    item = state[
        "visualizations"
    ][key]

    print(
        key,
        "->",
        item["id"],
        item["name"],
    )
