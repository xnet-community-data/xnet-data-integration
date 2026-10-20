# Approved catch-up and presentation verification — 20 October 2026

## Outcome and boundaries

The explicitly approved, one-off **65-credit-risk batch is complete**. All 13
executions succeeded; each had exactly one result retrieval, with no automatic
retry. This includes the separately authorized retry of the previously failed
16 October query. No standing paid schedule, budget-guard increase, dashboard
layout change, accounting-methodology change or external adapter change was
authorized or installed.

The recovery is **not a claim of continuously live chain coverage**. The newest
transfer event returned by Dune is **20 October, 05:43:06 UTC**. The 20 October
UTC day is incomplete. Network/device source dates remain 19 October; market
and revenue keep their independently collected timestamps.

## Published source generation

- Live-state revision: `573e07e2d267041bdec80db176cba86b92466f40`.
- Exact predecessor: `7dfe45cac3dcc903312491d51e194e25753d97da`.
- Generation: `8c7db148-d667-46ff-bd23-7def087a74a7`.
- Source acceptance receipt:
  `c015139569b84b8b42d8d296a6d2944d1d01e9a544febbe39f01e6563bfff925`.
- All **34 checksums** and bundle/component equality verified remotely. The
  public raw LiveFetch URL also returned the exact accepted bundle before the
  presentation executions were submitted.

The existing 3,470-row ledger is an unchanged byte prefix of the **4,240-row**
ledger. There are **770 additions, zero deletions, 4,240 unique event IDs**.
The 117 existing 16 October rows all match the complete 187-row provider result.

| Day | Execution | Provider rows | Matched existing | Added | Compute credits |
|---|---|---:|---:|---:|---:|
| 16 October | `01M5CA10TBS6435YJRFB5XJ05G` | 187 | 117 | 70 | 1.435 |
| 17 October | `01M5CA6812R7CT5SB3GH1MDQWQ` | 217 | 0 | 217 | 0.285 |
| 18 October | `01M5CA8DTD875DSF09VHJBB4MF` | 280 | 0 | 280 | 0.378 |
| 19 October | `01M5CAA3NCH6P0H3XRMNMD46QK` | 190 | 0 | 190 | 0.158 |
| 20 October, partial | `01M5CAD7DJNC67CAAB65ZXRYBK` | 13 | 0 | 13 | 0.299 |

All five use saved query `8894038` v4, medium engine, one absolute UTC day
expressed by relative offsets verified against the actual 20 October UTC date.
Complete provider responses for closed days are not proof against future
provider restatements; no previously accepted row was replaced by assumption.

Independent Decimal conservation review verified:

- Seed `1188412748.15881428` + zero post-cutoff mint − burns
  `6089352.67139496` = final balances `1182323395.48741932`, exact difference zero.
- **5,306 positive holders**; circulating supply `450854891.88866935994` XNET.
- Verified cumulative BBB burn `15788205.313266761` XNET; no balance clamping,
  seed resetting, amount rescaling or fabricated routed legs.
- Strict production reduction succeeded without stale-holder fallback.

## BBB catch-up

Saved query `8894132` advanced from v11 to v12. The only SQL change makes the
partition lower bound follow `lookback_hours`; its former two-calendar-day bound
silently truncated longer requests. Output schema, economic aggregation,
parameters, name and privacy were verified unchanged after saving.

Execution `01M5CAP1RBS3A6TX2PYWP6HBNH`, medium engine, 120 hours: **31 transaction-level
records**, 22 additional economic transactions, **0.355 compute credits**. Its
window overlaps the previous continuous BBB coverage; all captured rows belong
to the BBB wallet. A separate single finalized public RPC call observed
**12,768.416038 USDC** at **09:43:14.975129 UTC**, slot `459184478`. The receipt binds
that capture without treating its observation clock as a transfer collection time.

## Dune presentation verification

