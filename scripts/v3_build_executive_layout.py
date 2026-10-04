#!/usr/bin/env python3

from __future__ import annotations

import collections
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

DASHBOARD_ID = 221353
QUERY_ID = 8894185

STATE_PATH = (
    ROOT
    / "state/v3_headline_visuals.json"
)

V3_BEFORE = (
    ROOT
    / "state/v3_before_executive_layout.json"
)

V2_REF = (
    ROOT
    / "state/v2_layout_reference.json"
)


def run(cmd):
    p = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    if p.returncode != 0:
        print(p.stdout)
        print(p.stderr)
        raise RuntimeError(
            "Command failed: "
            + " ".join(cmd)
        )

    return p.stdout


def save_state(state):
    tmp = STATE_PATH.with_suffix(
        ".json.tmp"
    )

    tmp.write_text(
        json.dumps(
            state,
            indent=2,
        )
        + "\n"
    )

    tmp.replace(
        STATE_PATH
    )


state = json.loads(
    STATE_PATH.read_text()
)

visuals = state.setdefault(
    "visualizations",
    {},
)


# --------------------------------------------------
# Existing visual naming cleanup
# --------------------------------------------------

rename_existing = {
    "price": (
        "XNET Price",
        "Current XNET USD market price."
    ),

    "market_cap": (
        "Circulating Market Cap",
        (
            "Live XNET price multiplied by "
            "verified reconstructed circulating supply."
        ),
    ),

    "dex_liquidity": (
        "DEX Liquidity",
        (
            "Total observed XNET liquidity "
            "across current DEX pools."
        ),
    ),

    "latest_offload": (
        "Latest Daily Offload",
        "Latest verified daily network offload.",
    ),

    "avg_offload_30d": (
        "30-Day Avg Daily Offload",
        (
            "Average daily offload across "
            "the latest available 30-day period."
        ),
    ),

    "operational_devices": (
        "Operational Devices",
        "Latest verified operational-device count.",
    ),

    "revenue_arr": (
        "Annualized Wi-Fi Revenue Run Rate",
        (
            "Annualized from the latest "
            "projected Wi-Fi service-revenue month."
        ),
    ),

    "latest_payment": (
        "Latest Wi-Fi Payment",
        "Latest recorded Wi-Fi cash receipt.",
    ),

    "outstanding": (
        "Outstanding Transfer Balance",
        (
            "Latest source-reported balance "
            "outstanding to transfer."
        ),
    ),

    "circulating": (
        "Circulating Supply",
        (
            "Verified reconstructed XNET "
            "circulating supply."
        ),
    ),

    "holders": (
        "Positive-Balance Holders",
        "Owners with a positive XNET balance.",
    ),

    "bbb_burned": (
        "Verified XNET Burned",
        (
            "Cumulative verified burns attributable "
            "to the primary BBB wallet flow."
        ),
    ),

    "freshness": (
        "Data Freshness & Source Status",
        (
            "Observation dates for market, chain, "
            "network and commercial sources."
        ),
    ),
}

for key, (
    name,
    description,
) in rename_existing.items():

    if key not in visuals:
        continue

    vid = visuals[key]["id"]

    run([
        "dune",
        "viz",
        "update",
        str(vid),
        "--name",
        name,
        "--description",
        description,
        "-o",
        "json",
    ])

    visuals[key]["name"] = name


# --------------------------------------------------
# Missing counters
# --------------------------------------------------

