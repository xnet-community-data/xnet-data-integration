# XNET Dune cost sustainability — 19 October 2026

**Historical forecast, not spending authority.** Subsequent one-off recovery is
recorded in [`APPROVED_CATCHUP_2026-10-20.md`](APPROVED_CATCHUP_2026-10-20.md):
3.961 reported compute credits; official usage 1,803.249. The forecast below is
anchored to its original analysis instant, not recomputed from the new balance.
No cadence change or standing paid-operation approval has been installed.

Pure read-only analysis. Sources: local Git objects only (no Dune calls, no
exports, no new computation, no config change, no remote mutation). The only
artifact of this session is this file.

## 1. Standing budget facts (all source-cited)

| Fact | Value | Source |
|---|---|---|
| Official current usage | 1,798.673 credits, period 2026-10-04 → renewal **2026-11-04T00:00Z** | ASTRA_DELIVERY_2026-10-18 (checked twice), `origin/live-state` @0fe79005 `data/current/v3_credit_usage.json` (blob 2eb3565…): `period_counters["2026-10-04"] = 1798.673`; last account-total sample 4,298.928 − prior period 2,500.255 = 1,798.673 (exact match) |
| Plan quota / sustainable target / emergency guard | 4,000 / **2,500** / **1,800** | config/v3_refresh.json @ca8a076a and @313dd0be (identical): `monthly_target_credits`, `monthly_spend_guard_credits`; quota per official metadata check |
| Reserve | 15 credits permanently unspendable at renewal | config `minimum_credit_reserve`; `v3_credit_budget.py` DEFAULT_* 1800/15/10 constants |
| Spend since Oct 17 03:33 | **0.0 billed** per repo evidence (no journal rows after Oct 16 21:00:06; no newer usage sample on `origin/live-state`); official figure re-checked unchanged at 1,798.673 on Oct 18–19 per the mission | `origin/live-state` billing lane (snapshot 1,798.437 @Oct 16 21:00) + Oct 17 sample; ASTRA delivery doc |
| Analysis instant | 2026-10-19T21:31:28Z | actual UTC clock at doc build |
| Remaining to renewal | 1,304,912 s = **15.1031 days** | computed |
| Target headroom available for NEW spend | 2,500 − 1,798.673 − 15 = **686.327 credits**, conditional: valid only after (a) the 2 unknown canceled liabilities below resolve and (b) the guard is released by user decision (usage+reserve 1,813.673 is already above the 1,800 guard → code denies all new compute now) | config + `v3_credit_budget.budget_admission` limit= min(credits_included, guard, target) + runbook step 5 |
| Journal context | execution_history = exactly 1,000 rows (993 `QUERY_STATE_COMPLETED`, 7 `QUERY_STATE_CANCELLED`); retention bound confirms full current blob | `v3_live_state_store.py` MAX_EXECUTION_HISTORY_ROWS=1000; state `execution_history` |

## 2. Per-query compute-cost distribution (1,000-row empirical history)

Sorted bucket stats: linear-interpolated percentiles; envelope =
max(observed_max, mean + 3·stdev) — the repo's official estimator
(`v3_credit_budget.py`: quorum 3 samples, uplift σ=3.0, comparable window 168 h,
performance=medium for all rows).

**Full-window identities (identity = query_id + exact parameters; 2026-10-11 → 16):**

