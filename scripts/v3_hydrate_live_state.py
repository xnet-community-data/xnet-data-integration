#!/usr/bin/env python3
"""Restore authoritative machine data and record its exact revision receipt."""
from pathlib import Path
from v3_live_state_store import LiveStateStore

ROOT = Path(__file__).resolve().parents[1]


def hydrate():
    return LiveStateStore(ROOT).hydrate()


if __name__ == "__main__":
    import json
    print(json.dumps(hydrate(), sort_keys=True))
