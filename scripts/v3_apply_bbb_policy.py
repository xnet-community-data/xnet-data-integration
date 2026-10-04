#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/xnet_bbb_execution_policy.json"
CHAIN = ROOT / "data/current/xnet_chain_snapshot.json"

cfg = json.loads(CONFIG.read_text())

direct = float(cfg["direct_bbb_share_of_received_revenue"])
liq = float(cfg["liquidity_share_of_received_revenue"])
liq_market_fraction = float(cfg["liquidity_xnet_market_buy_fraction"])
days = int(cfg["execution_days"])

for name, value in {
    "direct_bbb_share_of_received_revenue": direct,
    "liquidity_share_of_received_revenue": liq,
    "liquidity_xnet_market_buy_fraction": liq_market_fraction,
}.items():
    if not 0 <= value <= 1:
        raise SystemExit(f"Invalid {name}: {value}")

if days <= 0:
    raise SystemExit(f"Invalid execution_days: {days}")

effective = direct + liq * liq_market_fraction
if effective > 1:
    raise SystemExit(f"Effective market-buy share exceeds 100%: {effective}")

state = json.loads(CHAIN.read_text())
state["bbb_execution_policy"] = {
    "basis": cfg.get("basis", "latest_wifi_payment_received_usd"),
    "direct_bbb_share_of_received_revenue": direct,
    "liquidity_share_of_received_revenue": liq,
    "liquidity_xnet_market_buy_fraction": liq_market_fraction,
    "effective_xnet_market_buy_share": effective,
    "execution_days": days,
}

CHAIN.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

print(f"direct BBB share: {direct:.0%}")
print(f"liquidity share: {liq:.0%}")
print(f"liquidity XNET market-buy fraction: {liq_market_fraction:.0%}")
print(f"effective XNET market-buy share: {effective:.0%}")
print(f"execution days: {days}")
