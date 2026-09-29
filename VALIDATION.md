# Validation Record

**Run:** 29 September 2026 at 21:09 local time in the contributor's test environment.

## Network offload

- `/api/data` -> HTTP 200
- 793 records
- newest record -> 2026-09-29, 1,178 GB
- oldest record -> 2024-07-29, 329 GB
- 793 unique dates across a 793-day inclusive span
- no missing calendar dates in the stored history
- `/api/latest` -> HTTP 200
- `/api/data?days=1` -> HTTP 200
- `/api/summary?days=7` -> HTTP 200
- explicit date range -> HTTP 200

The 29 September value was captured while the calendar day was in progress and should not be treated as a finalized full-day comparison.

## Devices

- `/api/latest` -> HTTP 200
- scrape date -> 2026-09-29
- total devices -> 6,629
- operational devices -> 5,839
- `/api/history?limit=1` -> HTTP 200
- `/api/history?limit=30` -> HTTP 200
- `/api/history?limit=100` -> HTTP 200

History is a sequence of scrape observations and contains date gaps. Missing scrape dates must not be interpreted as zero devices.

## Revenue sheet

- public CSV export -> HTTP 200
- expected revenue and payment rows present
- projected revenue and payment-received series are separate
- the revenue-sheet `GB per month` series is the source to use for revenue calculations
- the daily network-offload API GB series is separate and must not be substituted for revenue billing GB


This is a dated validation record. Consumers should query the live upstream sources for current values.
