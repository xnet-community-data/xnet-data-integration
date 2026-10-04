WITH raw AS (
    SELECT json_parse(http_get('https://raw.githubusercontent.com/xnet-community-data/xnet-data-integration/live-state/data/derived/tokenomics_history.json')) AS j
),
market AS (
    SELECT json_parse(http_get('https://raw.githubusercontent.com/xnet-community-data/xnet-data-integration/live-state/data/current/xnet_market_state.json')) AS j
),
items AS (
    SELECT item
    FROM raw
    CROSS JOIN UNNEST(
        CAST(json_extract(j, '$.data') AS ARRAY(JSON))
    ) AS t(item)
)
SELECT
    CAST(json_extract_scalar(item, '$.month') AS DATE) AS month,
    TRY_CAST(json_extract_scalar(item, '$.scheduled_emissions_xnet') AS DOUBLE)
        AS scheduled_emissions_xnet,
    TRY_CAST(json_extract_scalar(item, '$.xnet_burned') AS DOUBLE)
        AS xnet_burned,
    TRY_CAST(json_extract_scalar(item, '$.emissions_per_network_gb') AS DOUBLE)
        AS emissions_per_network_gb,
    TRY_CAST(json_extract_scalar(item, '$.emissions_per_network_gb') AS DOUBLE)
        * TRY_CAST(json_extract_scalar(market.j, '$.xnet_price_usd') AS DOUBLE)
        AS emissions_usd_per_gb_at_current_price,
    TRY_CAST(json_extract_scalar(item, '$.circulating_supply_xnet') AS DOUBLE)
        AS circulating_supply_xnet,
    TRY_CAST(json_extract_scalar(item, '$.circulation_change_xnet') AS DOUBLE)
        AS circulation_change_xnet
FROM items CROSS JOIN market
ORDER BY month
