#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
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

mkdir -p \
  data/canonical \
  data/current \
  data/derived \
  data/network

SOURCE="$ROOT"

cp \
  "$SOURCE/data/canonical/xnet_transfers.csv" \
  data/canonical/

cp \
  "$SOURCE/data/canonical/bbb_trades.csv" \
  data/canonical/

cp \
  "$SOURCE/data/current/xnet_chain_snapshot.json" \
  data/current/

cp \
  "$SOURCE/data/current/xnet_holder_state.json" \
  data/current/

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

This branch is intentionally force-updated as a single-commit state branch.

Human-readable code and research live on `main`.
EOF

git add -A

git config user.name \
  "xnet-community-data-bot"

git config user.email \
  "xnet-community-data-bot@users.noreply.github.com"

if git rev-parse --verify HEAD >/dev/null 2>&1; then

    git commit \
      --amend \
      --no-edit \
      --quiet

else

    git commit \
      -m "XNET live production state" \
      --quiet
fi

git push \
  origin \
  live-state \
  --force \
  --quiet

echo "Published live-state branch."
