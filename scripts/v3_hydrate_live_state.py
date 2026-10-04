#!/usr/bin/env python3
"""Restore allowlisted machine data, never source code, from production."""
import json
import subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PREFIXES = ("data/canonical/", "data/current/", "data/network/", "data/history/")
EXACT = {"data/derived/xnet_owner_balances.csv", "data/derived/market_snapshots.csv",
         "data/derived/bbb_burn_daily.csv", "data/derived/bbb_trades_daily.csv",
         "data/xnet_supply_history.json", "data/xnet_supply_daily.csv"}

def hydrate():
    subprocess.run(["git", "fetch", "--quiet", "origin", "live-state"], cwd=ROOT, check=True)
    paths = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", "FETCH_HEAD"], cwd=ROOT, text=True).splitlines()
    seeded_path = ROOT / "data/history/bbb_wallet_usdc_daily.json"
    seeded = json.loads(seeded_path.read_text()) if seeded_path.exists() else {"data": []}
    for path in paths:
        if path not in EXACT and not path.startswith(PREFIXES):
            continue
        target = ROOT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(["git", "show", f"FETCH_HEAD:{path}"], cwd=ROOT))
    history = "data/history/bbb_wallet_usdc_daily.json"
    old = "data/derived/bbb_wallet_usdc_daily.json"
    live_path = history if history in paths else old if old in paths else None
    if live_path:
        live = json.loads(subprocess.check_output(["git", "show", f"FETCH_HEAD:{live_path}"], cwd=ROOT))
        merged = {r["day"]: r for r in seeded.get("data", [])}
        merged.update({r["day"]: r for r in live.get("data", [])})
        seeded["data"] = [merged[d] for d in sorted(merged)]
        seeded_path.parent.mkdir(parents=True, exist_ok=True)
        seeded_path.write_text(json.dumps(seeded, indent=2) + "\n")
    print("Production state restored.")

if __name__ == "__main__":
    hydrate()
