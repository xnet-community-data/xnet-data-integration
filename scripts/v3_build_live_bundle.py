#!/usr/bin/env python3
"""Build one atomic LiveFetch payload from already-verified production state."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SOURCES = {
    "chain": Path("data/current/xnet_chain_snapshot.json"),
    "network": Path("data/current/xnet_network_state.json"),
    "revenue": Path("data/current/xnet_revenue_state.json"),
    "market": Path("data/current/xnet_market_state.json"),
}

OUTPUT = Path("data/current/xnet_live_bundle.json")


def build_bundle(root: Path = ROOT) -> dict:
    bundle = {"schema_version": 1}
    for key, relative in SOURCES.items():
        path = root / relative
        if not path.exists():
            raise RuntimeError(f"Missing verified live state for bundle: {relative}")
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise RuntimeError(f"Invalid live state for bundle: {relative}")
        bundle[key] = value
    return bundle


def write_bundle(root: Path = ROOT) -> None:
    value = build_bundle(root)
    path = root / OUTPUT
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


if __name__ == "__main__":
    write_bundle()
    print("Built atomic XNET LiveFetch bundle.")
