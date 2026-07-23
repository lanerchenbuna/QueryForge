SELECT
    s.studio_name,
    ROUND(AVG(r.score), 2) AS average_rating,
    COUNT(*) AS rating_count
FROM fact_rating AS r
JOIN dim_anime AS a ON r.anime_id = a.anime_id
JOIN dim_studio AS s ON a.studio_id = s.studio_id
GROUP BY s.studio_name
HAVING COUNT(*) >= 100
ORDER BY average_rating DESC, rating_count DESC;
