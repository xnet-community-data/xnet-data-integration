WITH raw AS (
    SELECT json_parse(http_get('https://raw.githubusercontent.com/xnet-community-data/xnet-data-integration/live-state/data/derived/commercial_history.json')) AS j
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
    TRY_CAST(json_extract_scalar(item, '$.wifi_revenue_projected_usd') AS DOUBLE)
        AS wifi_revenue_projected_usd,
    TRY_CAST(json_extract_scalar(item, '$.wifi_payment_received_usd') AS DOUBLE)
        AS wifi_payment_received_usd,
    TRY_CAST(json_extract_scalar(item, '$.dated_cash_received_usd') AS DOUBLE)
        AS dated_cash_received_usd,
    TRY_CAST(json_extract_scalar(item, '$.cumulative_projected_wifi_revenue_usd') AS DOUBLE)
        AS cumulative_projected_wifi_revenue_usd,
    TRY_CAST(json_extract_scalar(item, '$.cumulative_dated_cash_received_usd') AS DOUBLE)
        AS cumulative_dated_cash_received_usd,
    TRY_CAST(json_extract_scalar(item, '$.blended_rate_per_gb_projected_usd') AS DOUBLE)
        AS blended_rate_per_gb_projected_usd,
    TRY_CAST(json_extract_scalar(item, '$.projected_wifi_revenue_per_operational_device') AS DOUBLE)
        AS projected_wifi_revenue_per_operational_device,
    TRY_CAST(json_extract_scalar(item, '$.projected_buy_burn_usd') AS DOUBLE)
        AS projected_buy_burn_usd,
    TRY_CAST(json_extract_scalar(item, '$.transferred_to_buy_burn_usd') AS DOUBLE)
        AS transferred_to_buy_burn_usd,
    TRY_CAST(json_extract_scalar(item, '$.unused_series_1') AS DOUBLE)
        AS unused_series_1,
    TRY_CAST(json_extract_scalar(item, '$.unused_series_2') AS DOUBLE)
        AS unused_series_2
FROM items
WHERE CAST(json_extract_scalar(item, '$.month') AS DATE) <= CURRENT_DATE
ORDER BY month
