#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="$ROOT"
STATE_ONLY=false
if [ "${1:-}" = "--refresh-state-only" ]; then
  STATE_ONLY=true
elif [ "$#" -ne 0 ]; then
  echo "Unknown publication option" >&2
  exit 1
fi
if [ "$STATE_ONLY" = false ]; then
  python3 "$SOURCE/scripts/v3_record_bbb_usdc_history.py"
  python3 "$SOURCE/scripts/v3_build_presentation_history.py"
fi



cd "$ROOT"

REMOTE_URL="$(git remote get-url origin)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Publishing XNET live state..."


if git ls-remote --exit-code --heads origin live-state >/dev/null 2>&1; then

    git clone \
      --quiet \
      --depth 1 \
      --branch live-state \
      "$REMOTE_URL" \
      "$TMP/repo"

    cd "$TMP/repo"

else

    git clone \
      --quiet \
      --no-checkout \
      "$REMOTE_URL" \
      "$TMP/repo"

    cd "$TMP/repo"

    git checkout --orphan live-state

    git rm -rf . >/dev/null 2>&1 || true
fi

if [ "$STATE_ONLY" = true ]; then
  mkdir -p data/current
  cp "$SOURCE/data/current/v3_refresh_state.json" data/current/
  if [ -f "$SOURCE/data/current/xnet_chain_health.json" ]; then
    cp "$SOURCE/data/current/xnet_chain_health.json" data/current/
  fi
  git add data/current/v3_refresh_state.json
  if [ -f data/current/xnet_chain_health.json ]; then
    git add data/current/xnet_chain_health.json
  fi
  git config user.name "xnet-community-data-bot"
  git config user.email "xnet-community-data-bot@users.noreply.github.com"
  if ! git diff --cached --quiet; then
    git commit -m "Track XNET Dune execution state" --quiet
    git push origin live-state --quiet
  fi
  echo "Published Dune execution state."
  exit 0
fi

mkdir -p \
  data/canonical \
  data/current \
  data/derived \
  data/network \
  data/history


cp \
  "$SOURCE/data/canonical/xnet_transfers.csv" \
  data/canonical/

cp \
  "$SOURCE/data/canonical/bbb_trades.csv" \
  data/canonical/

for file in market_pair_snapshots.csv; do
  if [ -f "$SOURCE/data/canonical/$file" ]; then
    cp "$SOURCE/data/canonical/$file" data/canonical/
  fi
done

cp "$SOURCE/data/derived/market_snapshots.csv" data/derived/
cp "$SOURCE/data/current/xnet_market_state.json" data/current/
if [ -f "$SOURCE/data/current/xnet_market_pairs.json" ]; then
  cp "$SOURCE/data/current/xnet_market_pairs.json" data/current/
fi

cp "$SOURCE/data/history/bbb_wallet_usdc_daily.json" data/history/
if [ -f "$SOURCE/data/history/circulating_supply_seed.json" ]; then
  cp "$SOURCE/data/history/circulating_supply_seed.json" data/history/
fi
cp "$SOURCE/data/xnet_supply_history.json" data/

if [ -f "$SOURCE/data/current/v3_refresh_state.json" ]; then
  cp "$SOURCE/data/current/v3_refresh_state.json" data/current/
fi

cp \
  "$SOURCE/data/current/xnet_chain_snapshot.json" \
  data/current/

cp \
  "$SOURCE/data/current/xnet_holder_state.json" \
  data/current/
if [ -f "$SOURCE/data/current/xnet_holder_integrity.json" ]; then
  cp "$SOURCE/data/current/xnet_holder_integrity.json" data/current/
fi

cp \
  "$SOURCE/data/current/xnet_supply_state.json" \
  data/current/

cp \
  "$SOURCE/data/current/xnet_bbb_burn_state.json" \
  data/current/

cp \
  "$SOURCE/data/current/xnet_bbb_trade_state.json" \
  data/current/

cp \
  "$SOURCE/data/current/xnet_chain_health.json" \
  data/current/

cp \
  "$SOURCE/data/current/xnet_network_state.json" \
  data/current/

cp \
  "$SOURCE/data/current/xnet_revenue_state.json" \
  data/current/

cp \
  "$SOURCE/data/network/device_history.json" \
  data/network/

cp \
  "$SOURCE/data/network/offload_monthly.json" \
  data/network/

cp \
  "$SOURCE/data/derived/xnet_owner_balances.csv" \
  data/derived/

cp \
  "$SOURCE/data/derived/bbb_burn_daily.csv" \
  "$SOURCE/data/history/bbb_wallet_usdc_daily.json" \
  "$SOURCE/data/presentation/network_history.json" \
  "$SOURCE/data/presentation/commercial_history.json" \
  "$SOURCE/data/presentation/burn_history.json" \
  "$SOURCE/data/presentation/tokenomics_history.json" \
  "$SOURCE/data/presentation/holder_distribution.json" \
  "$SOURCE/data/presentation/market_pools.json" \
  data/derived/

cp \
  "$SOURCE/data/derived/bbb_trades_daily.csv" \
  data/derived/

cp \
  "$SOURCE/data/xnet_supply_daily.csv" \
  data/

cat > README.md <<'EOF'
# XNET Live State

Machine-maintained production state for the XNET V3 dashboard.

Updates preserve history and use fast-forward pushes.

Human-readable code and research live on `main`.
EOF

git add -A

git config user.name \
  "xnet-community-data-bot"

git config user.email \
  "xnet-community-data-bot@users.noreply.github.com"

if git diff --cached --quiet; then
  echo "Live state unchanged."
  exit 0
fi
git commit -m "Refresh XNET live state" --quiet

git push \
  origin \
  live-state \
  --quiet

echo "Published live-state branch."
