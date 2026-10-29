# Revenue sheet compatibility — 29 October 2026

The source now distinguishes XNET Inc.'s payable ledger from the Foundation's
booked receivables ledger. This is not a global string-replacement migration.

## Source contract

`scripts/sync_revenue.py` indexes rows by **section and exact normalized label**.
Whitespace variations are normalized; unrelated or ambiguous financial labels
are not guessed. Both the old and new published labels are supported:

| Foundation series | Previous label | New label |
|---|---|---|
| Monthly receivable | Projected Buy & Burn | Monthly Receivables from XNET |
| Funding transfer | Transferred to Buy & Burn | Transferred to Foundation |
| Booked balance | Balance Due to Buy/Burn | Balance Due to Foundation |
| Due from Inc. | Due from XNET Inc | Due Balance from XNET Inc |

Mirrored Inc./Foundation transfer and payable/due-from rows must agree; they are
counted **once**. Duplicate metrics inside a section and duplicate calendar
columns fail closed. Blanks remain null. Nonfinite money is refused.

Explicit normalized fields preserve all five concepts:
`foundation_monthly_receivable_usd`, `transferred_to_foundation_usd`,
`foundation_booked_balance_usd`, `inc_payable_to_foundation_usd` and
`foundation_due_from_inc_usd`.

The old `projected_buy_burn_usd`, `transferred_to_buy_burn_usd` and
`balance_outstanding_to_transfer_usd` names are **documented compatibility
identifiers for the existing Foundation series**, not evidence of DEX buys or
token burns. Existing query IDs, SQL, budget-policy hash and chart layout are
unchanged. Existing widgets/series are relabeled as Foundation funding; on-chain
buy/burn/wallet metrics are not relabeled or modified.

## Verified source facts

The frozen official sheet captured on 29 October includes:

- Carrier cash **$32,643.19**, received **26 October 2026**. Existing settlement
  reconciliation associates it with **August 2026** service revenue.
- October Foundation booked balance **$38,715.05**.
- October Inc. payable / Foundation due-from mirror **$64,829.61**.
- October Foundation funding transfer **$23,985**, recorded once.
- November Foundation monthly receivable **$26,114.55**, left in November, not
  silently moved into October's booked balance.
- Existing **$1,108.18** fiat payout remains attributed by its own reconciled
  settlement to **July 2026**, not relabeled to August when a new carrier receipt
  arrives. The dynamic labels now use the payout's actual service mapping.

Historical pre-October normalized values are unchanged in the fixture replay.
The legacy fixture reproduces every pre-migration normalized metric exactly.

## Publication and freshness

Both revenue and epoch contracts validate before any local normalized output is
replaced. The existing source workflow publishes the raw sheet, monthly data,
epoch metadata, offload cache and derived accrual feed in its normal Git commit.

The Dune revenue workflow builds from that accepted source, atomically publishes
the revenue component/bundle and queues `current`/`commercial` presentation work
**only if source contents changed**. The next normal runner consumes that queue
under the existing standing budget; a daily success timestamp alone does not buy
another chart refresh. The financial journal and standing book are not reset.

The normalized source carries its successful fetch clock, raw digest and exact
section/row mapping. The live revenue component retains that provenance. The
existing independent watchdog checks source ingestion separately from generated
time: an import older than 48 hours cannot be hidden by rebuilding an old feed.

## Validation and rollout

Regression fixtures capture the old accepted and new official CSVs plus the epoch
Summary. Tests cover both ledgers, mirrored postings, historical compatibility,
future-month isolation, duplicate/unknown labels, invalid money, failed epoch
validation preserving prior outputs, source-to-settlement-to-bundle replay,
nonfinancial chart invalidation, fiat-service attribution, and metadata-only label
changes preserving query columns, axes and layout.

Rollout order: merge after CI; wait for the source importer to publish; run the
existing Dune revenue publisher against that accepted revision; let the ordinary
bounded runner consume the two queued presentations; verify public components,
cached presentation values, visualization labels and unchanged accounting/spend
authority. No historical scan or new Dune query is needed.
