# XNET V3 starter v0.1

This package starts V3 without touching the existing public V2 dashboard.

## First run

From the repository root:

```bash
bash scripts/bootstrap_v3_dashboard.sh
bash scripts/bootstrap_v3_market.sh
bash scripts/discover_solana_supply_datasets.sh
```

The first script creates one PRIVATE dashboard only.
The second creates the first canonical V3 data domain: a single DexScreener pool query, a 15-minute materialized view, and a tiny market snapshot query.
The third is read-only discovery for the holder/circulating-supply implementation.

## Safety

- Existing V2 query IDs are not modified.
- Existing V2 dashboard is not modified.
- Legacy circulating-supply wallets are marked DO_NOT_USE_UNTIL_VERIFIED.
- Supply reconstruction remains private until reconciled.
- Market external I/O occurs in one canonical query only.

## Next build stages

1. Verify Market Base output/cost.
2. Resolve Solana supply/owner-balance datasets.
3. Build versioned supply + holder reconstruction.
4. Build one incremental XNET transfer base.
5. Build one incremental XNET DEX trade base.
6. Migrate BBB derivations onto those bases.
7. Restore offload/device mirrors when their upstream 402 is resolved.
8. Build daily off-chain canonical base.
9. Create V3 visualizations from the visible-dashboard spec.
10. Clone/rearrange the private V3 dashboard via API/CLI.
11. Reconcile V2 vs V3 before publication.
