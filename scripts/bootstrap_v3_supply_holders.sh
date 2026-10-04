#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DUNE_API_KEY:-}" ]]; then
  echo "ERROR: DUNE_API_KEY is not set"
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$ROOT/state"
mkdir -p "$STATE"

OWNER_SQL="$ROOT/dune/v3/sql/30_owner_balances_current.sql"
SNAP_SQL="$ROOT/dune/v3/sql/31_supply_holder_snapshot.sql"
REGISTRY_SQL="$ROOT/dune/v3/sql/01_legacy_wallet_registry.sql"
RECON_TEMPLATE="$ROOT/dune/v3/sql/32_legacy_supply_reconciliation_template.sql"

OWNER_STATE="$STATE/v3_owner_balances_query.json"
SNAP_STATE="$STATE/v3_supply_holder_snapshot_query.json"
REGISTRY_STATE="$STATE/v3_legacy_wallet_registry_query.json"
RECON_STATE="$STATE/v3_supply_reconciliation_query.json"
MARKET_STATE="$STATE/v3_market_snapshot_query.json"

json_id () {
  python3 -c 'import json,sys; p=json.load(sys.stdin); print(p.get("query_id") or p.get("id") or "")'
}

create_query_if_missing () {
  local state_file="$1"
  local name="$2"
  local description="$3"
  local sql_file="$4"

  if [[ ! -s "$state_file" ]]; then
    dune query create \
      --name "$name" \
      --description "$description" \
      --private \
      --sql "$(cat "$sql_file")" \
      -o json | tee "$state_file"
  fi
}

echo "=== 1. LEGACY REGISTRY (REFERENCE ONLY) ==="
create_query_if_missing \
  "$REGISTRY_STATE" \
  "XNET V3 — Legacy Supply Wallet Registry" \
  "Legacy XNET supply-exclusion wallet list preserved for reconciliation only. Not authoritative for V3 circulation until verified." \
  "$REGISTRY_SQL"

REGISTRY_ID="$(cat "$REGISTRY_STATE" | json_id)"
[[ -n "$REGISTRY_ID" ]] || { echo "ERROR: missing registry query id"; exit 1; }
echo "Registry query: $REGISTRY_ID"

echo
echo "=== 2. CURRENT OWNER BALANCE BASE ==="
create_query_if_missing \
  "$OWNER_STATE" \
  "XNET V3 — Current Owner Balances" \
  "Canonical current owner-level XNET balances from solana_utils.latest_balances. Aggregates SPL token accounts to token_balance_owner." \
  "$OWNER_SQL"

OWNER_ID="$(cat "$OWNER_STATE" | json_id)"
[[ -n "$OWNER_ID" ]] || { echo "ERROR: missing owner-balance query id"; exit 1; }
echo "Owner-balance query: $OWNER_ID"

echo
echo "Running owner-balance syntax/sample QA..."
dune query run "$OWNER_ID" --limit 5 -o json

OWNER_MV="dune.xnet_community_data.result_xnet_v3_owner_balances_current"

if ! dune matview get "$OWNER_MV" -o json >/dev/null 2>&1; then
  echo
  echo "Creating hourly owner-balance materialized view..."
  dune matview create \
    --name result_xnet_v3_owner_balances_current \
    --query-id "$OWNER_ID" \
    --private \
    --performance small \
    --cron "7 * * * *" \
    -o json
else
  echo "Owner-balance materialized view already exists."
fi

echo
echo "Refreshing owner-balance materialized view now..."
dune matview refresh "$OWNER_MV" --performance small -o json

echo
echo "=== 3. SUPPLY + HOLDER SNAPSHOT ==="
create_query_if_missing \
  "$SNAP_STATE" \
  "XNET V3 — Supply & Holder Snapshot" \
  "Current holder count, owner-balance supply reconciliation, holder thresholds and concentration metrics from the canonical owner-balance materialized view." \
  "$SNAP_SQL"

SNAP_ID="$(cat "$SNAP_STATE" | json_id)"
[[ -n "$SNAP_ID" ]] || { echo "ERROR: missing supply snapshot query id"; exit 1; }
echo "Supply/holder snapshot query: $SNAP_ID"

echo
echo "Running supply/holder QA..."
dune query run "$SNAP_ID" --limit 10 -o json

echo
echo "=== 4. LEGACY CIRCULATING-SUPPLY RECONCILIATION ==="
if [[ ! -s "$MARKET_STATE" ]]; then
  echo "ERROR: $MARKET_STATE is missing. Market bootstrap must run first."
  exit 1
fi

MARKET_ID="$(cat "$MARKET_STATE" | json_id)"
[[ -n "$MARKET_ID" ]] || { echo "ERROR: missing market snapshot query id"; exit 1; }

RECON_SQL="$STATE/v3_supply_reconciliation_rendered.sql"
python3 - "$RECON_TEMPLATE" "$RECON_SQL" "$REGISTRY_ID" "$MARKET_ID" <<'PY'
from pathlib import Path
import sys

src, dst, registry_id, market_id = sys.argv[1:]
s = Path(src).read_text()
s = s.replace("__LEGACY_REGISTRY_QUERY_ID__", registry_id)
s = s.replace("__MARKET_SNAPSHOT_QUERY_ID__", market_id)
Path(dst).write_text(s)
PY

if [[ ! -s "$RECON_STATE" ]]; then
  dune query create \
    --name "XNET V3 — Circulating Supply Reconciliation QA" \
    --description "PRIVATE QA ONLY. Compares current owner-balance supply, the legacy exclusion-wallet candidate, and provider-implied circulating/max supply. Not for publication until classifications are verified." \
    --private \
    --sql "$(cat "$RECON_SQL")" \
    -o json | tee "$RECON_STATE"
fi

RECON_ID="$(cat "$RECON_STATE" | json_id)"
[[ -n "$RECON_ID" ]] || { echo "ERROR: missing reconciliation query id"; exit 1; }

echo "Reconciliation query: $RECON_ID"
echo
echo "Running reconciliation QA..."
dune query run "$RECON_ID" --limit 10 -o json

echo
echo "=== SUPPLY / HOLDERS BOOTSTRAP COMPLETE ==="
echo "Registry query:          $REGISTRY_ID"
echo "Owner balance query:     $OWNER_ID"
echo "Owner balance matview:   $OWNER_MV"
echo "Supply snapshot query:   $SNAP_ID"
echo "Reconciliation QA query: $RECON_ID"
echo
echo "IMPORTANT: do not publish reconstructed circulating supply yet."
