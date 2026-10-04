WITH raw AS (
    SELECT json_parse(http_get('https://raw.githubusercontent.com/xnet-community-data/xnet-data-integration/live-state/data/presentation/market_pools.json')) AS j
),
items AS (
    SELECT item
    FROM raw
    CROSS JOIN UNNEST(
        CAST(json_extract(j, '$.data') AS ARRAY(JSON))
    ) AS t(item)
)
SELECT
    json_extract_scalar(item, '$.dex') AS dex,
    json_extract_scalar(item, '$.pair') AS pair,
    TRY_CAST(json_extract_scalar(item, '$.liquidity_usd') AS DOUBLE)
        AS liquidity_usd,
    TRY_CAST(json_extract_scalar(item, '$.volume_h24_usd') AS DOUBLE)
        AS volume_h24_usd,
    TRY_CAST(json_extract_scalar(item, '$.buys_h24') AS BIGINT)
        AS buys_h24,
    TRY_CAST(json_extract_scalar(item, '$.sells_h24') AS BIGINT)
        AS sells_h24,
    TRY_CAST(json_extract_scalar(item, '$.liquidity_share_pct') AS DOUBLE)
        AS liquidity_share_pct,
    json_extract_scalar(item, '$.pair_address') AS pair_address
FROM items
ORDER BY liquidity_usd DESC
