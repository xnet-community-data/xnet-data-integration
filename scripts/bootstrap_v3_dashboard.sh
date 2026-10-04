#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DUNE_API_KEY:-}" ]]; then
  echo "ERROR: DUNE_API_KEY is not set"
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$ROOT/state/v3_dashboard_created.json"

if [[ -s "$STATE" ]]; then
  echo "STOP: $STATE already exists."
  echo "Refusing to create a second V3 dashboard."
  cat "$STATE"
  exit 0
fi

echo "Creating PRIVATE V3 dashboard. V2 is untouched."

dune dashboard create \
  --name "XNET — Network, Revenue & Buy/Burn V3" \
  --private \
  -o json | tee "$STATE"

echo
echo "=== V3 DASHBOARD CREATED ==="
python3 - "$STATE" <<'PY'
import json, sys
p=json.load(open(sys.argv[1]))
print("dashboard_id:", p.get("dashboard_id") or p.get("id"))
print("name:", p.get("name"))
print("private:", p.get("is_private"))
print("url:", p.get("dashboard_url") or p.get("url"))
PY
