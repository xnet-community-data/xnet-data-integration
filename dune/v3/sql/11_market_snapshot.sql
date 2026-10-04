WITH pools AS (
    SELECT *
    FROM dune.xnet_community_data.result_xnet_v3_market_pools_live
),
primary_pool AS (
    SELECT *
    FROM pools
    WHERE
        base_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
        AND xnet_price_usd IS NOT NULL
        AND liquidity_usd IS NOT NULL
    ORDER BY liquidity_usd DESC
    LIMIT 1
),
summary AS (
    SELECT
        SUM(COALESCE(liquidity_usd, 0)) AS total_dex_liquidity_usd,
        SUM(COALESCE(volume_24h_usd, 0)) AS total_dex_volume_24h_usd,
        COUNT_IF(liquidity_usd > 0) AS liquidity_pool_count,
        SUM(COALESCE(xnet_buys_24h, 0)) AS xnet_buys_24h,
        SUM(COALESCE(xnet_sells_24h, 0)) AS xnet_sells_24h
    FROM pools
)
SELECT
    p.observed_at_utc,
    p.xnet_price_usd,

    p.provider_market_cap_usd,
    p.provider_fdv_usd,

    s.total_dex_liquidity_usd,
    s.total_dex_volume_24h_usd,
    s.liquidity_pool_count,
    s.xnet_buys_24h,
    s.xnet_sells_24h,

    p.xnet_price_change_1h_pct,
    p.xnet_price_change_6h_pct,
    p.xnet_price_change_24h_pct,

    p.dex_id AS primary_pool_dex,
    p.quote_symbol AS primary_pool_quote_asset,
    p.pair_address AS primary_pool_address,
    p.liquidity_usd AS primary_pool_liquidity_usd,
    p.volume_24h_usd AS primary_pool_volume_24h_usd,

    p.liquidity_usd
        / NULLIF(s.total_dex_liquidity_usd, 0)
        AS primary_pool_liquidity_share

FROM primary_pool p
CROSS JOIN summary s
