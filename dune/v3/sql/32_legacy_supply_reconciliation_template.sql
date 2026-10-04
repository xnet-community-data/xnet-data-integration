WITH balances AS (
    SELECT
        owner,
        xnet_balance
    FROM dune.xnet_community_data.result_xnet_v3_owner_balances_current
    WHERE xnet_balance > 0
),
legacy_registry AS (
    SELECT address
    FROM query___LEGACY_REGISTRY_QUERY_ID__
),
supply AS (
    SELECT
        SUM(xnet_balance) AS outstanding_supply_from_balances_xnet,
        COUNT(*) AS positive_balance_owner_count
    FROM balances
),
legacy AS (
    SELECT
        SUM(CASE WHEN r.address IS NOT NULL THEN b.xnet_balance ELSE CAST(0 AS DECIMAL(38,18)) END)
            AS legacy_list_current_balance_xnet,
        SUM(CASE WHEN r.address IS NOT NULL THEN 1 ELSE 0 END)
            AS legacy_list_positive_owner_count
    FROM balances b
    LEFT JOIN legacy_registry r
      ON b.owner = r.address
),
market AS (
    SELECT
        xnet_price_usd,
        provider_market_cap_usd,
        provider_fdv_usd
    FROM query___MARKET_SNAPSHOT_QUERY_ID__
    LIMIT 1
)
SELECT
    CAST(1307098713 AS DOUBLE) AS published_max_supply_xnet,

    CAST(s.outstanding_supply_from_balances_xnet AS DOUBLE)
        AS outstanding_supply_from_balances_xnet,

    s.positive_balance_owner_count,

    CAST(l.legacy_list_current_balance_xnet AS DOUBLE)
        AS legacy_list_current_balance_xnet,

    l.legacy_list_positive_owner_count,

    CAST(s.outstanding_supply_from_balances_xnet - l.legacy_list_current_balance_xnet AS DOUBLE)
        AS legacy_list_candidate_circulating_xnet,

    m.provider_market_cap_usd / NULLIF(m.xnet_price_usd, 0)
        AS provider_implied_circulating_xnet,

    m.provider_fdv_usd / NULLIF(m.xnet_price_usd, 0)
        AS provider_implied_max_supply_xnet,

    CAST(s.outstanding_supply_from_balances_xnet - l.legacy_list_current_balance_xnet AS DOUBLE)
      - (m.provider_market_cap_usd / NULLIF(m.xnet_price_usd, 0))
        AS candidate_minus_provider_circulating_xnet,

    100.0 * (
        CAST(s.outstanding_supply_from_balances_xnet - l.legacy_list_current_balance_xnet AS DOUBLE)
        / NULLIF(m.provider_market_cap_usd / NULLIF(m.xnet_price_usd, 0), 0)
        - 1.0
    ) AS candidate_vs_provider_pct_diff,

    m.xnet_price_usd,
    m.provider_market_cap_usd,
    m.provider_fdv_usd

FROM supply s
CROSS JOIN legacy l
CROSS JOIN market m
