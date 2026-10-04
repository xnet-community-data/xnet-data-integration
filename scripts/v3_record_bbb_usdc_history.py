#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "data/current/xnet_chain_snapshot.json"
OUT = ROOT / "data/history/bbb_wallet_usdc_daily.json"

snapshot = json.loads(SNAPSHOT.read_text())

balance = snapshot.get("bbb_wallet_usdc_balance")
observed = snapshot.get("bbb_wallet_usdc_observed_at_utc")

if balance is None or not observed:
    raise SystemExit(
        "BBB USDC current state missing; history not changed."
    )

day = str(observed)[:10]

if OUT.exists():
    obj = json.loads(OUT.read_text())
else:
    obj = {
        "schema_version": 1,
        "methodology": "forward daily Solana RPC snapshots",
        "data": [],
    }

rows = {
    str(row["day"])[:10]: dict(row)
    for row in obj.get("data", [])
    if row.get("day")
}

rows[day] = {
    "day": day,
    "bbb_wallet_usdc_balance": str(balance),
    "source": "solana_rpc_current",
}

obj["data"] = [
    rows[key]
    for key in sorted(rows)
]

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(obj, indent=2) + "\n")

print(
    "BBB USDC history:",
    len(obj["data"]),
    "daily rows",
)