| Identity (query/key: params) | n | sum | p50 | p90 | p95 | max | mean | sd | envelope |
|---|---|---|---|---|---|---|---|---|---|
| 8894185 `current` {} | 197 | 67.679 | 0.1863 | 0.3710 | 0.6574 | **12.4180** | 0.3435 | 1.0214 | 12.4180 |
| 8895093 `pools` {} | 199 | 23.065 | 0.1105 | 0.1271 | 0.1418 | 0.3553 | 0.1159 | 0.0265 | 0.3553 |
| 8895090 `burns` {} | 146 | 18.382 | 0.1198 | 0.1425 | 0.1576 | 0.4015 | 0.1259 | 0.0290 | 0.4015 |
| 8895091 `tokenomics` {} | 145 | 17.003 | 0.1117 | 0.1281 | 0.1526 | 0.3099 | 0.1173 | 0.0238 | 0.3099 |
| 8895092 `holders` {} | 144 | 16.395 | 0.1077 | 0.1275 | 0.1362 | 0.3318 | 0.1139 | 0.0288 | 0.3318 |
| 8894037 `xnet_transfers` lb=2 | 122 | 31.678 | 0.0626 | 0.3044 | 0.5519 | **10.4398** | 0.2597 | 1.0289 | 10.4398 |
| 8894037 `xnet_transfers` lb=3 | 14 | 1.413 | 0.0725 | 0.2301 | 0.2822 | 0.3108 | 0.1009 | 0.0821 | 0.3473 |
| 8894132 `bbb_dex` lb=29 | 4 | 0.304 | 0.0635 | 0.1038 | 0.1121 | 0.1204 | 0.0759 | 0.0259 | 0.1535 |
| 8893945 manifest lb=7 (daily) | 4 | 9.956 | 2.4237 | 3.5496 | 3.7454 | 3.9413 | 2.4890 | 0.9943 | 5.4719 |
| 8893945 manifest lb=30 (weekly) | 2 | 48.097 | 24.0484 | 36.1190 | 37.6278 | **39.1366** | 24.0484 | 15.0882 | 69.3131 |
| 8894038 `historical_repair` 1-day rows | 4 | 0.995 | 0.2666 | 0.2836 | 0.2837 | 0.2839 | 0.2488 | 0.0430 | 0.3778 |
| 8895088 `network`, 8895089 `commercial` {} | 4 + 4 | 0.496 + 0.786 | 0.1164 / 0.1887 | 0.1483 / 0.2787 | 0.1546 / 0.2870 | 0.1610 / 0.2952 | 0.1239 / 0.1964 | 0.0223 / 0.0744 | 0.1908 / 0.4197 |

