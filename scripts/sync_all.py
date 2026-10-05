#!/usr/bin/env python3

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

for script in (
    "scripts/sync_revenue.py",
    "scripts/sync_offload.py",
    "scripts/build_defillama_revenue.py",
):
    print(f"\n=== Running {script} ===")
    subprocess.run(
        [sys.executable, script],
        cwd=ROOT,
        check=True,
    )

print("\nAll XNET public revenue/offload datasets refreshed successfully.")
