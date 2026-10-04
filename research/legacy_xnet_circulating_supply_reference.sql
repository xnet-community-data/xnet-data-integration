WITH account_list AS (
    SELECT account
    FROM UNNEST(
       ARRAY[
    'A5GbZKLwUM91CkKjKvan85No4VDQqMcV68yG9nLzqGXk', --- Insider lock or Ops pool
    'DDfCTd8HW29AmLHwqWfLREtG46iJsRrxceTHc77Xgw4H', --- Insider lock or Ops pool
    'FpwyBEgucz93BGNMoBqwFY9FEnLtu5av6qr1UxmEUR9R', --- Insider lock or Ops pool
    'DagafaFnDoackVFnqWfqTYDD4PKjT5hnzRZNaQg6LdLp', --- Insider lock or Ops pool
    '6k2SHJaMWy6TwLAQaAKnnGFUYp3gNgipxE33dbkCaf2Z', --- Insider lock or Ops pool
    'BXkmF1cAJqMwXWeTh4igLjD3XQ7nMw3fbtPRY8RR1DgQ', --- Insider lock or Ops pool
    '7Ko46nx9r9cPg9nitKjcX4BCyNqyRAfdaU7VUhq6eY8T', --- Insider lock or Ops pool
    '8WeWN41odRZt5Qe5VB9rYGt4yeH6FjfeKSsLS5vopdXo', --- Insider lock or Ops pool
    'Ce4eRUYL8xtofWMwrbqxZj1tx7qs8iuprV22iynpyfSo', --- Insider lock or Ops pool
    '8hiVWeu3i59xMNy1Jp72o4x1CHzFjKKbc78mhaxsF6wx', --- Insider lock or Ops pool
    'AMDupZnnuoZHoS1kHbipyTqqceHuh82Rp1qEwJpHEZPh', --- Insider lock or Ops pool
    '3aBbxLN19LNNFEfHYZYwJrQtV3vbWCyY9QMf1nuRdwo1', --- Insider lock or Ops pool
    'C2w7jQ8nbNVogepgjxwKBem9JYNWR3ocPc3eJsgpwPMC', --- Insider lock or Ops pool
    'Hwk2gsNVxEAGj7kz5Ex7Tu9wAWsctFoTe7KEbzrxBQj1', --- Insider lock or Ops pool
    '3bw4c64JkTPfZ9jcMXnAzbXXweb9by7TAQaWTKj6Yi4h', --- Insider lock or Ops pool
    'DF9rLSSMpJHrHeLz6twko9Ymv8fsKe2emq9dSEQp4HhZ', --- Insider lock or Ops pool
    'DeXjsmBPJiRWkrjoZm15Gjjea4LtDoXdVritNvVicUmg', --- Insider lock or Ops pool
    'As4nhNHHrhNZMoReGZvEbDTiooNmNATjyjjbadArEJVb', --- Insider lock or Ops pool
    '2pACwttxpQEk1gs62rZLn78Bb9iAjPvKptBYqwKVk7Ge', --- Insider lock or Ops pool
    '3W6AdCnjAKxA2GjrAJ48bt3n3wr9pG7F8jasrcciGAkf', --- Insider lock or Ops pool
    'DVVWXkWLio5MRGeopBMhGayEDJkPecz8T2Q8LxzEsEos', --- Insider lock or Ops pool
    '4EyVbx6MYCBk79LYQFSw1PZ74RFpvJ3TYP3ME7DgSuxB', --- Insider lock or Ops pool
    'GwzXxiNKZGZCqHQK1tzPP8ANyCS4YkYa5F6P9upPqrsb', --- Insider lock or Ops pool
    'Zg2CKtyca14FmBk4LPkkZEVnsYE4LNeyhQQsFE3hcNN', --- Insider lock or Ops pool
    'HhQAW4wtZYfbk7WmETgDMB4GtGwRBXtfiShngdjUpq1t', --- Insider lock or Ops pool
    'HYVem62h69nbyCJHdzwZsv7bKLL15D8egLeRcCkmfS2c', --- Insider lock or Ops pool
    'B3ePZXj66duAn2Zhgy2MXeUNacbMfHuZ1DukM49wK4oz', --- Insider lock or Ops pool
    '6rxSLPkpEQXDvyXeoRDVxyYrQvQpptPbZDox9qhJ1YgF', --- Insider lock or Ops pool
    '3mVqotZhkNrfi23NnVDrgLFHgvG6qQUUQwYUJbix5Vyp', --- Insider lock or Ops pool
    '4thbQayu3hLUZYrUHucup1MrqhEcxCj1aSKYFBEzLq9V', --- Insider lock or Ops pool
    '7eMLQvC6qkvqP7izpUdB3nJHzkeTu66UWLkRyTrAuBkS', --- Insider lock or Ops pool
    'EYNurCP6Mhp9nvdggCMHkYmhjwudUBcMfcz4qUXWgCS6', --- Insider lock or Ops pool
    '28uWRJsdBt3sM9GNZ7j39WHr8oXEZHFJtnN5m3pSchff', --- Temporary usage 
    'ATmkAJuCP117WTRLXZ9L5SBrMJpkcQoSGDgNf7rhGMaV', --- Temporary usage 
    '3duyMwKk6vpGQ9WjTfrwLacgA4KfaV6yM3YxXQQJ7ogV', --- Temporary usage 
    '5DNgfKSc4PJ9JmMneM6AdctVPkdi8cc3xgry8ksRtt95', --- Temporary usage 
    'DZgtrNzKLCKvU4ekf67kzohv3EW1hwdmDwPgiwXaGCZr', --- Temporary usage 
    'DY2rLdpbzDjDqUVHEvHQvPTShhM6xbJh4b5Po4DxXRV4', --- Temporary usage 
    'CV3nbqgvQm8NbQsudmKmr84zszJegLkrJkzrJY4X8znF', --- Temporary usage 
    '5dcpUq4r4TG3drg3JQSskGD1xGUepDguiJoMjj83G6hf', --- Temporary usage 
    '6PRskNzL8755ktdp25itkW8af6f1B3pGsQqjdMjjEZr1', --- Temporary usage 
    'eFttF7Ahk6haDqEsDgMQggKweDGxJd6u4CUQg1HkzDH', --- Temporary usage 
    '8i6bq2fYxDybk3foeaBXGen8s1pJFx5Vs4PDx38J6ho8', --- Temporary usage 
    'iwkwPri1U3PocwvxLBGLBrKCLvwSxLHch5aHbkaNuM3', --- Temporary usage 
    '9CkUYinXiHv1Juwgt4gzQjttvrxRzUeXEw4gurrs19Yu', --- Temporary usage 
    'DkBvptEyJaV4JFkd645rW7euVZobKWfNC9LGWziueYiR', --- Temporary usage 
    'EgUJL13AD9zkq8BeJ4f6t1BxLmeFdZEEb2Kvwz4K2odf', --- Temporary usage 
    '5PAjGzHJhdmBguP6DHAYpPLxCv1TydX9a1RZApMjM5n3', --- Temporary usage 
    '425VB7Phq4EApekN5qkrZWefeZwGwThThXhXT8SPbvtU', --- Temp Dev accounts
    '6kd4Z1rkto4y1PGQqfwNhxbEe7rcyP8TRsZ2PCiDR5r2', --- Temp Dev accounts
    'Exbfb9A4grsUNeuwf1aKeWDERQKmkTHjmUtfd5vLR5bq', --- Temp Dev accounts
    '7A6PQV93jCGfscAuRu69CuKyf5HPMQa9xqRBMrBT6Dvv', --- Temp Dev accounts
    '2oiMzQwp8JW1vLzuqHePgaLdoG1ypnn37nMjCSEy4tvK', --- Insider lock or Ops pool
    '9Su1HC6LNg8FVZH346xipVbEYUVvXr62nJX8Kbmk45CW', --- Insider lock or Ops pool
    '13UGBJmRx5eC2aRS1PVvdvQbA8XtnKrKNZp6XBinuakZ', --- Insider lock or Ops pool
    '4HAjZX8JA662tY5ptVTrXHu3nfz5oPLi1mkjhbh8VjFP', --- Insider lock or Ops pool
    'BuxUbBh4D7KBsNXRo283nihaQV8wZ6QDtudwrfyTAWBP', --- Insider lock or Ops pool
    'E72wWDCKCiS32SgEFocVs3EHEJTJkJRm58RRvpnLQcLz', --- Insider lock or Ops pool
    'BGMgUvHk6Yu1ouqjP2LM6Lwj3LEqdxqYaiu4p2QcN2CD', --- Burn acct.
    '6UE1gdvgFPbu8REp5YWKRkC5CZiXgwd7iEfTAqwbzUqV',  --- Burn acct.
    'FUAExZHqG6C1HvXQABfy7vv2mR3CKcU4WPHEpQhUKFgb'  --- Insider lock or Ops pool
    
]
    ) AS t(account)
),
-- Step 1: Calculate daily token flow for specified accounts
transfers AS (
    SELECT
        DATE_TRUNC('day', block_time) AS day,
        to_owner AS account,
        SUM(amount) / 1e8 AS flow
    FROM
        tokens_solana.transfers
    WHERE
        token_mint_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
        AND to_owner IN (SELECT account FROM account_list)
    GROUP BY
        DATE_TRUNC('day', block_time), to_owner
    UNION ALL
    SELECT
        DATE_TRUNC('day', block_time) AS day,
        from_owner AS account,
        -SUM(amount) / 1e8 AS flow
    FROM
        tokens_solana.transfers
    WHERE
        token_mint_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
        AND from_owner IN (SELECT account FROM account_list)
    GROUP BY
        DATE_TRUNC('day', block_time), from_owner
),
-- Step 2: Calculate daily and cumulative balances
daily_balance AS (
    SELECT
        day,
        SUM(flow) AS daily_flow
    FROM
        transfers
    GROUP BY
        day
),
cumulative_balance AS (
    SELECT
        day,
        2400000000 - SUM(daily_flow) OVER (ORDER BY day) AS gross_token
    FROM
        daily_balance
),
-- Step 3: Calculate daily burns and ensure all dates are represented
all_dates AS (
    SELECT DISTINCT day FROM cumulative_balance
    UNION
    SELECT DISTINCT day FROM (
        SELECT
            DATE_TRUNC('day', tr.block_time) AS day
        FROM tokens_solana.transfers AS tr
        WHERE
            tr.token_mint_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
            AND tr.action = 'burn'
    )
),
daily_burn AS (
    SELECT
        ad.day,
        COALESCE(SUM(tr.amount / POWER(10, tks.decimals)), 0) AS daily_burn
    FROM all_dates ad
    LEFT JOIN tokens_solana.transfers AS tr
        ON DATE_TRUNC('day', tr.block_time) = ad.day
        AND tr.token_mint_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
        AND tr.action = 'burn'
    LEFT JOIN tokens_solana.fungible AS tks
        ON tks.token_mint_address = tr.token_mint_address
    GROUP BY
        ad.day
),
cumulative_burn AS (
    SELECT
        day,
        SUM(daily_burn) OVER (ORDER BY day) AS burn
    FROM daily_burn
)
-- Step 4: Merge balances and burns
SELECT
    cb.day AS date,
    cb.gross_token AS gross_token,
    COALESCE(cum_burn.burn, 0) AS burn,
    cb.gross_token - COALESCE(cum_burn.burn, 0) AS balance