Recent-comparable checks (`anchor ≤ 168 h` of the analysis instant, i.e. after
2026-10-12T21:31Z; the official estimator's own window): `transfers lb=2`
n=72 (mean 0.2116, env 3.7065), `current` n=102 (mean 0.4250 — the 12.4180
outlier is still inside the window), `pools/burns/tokenomics/holders` mean
0.115 / 0.124 / 0.115 / 0.111 with envelopes 0.31 / 0.18 / 0.16 / 0.22,
bbb lb=29 n=2 (0.0591) — below quorum, manifest lb=7 n=2 (mean 3.077),
manifest lb=30 n=1 — below quorum. Outliers are **inside** every mean; they
are neither multiplied by frequency nor dropped.

Identity caveats (hard fail-closed facts under the official estimator):
- scheduled `bbb_dex` configured lookback is 26, but **zero samples** exist for
  lb=26; executed lookbacks are 29 (n=4) and 46 (n=1) because the runner uses
  `lookback = max(configured, ceil(elapsed_hours + 1))` and GitHub cron drifts
  ~1–3 h/day (`v3_refresh_dashboard.py` source loop).
- weekly manifest lb=30 has n=2 < quorum 3 → official admission cannot pre-quote
  it and blocks (fail-closed, as required).
- the 168 h staleness window expires for ~Oct 11–12 evidence on Oct 18–19;
  fresh comparable samples require resumed operation — one more reason the
  next quote needs verified new runs, not the old average.

## 3. Empirical totals by completed normal runs (excluding explicit historical backfills)

- Total observed billing realized this period (all 1,000 rows with provider
  costs): **245.013 credits** — of which completed-normal-runs compute
  **239.760** and canceled-with-known-cost **5.253** (4 cancellations at the
  observed 0.0025 metering grain + one `current` cancel at 5.243126 billed
  before cancellation; 2 cancels have cost=None, see below).
- 5.73-day panel (Oct 11 03:23 → Oct 16 21:00, six calendar days): day totals 60.2 / 45.6 / 56.4 /
  33.9 / 47.7 / 1.2 credits/day; last 4 credit samples average **33.21
  credits/day** (usage file window); account-total sample path confirms
  day-over-day 44.4 / 55.5 / 32.4 / 43.9 / 1.2.
- Outlier inventory (the real liability band, kept and quantified, neither
  max×frequency nor ignored): `current` >1.0: 7 completed runs = 29.011
  (largest 12.418 @Oct 13 15:26) + the 5.2431 cancel; `transfers lb=2` >1.0:
  5 runs = 19.835 (largest 10.4398 @Oct 11 21:40; context days followed chain
  catch-up events); one 2.741 240 h-keyed approved backfill (excluded from
  standing cadences by definition). Outlier stack ≈ **9.4/day** across the panel.
- **The known overrun**: weekly manifest lb=30 run @2026-10-15T21:35Z
  (execution **01M50QVNESBXMT31576X7058MG**) **cost 39.1366 credits against
  its configured 15-credit cap → +24.1366 credits overrun**, quarantined automatically
  (`source_errors.historical_reconciliation`), never retried. It is already
  inside usage (1,798.673 includes it); it is the event that closed the guard
  (usage 1,757.627 verified unchanged at 21:30 → +39.14 → 1,798+ by Oct 16).
- 2 **unknown** canceled liabilities (`xnet_transfers` lb=2, IDs 01M4NYM9771GKC
  @2026-10-11T17:02Z and 01M4SSV28786RZ @2026-10-13T04:55Z): provider cost never
  reported; **no hard upper estimate can be claimed** (observed cancel metering
  is 0.0025/run, but that is not evidence for these two). Until provider
  reconciliation, admission stays blocked on principle (no aging, no estimate
  invention — `v3_financial_journal` "unknown exposure is never aged away").

## 4. Export wire cost (measured at 10 credits/MB incl. source)

Source lane exports were metered with real wire bytes on 134 of 1,000
executions (`export_wire_bytes` present, every one a paid fetch; zero
cached-reuse rows):

| Lane | n | paid-credit sum | mean/run | max/run | max wire bytes |
|---|---|---|---|---|---|
| xnet_transfers lb=2 | 108 | 0.1879 | 0.0017 | 0.1879 | 18,788 |
| xnet_transfers lb=3 | 13 | 0.0194 | 0.0015 | 0.0194 | 1,938 |
| reconciliation_manifest lb=7 | 3 | 0.0720 | 0.0240 | 0.0240 | 2,400 |
| historical_repair | 3 | 3.1488 | 1.0496 | 1.2043 | 120,427 |
| bbb_dex | 5 | 0.2149 | 0.0430 | 0.0697 | 6,966 |
| **presentation lanes** (`current`, `pools`, `burns`, `tokenomics`, `holders`, `network`, `commercial`) | **0 of 926** | **0.0** | 0 | 0 | — |

Total **5.2247 credits** paid for wire exports across the period; the catch-up
runs lb=16 (0.9291) and lb=25 (0.6526) are inside that figure. Presentation
refresh is cached/status-only by design (`export_policy:"none"` in
`build_scheduled_entries`); no presentation run exported anything. Max
configured per-run export allowance is 1,000,000 bytes = 10 credits/run
(config `max_export_bytes_per_run`); measured max is 0.121 MB = 1.204.

## 5. Forecast to renewal (exact now → 2026-11-04T00:00Z)

Runs are counted with the repo's conservative rule
(`divmod(1,304,912 s, cadence)` → ceil, includes the trailing partial window),
unit cost bases from §2: p50 = typical, mean = outlier-inclusive empirical
expected value, envelope = official admission quote. Cross-check at observed
effective frequencies (GitHub cron stretches: transfer median interval 40.2 min
at 30-min cadence; presentation realized ≈2.15× cadence): A-observed p50 521.2 /
mean 741.7.

### A — preserve every prior cadence (transfers 30 min/lb2; current,pools 15 min; burns/tokenomics/holders 30 min; network,commercial,bbb 1/day; manifest lb30 weekly)

| Component | runs | ×p50 | ×mean | ×envelope |
|---|---|---|---|---|
| current | 1,450 | 270.1 | 498.1 | 18,006.1 |
| pools | 1,450 | 160.2 | 168.1 | 515.2 |
| burns | 725 | 86.9 | 91.3 | 291.1 |
| tokenomics | 725 | 81.0 | 85.0 | 224.7 |
| holders | 725 | 78.1 | 82.6 | 240.6 |
| transfers lb=2 (compute; export add-on below) | 725 | 45.4 | 188.3 | 7,568.9 |
| transfers lb=2 export (quorum n=108, env 0.1879/run) | 725 | 0.0 | 1.2 | 136.2 |
| network / commercial / bbb | 16 / 16 / 16 | 5.9 | 6.3 | 12.2 |
| manifest lb=7 daily | 16 | 38.8 | 39.8 | 87.6 |
| manifest lb=30 weekly | 3 | 72.1 | 72.1 | 207.9 |
| **A total → projected period-end usage** | | **838.5 → 2,637.2** | **1,232.9 → 3,031.5** | (official quote also fails on bbb/weekly compute quorums + 2 unknowns) |
| A at observed engine rates | | **521.2 → 2,319.9** | **741.7 → 2,540.4** | |

**Cost proof of the need — set against hard gates:**
- 2,500 target: A-configured breaches by ≥137 credits even at p50 (2,637.2);
  mean basis breaches by 531 (3,031.5). A-observed breaches at mean
  (2,540.4) — and that basis still assumes a clean remainder.
- 2,200 weekly-soft gate (`weekly_soft_projected_monthly_credits`): every A
  basis ≥ 2,319.9 → the weekly manifest would defer by gate even under A.
- The mandatory 3-day chain-stale recovery one-off (§6) and the 2 unknown
  canceled liabilities then stack on top of an already-failing budget.
- Officially, A is flat-out inadmissible: usage+reserve 1,813.673 > 1,800
  guard fails admission before any other check.
Prior cadence is therefore **proved** unaffordable to this period's remaining
headroom; preserving it unchanged would either breach the 2,500 target or keep
the production lane off (zero-spend stale mode) — no third state exists under
the standing policy.

### B — recommended selectable cadence (quantified)

Transfers 120 min with lb=3; `current`/`pools` 60 min; burns/tokenomics/
holders 120 min; network/commercial/bbb **unchanged 1/day**; manifest: weekly
lb=30 only + selective lb=7/day-scan **only when a source gap/mismatch day is
detected** (same query 8893945, same daily/weekly algorithm, same repair
method — scheduling only, no methodology change; uses the existing
`pending_mismatch_days` / deferral gates). All existing caps stay: per-run
`max_run_credits` (current 1.5, transfers 1.5, bbb 0.75, others ≤1), post-run
cost breakers with cooldown, manifest cap 15, weekly soft gate 2,200,
target headroom 150.

| Component | cadence | runs | ×p50 | ×mean | ×envelope (admission quote) |
|---|---|---|---|---|---|
| current | 60 | 363 | 67.6 | 124.7 | 4,507.7 |
| pools | 60 | 363 | 40.1 | 42.1 | 129.0 |
| burns | 120 | 182 | 21.8 | 22.9 | 73.1 |
| tokenomics | 120 | 182 | 20.3 | 21.4 | 56.4 |
| holders | 120 | 182 | 19.6 | 20.7 | 60.4 |
| transfers lb=3 | 120 | 182 | 13.2 | 18.4 | 63.2 |
| transfers export (lb=3 quorum: n=13) | 120 | 182 | 0.3 | 0.3 | 3.6 |
| network / commercial / bbb | 1,440 | 16/16/16 | 5.9 | 6.3 | 12.2 |
| manifest lb=30 weekly (selective daily on-gap extra: conditional, ≤ small) | 10,080 | 3 | 72.1 | 72.1 | (n=2 quorum: official quote blocked until a 3rd verified comparable run) |
| **B total → projected period-end usage** | | | **260.7 → 2,059.4** | **328.9 → 2,127.6** | |

- B fits every standing gate at p50 **and** outlier-inclusive mean:
  projected 2,059.4–2,127.6 ≤ 2,200 soft gate ✓, ≤ 2,500 target ✓, and leaves
  **372.4 credits of target headroom unused** for the mandatory recovery
  one-off (§6) and renewal safety.
- Stress bands: holding every unit at its per-identity p95 lifts B to ≈ 501.3
  standing (current 238.6, transfers lb=3 51.4, others +26 cumulative) →
  projected ≈ 2,300: still inside the 2,500 target but above the 2,200 soft
  gate, i.e. p95 stress defers the weekly manifest by gate rather than
  breaching the target. Only the env×frequency quote remains over-scale, and
  that is driven solely by the
  single 12.4180 `current` pre-breaker outlier; under current code that class
  of run trips `ExecutionCostExceeded` + cost-breaker cooldown at the 1.5 cap
  instead of recurring silently — an operational trip, not a budget line.
- Freshness cost of B (the non-transaction price): dashboard age for
  current/pools → ≤ ~60 min + cron drain (vs ~32 min realized now);
  burn/tokenomics/holders/holder-state chain input → ≤ ~2 h; transfers windows
  stay non-gapped **because the runner itself widens lookback**:
  `lookback = max(configured lb=2, ceil(elapsed_hours + 1))` = exactly **3 h at
  a clean 120-min elapsed** (1 h overlap over the exact provider indexing
  boundary — no overlap race at lb=2), validated by the 14 historical lb=3
  runs (p50 0.0725, max 0.3108) that occurred under elapsed drift, plus its
  120-min realized cadence stretches bounded by `max_catchup_hours` (24/48 h)
  which force reviewed repair instead of drifted giant scans.
- Realized-rate sanity: since today's engine realizes ~2.15×/1.34× configured
  cadences, B expected realized spend ≈ 260.7–328.6 ±30% for the same
  definitions; the A-observed/B-configured ordering is unchanged.

### Selectable variants (same evidence bases)

| Variant | Standing forecast (mean basis) | Projected period-end | Gate verdicts (2,200 / 2,500) |
|---|---|---|---|
| B-min: §5-B exactly | 328.9 | 2,127.6 | ✓ / ✓ |
| B + daily manifest lb=7 restored (16 × 2.49) | 368.7 | 2,167.4 | ✓ / ✓ (weekly manifest still passes the 2,200 projected gate at this basis with only 32.6 credits of gate margin — any outlier-stack day defers it by gate as coded; visible, not silent) |
| B with manifest lb=30 split into two lb=15 nightly scans (n<3 quorum still: cost unknown until quoted) | not quotable now | — | stays gated |

## 6. Explicit one-off additions NOT inside the B forecast (must be added explicitly)

1. **3-day chain-stale recovery** — chain paused since 2026-10-16T20:59Z, BBB
   holder data as-of 2026-10-13 22:41; resumption run exceeds
   `max_catchup_hours` → code routes to reviewed repair, not a simple catch-up.
   Analog scale only (each an outlier to its own identity, no analog quote
   exists): lb=16 0.147, lb=25 0.513, lb=240 2.741 — real quote requires the
   approved-repair flow and is a line item with unknown cost until then.
2. **2 unknown canceled executions** — no provider cost, no hard upper bound
   claimable; admission fails on them (`unknown_exposure:…` blockers) until
   Dune-side reconciliation of those exact IDs.
3. Any meaningful fresh-bound export (source export quorums already met for
   lb=2 n=108 / lb=3 n=13; the export-risk reservation path is staged, capped
   at 10 credits/run config).

## 7. Recommendation

Adopt **B-min** (with B + selective daily-manifest-on-gap as the first
re-escalation step if gap alarms fire): it is the only quantified configuration
that fits target/soft-gate simultaneously at both p50 and outlier-inclusive
mean bases while keeping every untouched component (network/commercial/bbb
1/day, identical SQL and derivations, manifest methodology preserved) and all
existing per-run caps/breakers. Sequence to act: (user approval) → lift the
1,800-guard decision for the remaining window with B cadences → fix `bbb_dex`
scheduled lookback 26→29 (matches observed engine behavior and existing
evidence; lb=26 has zero samples; admission still stays unknown for bbb until
B's first run supplies the 3rd comparable lb=29 sample) → the recovery one-off
(§6.1) → re-enable cadences → verify the first comparable counts so the 168 h
estimator window restocks with fresh quorum before the next weekly manifest
gets gate-allowed.
Until approval: zero fresh Dune compute stays the standing state, cached
presentation and metadata-only usage checks keep working (current practice
since Oct 17), and nothing in this document triggers Dune compute or exports.

## 8. Evidence index

- State/journal blob: `origin/live-state` @0fe79005 (`data/current/
  v3_refresh_state.json`, blob e30bdc556a8b0a2476f9a1999a0b3938c4ad1261;
  `data/current/v3_credit_usage.json`, blob 2eb356529fcaec8…) — 1,000-row
  history, billing lane, monthly counters, source_errors, manual
  reconciliations, recovery_review, result_export markers (none outstanding).
- Config: `config/v3_refresh.json` @ca8a076a ≡ @313dd0be (blob 268f807c…)
  — cadences 30/1440/15/30, max_run caps, manifest daily/weekly lb 7/30 +
  cap 15 + soft 2,200 + headroom 150, guard 1800, reserve 15, target 2,500,
  export rate 10/MB, max export 1 MB/run, catch-up 24/48 h.
- Estimator/admission semantics: `scripts/v3_credit_budget.py` @HEAD
  (blob c68f480e…) — envelope= max(observed_max, mean+3σ), quorum 3, 168 h
  window, 10 credits/MB, unknown-blocking; `scripts/v3_budget_runtime.py`
  — `scheduled_forecast` × cadence counts to exact renewal; §2/§5 numbers are
  computed against these definitions from the state blob.
- Engine semantics: `scripts/v3_refresh_dashboard.py` (`due()` cadence−60 s,
  source lookback ceil(elapsed+1), `max_presentation_queries_per_run`=5,
  reconciliation gates incl. weekly soft-projected check and quarantine at
  cap overrun).
- Mission evidence: docs/reliability/ASTRA_DELIVERY_2026-10-18.md (usage
  checked twice at 1,798.673; 4,000 quota; renewal instant; no fresh compute
  statement), RELIABILITY_RUNBOOK.md step 5 (fail-closed admission).
- Momentum facts: last observed full chain cycle Oct 16 20:58–21:00 costs
  1.2143 total; usage motion 4,298.928 account-total at Oct 17 03:33 is the
  last sample; nothing has been billed since.
# Live-update caveat from recovery operations

The calculations below use the explicitly pinned historical usage snapshot of
1,798.673 credits, not a current balance. Subsequent official usage reads reached
**1,799.288** after the approved 14/15 October computations (0.342 + 0.273).
The approved 16 October execution failed on upstream Dune metadata; its charge
was not supplied. Export charges and the two earlier canceled-execution costs
remain unresolved. Add these exposures and actual one-off recovery spending to
any renewed forecast; do not infer charge settlement from aggregate equality.

The cadence analysis is a recommendation, **not installed configuration or
spending authorization**. PR #42 preserves existing cadence and the 1,800 guard.
Every new execution/export still requires operation-specific approval under
`AGENTS.md`; a forecast or available quota is not that approval.
