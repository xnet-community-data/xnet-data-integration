#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state/v3_headline_visuals.json"

state = json.loads(STATE.read_text())
visuals = state["visualizations"]


def run(cmd):
    p = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if p.returncode != 0:
        print(p.stdout)
        print(p.stderr)
        raise RuntimeError("Command failed: " + " ".join(cmd))
    return p.stdout


def counter(
    key,
    title,
    column,
    description,
    decimals=0,
    prefix=None,
    suffix=None,
    label=None,
):
    vid = int(visuals[key]["id"])
    options = {
        "counterColName": column,
        "rowNumber": 1,
        "stringDecimal": decimals,
        "counterLabel": title if label is None else label,
    }

    if prefix is not None:
        options["stringPrefix"] = prefix
    if suffix is not None:
        options["stringSuffix"] = suffix

    run([
        "dune",
        "viz",
        "update",
        str(vid),
        "--name",
        title,
        "--description",
        description,
        "--options",
        json.dumps(options, separators=(",", ":")),
        "-o",
        "json",
    ])

    visuals[key]["name"] = title
    print(f"{key:28} -> {title} | label={options['counterLabel']}")


specs = [
    ("price", "XNET Price", "xnet_price_usd",
     "Current XNET price.", 6, "$", None, None),

    ("market_cap", "Market Cap", "market_cap_usd",
     "Current XNET market capitalization.", 0, "$", None, None),

    ("fdv", "FDV", "fdv_usd",
     "Current XNET fully diluted valuation.", 0, "$", None, None),

    ("dex_volume_24h", "24h DEX Volume", "total_dex_volume_h24_usd",
     "XNET DEX trading volume over the last 24 hours.", 0, "$", None, None),

    ("dex_liquidity", "DEX Liquidity", "total_dex_liquidity_usd",
     "Total XNET liquidity across observed DEX pools.", 0, "$", None, None),

    ("circulating", "Circulating Supply", "circulating_supply_xnet",
     "Current XNET circulating supply.", 0, None, " XNET", None),

    ("revenue_arr", "Annualized Revenue Run Rate",
     "annualized_revenue_run_rate_usd",
     "Latest projected monthly WiFi revenue annualized.",
     0, "$", None, None),

    ("market_cap_arr", "P/S Ratio",
     "market_cap_to_revenue_run_rate",
     "Market Cap divided by the annualized projected WiFi revenue run rate.",
     2, None, "×", None),

    ("latest_offload", "Latest Daily Offload", "latest_daily_offload_gb",
     "Latest daily XNET network offload.", 0, None, " GB", None),

    ("avg_offload_30d", "30-Day Avg Daily Offload",
     "avg_daily_offload_gb_30d",
     "Average daily network offload over the latest 30 days.",
     0, None, " GB/day", None),

    ("all_time_offload", "All-Time Network Offload",
     "all_time_network_offload_gb",
     "Cumulative XNET network offload.", 0, None, " GB", None),

    ("total_devices", "Total Devices", "total_devices",
     "Total XNET devices.", 0, None, None, None),

    ("operational_devices", "Operational Devices", "operational_devices",
     "Operational XNET devices.", 0, None, None, None),

    ("device_growth_30d", "30-Day Device Growth", "device_growth_30d_pct",
     "Change in total devices over 30 days.", 1, None, "%", None),

    ("latest_projected_revenue", "WiFi Revenue (Projected)",
     "latest_projected_wifi_revenue_usd",
     "Projected WiFi revenue for Aug 2026, the latest service month in the revenue sheet. Service-month reporting is delayed, so this can lag the current month.",
     2, "$", None, "Aug 2026"),

    ("latest_payment", "WiFi Payment (Received)",
     "latest_wifi_payment_received_usd",
     "Carrier payment received 25 Sep 2026 for Jul 2026 service. Carrier payments settle after the service month, so cash receipts lag network activity.",
     2, "$", None, "Jul 2026"),

    ("outstanding", "Balance Outstanding to Transfer",
     "balance_outstanding_to_transfer_usd",
     "Balance outstanding to transfer from the XNET revenue sheet. The sheet is updated manually, so this figure can lag actual transfers.",
     2, "$", None, None),

    ("bbb_burned", "XNET Burned", "verified_bbb_burned_xnet",
     "Total XNET burned through Buy & Burn.",
     0, None, " XNET", None),

    ("bbb_transfers", "Transferred to Buy & Burn",
     "cumulative_bbb_transfers_usd",
     "Total USD transferred to Buy & Burn.",
     2, "$", None, None),

    ("bbb_wallet_balance", "Buy & Burn Wallet Balance",
     "bbb_wallet_usdc_balance",
     "Current USDC balance held by the primary Buy & Burn wallet.",
     2, "$", None, None),

    ("holders", "XNET Holders", "holder_count_positive",
     "Current XNET holder count.", 0, None, None, None),

    ("holders_ge_100", "Holders ≥ 100 XNET", "holders_ge_100_xnet",
     "XNET holders with at least 100 XNET.", 0, None, None, None),

    ("holders_ge_1000", "Holders ≥ 1,000 XNET", "holders_ge_1000_xnet",
     "XNET holders with at least 1,000 XNET.", 0, None, None, None),
]

for spec in specs:
    counter(*spec)

STATE.write_text(json.dumps(state, indent=2) + "\n")
print()
print("Counter naming PASS")
