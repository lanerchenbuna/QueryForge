SELECT
    device_type,
    ROUND(
        CAST(SUM(completed_flag) AS REAL) / NULLIF(COUNT(*), 0),
        4
    ) AS completion_rate
FROM fact_watch_session
GROUP BY device_type
ORDER BY completion_rate DESC;
