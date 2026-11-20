# Network acquisition — collector issue is now surfaced; upstream repair outstanding

20 November 2026. Scope: the free independent network lane
(`scripts/v3_sync_network.py`, invoked by `sync_independent_network` in
`scripts/v3_refresh_dashboard.py`). No financial, budget, Dune, device or
offload definition changed.

## Observed condition

`external_sources.network` holds

```
status            preserved_last_good
last_success_utc  2026-11-13T16:48:57Z
last_attempt_utc  2026-11-19T13:31:31Z
last_error        Offload API is stale: latest completed day is 2026-11-11
offload_data_as_of 2026-11-11
device_data_as_of  2026-11-12
```

The device API is healthy (`scrapeDate` 2026-11-19, 6,750 total / 3,929
operational). Only the offload feed stopped advancing. The last acquisition
that passed validation landed exactly on the `today-2` boundary — 13 November
with `today-2 = 11 November` — and every attempt since then fails the same
staleness gate, correctly retaining last-good data instead of publishing a
fabricated day.

The collector was never at fault: it exits 0 by contract, records
`preserved_last_good`, and refuses the payload. The parent, however, popped
`source_errors["network"]` whenever the child process returned 0 — including a
pure cadence ("not due") skip — so the deployment stayed silent about a genuine
72-hour-plus staleness alarm. That is the observability defect fixed here.

## Why the alarm must stay red

Acquisition age on 20 November is ~152 h, past the 72 h watchdog bound. The
alarm is truthful and is deliberately **not** relaxed: no loosening of the
`today-2` gate, the 6 h/72 h bounds, or the gapless-calendar rule, and no
device/offload decoupling or filled days. The parent now surfaces the
collector's own status, error and attempt time, and clears the issue only after
a validated acquisition is actually published.

## Evidence from the upstream payload (free read, no execution)

An unauthenticated read of the already-configured offload endpoint returned
HTTP 200 with 836 rows and newest `{"day":"2026-11-11","gigabytes":85}`.

Interval context for that value (GB/day):

| measure | value |
| --- | --- |
| prior 7-day median | 3,808 |
| historical p1 | 112 |
| historical minimum | 27 |
| 11 November | 85 (2.2 % of the prior-7-day median) |

**85 GB is suspect, not proven corrupt.** A single-point collapse of that size
is consistent with a truncated or partial-day export at the upstream scraper,
but it is also consistent with a genuine outage day. Nothing in this repository
can distinguish those cases, and no local code may silently drop or repair the
day. Treat it as unverified upstream evidence until the feed owner confirms it.

## Owner action required (outside the XNET repository)

The offload scraper (`xnet-offload-scraper.vercel.app/api/data`) is external to
this repository; no authorized upstream repair/backfill path has been established
within this repository's scope. The feed owner must:

1. restore daily publication for 2026-11-12 onward, and
2. backfill 2026-11-12 … 2026-11-20 (or state that those days do not exist), and
3. confirm whether 2026-11-11 is a complete day or a truncated export.

## Forward risks until then

* If the feed resumes **without** backfilling, the collector's gapless-calendar
  validation will continue to refuse the payload: a resume alone does not clear
  the alarm.
* The observed 85 GB entry remains part of the source totals unless upstream
  revises it. It must not be silently dropped or rescaled locally.
* Until the owner acts, the watchdog is expected to remain red on the network
  acquisition bound. That is the correct signal, not a regression.

## Verification

`tests/test_v3_network_source_issue.py` pins: a zero-exit
`preserved_last_good` surfaces the child's own error and attempt time; a not-due
skip neither clears nor re-stamps nor resets backoff; a new failed attempt
increments once; only a validated published acquisition clears the network issue
while unrelated issues and the financial journal are untouched; a success
checkpoint ahead of its published data never clears; an invalid or missing child
record fails closed through the existing `record_source_issue` path.
