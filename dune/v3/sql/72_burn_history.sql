WITH raw AS (
    SELECT json_parse(http_get('https://raw.githubusercontent.com/xnet-community-data/xnet-data-integration/live-state/data/presentation/burn_history.json')) AS j
),
items AS (
    SELECT item
    FROM raw
    CROSS JOIN UNNEST(
        CAST(json_extract(j, '$.data') AS ARRAY(JSON))
    ) AS t(item)
)
SELECT
    CAST(json_extract_scalar(item, '$.day') AS DATE) AS day,
    TRY_CAST(json_extract_scalar(item, '$.xnet_burned') AS DOUBLE)
        AS xnet_burned,
    TRY_CAST(json_extract_scalar(item, '$.burn_transactions') AS BIGINT)
        AS burn_transactions,
    TRY_CAST(json_extract_scalar(item, '$.cumulative_xnet_burned') AS DOUBLE)
        AS cumulative_xnet_burned,
    TRY_CAST(json_extract_scalar(item, '$.bbb_wallet_usdc_balance') AS DOUBLE)
        AS bbb_wallet_usdc_balance,

    CAST(json_extract_scalar(raw.j, '$.summary.latest_burn_day') AS DATE)
        AS latest_burn_day,
    TRY_CAST(json_extract_scalar(raw.j, '$.summary.burn_last_7d_xnet') AS DOUBLE)
        AS burn_last_7d_xnet,
    TRY_CAST(json_extract_scalar(raw.j, '$.summary.burn_last_30d_xnet') AS DOUBLE)
        AS burn_last_30d_xnet,
    TRY_CAST(json_extract_scalar(raw.j, '$.summary.total_xnet_burned') AS DOUBLE)
        AS total_xnet_burned,
    TRY_CAST(json_extract_scalar(raw.j, '$.summary.total_burn_transactions') AS BIGINT)
        AS total_burn_transactions,
    TRY_CAST(json_extract_scalar(raw.j, '$.summary.verified_bbb_pct_max_supply') AS DOUBLE)
        AS verified_bbb_pct_max_supply,
    TRY_CAST(json_extract_scalar(raw.j, '$.summary.verified_bbb_pct_circulating_supply') AS DOUBLE)
        AS verified_bbb_pct_circulating_supply

FROM raw
CROSS JOIN items
ORDER BY day
