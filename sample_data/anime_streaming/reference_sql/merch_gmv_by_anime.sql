SELECT
    a.title,
    ROUND(SUM(i.net_amount_usd), 2) AS merch_gmv,
    SUM(i.quantity) AS merch_units
FROM fact_merch_order_item AS i
JOIN dim_merch_product AS p ON i.product_id = p.product_id
JOIN dim_anime AS a ON p.anime_id = a.anime_id
GROUP BY a.title
ORDER BY merch_gmv DESC
LIMIT 20;
