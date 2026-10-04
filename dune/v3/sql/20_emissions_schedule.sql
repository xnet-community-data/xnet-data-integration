WITH months AS (
    SELECT month
    FROM UNNEST(
        SEQUENCE(
            DATE '2026-01-01',
            DATE '2029-12-01',
            INTERVAL '1' MONTH
        )
    ) AS t(month)
)
SELECT
    month,
    CASE
        WHEN month < DATE '2027-01-01' THEN 2500000.0
        WHEN month < DATE '2029-01-01' THEN 1250000.0
        ELSE 833334.0
    END AS scheduled_emissions_per_14d_epoch_xnet
FROM months
ORDER BY month
