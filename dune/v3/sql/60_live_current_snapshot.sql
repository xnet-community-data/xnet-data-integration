-- XNET V3 live current-state layer.
--
-- Live sources only:
--   1. verified XNET chain state from public canonical snapshot
--   2. DexScreener current market state
--
-- No blockchain scan occurs in this query.
-- Intended dashboard cadence: 15 minutes if benchmark remains cheap.

WITH chain AS (
    SELECT
        json_parse(
            http_get('https://raw.githubusercontent.com/xnet-community-data/xnet-data-integration/live-state/data/current/xnet_chain_snapshot.json')
        ) AS j
),

network AS (
    SELECT
        json_parse(
            http_get('https://raw.githubusercontent.com/xnet-community-data/xnet-data-integration/live-state/data/current/xnet_network_state.json')
        ) AS j
),

revenue AS (
    SELECT
        json_parse(
            http_get('https://raw.githubusercontent.com/xnet-community-data/xnet-data-integration/live-state/data/current/xnet_revenue_state.json')
        ) AS j
),

raw_pairs AS (
    SELECT pair
    FROM UNNEST(
        CAST(
            json_parse(
                http_get('https://api.dexscreener.com/token-pairs/v1/solana/xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L')
            )
            AS ARRAY(JSON)
        )
    ) AS t(pair)
),

pairs AS (
    SELECT
        json_extract_scalar(
            pair,
            '$.pairAddress'
        ) AS pair_address,

        json_extract_scalar(
            pair,
            '$.dexId'
        ) AS dex_id,

        json_extract_scalar(
            pair,
            '$.baseToken.address'
        ) AS base_mint,

        json_extract_scalar(
            pair,
            '$.baseToken.symbol'
        ) AS base_symbol,

        json_extract_scalar(
            pair,
            '$.quoteToken.address'
        ) AS quote_mint,

        json_extract_scalar(
            pair,
            '$.quoteToken.symbol'
        ) AS quote_symbol,

        TRY_CAST(
            json_extract_scalar(
                pair,
                '$.priceUsd'
            )
            AS DOUBLE
        ) AS price_usd,

        TRY_CAST(
            json_extract_scalar(
                pair,
                '$.liquidity.usd'
            )
            AS DOUBLE
        ) AS liquidity_usd,

        TRY_CAST(
            json_extract_scalar(
                pair,
                '$.volume.h24'
            )
            AS DOUBLE
        ) AS volume_h24_usd,

        TRY_CAST(
            json_extract_scalar(
                pair,
                '$.txns.h24.buys'
            )
            AS BIGINT
        ) AS buys_h24,

        TRY_CAST(
            json_extract_scalar(
                pair,
                '$.txns.h24.sells'
            )
            AS BIGINT
        ) AS sells_h24,

        TRY_CAST(
            json_extract_scalar(
                pair,
                '$.priceChange.h1'
            )
            AS DOUBLE
        ) AS price_change_h1_pct,

        TRY_CAST(
            json_extract_scalar(
                pair,
                '$.priceChange.h6'
            )
            AS DOUBLE
        ) AS price_change_h6_pct,

        TRY_CAST(
            json_extract_scalar(
                pair,
                '$.priceChange.h24'
            )
            AS DOUBLE
        ) AS price_change_h24_pct

    FROM raw_pairs

    WHERE LOWER(
        json_extract_scalar(
            pair,
            '$.chainId'
        )
    ) = 'solana'

      AND (
          json_extract_scalar(
              pair,
              '$.baseToken.address'
          ) = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'

          OR

          json_extract_scalar(
              pair,
              '$.quoteToken.address'
          ) = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
      )
),

price_pool AS (
    SELECT *
    FROM pairs
    WHERE base_mint = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
      AND price_usd > 0
    ORDER BY liquidity_usd DESC NULLS LAST
    LIMIT 1
),

primary_pool AS (
    SELECT *
    FROM pairs
    ORDER BY liquidity_usd DESC NULLS LAST
    LIMIT 1
),

