WITH raw AS (
    SELECT json_parse(
        http_get(
            'https://raw.githubusercontent.com/xnet-community-data/xnet-data-integration/live-state/data/derived/commercial_history.json'
        )
    ) AS j
),

items AS (
    SELECT item
    FROM raw
    CROSS JOIN UNNEST(
        CAST(json_extract(j, '$.data') AS ARRAY(JSON))
    ) AS t(item)
),

monthly AS (
    SELECT
        CAST(json_extract_scalar(item, '$.month') AS DATE) AS month,

        TRY_CAST(
            json_extract_scalar(item, '$.wifi_revenue_projected_usd')
            AS DOUBLE
        ) AS wifi_revenue_projected_usd,

        TRY_CAST(
            json_extract_scalar(item, '$.wifi_payment_received_usd')
            AS DOUBLE
        ) AS wifi_payment_received_usd,

        TRY_CAST(
            json_extract_scalar(item, '$.dated_cash_received_usd')
            AS DOUBLE
        ) AS dated_cash_received_usd,

        TRY_CAST(
            json_extract_scalar(
                item,
                '$.cumulative_projected_wifi_revenue_usd'
            )
            AS DOUBLE
        ) AS cumulative_projected_wifi_revenue_usd,

        TRY_CAST(
            json_extract_scalar(
                item,
                '$.cumulative_dated_cash_received_usd'
            )
            AS DOUBLE
        ) AS cumulative_dated_cash_received_usd,

        TRY_CAST(
            json_extract_scalar(
                item,
                '$.blended_rate_per_gb_projected_usd'
            )
            AS DOUBLE
        ) AS blended_rate_per_gb_projected_usd,

        TRY_CAST(
            json_extract_scalar(
                item,
                '$.projected_wifi_revenue_per_operational_device'
            )
            AS DOUBLE
        ) AS projected_wifi_revenue_per_operational_device,

        TRY_CAST(
            json_extract_scalar(item, '$.projected_buy_burn_usd')
            AS DOUBLE
        ) AS projected_buy_burn_usd,

        TRY_CAST(
            json_extract_scalar(item, '$.transferred_to_buy_burn_usd')
            AS DOUBLE
        ) AS transferred_to_buy_burn_usd,

        TRY_CAST(
            json_extract_scalar(item, '$.fiat_operator_payout_usd')
            AS DOUBLE
        ) AS fiat_operator_payout_usd,

        TRY_CAST(
            json_extract_scalar(item, '$.fiat_gross_allocation_usd')
            AS DOUBLE
        ) AS fiat_gross_allocation_usd,

        TRY_CAST(
            json_extract_scalar(item, '$.fiat_bbb_allocation_usd')
            AS DOUBLE
        ) AS fiat_bbb_allocation_usd,

        TRY_CAST(
            json_extract_scalar(item, '$.fiat_operations_allocation_usd')
            AS DOUBLE
        ) AS fiat_operations_allocation_usd,

        TRY_CAST(
            json_extract_scalar(item, '$.unused_series_1')
            AS DOUBLE
        ) AS unused_series_1,

        TRY_CAST(
            json_extract_scalar(item, '$.unused_series_2')
            AS DOUBLE
        ) AS unused_series_2

    FROM items
),

latest_fiat AS (
    SELECT
        f.month AS latest_fiat_source_month,
        DATE_ADD('month', -2, f.month) AS latest_fiat_service_month,
        f.fiat_operator_payout_usd AS latest_fiat_operator_payout_usd,
        f.fiat_gross_allocation_usd AS latest_fiat_gross_allocation_usd,
        100.0
        * f.fiat_gross_allocation_usd
        / NULLIF(s.wifi_revenue_projected_usd, 0)
            AS latest_fiat_routed_share_of_service_revenue_pct

    FROM monthly f
    LEFT JOIN monthly s
        ON s.month = DATE_ADD('month', -2, f.month)

    WHERE f.fiat_operator_payout_usd IS NOT NULL

    ORDER BY f.month DESC
    LIMIT 1
)

SELECT
    m.*,
    lf.latest_fiat_source_month,
    lf.latest_fiat_service_month,
    lf.latest_fiat_operator_payout_usd,
    lf.latest_fiat_gross_allocation_usd,
    lf.latest_fiat_routed_share_of_service_revenue_pct

FROM monthly m
LEFT JOIN latest_fiat lf
    ON TRUE

WHERE m.month <= CURRENT_DATE

ORDER BY m.month
