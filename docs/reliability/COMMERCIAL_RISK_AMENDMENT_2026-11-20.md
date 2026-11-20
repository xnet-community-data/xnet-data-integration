# Commercial aging-margin risk amendment — 20 November 2026

The real completed execution `01M6CKVMT0K7CAF3YH0ET4RMB5` on November1 cost
10.737029412 credits. It remains in the financial journal and estimator pool.
The unchanged estimator can uplift a sample by at most1.5× as it ages, so this
known exposure can reach16.105544118 credits. Its November20 quote is about13.33,
which correctly exceeds the November2 approved12-credit authority. That refusal
must not be fixed by dropping the sample, clamping its estimate or faking freshness.

The user explicitly selected **“Approve scoped exception”** on November20 after
disclosure of both:

- Raising only query8895089's `max_operation_risk_credits` from12 to **16.11**.
- One recovery run estimated conservatively at **45 credits**, covering an
  8–10h transfer collection, the existing due30-day manifest and at most five
  chart queries. The operation must be repriced/admitted immediately before
  dispatch; the estimate and operation allowance are not provider charge caps.

Medium engine, no parameters, pinned SQL/hash, query ID, post-run diagnostic
threshold, cadence, retries, unknown-ID protection and every other query's
authority remain unchanged. So do the2500 target,3000 ceiling (subject to quota
minus1000), daily100 allowance, safety100 and legacy-uncertainty100 reserves.

The approved policy hash is
`4d35df425635bcec151e88d1a45fe258162fefff38c9056d4d96ea185b40fc63`;
its predecessor is
`bf5b8ca9a6701d6ab7551b6e84a61f2012740244bf94159f78d6c55c52bf72f6`.
Restoring only this one allowance to12 reproduces the predecessor exactly.

## Installation and acceptance

Config alone is insufficient authority. Publish an explicit amendment receipt
through the existing live-state compare-and-swap checkpoint path, binding the
installed authorization and current billing-period authority hash to the new
policy. Retain the prior hash, user selection, reviewed main revision and exact
live-state predecessor. Preserve all obligations, operations, unknowns, opening
usage, opening obligations, carry entries and high-water counters byte-for-byte;
never reopen/re-anchor a period or relabel legacy charges as settled. Closed
periods retain their historical bindings.

Authority installation performs no Dune operation. Verify the installed book
with the existing pure admission evaluator before dispatching the one approved
run. Record the actual costs, accepted execution IDs, coverage and integrity
afterward. An unrelated stale upstream source must remain explicitly degraded;
this amendment is not permission to invent missing source data.
