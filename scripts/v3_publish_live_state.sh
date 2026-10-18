#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# The store validates the hydrated predecessor BEFORE invoking any builders.
exec python3 "$ROOT/scripts/v3_live_state_store.py" publish "$@"
