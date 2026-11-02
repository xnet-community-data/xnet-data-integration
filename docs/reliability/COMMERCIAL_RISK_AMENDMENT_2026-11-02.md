# Commercial presentation risk amendment — 2 November 2026

During verification of the one-sided revenue mirror repair, current counters
updated successfully but the commercial-history refresh was blocked before POST.
Its genuine 1 November execution `01M6CKVMT0K7CAF3YH0ET4RMB5` cost
**10.737029412 credits**, above this query's **3-credit** per-operation authority.
The estimator correctly retained that outlier instead of manufacturing a cheaper
estimate. The historical charge remains in the financial journal and quote pool.

The user was explicitly asked whether only query **8895089** could have its
standing per-operation risk allowance raised to **12 credits**, informed that
this is **not** a provider-enforced charge cap, and selected **“Allow 12-credit
risk”** on 2 November 2026.

Only `autonomous_operation.queries.8895089.max_operation_risk_credits` changes
from 3 to 12. Medium engine, no parameters, exact SQL hash, query ID, ordinary
cadence, retry and duplicate-submission protections remain unchanged. The
post-run 1-credit diagnostic threshold also remains unchanged: an actual charge
above it must still be recorded honestly.

The 2,500-credit operating target, 3,000-credit ceiling (subject to quota minus
1,000), 100-credit safety reserve, 100-credit legacy-uncertainty reserve,
100-credit daily risk limit and every other query's authority are untouched.
All obligations, unknowns, period anchors and conservative opening reservations
must remain intact. This approval does not authorize resetting or re-anchoring
the standing book.

Deployment requires a separate explicit, compare-and-swap journal publication:
bind the installed policy digest to the newly reviewed policy, retaining the
previous digest and this approval in an amendment receipt. Config changes alone
remain insufficient authority. Publish using the existing live-state store's
checkpoint mode, with no Dune operation during authority installation; then use
the normal bounded runner and verify the actual cached commercial results.
