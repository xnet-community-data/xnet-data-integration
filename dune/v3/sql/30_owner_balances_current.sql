WITH xnet_accounts AS (
    SELECT
        address AS token_account_address,
        NULLIF(token_balance_owner, '') AS token_balance_owner,
        COALESCE(NULLIF(token_balance_owner, ''), address) AS resolved_owner,
        token_balance AS xnet_balance,
        block_time,
        block_slot,
        updated_at
    FROM solana_utils.latest_balances
    WHERE token_mint_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
      AND token_balance IS NOT NULL
),
owner_balances AS (
    SELECT
        resolved_owner AS owner,
        SUM(xnet_balance) AS xnet_balance,
        COUNT(*) AS token_account_count,
        MAX(block_time) AS latest_balance_event_time,
        MAX(updated_at) AS latest_source_update,
        MAX(CASE WHEN token_balance_owner IS NULL THEN 1 ELSE 0 END) AS has_missing_owner_component
    FROM xnet_accounts
    GROUP BY 1
)
SELECT
    owner,
    xnet_balance,
    token_account_count,
    latest_balance_event_time,
    latest_source_update,
    has_missing_owner_component
FROM owner_balances
WHERE xnet_balance > 0
