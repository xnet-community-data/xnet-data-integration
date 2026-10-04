-- XNET V3 canonical DEX-trade hot layer.
--
-- One reusable source for:
--   * BBB XNET buys
--   * total XNET DEX volume
--   * buy/sell activity
--   * venue mix
--   * quote-asset mix
--   * trader activity
--   * future DEX analytics
--
-- Hot query scans only CURRENT_DATE.
-- A separate repair query will cover the rolling 3-day window.

SELECT
    block_date,
    block_time,
    block_slot,

    project,
    version,
    trade_source,

    token_bought_symbol,
    token_sold_symbol,

    token_bought_amount,
    token_sold_amount,

    token_bought_amount_raw,
    token_sold_amount_raw,

    amount_usd,

    token_bought_mint_address,
    token_sold_mint_address,

    token_bought_vault,
    token_sold_vault,

    project_program_id,
    project_main_id,

    trader_id,
    tx_id,

    outer_instruction_index,
    inner_instruction_index,
    tx_index,

    _updated_at,

    CASE
        WHEN token_bought_mint_address =
             'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
        THEN 'BUY_XNET'
        WHEN token_sold_mint_address =
             'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
        THEN 'SELL_XNET'
    END AS xnet_side,

    CASE
        WHEN token_bought_mint_address =
             'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
        THEN token_bought_amount
        ELSE token_sold_amount
    END AS xnet_amount,

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

WHERE block_date = CURRENT_DATE

  AND (
      token_bought_mint_address =
          'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'

      OR

      token_sold_mint_address =
          'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
  )

ORDER BY
    block_time,
    block_slot,
    tx_index,
    outer_instruction_index,
    inner_instruction_index
