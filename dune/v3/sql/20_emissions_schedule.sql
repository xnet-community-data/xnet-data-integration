-- XNET source-confirmed reward epoch schedule.
--
-- The live XNET source sheet publishes exact epoch start/end dates and
-- dispersal totals. This helper intentionally mirrors only those published
-- epochs. It does not invent future epoch boundaries or calendar-year decay
-- dates; future reductions become visible when the source sheet publishes
-- them.

WITH raw AS (
    SELECT json_parse(
        http_get(
            'https://raw.githubusercontent.com/xnet-community-data/xnet-data-integration/main/data/xnet_epoch_schedule.json'
        )
    ) AS j
),
items AS (
    SELECT item
    FROM raw
    CROSS JOIN UNNEST(
        CAST(json_extract(j, '$.data') AS ARRAY(JSON))
    ) AS t(item)
)
SELECT
    TRY_CAST(
        json_extract_scalar(item, '$.epoch')
        AS BIGINT
    ) AS epoch,
    CAST(
        json_extract_scalar(item, '$.start_date')
        AS DATE
    ) AS epoch_start_date,
    CAST(
        json_extract_scalar(item, '$.end_date')
        AS DATE
    ) AS epoch_end_date,
    TRY_CAST(
        json_extract_scalar(item, '$.poc_dispersal_xnet')
        AS DOUBLE
    ) AS poc_dispersal_xnet,
    TRY_CAST(
        json_extract_scalar(item, '$.data_dispersal_xnet')
        AS DOUBLE
    ) AS data_dispersal_xnet,
    TRY_CAST(
        json_extract_scalar(item, '$.bonus_dispersal_xnet')
        AS DOUBLE
    ) AS bonus_dispersal_xnet,
    TRY_CAST(
        json_extract_scalar(item, '$.total_dispersal_xnet')
        AS DOUBLE
    ) AS total_dispersal_xnet,
    TRY_CAST(
        json_extract_scalar(item, '$.fiat_operator_burn_xnet')
        AS DOUBLE
    ) AS fiat_operator_burn_xnet
FROM items
ORDER BY epoch