specs = {
    "fdv": {
        "name":
            "Fully Diluted Valuation",
        "column":
            "fdv_usd",
        "prefix":
            "$",
        "suffix":
            "",
        "decimals":
            0,
        "label":
            "Published max supply × live price",
        "description":
            (
                "Published maximum XNET supply "
                "multiplied by current market price."
            ),
    },

    "dex_volume_24h": {
        "name":
            "24h DEX Volume",
        "column":
            "total_dex_volume_h24_usd",
        "prefix":
            "$",
        "suffix":
            "",
        "decimals":
            0,
        "label":
            "Observed XNET pool volume",
        "description":
            (
                "Aggregate 24-hour volume across "
                "observed XNET DEX pools."
            ),
    },

    "market_cap_arr": {
        "name":
            "Market Cap / Revenue Run Rate",
        "column":
            "market_cap_to_revenue_run_rate",
        "prefix":
            "",
        "suffix":
            "×",
        "decimals":
            2,
        "label":
            "Circulating market cap / annualized revenue",
        "description":
            (
                "Circulating market capitalization "
                "divided by annualized projected "
                "Wi-Fi service revenue."
            ),
    },

    "fdv_arr": {
        "name":
            "FDV / Revenue Run Rate",
        "column":
            "fdv_to_revenue_run_rate",
        "prefix":
            "",
        "suffix":
            "×",
        "decimals":
            2,
        "label":
            "FDV / annualized revenue",
        "description":
            (
                "Fully diluted valuation divided by "
                "annualized projected Wi-Fi service revenue."
            ),
    },

    "all_time_offload": {
        "name":
            "All-Time Network Offload",
        "column":
            "all_time_network_offload_gb",
        "prefix":
            "",
        "suffix":
            " GB",
        "decimals":
            0,
        "label":
            "Cumulative verified network usage",
        "description":
            "Cumulative reported XNET network offload.",
    },

    "total_devices": {
        "name":
            "Total Devices",
        "column":
            "total_devices",
        "prefix":
            "",
        "suffix":
            "",
        "decimals":
            0,
        "label":
            "Latest verified device observation",
        "description":
            "Latest reported total device count.",
    },

    "device_growth_30d": {
        "name":
            "30-Day Device Growth",
        "column":
            "device_growth_30d_pct",
        "prefix":
            "",
        "suffix":
            "%",
        "decimals":
            1,
        "label":
            "Total-device growth",
        "description":
            (
                "Change in total reported devices "
                "versus the latest observation on "
                "or before 30 days earlier."
            ),
    },

    "latest_projected_revenue": {
        "name":
            "Latest Projected Service Revenue",
        "column":
            "latest_projected_wifi_revenue_usd",
        "prefix":
            "$",
        "suffix":
            "",
        "decimals":
            2,
        "label":
            "Latest reported service month",
        "description":
            (
                "Projected Wi-Fi service revenue "
                "for the latest available service month."
            ),
    },

    "bbb_transfers": {
        "name":
            "Recorded BBB Transfers",
        "column":
            "cumulative_bbb_transfers_usd",
        "prefix":
            "$",
        "suffix":
            "",
        "decimals":
            2,
        "label":
            "Cumulative source-recorded transfers",
        "description":
            (
                "Cumulative USD recorded by the "
                "commercial source as transferred "
                "to Buy & Burn."
            ),
    },

    "bbb_wallet_balance": {
        "name":
            "BBB Wallet Balance",
        "column":
            "bbb_wallet_xnet_balance",
        "prefix":
            "",
        "suffix":
            " XNET",
        "decimals":
            0,
        "label":
            "Primary verified BBB wallet",
        "description":
            (
                "Current XNET balance of the "
                "primary verified BBB wallet."
            ),
    },

    "holders_ge_100": {
        "name":
            "Holders ≥ 100 XNET",
        "column":
            "holders_ge_100_xnet",
        "prefix":
            "",
        "suffix":
            "",
        "decimals":
            0,
        "label":
            "Owners with at least 100 XNET",
        "description":
            (
                "Current positive-balance owners "
                "holding at least 100 XNET."
            ),
    },

    "holders_ge_1000": {
        "name":
            "Holders ≥ 1,000 XNET",
        "column":
            "holders_ge_1000_xnet",
        "prefix":
            "",
        "suffix":
            "",
        "decimals":
            0,
        "label":
            "Owners with at least 1,000 XNET",
        "description":
            (
                "Current positive-balance owners "
                "holding at least 1,000 XNET."
            ),
    },
}


