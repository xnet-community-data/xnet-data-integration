WITH raw AS (
    SELECT json_parse(http_get('https://raw.githubusercontent.com/xnet-community-data/xnet-data-integration/live-state/data/presentation/holder_distribution.json')) AS j
),
items AS (
    SELECT item
    FROM raw
    CROSS JOIN UNNEST(
        CAST(json_extract(j, '$.data') AS ARRAY(JSON))
    ) AS t(item)
)
SELECT
    TRY_CAST(json_extract_scalar(item, '$.bucket_order') AS BIGINT)
        AS bucket_order,
    json_extract_scalar(item, '$.bucket') AS bucket,
    TRY_CAST(json_extract_scalar(item, '$.holder_count') AS BIGINT)
        AS holder_count
FROM items
ORDER BY bucket_order
