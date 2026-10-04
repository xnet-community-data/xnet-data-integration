#!/usr/bin/env python3
from __future__ import annotations
import json, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state/v3_headline_visuals.json"
state = json.loads(STATE.read_text())
visuals = state["visualizations"]

def run(cmd):
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stdout)
        print(p.stderr)
        raise RuntimeError("Command failed: " + " ".join(cmd))
    return p.stdout

def update_counter(key, name, description, column, label, decimals=0, prefix=None, suffix=None):
    if key not in visuals:
        raise RuntimeError(f"Missing visualization key: {key}")
    vid = int(visuals[key]["id"])
    options = {
        "counterColName": column,
        "rowNumber": 1,
        "stringDecimal": decimals,
        "counterLabel": label,
    }
    if prefix is not None:
        options["stringPrefix"] = prefix
    if suffix is not None:
        options["stringSuffix"] = suffix
    run([
        "dune", "viz", "update", str(vid),
        "--name", name,
        "--description", description,
        "--options", json.dumps(options, separators=(",", ":")),
        "-o", "json",
    ])
    visuals[key]["name"] = name
    print(f"{key:28} -> {name}")

specs = [
    ("price", "XNET Price", "Current XNET price.", "xnet_price_usd", "USD", 6, "$", None),
    ("market_cap", "Market Cap", "XNET price × circulating supply.", "market_cap_usd", "Circulating supply × price", 0, "$", None),
    ("fdv", "FDV", "XNET price × max supply.", "fdv_usd", "Max supply × price", 0, "$", None),
    ("dex_volume_24h", "24h DEX Volume", "XNET DEX trading volume over the last 24 hours.", "total_dex_volume_h24_usd", "Last 24 hours", 0, "$", None),
    ("dex_liquidity", "DEX Liquidity", "Total XNET liquidity across observed DEX pools.", "total_dex_liquidity_usd", "XNET DEX liquidity", 0, "$", None),
    ("circulating", "Circulating Supply", "Current XNET circulating supply.", "circulating_supply_xnet", "XNET in circulation", 0, None, " XNET"),
    ("revenue_arr", "Annualized Revenue Run Rate", "Latest monthly Wi-Fi revenue annualized.", "annualized_revenue_run_rate_usd", "Latest service month annualized", 0, "$", None),
    ("market_cap_arr", "Market Cap / ARR", "Market Cap divided by the annualized revenue run rate.", "market_cap_to_revenue_run_rate", "Market Cap / ARR", 2, None, "×"),
    ("fdv_arr", "FDV / ARR", "FDV divided by the annualized revenue run rate.", "fdv_to_revenue_run_rate", "FDV / ARR", 2, None, "×"),
    ("latest_offload", "Latest Daily Offload", "Latest daily XNET network offload.", "latest_daily_offload_gb", "Latest daily reading", 0, None, " GB"),
    ("avg_offload_30d", "30-Day Avg Daily Offload", "Average daily network offload over the latest 30 days.", "avg_daily_offload_gb_30d", "30-day average", 0, None, " GB/day"),
    ("all_time_offload", "All-Time Network Offload", "Cumulative XNET network offload.", "all_time_network_offload_gb", "Cumulative network offload", 0, None, " GB"),
    ("total_devices", "Total Devices", "Total XNET devices.", "total_devices", "Latest device reading", 0, None, None),
    ("operational_devices", "Operational Devices", "Operational XNET devices.", "operational_devices", "Latest device reading", 0, None, None),
    ("device_growth_30d", "30-Day Device Growth", "Change in total devices over 30 days.", "device_growth_30d_pct", "30-day change", 1, None, "%"),
    ("latest_projected_revenue", "Wi-Fi Revenue", "Projected Wi-Fi revenue for the latest service month.", "latest_projected_wifi_revenue_usd", "Latest service month", 2, "$", None),
    ("latest_payment", "Latest Wi-Fi Payment", "Latest Wi-Fi payment received.", "latest_wifi_payment_received_usd", "Latest payment received", 2, "$", None),
    ("outstanding", "Balance Outstanding to Transfer", "Latest balance outstanding to transfer.", "balance_outstanding_to_transfer_usd", "Current balance", 2, "$", None),
    ("bbb_burned", "XNET Burned", "Total XNET burned through Buy & Burn.", "verified_bbb_burned_xnet", "Cumulative XNET burned", 0, None, " XNET"),
    ("bbb_transfers", "Transferred to Buy & Burn", "Total USD transferred to Buy & Burn.", "cumulative_bbb_transfers_usd", "Cumulative transfers", 2, "$", None),
    ("bbb_wallet_balance", "BBB Wallet Balance", "Current XNET balance in the Buy & Burn wallet.", "bbb_wallet_xnet_balance", "Buy & Burn wallet", 0, None, " XNET"),
    ("holders", "XNET Holders", "Current XNET holder count.", "holder_count_positive", "XNET holders", 0, None, None),
    ("holders_ge_100", "Holders ≥ 100 XNET", "XNET holders with at least 100 XNET.", "holders_ge_100_xnet", "≥ 100 XNET", 0, None, None),
    ("holders_ge_1000", "Holders ≥ 1,000 XNET", "XNET holders with at least 1,000 XNET.", "holders_ge_1000_xnet", "≥ 1,000 XNET", 0, None, None),
]

for spec in specs:
    update_counter(*spec)

fresh_id = int(visuals["freshness"]["id"])
fresh_options = {
    "itemsPerPage": 5,
    "columns": [
        {"name": "freshness_source", "title": "Source", "type": "normal", "alignContent": "left", "isHidden": False},
        {"name": "freshness", "title": "Freshness", "type": "normal", "alignContent": "left", "isHidden": False},
        {"name": "freshness_covers", "title": "Data Points", "type": "normal", "alignContent": "left", "isHidden": False},
    ],
}
run([
    "dune", "viz", "update", str(fresh_id),
    "--name", "Data Freshness",
    "--description", "Source, latest available reading and dashboard metrics powered by each feed.",
    "--options", json.dumps(fresh_options, separators=(",", ":")),
    "-o", "json",
])
visuals["freshness"]["name"] = "Data Freshness"
STATE.write_text(json.dumps(state, indent=2) + "\n")
print("freshness                    -> Data Freshness")
print()
print("Public naming + freshness visualization PASS")
