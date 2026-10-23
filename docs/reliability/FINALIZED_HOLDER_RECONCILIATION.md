# Autonomous finalized holder reconciliation

## Current balances no longer depend on repairing a historical seed

Holder accounting uses a complete **current finalized account inventory**, not a
guessed adjustment to the original Dune seed. The original seed CSV, metadata,
canonical transfers, financial journal and historical conflict captures remain
unchanged and auditable. The earlier opening conflict is resolved for **current
operations by changing the verified working basis**, not by pretending the old
seed was historically correct.

The definition remains every genuinely positive owner balance. All token accounts
belonging to an owner are aggregated using exact unsigned raw units; frozen
positive accounts and a single raw unit still count. No balances are clamped,
rescaled, assigned to guessed owners or represented by fabricated transfer legs.

## Acceptance proof

The read-only context sampler starts each attempt with a batch requesting:

1. All Token-2022 accounts filtered by the exact XNET mint, with raw account data
   and `withContext=true` at finalized commitment.
2. That mint's raw account at finalized commitment.

The accepted responses **must have the exact same finalized context slot**.
If an HTTP batch spans banks, bounded additional mint samples are indexed by
context and can pair with an inventory from another HTTP call at that **identical
immutable finalized slot**. The selected source attempts and exact transport
requests are retained. No projection across slots, averaging or quiet-period
assumption is used. Equal supplies at different slots are still refused.
The decoder validates
request provenance, program ownership, mint identity, initialized/frozen account
state, eight decimals, unique public keys and complete raw bytes.

The sum of all unsigned account amounts must equal the independently decoded
mint supply **exactly**. A missing positive account, duplicated account or
unexplained withheld amount cannot silently pass. The owner vector, account
capture and receipt are digest-bound and replayable. A separate `getBlockTime`
request binds the balance-as-of to that exact slot, not to workflow/generated time.

The production-data replay used 11,920 accounts at finalized slot **460555368**,
19:04:51 UTC on 23 October 2026. It reconstructed **5,311 positive owners** and
**1,289,244,880.79419788 XNET**, with a zero raw-unit mint residual. That is replay
evidence; each live renewal records its own slot, counts and totals.

## Renewal and recovery

- The fixed-mint official Solana RPC source renews every **120 minutes**.
- At most **eight alignment attempts** per renewal, each with a 30-second HTTP
  timeout and 32 MB response bound, under a 64 MB admission budget and a
  90-second admission deadline. An in-flight response remains individually
  byte/timeout-bounded; uncertain reads reserve their full allowance rather
  than being reported as zero-byte successes.
  Each attempt uses one two-method batch, at most one cheap mint follow-up if
  needed, and only after exact context alignment one block-time read. No paid
  Dune fallback exists.
  Different finalized banks are never averaged or treated as equivalent. The
  sampled context slots and byte/attempt counts remain in the accepted proof.
- A failed acquisition preserves last-good balances, records the error and retries
  automatically after one hour. A snapshot older than six hours cannot be labeled
  healthy, regardless of a fresh build timestamp.
- Late or incomplete Dune transfer indexing does not corrupt the complete holder
  vector: incomplete indexed events are **not overlaid** on the finalized inventory.
  Canonical transfer/burn/circulating-supply reconciliation continues independently
  under its existing spending controls.
- Snapshot-source failures cannot trigger speculative holder day scans or expensive
  holder manifests. Precisely witnessed canonical omissions remain eligible for
  ordinary quoted day recovery.

The complete capture, owner vector, holder quality, journal reference, public
chain snapshot and LiveFetch bundle publish in **one exact-predecessor generation**.
Pre-publication build failures restore candidate machine files only. Ambiguous
publication is never compensated by overwriting a possibly accepted generation.
Previous complete captures remain in the append-only Git publication history.

## Authority, cost and publication

`--activate-holder-snapshot --cached-only` records the user's end-to-end recovery
authorization once. Configuration alone does not activate the source. The flag
cannot be combined with a paid invocation; the workflow forces cached-only for
activation. Later genuine scheduled runs renew automatically without more prompts.

This authority grants **no Dune spending**. The approved Dune standing-policy hash
remains `acb6fccc05230070c274d8a296f59dc26f51068738ff588138c55ffd7d69d9e4`;
targets, ceilings, reserves and all prior charge obligations are unchanged. The
holder source cannot clear an independent financial/global operator pause.

The proof lives in `data/current/v3_holder_baseline.json`. Public holder/chain
outputs include its baseline ID, finalized slot and explicit balance-as-of. The
watchdog independently replays the raw proof, checks every derived owner balance,
threshold count and mint total, and checks exact chain/bundle equality.

Legacy `holders_created_since_seed`/`holders_zeroed_since_seed`/net-growth fields
are null under this source: an incomplete old seed cannot prove historical
creations/deletions. Explicit reference-comparison fields are provided instead.
The existing displayed holder count/distribution and positive-owner definition
are preserved; no dashboard layout or external adapter change is required.
