SELECT
    a.content_format,
    ROUND(SUM(w.watch_seconds) / 3600.0, 2) AS watch_hours
FROM fact_watch_session AS w
JOIN dim_episode AS e ON w.episode_id = e.episode_id
JOIN dim_anime AS a ON e.anime_id = a.anime_id
GROUP BY a.content_format
ORDER BY watch_hours DESC;