FROM cumulative_balance cb
LEFT JOIN cumulative_burn cum_burn
    ON cb.day = cum_burn.day
ORDER BY
    cb.day desc


-- WITH account_list AS (
--     SELECT account
--     FROM UNNEST(
--    ARRAY[
--     'A5GbZKLwUM91CkKjKvan85No4VDQqMcV68yG9nLzqGXk', --- Insider lock or Ops pool
--     'DDfCTd8HW29AmLHwqWfLREtG46iJsRrxceTHc77Xgw4H', --- Insider lock or Ops pool
--     'FpwyBEgucz93BGNMoBqwFY9FEnLtu5av6qr1UxmEUR9R', --- Insider lock or Ops pool
--     'DagafaFnDoackVFnqWfqTYDD4PKjT5hnzRZNaQg6LdLp', --- Insider lock or Ops pool
--     '6k2SHJaMWy6TwLAQaAKnnGFUYp3gNgipxE33dbkCaf2Z', --- Insider lock or Ops pool
--     'BXkmF1cAJqMwXWeTh4igLjD3XQ7nMw3fbtPRY8RR1DgQ', --- Insider lock or Ops pool
--     '7Ko46nx9r9cPg9nitKjcX4BCyNqyRAfdaU7VUhq6eY8T', --- Insider lock or Ops pool
--     '8WeWN41odRZt5Qe5VB9rYGt4yeH6FjfeKSsLS5vopdXo', --- Insider lock or Ops pool
--     'Ce4eRUYL8xtofWMwrbqxZj1tx7qs8iuprV22iynpyfSo', --- Insider lock or Ops pool
--     '8hiVWeu3i59xMNy1Jp72o4x1CHzFjKKbc78mhaxsF6wx', --- Insider lock or Ops pool
--     'AMDupZnnuoZHoS1kHbipyTqqceHuh82Rp1qEwJpHEZPh', --- Insider lock or Ops pool
--     '3aBbxLN19LNNFEfHYZYwJrQtV3vbWCyY9QMf1nuRdwo1', --- Insider lock or Ops pool
--     'C2w7jQ8nbNVogepgjxwKBem9JYNWR3ocPc3eJsgpwPMC', --- Insider lock or Ops pool
--     'Hwk2gsNVxEAGj7kz5Ex7Tu9wAWsctFoTe7KEbzrxBQj1', --- Insider lock or Ops pool
--     '3bw4c64JkTPfZ9jcMXnAzbXXweb9by7TAQaWTKj6Yi4h', --- Insider lock or Ops pool
--     'DF9rLSSMpJHrHeLz6twko9Ymv8fsKe2emq9dSEQp4HhZ', --- Insider lock or Ops pool
--     'DeXjsmBPJiRWkrjoZm15Gjjea4LtDoXdVritNvVicUmg', --- Insider lock or Ops pool
--     'As4nhNHHrhNZMoReGZvEbDTiooNmNATjyjjbadArEJVb', --- Insider lock or Ops pool
--     '2pACwttxpQEk1gs62rZLn78Bb9iAjPvKptBYqwKVk7Ge', --- Insider lock or Ops pool
--     '3W6AdCnjAKxA2GjrAJ48bt3n3wr9pG7F8jasrcciGAkf', --- Insider lock or Ops pool
--     'DVVWXkWLio5MRGeopBMhGayEDJkPecz8T2Q8LxzEsEos', --- Insider lock or Ops pool
--     '4EyVbx6MYCBk79LYQFSw1PZ74RFpvJ3TYP3ME7DgSuxB', --- Insider lock or Ops pool
--     'GwzXxiNKZGZCqHQK1tzPP8ANyCS4YkYa5F6P9upPqrsb', --- Insider lock or Ops pool
--     'Zg2CKtyca14FmBk4LPkkZEVnsYE4LNeyhQQsFE3hcNN', --- Insider lock or Ops pool
--     'HhQAW4wtZYfbk7WmETgDMB4GtGwRBXtfiShngdjUpq1t', --- Insider lock or Ops pool
--     'HYVem62h69nbyCJHdzwZsv7bKLL15D8egLeRcCkmfS2c', --- Insider lock or Ops pool
--     'B3ePZXj66duAn2Zhgy2MXeUNacbMfHuZ1DukM49wK4oz', --- Insider lock or Ops pool
--     '6rxSLPkpEQXDvyXeoRDVxyYrQvQpptPbZDox9qhJ1YgF', --- Insider lock or Ops pool
--     '3mVqotZhkNrfi23NnVDrgLFHgvG6qQUUQwYUJbix5Vyp', --- Insider lock or Ops pool
--     '4thbQayu3hLUZYrUHucup1MrqhEcxCj1aSKYFBEzLq9V', --- Insider lock or Ops pool
--     '7eMLQvC6qkvqP7izpUdB3nJHzkeTu66UWLkRyTrAuBkS', --- Insider lock or Ops pool
--     'EYNurCP6Mhp9nvdggCMHkYmhjwudUBcMfcz4qUXWgCS6', --- Insider lock or Ops pool
--     '28uWRJsdBt3sM9GNZ7j39WHr8oXEZHFJtnN5m3pSchff', --- Temporary usage 
--     'ATmkAJuCP117WTRLXZ9L5SBrMJpkcQoSGDgNf7rhGMaV', --- Temporary usage 
--     '3duyMwKk6vpGQ9WjTfrwLacgA4KfaV6yM3YxXQQJ7ogV', --- Temporary usage 
--     '5DNgfKSc4PJ9JmMneM6AdctVPkdi8cc3xgry8ksRtt95', --- Temporary usage 
--     'DZgtrNzKLCKvU4ekf67kzohv3EW1hwdmDwPgiwXaGCZr', --- Temporary usage 
--     'DY2rLdpbzDjDqUVHEvHQvPTShhM6xbJh4b5Po4DxXRV4', --- Temporary usage 
--     'CV3nbqgvQm8NbQsudmKmr84zszJegLkrJkzrJY4X8znF', --- Temporary usage 
--     '5dcpUq4r4TG3drg3JQSskGD1xGUepDguiJoMjj83G6hf', --- Temporary usage 
--     '6PRskNzL8755ktdp25itkW8af6f1B3pGsQqjdMjjEZr1', --- Temporary usage 
--     'eFttF7Ahk6haDqEsDgMQggKweDGxJd6u4CUQg1HkzDH', --- Temporary usage 
--     '8i6bq2fYxDybk3foeaBXGen8s1pJFx5Vs4PDx38J6ho8', --- Temporary usage 
--     'iwkwPri1U3PocwvxLBGLBrKCLvwSxLHch5aHbkaNuM3', --- Temporary usage 
--     '9CkUYinXiHv1Juwgt4gzQjttvrxRzUeXEw4gurrs19Yu', --- Temporary usage 
--     'DkBvptEyJaV4JFkd645rW7euVZobKWfNC9LGWziueYiR', --- Temporary usage 
--     'EgUJL13AD9zkq8BeJ4f6t1BxLmeFdZEEb2Kvwz4K2odf', --- Temporary usage 
--     '5PAjGzHJhdmBguP6DHAYpPLxCv1TydX9a1RZApMjM5n3', --- Temporary usage 
--     '425VB7Phq4EApekN5qkrZWefeZwGwThThXhXT8SPbvtU', --- Temp Dev accounts
--     '6kd4Z1rkto4y1PGQqfwNhxbEe7rcyP8TRsZ2PCiDR5r2', --- Temp Dev accounts
--     'Exbfb9A4grsUNeuwf1aKeWDERQKmkTHjmUtfd5vLR5bq', --- Temp Dev accounts
--     '7A6PQV93jCGfscAuRu69CuKyf5HPMQa9xqRBMrBT6Dvv', --- Temp Dev accounts
--     '2oiMzQwp8JW1vLzuqHePgaLdoG1ypnn37nMjCSEy4tvK', --- Insider lock or Ops pool
--     '9Su1HC6LNg8FVZH346xipVbEYUVvXr62nJX8Kbmk45CW', --- Insider lock or Ops pool
--     '13UGBJmRx5eC2aRS1PVvdvQbA8XtnKrKNZp6XBinuakZ', --- Insider lock or Ops pool
--     '4HAjZX8JA662tY5ptVTrXHu3nfz5oPLi1mkjhbh8VjFP', --- Insider lock or Ops pool
--     'BuxUbBh4D7KBsNXRo283nihaQV8wZ6QDtudwrfyTAWBP', --- Insider lock or Ops pool
--     'E72wWDCKCiS32SgEFocVs3EHEJTJkJRm58RRvpnLQcLz', --- Insider lock or Ops pool
--     'BGMgUvHk6Yu1ouqjP2LM6Lwj3LEqdxqYaiu4p2QcN2CD', --- Burn acct.
--     '6UE1gdvgFPbu8REp5YWKRkC5CZiXgwd7iEfTAqwbzUqV'  --- Burn acct.
    
-- ]
-- ) AS t(account)
-- ),
-- transfers AS (
--     SELECT
--         DATE_TRUNC('day', block_time) AS day,
--         to_owner AS account,
--         SUM(amount) / 1e8 AS flow
--     FROM
--         tokens_solana.transfers
--     WHERE
--         token_mint_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
--         AND to_owner IN (SELECT account FROM account_list)
--     GROUP BY
--         DATE_TRUNC('day', block_time), to_owner
--     UNION ALL
--     SELECT
--         DATE_TRUNC('day', block_time) AS day,
--         from_owner AS account,
--         -SUM(amount) / 1e8 AS flow
--     FROM
--         tokens_solana.transfers
--     WHERE
--         token_mint_address = 'xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L'
--         AND from_owner IN (SELECT account FROM account_list)
--     GROUP BY
--         DATE_TRUNC('day', block_time), from_owner
-- ),
-- daily_balance AS (
--     SELECT
--         day,
--         SUM(flow) AS daily_flow
--     FROM
--         transfers
--     GROUP BY
--         day
-- ),
-- cumulative_balance AS (
--     SELECT
--         day,
--         2400000000 - SUM(daily_flow) OVER (ORDER BY day) AS balance
--     FROM
--         daily_balance
-- )
-- SELECT
--     day,
--     balance
-- FROM
--     cumulative_balance
-- ORDER BY
--     day DESC;