for key, spec in specs.items():

    if key in visuals:
        print(
            "SKIP existing:",
            key,
            visuals[key]["id"],
        )
        continue

    options = {
        "counterColName":
            spec["column"],

        "rowNumber":
            1,

        "stringDecimal":
            spec["decimals"],

        "counterLabel":
            spec["label"],
    }

    if spec["prefix"]:
        options[
            "stringPrefix"
        ] = spec["prefix"]

    if spec["suffix"]:
        options[
            "stringSuffix"
        ] = spec["suffix"]

    print(
        "Creating:",
        spec["name"],
    )

    raw = run([
        "dune",
        "viz",
        "create",
        "--query-id",
        str(QUERY_ID),
        "--name",
        spec["name"],
        "--type",
        "counter",
        "--description",
        spec["description"],
        "--options",
        json.dumps(
            options,
            separators=(",", ":"),
        ),
        "-o",
        "json",
    ])

    result = json.loads(raw)

    vid = (
        result.get("id")
        or result.get(
            "visualization_id"
        )
    )

    if not vid:
        print(result)
        raise RuntimeError(
            "Visualization ID missing"
        )

    visuals[key] = {
        "id":
            int(vid),

        "name":
            spec["name"],

        "type":
            "counter",

        "column":
            spec["column"],
    }

    save_state(state)

    print(
        key,
        "->",
        vid,
    )


save_state(state)


# --------------------------------------------------
# Read layout references
# --------------------------------------------------

current = json.loads(
    V3_BEFORE.read_text()
)

v2 = json.loads(
    V2_REF.read_text()
)

existing_positions = {}

for item in (
    current.get(
        "visualization_widgets"
    )
    or []
):

    vid = item.get(
        "visualization_id"
    )

    pos = item.get(
        "position"
    )

    if (
        vid is not None
        and isinstance(
            pos,
            dict,
        )
    ):
        existing_positions[
            int(vid)
        ] = pos


def mode(values, fallback):
    values = [
        int(v)
        for v in values
        if v is not None
        and int(v) > 0
    ]

    if not values:
        return fallback

    return collections.Counter(
        values
    ).most_common(1)[0][0]


fresh_id = visuals[
    "freshness"
]["id"]

counter_ids = [
    item["id"]
    for key, item
    in visuals.items()
    if key != "freshness"
]

counter_positions = [
    existing_positions[vid]
    for vid in counter_ids
    if vid in existing_positions
]


counter_w = mode(
    [
        p.get("size_x")
        for p
        in counter_positions
    ],
    4,
)

counter_h = mode(
    [
        p.get("size_y")
        for p
        in counter_positions
    ],
    5,
)

grid_w = (
    counter_w
    * 3
)


fresh_pos = (
    existing_positions.get(
        fresh_id
    )
    or {}
)

fresh_h = int(
    fresh_pos.get(
        "size_y",
        counter_h + 2,
    )
)


v2_text_heights = []

for item in (
    v2.get(
        "text_widgets"
    )
    or []
):
    pos = item.get(
        "position"
    ) or {}

    h = pos.get(
        "size_y"
    )

    if (
        h is not None
        and int(h) > 0
    ):
        v2_text_heights.append(
            int(h)
        )


section_h = (
    min(v2_text_heights)
    if v2_text_heights
    else max(
        2,
        counter_h // 2,
    )
)

section_h = max(
    2,
    section_h,
)


current_text = (
    current.get(
        "text_widgets"
    )
    or []
)

intro_h = (
    int(
        (
            current_text[0]
            .get(
                "position",
                {},
            )
            .get(
                "size_y",
                section_h * 2,
            )
        )
    )
    if current_text
    else section_h * 2
)

intro_h = max(
    intro_h,
    section_h * 2,
)


print()
print(
    "Layout dimensions:"
)

print(
    "counter:",
    counter_w,
    "x",
    counter_h,
)

print(
    "grid width:",
    grid_w,
)

print(
    "section height:",
    section_h,
)

print(
    "freshness height:",
    fresh_h,
)


# --------------------------------------------------
# Build manual layout
# --------------------------------------------------

viz_widgets = []
text_widgets = []

row = 0