Receipt `c0a93b87087c680c6a98c7316cb3fd612dd114d34cfc57f36b941a0e7f429aaa`
was accepted in checkpoint `fd3a611f6dc7744d2dcc73ac396ef3e7a65dc870`.
All seven result sets were compared offline with the accepted LiveFetch inputs:
all history projection columns and the 18 checked chain columns in every current
snapshot row, including source timestamps and observed USDC provenance.
Read-only saved-query metadata subsequently confirmed that all seven latest
execution pointers match these verified IDs and their query versions are
unchanged. That metadata endpoint returned NULL execution states; completion is
proven by the captured execution-result responses, not inferred from those NULLs.

| Presentation / query | Execution | Rows | Compute credits |
|---|---|---:|---:|
| Current / 8894185 v18 | `01M5CBBVB51YWT33FGWS1EAWPC` | 5 | 0.279 |
| Burns / 8895090 v5 | `01M5CBCJ6KEMHVWK6TPMXBQV0N` | 295 | 0.175 |
| Tokenomics / 8895091 v6 | `01M5CBD7K2VKSDY74SKTS1S7T9` | 26 | 0.109 |
| Holders / 8895092 v3 | `01M5CBDWJ87W7H4QGRRXCHWETC` | 6 | 0.115 |
| Pools / 8895093 v3 | `01M5CBEP7J41YPZ930DR4YSST7` | 4 | 0.103 |
| Network / 8895088 v4 | `01M5CBFGSA72XH2HPZGFYV06R7` | 27 | 0.110 |
| Commercial / 8895089 v8 | `01M5CBG9ERKRJJC97Z7K817EXA` | 26 | 0.160 |

Two optional current-query fields (`bbb_wallet_xnet_balance`,
`latest_bbb_trade_utc`) are absent from the pre-existing snapshot contract and
remain SQL NULL; their presence was not invented or presented as verified data.
No extra refresh was performed to change that contract. Browser rendering was
not inspected; verification covers the exact query results and unchanged
dashboard metadata (58 visualizations, 13 texts, zero parameter widgets).
Layout/text comparison checksums remain `826ed5ed` / `9df2e2ce`.

## Costs, remaining holds and reproduction

Reported compute: **2.910 source + 1.051 presentation = 3.961 credits**.
Official usage moved from **1,799.288 to 1,803.249**, quota 4,000, renewal
**4 November 00:00 UTC**. This aggregate equality is **not settlement proof** for
exports, the prior failed execution or older canceled executions. Unknown
liabilities and every retrieval attempt remain in the durable financial journal.
The finite batch is marked `completed_with_unsettled_charges`, with zero remaining
authorized executions/retrievals. The 1,800-credit emergency guard is unchanged.
Independent financial review confirmed **all 1,140 predecessor obligations
unchanged**, plus exactly 13 new execution and 13 new export obligations. All five
prior unknown export charges and all 13 new unknown export charges remain
unresolved. Old `billing.guard_credits` observations are historical diagnostics;
the active configuration/admission policy remains 1,800, not those old values.

The cost-sustainability recommendation remains an uninstalled, separately
unapproved operating plan. Historical manifest quarantine also remains; this
batch did not execute that expensive query. Broad recovery-planner and scoped
replacement drafts remain excluded from production.

Exact captured response bytes and sealed receipts are archived in
`tests/fixtures/approved_batch_20261020/`. The receipts retain their original
local capture paths; the fixture copies have identical SHA-256 digests. Offline
regressions verify artifact bindings, day scope, source counts, event identities,
raw amount precision, BBB ownership, finalized USDC evidence and Dune output
anchors. Run:

```sh
python3 -m unittest tests.test_v3_approved_batch_evidence tests.test_v3_bbb_partition_window
```

Reviewed staged-code suite: **513 tests successful**, one existing
remote-ref-dependent skip, no expected failures. The broader working-tree suite
also passed 671 tests before these four new evidence gates were added; unfinished
planner/replacement drafts from that broader tree are not included in this release.
