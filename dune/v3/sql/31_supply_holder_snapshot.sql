WITH balances AS (
    SELECT *
    FROM dune.xnet_community_data.result_xnet_v3_owner_balances_current
    WHERE xnet_balance > 0
),
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (ORDER BY xnet_balance DESC, owner) AS balance_rank,
        SUM(xnet_balance) OVER () AS total_positive_balance_xnet
    FROM balances
)
SELECT
    MAX(latest_source_update) AS latest_balance_source_update,
    MAX(latest_balance_event_time) AS latest_balance_event_time,

    COUNT(*) AS positive_balance_owner_count,
    SUM(CASE WHEN xnet_balance >= 1 THEN 1 ELSE 0 END) AS holders_ge_1_xnet,
    SUM(CASE WHEN xnet_balance >= 100 THEN 1 ELSE 0 END) AS holders_ge_100_xnet,
    SUM(CASE WHEN xnet_balance >= 1000 THEN 1 ELSE 0 END) AS holders_ge_1000_xnet,

    SUM(xnet_balance) AS outstanding_supply_from_balances_xnet,

    APPROX_PERCENTILE(CAST(xnet_balance AS DOUBLE), 0.5) AS median_positive_holder_balance_xnet,

    100.0 * SUM(CASE WHEN balance_rank <= 10 THEN CAST(xnet_balance AS DOUBLE) ELSE 0 END)
        / NULLIF(MAX(CAST(total_positive_balance_xnet AS DOUBLE)), 0)
        AS top_10_holder_share_pct,

    100.0 * SUM(CASE WHEN balance_rank <= 50 THEN CAST(xnet_balance AS DOUBLE) ELSE 0 END)
        / NULLIF(MAX(CAST(total_positive_balance_xnet AS DOUBLE)), 0)
        AS top_50_holder_share_pct,

    100.0 * SUM(CASE WHEN balance_rank <= 100 THEN CAST(xnet_balance AS DOUBLE) ELSE 0 END)
        / NULLIF(MAX(CAST(total_positive_balance_xnet AS DOUBLE)), 0)
        AS top_100_holder_share_pct,

    SUM(CASE WHEN has_missing_owner_component = 1 THEN 1 ELSE 0 END)
        AS resolved_owner_rows_with_missing_owner_metadata,

    SUM(CASE WHEN has_missing_owner_component = 1 THEN xnet_balance ELSE CAST(0 AS DECIMAL(38,18)) END)
        AS balance_with_missing_owner_metadata_xnet

FROM ranked
