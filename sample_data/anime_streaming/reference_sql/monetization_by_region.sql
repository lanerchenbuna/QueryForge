WITH subscription AS (
    SELECT u.region, SUM(s.recognized_revenue_usd) AS revenue
    FROM fact_subscription AS s
    JOIN dim_user AS u ON s.user_id = u.user_id
    GROUP BY u.region
),
advertising AS (
    SELECT u.region, SUM(a.revenue_usd) AS revenue
    FROM fact_ad_impression AS a
    JOIN dim_user AS u ON a.user_id = u.user_id
    GROUP BY u.region
)
SELECT
    s.region,
    ROUND(s.revenue, 2) AS subscription_revenue,
    ROUND(COALESCE(a.revenue, 0), 2) AS ad_revenue
FROM subscription AS s
LEFT JOIN advertising AS a ON s.region = a.region
ORDER BY subscription_revenue DESC;