market AS (
    SELECT
        SUM(
            COALESCE(liquidity_usd, 0)
        ) AS total_dex_liquidity_usd,

        SUM(
            COALESCE(volume_h24_usd, 0)
        ) AS total_dex_volume_h24_usd,

        SUM(
            COALESCE(buys_h24, 0)
        ) AS dex_pool_buys_h24,

        SUM(
            COALESCE(sells_h24, 0)
        ) AS dex_pool_sells_h24,

        COUNT(*) AS liquidity_pool_count

    FROM pairs
),

current_state AS (
SELECT
    CURRENT_TIMESTAMP
        AS observed_at_utc,

    json_extract_scalar(
        c.j,
        '$.generated_at_utc'
    ) AS chain_snapshot_generated_at_utc,

    pp.price_usd
        AS xnet_price_usd,

    pp.price_usd
        * TRY_CAST(
            json_extract_scalar(
                c.j,
                '$.circulating_supply_xnet'
            )
            AS DOUBLE
        )
        AS market_cap_usd,

    pp.price_usd
        * TRY_CAST(
            json_extract_scalar(
                c.j,
                '$.published_max_supply_xnet'
            )
            AS DOUBLE
        )
        AS fdv_usd,

    m.total_dex_liquidity_usd,
    m.total_dex_volume_h24_usd,

    m.dex_pool_buys_h24,
    m.dex_pool_sells_h24,

    m.liquidity_pool_count,

    p.pair_address
        AS primary_pool_address,

    p.dex_id
        AS primary_pool_dex,

    CASE
        WHEN p.base_mint = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
        THEN p.quote_symbol
        ELSE p.base_symbol
    END AS primary_pool_quote_symbol,

    p.liquidity_usd
        AS primary_pool_liquidity_usd,

    p.liquidity_usd
        / NULLIF(
            m.total_dex_liquidity_usd,
            0
        )
        AS primary_pool_liquidity_share,

    pp.price_change_h1_pct,
    pp.price_change_h6_pct,
    pp.price_change_h24_pct,

    TRY_CAST(
        json_extract_scalar(
            c.j,
            '$.circulating_supply_xnet'
        )
        AS DOUBLE
    ) AS circulating_supply_xnet,

    TRY_CAST(
        json_extract_scalar(
            c.j,
            '$.published_max_supply_xnet'
        )
        AS DOUBLE
    ) AS published_max_supply_xnet,

    TRY_CAST(
        json_extract_scalar(
            c.j,
            '$.holder_count_positive'
        )
        AS BIGINT
    ) AS holder_count_positive,

    TRY_CAST(
        json_extract_scalar(
            c.j,
            '$.holders_ge_100_xnet'
        )
        AS BIGINT
    ) AS holders_ge_100_xnet,

    TRY_CAST(
        json_extract_scalar(
            c.j,
            '$.holders_ge_1000_xnet'
        )
        AS BIGINT
    ) AS holders_ge_1000_xnet,

    TRY_CAST(
        json_extract_scalar(
            c.j,
            '$.verified_bbb_burned_xnet'
        )
        AS DOUBLE
    ) AS verified_bbb_burned_xnet,

    TRY_CAST(
        json_extract_scalar(
            c.j,
            '$.bbb_wallet_xnet_balance'
        )
        AS DOUBLE
    ) AS bbb_wallet_xnet_balance,

    TRY_CAST(
        json_extract_scalar(
            c.j,
            '$.bbb_wallet_usdc_balance'
        )
        AS DOUBLE
    ) AS bbb_wallet_usdc_balance,

    TRY_CAST(
        json_extract_scalar(
            c.j,
            '$.bbb_execution_policy.direct_bbb_share_of_received_revenue'
        )
        AS DOUBLE
    ) AS bbb_policy_direct_share,

    TRY_CAST(
        json_extract_scalar(
            c.j,
            '$.bbb_execution_policy.liquidity_share_of_received_revenue'
        )
        AS DOUBLE
    ) AS bbb_policy_liquidity_share,

    TRY_CAST(
        json_extract_scalar(
            c.j,
            '$.bbb_execution_policy.liquidity_xnet_market_buy_fraction'
        )
        AS DOUBLE
    ) AS bbb_policy_liquidity_xnet_market_buy_fraction,

    TRY_CAST(
        json_extract_scalar(
            c.j,
            '$.bbb_execution_policy.effective_xnet_market_buy_share'
        )
        AS DOUBLE
    ) AS bbb_policy_effective_xnet_market_buy_share,

    TRY_CAST(
        json_extract_scalar(
            c.j,
            '$.bbb_execution_policy.execution_days'
        )
        AS DOUBLE
    ) AS bbb_policy_execution_days,


    TRY_CAST(
        json_extract_scalar(
            c.j,
            '$.bbb_recent_gross_xnet_bought'
        )
        AS DOUBLE
    ) AS bbb_recent_gross_xnet_bought,

    TRY_CAST(
        json_extract_scalar(
            c.j,
            '$.bbb_recent_trade_value_usd'
        )
        AS DOUBLE
    ) AS bbb_recent_trade_value_usd,

    -- NETWORK

    json_extract_scalar(
        n.j,
        '$.status'
    ) AS network_status,

    json_extract_scalar(
        n.j,
        '$.offload.status'
    ) AS offload_status,

    json_extract_scalar(
        n.j,
        '$.devices.status'
    ) AS device_status,


    json_extract_scalar(
        n.j,
        '$.offload.data_as_of'
    ) AS offload_data_as_of,

    TRY_CAST(
        json_extract_scalar(
            n.j,
            '$.offload.latest_daily_offload_gb'
        )
        AS DOUBLE
    ) AS latest_daily_offload_gb,

    TRY_CAST(
        json_extract_scalar(
            n.j,
            '$.offload.avg_daily_offload_gb_30d'
        )
        AS DOUBLE
    ) AS avg_daily_offload_gb_30d,

    TRY_CAST(
        json_extract_scalar(
            n.j,
            '$.offload.all_time_network_offload_gb'
        )
        AS DOUBLE
    ) AS all_time_network_offload_gb,

    TRY_CAST(
        json_extract_scalar(
            n.j,
            '$.offload.latest_complete_month_offload_gb'
        )
        AS DOUBLE
    ) AS latest_complete_month_offload_gb,

    TRY_CAST(
        json_extract_scalar(
            n.j,
            '$.offload.latest_month_mom_growth_pct'
        )
        AS DOUBLE
    ) AS latest_month_offload_growth_pct,

    json_extract_scalar(
        n.j,
        '$.devices.data_as_of'
    ) AS device_data_as_of,

    TRY_CAST(
        json_extract_scalar(
            n.j,
            '$.devices.total_devices'
        )
        AS BIGINT
    ) AS total_devices,

    TRY_CAST(
        json_extract_scalar(
            n.j,
            '$.devices.operational_devices'
        )
        AS BIGINT
    ) AS operational_devices,

    TRY_CAST(
        json_extract_scalar(
            n.j,
            '$.devices.operational_ratio_pct'
        )
        AS DOUBLE
    ) AS operational_device_ratio_pct,

    TRY_CAST(
        json_extract_scalar(
            n.j,
            '$.devices.device_growth_30d_pct'
        )
        AS DOUBLE
    ) AS device_growth_30d_pct,

    TRY_CAST(
        json_extract_scalar(
            n.j,
            '$.productivity.latest_offload_gb_per_operational_device'
        )
        AS DOUBLE
    ) AS latest_offload_gb_per_operational_device,

    -- REVENUE

    json_extract_scalar(
        r.j,
        '$.source.source_latest_month'
    ) AS revenue_source_latest_month,

    json_extract_scalar(
        r.j,
        '$.generated_at_utc'
    ) AS revenue_generated_at_utc,


    json_extract_scalar(
        r.j,
        '$.latest_service.month'
    ) AS revenue_service_month,

    TRY_CAST(
        json_extract_scalar(
            r.j,
            '$.latest_service.gb'
        )
        AS DOUBLE
    ) AS latest_revenue_sheet_gb,

    TRY_CAST(
        json_extract_scalar(
            r.j,
            '$.latest_service.projected_revenue_usd'
        )
        AS DOUBLE
    ) AS latest_projected_wifi_revenue_usd,

    TRY_CAST(
        json_extract_scalar(
            r.j,
            '$.latest_service.annualized_revenue_run_rate_usd'
        )
        AS DOUBLE
    ) AS annualized_revenue_run_rate_usd,

    TRY_CAST(
        json_extract_scalar(
            r.j,
            '$.latest_service.derived_revenue_per_gb_usd'
        )
        AS DOUBLE
    ) AS latest_revenue_per_gb_usd,

    json_extract_scalar(
        r.j,
        '$.latest_payment.payment_date'
    ) AS latest_wifi_payment_date,

    TRY_CAST(
        json_extract_scalar(
            r.j,
            '$.latest_payment.amount_usd'
        )
        AS DOUBLE
    ) AS latest_wifi_payment_received_usd,

    TRY_CAST(
        json_extract_scalar(
            r.j,
            '$.outstanding.balance_outstanding_to_transfer_usd'
        )
        AS DOUBLE
    ) AS balance_outstanding_to_transfer_usd,

    TRY_CAST(
        json_extract_scalar(
            r.j,
            '$.cumulative_source_sheet.projected_wifi_revenue_usd'
        )
        AS DOUBLE
    ) AS cumulative_projected_wifi_revenue_usd,

    TRY_CAST(
        json_extract_scalar(
            r.j,
            '$.cumulative_source_sheet.wifi_payments_received_usd'
        )
        AS DOUBLE
    ) AS cumulative_wifi_payments_received_usd,

    TRY_CAST(
        json_extract_scalar(
            r.j,
            '$.cumulative_source_sheet.projected_buy_burn_usd'
        )
        AS DOUBLE
    ) AS cumulative_projected_buy_burn_usd,

    TRY_CAST(
        json_extract_scalar(
            r.j,
            '$.cumulative_source_sheet.transferred_to_buy_burn_usd'
        )
        AS DOUBLE
    ) AS cumulative_bbb_transfers_usd,

    TRY_CAST(
        json_extract_scalar(
            r.j,
            '$.settled_accounting.recognized_service_revenue_usd'
        )
        AS DOUBLE
    ) AS recognized_service_revenue_usd,

    TRY_CAST(
        json_extract_scalar(
            r.j,
            '$.settled_accounting.unattributed_payments_usd'
        )
        AS DOUBLE
    ) AS unattributed_payments_usd,

    -- VALUATION

    (
        pp.price_usd
        * TRY_CAST(
            json_extract_scalar(
                c.j,
                '$.circulating_supply_xnet'
            )
            AS DOUBLE
        )
    )
    /
    NULLIF(
        TRY_CAST(
            json_extract_scalar(
                r.j,
                '$.latest_service.annualized_revenue_run_rate_usd'
            )
            AS DOUBLE
        ),
        0
    )
        AS market_cap_to_revenue_run_rate,

    (
        pp.price_usd
        * TRY_CAST(
            json_extract_scalar(
                c.j,
                '$.published_max_supply_xnet'
            )
            AS DOUBLE
        )
    )
    /
    NULLIF(
        TRY_CAST(
            json_extract_scalar(
                r.j,
                '$.latest_service.annualized_revenue_run_rate_usd'
            )
            AS DOUBLE
        ),
        0
    )
        AS fdv_to_revenue_run_rate,

    100.0
    * TRY_CAST(
        json_extract_scalar(
            c.j,
            '$.circulating_supply_xnet'
        )
        AS DOUBLE
    )
    /
    NULLIF(
        TRY_CAST(
            json_extract_scalar(
                c.j,
                '$.published_max_supply_xnet'
            )
            AS DOUBLE
        ),
        0
    )
        AS circulating_supply_pct_max,

    -- CHAIN FRESHNESS

    json_extract_scalar(
        c.j,
        '$.latest_transfer_event_utc'
    ) AS latest_transfer_event_utc,

    json_extract_scalar(
        c.j,
        '$.latest_bbb_trade_utc'
    ) AS latest_bbb_trade_utc

FROM chain c
CROSS JOIN network n
CROSS JOIN revenue r
CROSS JOIN price_pool pp
CROSS JOIN primary_pool p
CROSS JOIN market m
)


