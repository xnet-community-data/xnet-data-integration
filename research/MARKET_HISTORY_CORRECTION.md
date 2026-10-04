# Market history correction

The current V3 market materialized view is a current-state cache. A refresh recomputes the query result; it does not by itself create an append-only 15-minute history.

Keep it running temporarily because it gives V3 a cheap canonical live market state.

Before V3 publication, replace the 15-minute LiveFetch refresh path with an append-only collector:
1. GitHub Action fetches DexScreener every 15 minutes.
2. The action inserts one normalized snapshot per pool into a private Dune uploaded table.
3. Dune derives both latest market state and historical price/liquidity charts from that table.
4. Once validated, disable the 15-minute LiveFetch materialized-view schedule.

This both preserves historical liquidity snapshots and avoids paying Dune query credits merely to call DexScreener 96 times/day.

Dune's current upload API supports creating private uploaded tables and inserting additional CSV/NDJSON rows into existing tables.
