# BBB ledger source evidence — implicated post-seed rows, bounded readonly probe

Revision 2. Supersedes the first draft: **the first draft sampled the wrong
rows** (2026-10-01/02 pre-seed rows that pre-date the implicated events), so
its refutation claim did not apply to the implicated rows. This revision
retracts that claim, records the correct scope of the first sample, and adds
the exact implicated rows. Cutoff separating pre-seed ledger inputs from the
implicated post-seed events: **2026-10-03 12:46:12 UTC** (delegator-cited);
both implicated events post-date that cutoff.

Authorisation record: readonly verification only, existing public endpoint
already configured in `scripts/v3_build_chain_snapshot.py`
(`https://api.mainnet-beta.solana.com`), no API keys, no adapter, no retries.
Budget spent: first round ≤4 calls (wrong-row sample — kept only as appendix),
second round exactly 2 calls on the exact implicated transactions. Dune
untouched; no financial edits, no deletion, no rescaling.

## Implicated rows (verified EXACT in `origin/live-state:data/canonical/xnet_transfers.csv`)

| event_id | day | block_time | slot | action | ledger `amount_raw` | ledger `amount_xnet` | mult | full tx_id |
|---|---|---|---|---|---|---|---|---|
| 4b37755adf28bc31a6e95c13db297c74 | 2026-10-15 | 2026-10-15 16:30:22.000 UTC | 457304764 | transfer (incoming to BBB wallet) | 109347665891987 | 1093476.65891987 | 1 | 5JTByAxtY9BSr6xkswWVign8SBuEzyDbhTc7JGFuCaQXNisFcwx1hCHXSN7DKXnTduG94FZxyC7GJhzWYrfRCc2h |
| b3bfd66bfb6d0f351e3f535514542e77 | 2026-10-16 | 2026-10-16 09:32:11.000 UTC | 457588470 | burn (BBB wallet) | 123487555184967 | 1234875.55184967 | 1 | iMUfNrKHJjwfcuhBmpWNARFDqAvByKmQ7Po36HT6XGMJXqQ66N6ihwSa2oPcKADfthe56tA7rEri6UGXvVGpgGU |

The rows carry exactly the amounts cited in the prior root-cause round
(1,093,476.65891987 and 1,234,875.55184967). **Acknowledgement/retraction:** the
cited event ids and numbers were correct; the earlier *sample in this doc* was
wrong (pre-seed Oct-01/02 rows) and its "refuted for the sampled rows"
framing must not be read as covering the implicated rows. The pre-seed sample
is retained only as an appendix with no verification claim over the
implicated events.

## Chain verification (2 calls: getTransaction jsonParsed, commitment confirmed)

Decimals base (8) was established in round 1 at the mint
(`xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L`, `decimals: 8`) and both
implicated responses independently show `decimals: 8` in every XNET balance
entry — no extra call spend.

1. **Incoming 1,093,476.65891987 (tx 5JTByAxt…, slot 457304764,
   blockTime = 2026-10-15T16:30:22Z).** BBB-wallet XNET token balances:
   pre `801421657411` → post `110149087549398` — delta
   `109347665891987`, **exactly** the ledger `amount_raw × 1`
   (= 1093476.65891987 XNET at decimals 8). The same transaction also moved
   2000.000000 USDC (decimals 6) out of the BBB wallet (parsed instruction
   `amount "2000000000"`); the ledger's implicated amount is the XNET leg,
   which reconciles exactly.
2. **Burn 1,234,875.55184967 (tx iMUfNrKH…, slot 457588470,
   blockTime = 2026-10-16T09:32:11Z).** BBB-wallet XNET token balances:
   pre `123487555184967` → post `0` — burn delta
   `123487555184967`, **exactly** the ledger `amount_raw × 1`
   (= 1234875.55184967 XNET at decimals 8).

Both blocks carry `meta.err == null`, correct slots against the ledger's
`block_slot`, and blockTimes matching the ledger `block_time` strings
to the second.

## Conclusion (exact-implicated-rows scope only)

For the two exact implicated post-seed events, the ledger matches the chain
precisely: same raw amounts, same slots, same second-precision block times,
decimals 8. Within this 2-row evidence the ×10-scaling suspicion is
**refuted** and there is **no missing-coverage signal** at these events.
Nothing in this document extends that verdict to any other row, prior day,
or aggregate; bridge rows between 2026-10-03 12:46:12 UTC and these events
remain unverified by this evidence. No repair required by this evidence;
no financial edits were made.

## Appendix — round-1 pre-seed sample (no implicating claim)

Round 1 (4 calls) verified the following pre-seed rows exactly against the
chain (mint decimals 8; deltas/instruction amounts identical to ledger):
2.5M transfer 2026-10-01 (row `2da89ad3…`, tx `4kADyUFC…`), 2,076,064.69
transfer 2026-10-02 (row `98ac58a8…`, tx `3waLJaWs…`), BBB burn
1,682,221.33325025 2026-10-02 (row `3a36a53d…`, tx `33Ds4tAs…`). They are
kept for record-keeping only because they pre-date the implicated events and
cannot support any verdict about them.
