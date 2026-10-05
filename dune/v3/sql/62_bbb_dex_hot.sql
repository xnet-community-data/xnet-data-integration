-- XNET V3 BBB-only hot DEX source.
--
-- Deliberately NOT an all-XNET DEX feed.
--
-- Scope:
--   * primary BBB wallet
--   * XNET-facing DEX legs
--   * bounded parameterized overlap (daily in production)
--
-- Multiple XNET-facing decoded route legs are reduced into a single
-- transaction-level economic record.
--
-- trade_value_usd uses MAX(amount_usd) rather than SUM(amount_usd) so routed
-- legs are not double-counted as independent economic volume.

WITH legs AS (
    SELECT
        block_date,
        block_time,
        block_slot,
        tx_id,
        trader_id,

        project,
        trade_source,
        project_main_id,

        amount_usd,

        CASE
            WHEN token_bought_mint_address =
                 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
            THEN CAST(token_bought_amount AS DOUBLE)

            WHEN token_sold_mint_address =
                 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
            THEN -CAST(token_sold_amount AS DOUBLE)

            ELSE 0
        END AS xnet_delta,

        CASE
            WHEN token_bought_mint_address =
                 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
            THEN CAST(token_bought_amount AS DOUBLE)

            ELSE CAST(token_sold_amount AS DOUBLE)
        END AS xnet_leg_amount,

        CASE
            WHEN token_bought_mint_address =
                 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
            THEN token_sold_symbol

            ELSE token_bought_symbol
        END AS quote_symbol,

        CASE
            WHEN token_bought_mint_address =
                 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
            THEN token_sold_mint_address

            ELSE token_bought_mint_address
        END AS quote_mint

    FROM dex_solana.trades

    WHERE block_date >= CURRENT_DATE - INTERVAL '2' DAY
      AND block_time >= CURRENT_TIMESTAMP - INTERVAL '{{lookback_hours}}' HOUR

      AND trader_id =
          '5QsyByFVJcg7oN76Ma26KEDFQdHt1tsiVExK94zURzfd'

      AND (
          token_bought_mint_address =
              'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'

          OR

          token_sold_mint_address =
              'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
      )
),

tx AS (
    SELECT
        MIN(block_date) AS block_date,
        MIN(block_time) AS block_time,
        MAX(block_slot) AS block_slot,

        tx_id,
        MAX(trader_id) AS trader_id,

        MAX_BY(project, xnet_leg_amount)
            AS primary_project,

        MAX_BY(trade_source, xnet_leg_amount)
            AS trade_source,

        MAX_BY(project_main_id, xnet_leg_amount)
            AS project_main_id,

        MAX_BY(quote_symbol, xnet_leg_amount)
            AS quote_symbol,

        MAX_BY(quote_mint, xnet_leg_amount)
            AS quote_mint,

        SUM(
            CASE WHEN xnet_delta > 0
                 THEN xnet_delta ELSE 0 END
        ) AS gross_xnet_bought,

        SUM(
            CASE WHEN xnet_delta < 0
                 THEN -xnet_delta ELSE 0 END
        ) AS gross_xnet_sold,

        SUM(xnet_delta)
            AS net_xnet_change,

        MAX(amount_usd)
            AS trade_value_usd,

        COUNT(*)
            AS decoded_xnet_leg_count

    FROM legs

    GROUP BY tx_id
)

SELECT
    block_date,
    block_time,
    block_slot,
    tx_id,
    trader_id,

    CASE
        WHEN net_xnet_change > 0 THEN 'BUY_XNET'
        WHEN net_xnet_change < 0 THEN 'SELL_XNET'
        ELSE 'FLAT_ROUTE'
    END AS economic_side,

    gross_xnet_bought,
    gross_xnet_sold,
    net_xnet_change,

    trade_value_usd,

    primary_project,
    trade_source,
    project_main_id,

    quote_symbol,
    quote_mint,

    decoded_xnet_leg_count

FROM tx

-- Ignore transactions where XNET was merely an effectively flat
-- intermediate routing asset.
WHERE ABS(net_xnet_change) > 0.00000001

ORDER BY block_time, tx_id