SELECT
    cs.*,
    CONCAT(SUBSTR(CAST(cs.observed_at_utc AS VARCHAR), 1, 19), ' UTC') AS dashboard_updated_utc,
    json_extract_scalar(c_clock.j, '$.bbb_wallet_usdc_observed_at_utc') AS bbb_wallet_usdc_observed_at_utc,
    (
        cs.latest_wifi_payment_received_usd
        * cs.bbb_policy_effective_xnet_market_buy_share
        / NULLIF(
            cs.bbb_policy_execution_days,
            0
        )
    ) AS bbb_daily_rate_usd,


    f.freshness_source,

    CASE
        f.freshness_key

        WHEN 'market' THEN
            CONCAT(
                'Last retrieved · ',
                SUBSTR(
                    CAST(
                        cs.observed_at_utc
                        AS VARCHAR
                    ),
                    1,
                    16
                ),
                ' UTC'
            )

        WHEN 'chain' THEN
            CONCAT(
                'Last retrieved · ',
                REPLACE(
                    SUBSTR(
                        cs.chain_snapshot_generated_at_utc,
                        1,
                        16
                    ),
                    'T',
                    ' '
                ),
                ' UTC'
            )

        WHEN 'offload' THEN
            CONCAT(
                CASE
                    WHEN COALESCE(
                        cs.offload_status,
                        cs.network_status
                    ) = 'stale_fallback'
                    THEN 'Last retrieved'
                    ELSE 'Last retrieved'
                END,
                ' · ',
                cs.offload_data_as_of
            )

        WHEN 'devices' THEN
            CONCAT(
                CASE
                    WHEN COALESCE(
                        cs.device_status,
                        cs.network_status
                    ) = 'stale_fallback'
                    THEN 'Last retrieved'
                    ELSE 'Last retrieved'
                END,
                ' · ',
                cs.device_data_as_of
            )

        WHEN 'revenue' THEN
            CONCAT(
                'Last retrieved · ',
                REPLACE(
                    SUBSTR(
                        cs.revenue_generated_at_utc,
                        1,
                        16
                    ),
                    'T',
                    ' '
                ),
                ' UTC'
            )


    END
        AS freshness,

    f.freshness_covers

FROM current_state cs
CROSS JOIN chain c_clock

CROSS JOIN (
    VALUES

    (
        1,
        'DexScreener + Dune Solana',
        'market',
        'XNET Price · Market Cap · FDV · 24h DEX Volume · DEX Liquidity'
    ),

    (
        2,
        'Dune Solana',
        'chain',
        'Circulating Supply · XNET Holders · XNET Burned · Buy & Burn Wallet Balance'
    ),

    (
        3,
        'XNET Offload API',
        'offload',
        'Latest Daily Offload · 30-Day Avg Daily Offload · All-Time Network Offload'
    ),

    (
        4,
        'XNET Devices API',
        'devices',
        'Total Devices · Operational Devices · 30-Day Device Growth'
    ),

    (
        5,
        'XNET Revenue Sheet',
        'revenue',
        'Annualized Revenue Run Rate · P/S Ratio · WiFi Revenue (Projected) · WiFi Payment (Received) · Balance Outstanding to Transfer · BBB Daily Rate · Transferred to Buy & Burn'
    )

) AS f(
    freshness_order,
    freshness_source,
    freshness_key,
    freshness_covers
)

ORDER BY
    f.freshness_order
