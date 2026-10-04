WITH raw AS (
    SELECT json_parse(
        http_get(
            'https://api.dexscreener.com/token-pairs/v1/solana/xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
        )
    ) AS response
),
pairs AS (
    SELECT item
    FROM raw
    CROSS JOIN UNNEST(CAST(response AS ARRAY(JSON))) AS t(item)
),
parsed AS (
    SELECT
        CURRENT_TIMESTAMP AS observed_at_utc,

        json_extract_scalar(item, '$.dexId') AS dex_id,
        json_extract_scalar(item, '$.pairAddress') AS pair_address,

        json_extract_scalar(item, '$.baseToken.address') AS base_address,
        json_extract_scalar(item, '$.baseToken.symbol') AS base_symbol,
        json_extract_scalar(item, '$.quoteToken.address') AS quote_address,
        json_extract_scalar(item, '$.quoteToken.symbol') AS quote_symbol,

        TRY_CAST(json_extract_scalar(item, '$.priceUsd') AS DOUBLE) AS base_price_usd,
        TRY_CAST(json_extract_scalar(item, '$.priceNative') AS DOUBLE) AS base_price_in_quote,

        TRY_CAST(json_extract_scalar(item, '$.liquidity.usd') AS DOUBLE) AS liquidity_usd,
        TRY_CAST(json_extract_scalar(item, '$.marketCap') AS DOUBLE) AS provider_market_cap_usd,
        TRY_CAST(json_extract_scalar(item, '$.fdv') AS DOUBLE) AS provider_fdv_usd,

        TRY_CAST(json_extract_scalar(item, '$.volume.h24') AS DOUBLE) AS volume_24h_usd,

        TRY_CAST(json_extract_scalar(item, '$.priceChange.h1') AS DOUBLE) AS base_price_change_1h_pct,
        TRY_CAST(json_extract_scalar(item, '$.priceChange.h6') AS DOUBLE) AS base_price_change_6h_pct,
        TRY_CAST(json_extract_scalar(item, '$.priceChange.h24') AS DOUBLE) AS base_price_change_24h_pct,

        TRY_CAST(json_extract_scalar(item, '$.txns.h24.buys') AS BIGINT) AS base_buys_24h,
        TRY_CAST(json_extract_scalar(item, '$.txns.h24.sells') AS BIGINT) AS base_sells_24h

    FROM pairs
)
SELECT
    observed_at_utc,
    dex_id,
    pair_address,
    base_address,
    base_symbol,
    quote_address,
    quote_symbol,

    CASE
        WHEN base_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
        THEN base_price_usd
        WHEN quote_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
             AND base_price_in_quote > 0
        THEN base_price_usd / base_price_in_quote
    END AS xnet_price_usd,

    liquidity_usd,
    provider_market_cap_usd,
    provider_fdv_usd,
    volume_24h_usd,

    CASE
        WHEN base_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
        THEN base_price_change_1h_pct
    END AS xnet_price_change_1h_pct,

    CASE
        WHEN base_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
        THEN base_price_change_6h_pct
    END AS xnet_price_change_6h_pct,

    CASE
        WHEN base_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
        THEN base_price_change_24h_pct
    END AS xnet_price_change_24h_pct,

    CASE
        WHEN base_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
        THEN base_buys_24h
        WHEN quote_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
        THEN base_sells_24h
    END AS xnet_buys_24h,

    CASE
        WHEN base_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
        THEN base_sells_24h
        WHEN quote_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
        THEN base_buys_24h
    END AS xnet_sells_24h

FROM parsed
WHERE
    base_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
    OR quote_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
