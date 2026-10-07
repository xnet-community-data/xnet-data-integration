-- XNET source-confirmed reward epoch schedule.
--
-- The live XNET source sheet publishes exact epoch start/end dates and total
-- reward tokens. This helper mirrors only those published values. Historical
-- PoC/Data/Bonus component splits are intentionally excluded because they do
-- not represent the current offload-based reward model. No future epoch or
-- decay boundary is inferred.

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
        json_extract_scalar(item, '$.total_reward_tokens_xnet')
        AS DOUBLE
    ) AS total_reward_tokens_xnet,
    TRY_CAST(
        json_extract_scalar(item, '$.fiat_operator_burn_xnet')
        AS DOUBLE
    ) AS fiat_operator_burn_xnet
FROM items
ORDER BY epoch
