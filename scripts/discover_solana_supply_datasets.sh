#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DUNE_API_KEY:-}" ]]; then
  echo "ERROR: DUNE_API_KEY is not set"
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/state/solana_supply_dataset_discovery"
mkdir -p "$OUT"

queries=(
  "solana token balances"
  "solana token balance owners"
  "solana fungible balances"
  "solana token supply"
  "solana token accounts"
)

for q in "${queries[@]}"; do
  slug="$(echo "$q" | tr ' ' '_' | tr -cd '[:alnum:]_')"
  echo "=== $q ==="
  dune dataset search \
    --query "$q" \
    --blockchains solana \
    --include-schema \
    --include-metadata \
    --limit 50 \
    -o json | tee "$OUT/${slug}.json"
done

echo
echo "Saved discovery results to: $OUT"