def text_block(
    text,
    height=None,
):

    global row

    h = (
        height
        if height is not None
        else section_h
    )

    text_widgets.append({
        "text":
            text,

        "position": {
            "row":
                row,

            "col":
                0,

            "size_x":
                grid_w,

            "size_y":
                h,
        },
    })

    row += h


def full_viz(
    key,
    height,
):

    global row

    vid = visuals[key]["id"]

    viz_widgets.append({
        "visualization_id":
            vid,

        "position": {
            "row":
                row,

            "col":
                0,

            "size_x":
                grid_w,

            "size_y":
                height,
        },
    })

    row += height


def counter_row(keys):

    global row

    if len(keys) != 3:
        raise RuntimeError(
            "Counter row must contain 3 widgets"
        )

    for i, key in enumerate(keys):

        if key not in visuals:
            raise RuntimeError(
                f"Missing visualization: {key}"
            )

        viz_widgets.append({
            "visualization_id":
                visuals[key]["id"],

            "position": {
                "row":
                    row,

                "col":
                    i * counter_w,

                "size_x":
                    counter_w,

                "size_y":
                    counter_h,
            },
        })

    row += counter_h


# --------------------------------------------------
# INTRO
# --------------------------------------------------

text_block(
    (
        "# XNET — Network, Revenue & Buy/Burn\n\n"
        "A live view of XNET's token market, "
        "network usage, commercial performance "
        "and verified Buy & Burn activity. "
        "Market and on-chain state update independently "
        "from network and commercial reporting, so "
        "source-specific freshness is shown explicitly."
    ),
    intro_h,
)


# --------------------------------------------------
# FRESHNESS
# --------------------------------------------------

full_viz(
    "freshness",
    fresh_h,
)


# --------------------------------------------------
# MARKET
# --------------------------------------------------

text_block(
    (
        "## XNET Market Snapshot\n\n"
        "Current token valuation, trading activity, "
        "liquidity and verified circulating supply."
    )
)

counter_row([
    "price",
    "market_cap",
    "fdv",
])

counter_row([
    "dex_volume_24h",
    "dex_liquidity",
    "circulating",
])


# --------------------------------------------------
# FUNDAMENTALS / VALUATION
# --------------------------------------------------

text_block(
    (
        "## Fundamentals & Valuation\n\n"
        "Connecting XNET's current token valuation "
        "to the latest projected Wi-Fi revenue run rate. "
        "These multiples use annualized projected "
        "service revenue, not cash already received."
    )
)

counter_row([
    "revenue_arr",
    "market_cap_arr",
    "fdv_arr",
])


# --------------------------------------------------
# NETWORK
# --------------------------------------------------

text_block(
    (
        "## Network Activity\n\n"
        "Carrier offload usage and growth of the "
        "deployed XNET device footprint. "
        "The freshness panel above shows when "
        "last-known-good values are being displayed."
    )
)

counter_row([
    "latest_offload",
    "avg_offload_30d",
    "all_time_offload",
])

counter_row([
    "total_devices",
    "operational_devices",
    "device_growth_30d",
])


# --------------------------------------------------
# COMMERCIAL
# --------------------------------------------------

text_block(
    (
        "## Commercial Performance\n\n"
        "Projected service revenue, cash receipts "
        "and settlement balances are intentionally "
        "shown as separate stages of the commercial cycle."
    )
)

counter_row([
    "latest_projected_revenue",
    "latest_payment",
    "outstanding",
])


# --------------------------------------------------
# BBB
# --------------------------------------------------

text_block(
    (
        "## Buy & Burn\n\n"
        "Verified primary BBB-wallet activity and "
        "commercial-source transfers. "
        "Transfers, market purchases and burns occur "
        "on different clocks and are not assumed to "
        "map one-for-one."
    )
)

counter_row([
    "bbb_burned",
    "bbb_transfers",
    "bbb_wallet_balance",
])


# --------------------------------------------------
# SUPPLY / OWNERSHIP
# --------------------------------------------------

text_block(
    (
        "## Supply & Ownership\n\n"
        "Current positive-balance holder distribution "
        "derived from the canonical XNET transfer state."
    )
)

