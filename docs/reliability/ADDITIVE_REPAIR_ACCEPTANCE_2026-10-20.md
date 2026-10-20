# Verified additive repair — 20 October 2026

This records the **first** additive publication. The subsequently approved
770-event catch-up and seven presentation refreshes are recorded separately in
[`APPROVED_CATCHUP_2026-10-20.md`](APPROVED_CATCHUP_2026-10-20.md); the stale-chain
limits below describe this earlier generation, not the later accepted result.

Published live-state revision: `fa9150f014d85024df9934f5b54c5a0f4e39dee4`.
Exact predecessor: `218e758ff3a68894d2551ba4cab56a5a96e890dd`.
Generation: `43966dd9-6416-4605-b809-c4ce6e3925cb`.
Receipt: `65a16dfb2842c1ac20da739095cc4352bdde2d4f1d66a3461931aea4b26378e8`
in `v3_refresh_state.json.additive_transfer_repairs` on that revision.

## Accepted effects

- **327 additions**, zero deletions: 198 complete-day rows for 14 October;
  124 additions for 15 October, after verifying all 177 old day rows against the
  complete 301-row response; five rows from three finalized 16 October transactions.
- All **3,143 original CSV record bytes and their order are preserved** as the
  prefix of the new 3,470-row ledger. New and old event IDs are unique and use the
  same deterministic legacy grouping contract; no instruction-ID migration or
  destructive window replacement occurred.
- Canonical before SHA-256:
  `f3c5243def820dcd6f369705e5908c56483a9c7752634878706ce3a1498f4eb9`.
- Canonical after SHA-256:
  `a18e2283777770a89f07040554a4ba8182d4d756357ff7d3b4c565439d057c19`.
- The pre-reduction candidate hash was recorded and unchanged by strict rebuild.
  Canonical data, derived data, financial journal, acceptance markers and the
  proof receipt were published together against the exact predecessor. A sibling
  concurrent commit cannot be overwritten by the non-force fast-forward update.

## Independent verification

The full reducer succeeded **without** stale-holder fallback. Independent review
verified all accepted rows and exact Decimal conservation:

| Measure | Verified value |
|---|---:|
| Positive holders | 5,316 |
| Outstanding positive balances | 1,183,413,957.83469057 XNET |
| Circulating supply | 451,942,121.23594060994 XNET |
| Verified BBB burns | 14,697,667.371498791 XNET |
| BBB wallet XNET balance | 10,493.9044562 XNET |

Seed + post-cutoff mint − burn equals final balances exactly. Existing duplicate
locator groups (12 groups, 24 rows) are unchanged; they are not deleted or assumed
erroneous. All 17 finalized captures were decoded independently into **27** XNET
legs, including intermediate routed legs. Twenty-two already match the Dune
14/15 October evidence; only the five 16 October legs were additional.

The raw RPC fixture's historical header still describes an earlier partial
capture round. Its bytes are intentionally retained because the acceptance
receipt pins its hash. The final per-row fields contain full round-4/5 captures;
tests derive instructions and verify all XNET account balance flows, rather than
trusting the old header or balance-delta claims.

## Deliberate limits

- **Chain data remains stale**, latest verified event **16 October, 18:46:54 UTC**.
  The old collection clock is preserved. A current generation timestamp does not
  claim current collection or full 16 October coverage.
- The separately approved full-day 16 October Dune execution failed on upstream
  stored-view metadata. No retry was made. Its unknown cost and read-attempt
  evidence remain in the journal.
- The original holder-failure pause is resolved with an audit record. This does
  **not** authorize spending, settle unknown charges, raise the 1,800 guard, change
  cadence, or erase other source errors.
- Market/network/revenue inputs and unrelated presentation payloads retain the
  exact hydrated generation. USDC retains its last observed balance and timestamp
  with `last_good` provenance; acceptance made no live RPC or Dune request.
- No public dashboard layout, visualization, methodology or external DeFiLlama
  adapter was changed. Dune presentations have **not** been refreshed by this
  publication.
