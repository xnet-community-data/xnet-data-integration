# Circulating-supply reconciliation notes

The legacy XNET query supplied for V3 is valuable because it reveals the old page's methodology rather than merely a displayed number.

The active query:
- starts from a hard-coded 2.4B XNET baseline,
- tracks net flows into a curated exclusion list,
- subtracts those balances from the baseline,
- separately subtracts cumulative on-chain burns,
- and reports the remainder as `balance`.

The exclusion list mixes several legacy categories:
- Insider lock or Ops pool
- Temporary usage
- Temp Dev accounts
- Burn accounts

V3 MUST NOT publish this legacy result unchanged.

Reconciliation plan:
1. Preserve the legacy wallet list and labels exactly as evidence.
2. Establish the current on-chain outstanding supply independently.
3. Reconcile the 2.4B legacy baseline with XIP-10 / post-XIP-10 max supply.
4. Resolve each legacy wallet into a versioned wallet registry with evidence and effective dates.
5. Count token OWNERS, not SPL token accounts.
6. Reconstruct positive owner balances and holder counts.
7. Define non-circulating supply only from documented/verified classifications.
8. Reconcile sum(positive owner balances) against independently observed outstanding supply.
9. Publish reconstructed circulating supply only after the reconciliation error is acceptably small and classifications are documented.
10. Keep provider market cap and reconstructed market cap as separate QA fields.

Until then, V3 remains private and supply fields are QA-only.