counter_row([
    "holders",
    "holders_ge_100",
    "holders_ge_1000",
])


# --------------------------------------------------
# METHODOLOGY
# --------------------------------------------------

text_block(
    (
        "## Methodology & Definitions\n\n"
        "**Market cap** uses live XNET price × verified "
        "reconstructed circulating supply.  \n"
        "**FDV** uses live XNET price × published maximum supply.  \n"
        "**Network offload GB** is operational telemetry and is "
        "distinct from billing/revenue-sheet GB.  \n"
        "**Wi-Fi revenue** is projected service revenue unless "
        "explicitly labelled as received or recognized.  \n"
        "**Buy & Burn** counts verified burns attributable to the "
        "primary BBB wallet flow.  \n"
        "**Freshness** is source-specific; unavailable upstream "
        "feeds retain their last verified values rather than "
        "silently becoming zero."
    ),
    section_h * 3,
)


# --------------------------------------------------
# Collision QA
# --------------------------------------------------

boxes = []

for kind, items in (
    ("viz", viz_widgets),
    ("text", text_widgets),
):

    for item in items:

        p = item["position"]

        box = {
            "kind":
                kind,

            "row0":
                p["row"],

            "row1":
                p["row"]
                + p["size_y"],

            "col0":
                p["col"],

            "col1":
                p["col"]
                + p["size_x"],
        }

        boxes.append(box)


for i in range(len(boxes)):
    for j in range(
        i + 1,
        len(boxes),
    ):

        a = boxes[i]
        b = boxes[j]

        overlap = (
            a["row0"] < b["row1"]
            and b["row0"] < a["row1"]
            and a["col0"] < b["col1"]
            and b["col0"] < a["col1"]
        )

        if overlap:
            raise RuntimeError(
                f"Layout collision: {a} vs {b}"
            )


expected_keys = {
    "freshness",

    "price",
    "market_cap",
    "fdv",

    "dex_volume_24h",
    "dex_liquidity",
    "circulating",

    "revenue_arr",
    "market_cap_arr",
    "fdv_arr",

    "latest_offload",
    "avg_offload_30d",
    "all_time_offload",

    "total_devices",
    "operational_devices",
    "device_growth_30d",

    "latest_projected_revenue",
    "latest_payment",
    "outstanding",

    "bbb_burned",
    "bbb_transfers",
    "bbb_wallet_balance",

    "holders",
    "holders_ge_100",
    "holders_ge_1000",
}

used_ids = {
    item[
        "visualization_id"
    ]
    for item
    in viz_widgets
}

expected_ids = {
    visuals[key]["id"]
    for key
    in expected_keys
}

if used_ids != expected_ids:
    raise RuntimeError(
        "Visual layout completeness QA failed"
    )


out_viz = (
    ROOT
    / "state/v3_executive_visualization_widgets.json"
)

out_text = (
    ROOT
    / "state/v3_executive_text_widgets.json"
)

out_viz.write_text(
    json.dumps(
        viz_widgets,
        separators=(",", ":"),
    )
)

out_text.write_text(
    json.dumps(
        text_widgets,
        separators=(",", ":"),
    )
)

layout_manifest = {
    "schema_version":
        1,

    "dashboard_id":
        DASHBOARD_ID,

    "query_id":
        QUERY_ID,

    "grid_width":
        grid_w,

    "counter_width":
        counter_w,

    "counter_height":
        counter_h,

    "final_row":
        row,

    "visualization_count":
        len(viz_widgets),

    "text_widget_count":
        len(text_widgets),

    "visualization_widgets":
        viz_widgets,

    "text_widgets":
        text_widgets,
}

(
    ROOT
    / "state/v3_executive_layout_manifest.json"
).write_text(
    json.dumps(
        layout_manifest,
        indent=2,
    )
    + "\n"
)


print()
print(
    "=== EXECUTIVE LAYOUT READY ==="
)

print(
    "visualizations:",
    len(viz_widgets),
)

print(
    "text widgets:",
    len(text_widgets),
)

print(
    "final row:",
    row,
)
