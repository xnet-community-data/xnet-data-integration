WITH raw AS (
    SELECT json_parse(http_get('https://raw.githubusercontent.com/xnet-community-data/xnet-data-integration/live-state/data/presentation/network_history.json')) AS j
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
    TRY_CAST(json_extract_scalar(item, '$.network_offload_gb') AS DOUBLE)
        AS network_offload_gb,
    TRY_CAST(json_extract_scalar(item, '$.total_devices') AS DOUBLE)
        AS total_devices,
    TRY_CAST(json_extract_scalar(item, '$.total_operational') AS DOUBLE)
        AS total_operational,
    TRY_CAST(json_extract_scalar(item, '$.offload_per_operational_device') AS DOUBLE)
        AS offload_per_operational_device,
    TRY_CAST(json_extract_scalar(item, '$.indexed_operational_devices') AS DOUBLE)
        AS indexed_operational_devices,
    TRY_CAST(json_extract_scalar(item, '$.indexed_offload') AS DOUBLE)
        AS indexed_offload,
    TRY_CAST(json_extract_scalar(item, '$.indexed_revenue') AS DOUBLE)
        AS indexed_revenue,
    TRY_CAST(json_extract_scalar(item, '$.unused_series') AS DOUBLE)
        AS unused_series
FROM items
ORDER BY month
