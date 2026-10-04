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
)

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

    json_extract_scalar(
        c.j,
        '$.latest_transfer_event_utc'
    ) AS latest_transfer_event_utc,

    json_extract_scalar(
        c.j,
        '$.latest_bbb_trade_utc'
    ) AS latest_bbb_trade_utc

FROM chain c
CROSS JOIN price_pool pp
CROSS JOIN primary_pool p
CROSS JOIN market m
