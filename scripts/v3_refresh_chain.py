#!/usr/bin/env python3
"""Retired legacy chain entrypoint: fail closed, never compute.

This script used to run the two hot Dune queries (`xnet_transfers`, `bbb_dex`),
read their execution costs, reduce canonical state and publish. That is a paid
path with budget, pause and lease rules it cannot honour on its own, so it is
retired. It now refuses to do any work and points at the unified runner.

It deliberately does NOT reroute into the unified runner: rerouting would make a
legacy or scheduled invocation quietly spend credits outside the caller's
explicit intent. A hard nonzero exit is the only safe behaviour.
"""
from __future__ import annotations

import sys

UNIFIED_RUNNER = "scripts/v3_refresh_dashboard.py"

DEPRECATION = f"""\
v3_refresh_chain.py is retired and fails closed.
No Dune query, result export, reduction or publication is performed here.
Use the unified runner instead:
    python3 {UNIFIED_RUNNER}
This legacy entrypoint does not reroute automatically: computing silently would
bypass the unified budget, pause and single-commit publication controls.
"""


def main() -> int:
    """Refuse to run (exit 2); touches no network, Dune API, or canonical data."""
    sys.stderr.write(DEPRECATION)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
