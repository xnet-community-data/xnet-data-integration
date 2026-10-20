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
    WHERE block_date BETWEEN
          CAST(CURRENT_DATE - INTERVAL '{{oldest_days_ago}}' DAY AS DATE)
          AND CAST(CURRENT_DATE - INTERVAL '{{newest_days_ago}}' DAY AS DATE)
      AND token_mint_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
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
    CAST(amount AS DOUBLE) / 100000000.0 AS amount_xnet,
    COUNT(*) AS event_multiplicity
FROM xnet
GROUP BY 1,2,3,4,5,6,7,8,9,10
ORDER BY block_time, block_slot, tx_id
