-- XNET V3 reusable hot transfer source.
--
-- Runs on a 2-hour overlap so an hourly collector can safely deduplicate
-- locally and tolerate indexing delay.
--
-- This ONE source powers:
--   holders
--   circulating supply
--   burns
--   BBB wallet activity
--
-- Identical decoded rows inside a transaction are represented once with
-- event_multiplicity. Downstream arithmetic MUST multiply amount_xnet by
-- event_multiplicity.

WITH xnet AS (
    SELECT
        block_date,
        block_time,
        block_slot,
        tx_id,
        action,
        from_owner,
        to_owner,
        from_token_account,
        to_token_account,
        amount
    FROM tokens_solana.transfers
    WHERE block_date >= CURRENT_DATE - INTERVAL '1' DAY
      AND block_time >= CURRENT_TIMESTAMP - INTERVAL '{{lookback_hours}}' HOUR
      AND token_mint_address =
          'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
)

SELECT
    block_date,
    block_time,
    block_slot,
    tx_id,
    action,
    from_owner,
    to_owner,
    from_token_account,
    to_token_account,

    amount AS amount_raw,

    CAST(amount AS DOUBLE) / 100000000.0
        AS amount_xnet,

    COUNT(*) AS event_multiplicity

FROM xnet

GROUP BY
    block_date,
    block_time,
    block_slot,
    tx_id,
    action,
    from_owner,
    to_owner,
    from_token_account,
    to_token_account,
    amount

ORDER BY
    block_time,
    block_slot,
    tx_id
