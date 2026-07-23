"""Generate a deterministic, multi-domain anime streaming analytics sample."""

from __future__ import annotations

import csv
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from queryforge.domain.semantic.builder import SemanticModelBuilder


OUTPUT_ROOT = PROJECT_ROOT / "sample_data" / "anime_streaming"
DATABASE = OUTPUT_ROOT / "anime_streaming.sqlite"
CSV_ROOT = OUTPUT_ROOT / "tables"
SEMANTIC_MODEL = OUTPUT_ROOT / "semantic_model.yml"
SEMANTIC_BUILD_REPORT = OUTPUT_ROOT / "semantic_model.build.json"
SEED = 20260723

TABLES = (
    "dim_date",
    "dim_studio",
    "dim_genre",
    "dim_anime",
    "bridge_anime_genre",
    "dim_episode",
    "dim_user",
    "fact_subscription",
    "fact_watch_session",
    "fact_rating",
    "fact_ad_impression",
    "fact_user_follow",
    "dim_merch_product",
    "fact_merch_order",
    "fact_merch_order_item",
)

EXPECTED_COUNTS = {
    "dim_date": 1096,
    "dim_studio": 48,
    "dim_genre": 18,
    "dim_anime": 360,
    "bridge_anime_genre": 1080,
    "dim_episode": 5760,
    "dim_user": 6000,
    "fact_subscription": 9000,
    "fact_watch_session": 150000,
    "fact_rating": 40000,
    "fact_ad_impression": 80000,
    "fact_user_follow": 24000,
    "dim_merch_product": 900,
    "fact_merch_order": 15000,
    "fact_merch_order_item": 37500,
}

GENRES = (
    "Action",
    "Adventure",
    "Comedy",
    "Drama",
    "Fantasy",
    "Romance",
    "Sci-Fi",
    "Slice of Life",
    "Mystery",
    "Thriller",
    "Sports",
    "Music",
    "Historical",
    "Supernatural",
    "Mecha",
    "Iyashikei",
    "Isekai",
    "Cyberpunk",
)


def build_database(database_path: Path = DATABASE) -> dict[str, int]:
    """Build the complete deterministic sample and return table row counts."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    if database_path.exists():
        database_path.unlink()
    rng = random.Random(SEED)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        _create_schema(connection)
        dates = _insert_dates(connection)
        studios = _insert_studios(connection)
        genres = _insert_genres(connection)
        anime = _insert_anime(connection, rng, studios)
        _insert_anime_genres(connection, anime, genres)
        episodes = _insert_episodes(connection, anime)
        users = _insert_users(connection, rng)
        subscriptions = _insert_subscriptions(connection, rng, users, dates)
        watches = _insert_watch_sessions(
            connection, rng, users, episodes, subscriptions, dates
        )
        _insert_ratings(connection, rng, users, anime, dates)
        _insert_ad_impressions(connection, rng, users, anime, watches, dates)
        _insert_user_follows(connection, rng, users, dates)
        products = _insert_merch_products(connection, rng, anime)
        _insert_merch_orders(connection, rng, users, products, dates)
        connection.commit()
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"Generated foreign-key violations: {violations[:5]}")
        counts = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in TABLES
        }
        if counts != EXPECTED_COUNTS:
            raise RuntimeError(f"Unexpected deterministic row counts: {counts}")
        connection.execute("ANALYZE")
        connection.commit()
        return counts
    finally:
        connection.close()


def export_csv(database_path: Path = DATABASE, output_root: Path = CSV_ROOT) -> None:
    """Export every physical table to a same-name UTF-8 CSV file."""
    output_root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        for table in TABLES:
            cursor = connection.execute(f'SELECT * FROM "{table}" ORDER BY 1')
            path = output_root / f"{table}.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(column[0] for column in cursor.description)
                while rows := cursor.fetchmany(10_000):
                    writer.writerows(rows)
    finally:
        connection.close()


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE dim_date (
            date_key INTEGER PRIMARY KEY,
            full_date TEXT NOT NULL UNIQUE,
            year INTEGER NOT NULL,
            quarter INTEGER NOT NULL,
            month_number INTEGER NOT NULL,
            month_name TEXT NOT NULL,
            week_of_year INTEGER NOT NULL,
            day_of_week TEXT NOT NULL,
            is_weekend INTEGER NOT NULL CHECK (is_weekend IN (0, 1))
        );

        CREATE TABLE dim_studio (
            studio_id INTEGER PRIMARY KEY,
            studio_name TEXT NOT NULL UNIQUE,
            country TEXT NOT NULL,
            founded_year INTEGER NOT NULL,
            studio_tier TEXT NOT NULL
        );

        CREATE TABLE dim_genre (
            genre_id INTEGER PRIMARY KEY,
            genre_name TEXT NOT NULL UNIQUE,
            genre_group TEXT NOT NULL,
            is_mature INTEGER NOT NULL CHECK (is_mature IN (0, 1))
        );

        CREATE TABLE dim_anime (
            anime_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL UNIQUE,
            original_title TEXT NOT NULL,
            studio_id INTEGER NOT NULL REFERENCES dim_studio(studio_id),
            sequel_of_anime_id INTEGER REFERENCES dim_anime(anime_id),
            content_format TEXT NOT NULL,
            source_material TEXT NOT NULL,
            release_year INTEGER NOT NULL,
            age_rating TEXT NOT NULL,
            production_status TEXT NOT NULL,
            episode_count INTEGER NOT NULL,
            production_budget_usd REAL NOT NULL
        );

        CREATE TABLE bridge_anime_genre (
            anime_id INTEGER NOT NULL REFERENCES dim_anime(anime_id),
            genre_id INTEGER NOT NULL REFERENCES dim_genre(genre_id),
            genre_weight REAL NOT NULL CHECK (genre_weight > 0 AND genre_weight <= 1),
            is_primary INTEGER NOT NULL CHECK (is_primary IN (0, 1)),
            PRIMARY KEY (anime_id, genre_id)
        );

        CREATE TABLE dim_episode (
            episode_id INTEGER PRIMARY KEY,
            anime_id INTEGER NOT NULL REFERENCES dim_anime(anime_id),
            season_number INTEGER NOT NULL,
            episode_number INTEGER NOT NULL,
            episode_title TEXT NOT NULL,
            release_date_key INTEGER NOT NULL REFERENCES dim_date(date_key),
            duration_seconds INTEGER NOT NULL,
            is_filler INTEGER NOT NULL CHECK (is_filler IN (0, 1)),
            UNIQUE (anime_id, season_number, episode_number)
        );

        CREATE TABLE dim_user (
            user_id INTEGER PRIMARY KEY,
            user_handle TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            referred_by_user_id INTEGER REFERENCES dim_user(user_id),
            signup_date_key INTEGER NOT NULL REFERENCES dim_date(date_key),
            country TEXT NOT NULL,
            region TEXT NOT NULL,
            age_band TEXT NOT NULL,
            acquisition_channel TEXT NOT NULL,
            preferred_language TEXT NOT NULL,
            marketing_opt_in INTEGER NOT NULL CHECK (marketing_opt_in IN (0, 1))
        );

        CREATE TABLE fact_subscription (
            subscription_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES dim_user(user_id),
            start_date_key INTEGER NOT NULL REFERENCES dim_date(date_key),
            end_date_key INTEGER REFERENCES dim_date(date_key),
            plan_name TEXT NOT NULL,
            billing_cycle TEXT NOT NULL,
            status TEXT NOT NULL,
            monthly_price_usd REAL NOT NULL,
            discount_usd REAL NOT NULL,
            recognized_revenue_usd REAL NOT NULL,
            cancellation_reason TEXT
        );

        CREATE TABLE fact_watch_session (
            watch_session_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES dim_user(user_id),
            episode_id INTEGER NOT NULL REFERENCES dim_episode(episode_id),
            subscription_id INTEGER REFERENCES fact_subscription(subscription_id),
            watch_date_key INTEGER NOT NULL REFERENCES dim_date(date_key),
            started_at TEXT NOT NULL,
            device_type TEXT NOT NULL,
            playback_region TEXT NOT NULL,
            watch_seconds INTEGER NOT NULL,
            completion_pct REAL NOT NULL CHECK (completion_pct >= 0 AND completion_pct <= 1),
            completed_flag INTEGER NOT NULL CHECK (completed_flag IN (0, 1)),
            rewatch_flag INTEGER NOT NULL CHECK (rewatch_flag IN (0, 1)),
            buffering_seconds INTEGER NOT NULL
        );

        CREATE TABLE fact_rating (
            rating_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES dim_user(user_id),
            anime_id INTEGER NOT NULL REFERENCES dim_anime(anime_id),
            rating_date_key INTEGER NOT NULL REFERENCES dim_date(date_key),
            score REAL NOT NULL CHECK (score >= 1 AND score <= 10),
            review_length INTEGER NOT NULL,
            helpful_votes INTEGER NOT NULL,
            spoiler_flag INTEGER NOT NULL CHECK (spoiler_flag IN (0, 1)),
            UNIQUE (user_id, anime_id)
        );

        CREATE TABLE fact_ad_impression (
            impression_id INTEGER PRIMARY KEY,
            user_id INTEGER REFERENCES dim_user(user_id),
            anime_id INTEGER NOT NULL REFERENCES dim_anime(anime_id),
            watch_session_id INTEGER REFERENCES fact_watch_session(watch_session_id),
            impression_date_key INTEGER NOT NULL REFERENCES dim_date(date_key),
            ad_format TEXT NOT NULL,
            advertiser_industry TEXT NOT NULL,
            placement TEXT NOT NULL,
            completed_flag INTEGER NOT NULL CHECK (completed_flag IN (0, 1)),
            clicked_flag INTEGER NOT NULL CHECK (clicked_flag IN (0, 1)),
            revenue_usd REAL NOT NULL
        );

        CREATE TABLE fact_user_follow (
            follow_id INTEGER PRIMARY KEY,
            follower_user_id INTEGER NOT NULL REFERENCES dim_user(user_id),
            followed_user_id INTEGER NOT NULL REFERENCES dim_user(user_id),
            follow_date_key INTEGER NOT NULL REFERENCES dim_date(date_key),
            source_surface TEXT NOT NULL,
            CHECK (follower_user_id != followed_user_id),
            UNIQUE (follower_user_id, followed_user_id)
        );

        CREATE TABLE dim_merch_product (
            product_id INTEGER PRIMARY KEY,
            anime_id INTEGER REFERENCES dim_anime(anime_id),
            sku TEXT NOT NULL UNIQUE,
            product_name TEXT NOT NULL,
            product_category TEXT NOT NULL,
            license_type TEXT NOT NULL,
            unit_cost_usd REAL NOT NULL,
            list_price_usd REAL NOT NULL
        );

        CREATE TABLE fact_merch_order (
            order_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES dim_user(user_id),
            order_date_key INTEGER NOT NULL REFERENCES dim_date(date_key),
            sales_channel TEXT NOT NULL,
            shipping_region TEXT NOT NULL,
            order_status TEXT NOT NULL,
            gross_amount_usd REAL NOT NULL,
            discount_usd REAL NOT NULL,
            shipping_usd REAL NOT NULL,
            net_amount_usd REAL NOT NULL
        );

        CREATE TABLE fact_merch_order_item (
            order_item_id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL REFERENCES fact_merch_order(order_id),
            product_id INTEGER NOT NULL REFERENCES dim_merch_product(product_id),
            quantity INTEGER NOT NULL,
            unit_price_usd REAL NOT NULL,
            discount_usd REAL NOT NULL,
            net_amount_usd REAL NOT NULL
        );

        CREATE INDEX idx_anime_studio ON dim_anime(studio_id);
        CREATE INDEX idx_anime_sequel ON dim_anime(sequel_of_anime_id);
        CREATE INDEX idx_bridge_genre ON bridge_anime_genre(genre_id);
        CREATE INDEX idx_episode_anime ON dim_episode(anime_id);
        CREATE INDEX idx_episode_release ON dim_episode(release_date_key);
        CREATE INDEX idx_user_signup ON dim_user(signup_date_key);
        CREATE INDEX idx_user_referral ON dim_user(referred_by_user_id);
        CREATE INDEX idx_subscription_user ON fact_subscription(user_id);
        CREATE INDEX idx_subscription_start ON fact_subscription(start_date_key);
        CREATE INDEX idx_watch_user ON fact_watch_session(user_id);
        CREATE INDEX idx_watch_episode ON fact_watch_session(episode_id);
        CREATE INDEX idx_watch_date ON fact_watch_session(watch_date_key);
        CREATE INDEX idx_rating_user ON fact_rating(user_id);
        CREATE INDEX idx_rating_anime ON fact_rating(anime_id);
        CREATE INDEX idx_ad_user ON fact_ad_impression(user_id);
        CREATE INDEX idx_ad_anime ON fact_ad_impression(anime_id);
        CREATE INDEX idx_ad_session ON fact_ad_impression(watch_session_id);
        CREATE INDEX idx_follow_follower ON fact_user_follow(follower_user_id);
        CREATE INDEX idx_follow_followed ON fact_user_follow(followed_user_id);
        CREATE INDEX idx_product_anime ON dim_merch_product(anime_id);
        CREATE INDEX idx_order_user ON fact_merch_order(user_id);
        CREATE INDEX idx_order_date ON fact_merch_order(order_date_key);
        CREATE INDEX idx_order_item_order ON fact_merch_order_item(order_id);
        CREATE INDEX idx_order_item_product ON fact_merch_order_item(product_id);
        """
    )


def _insert_dates(connection: sqlite3.Connection) -> list[date]:
    start = date(2023, 1, 1)
    end = date(2025, 12, 31)
    dates: list[date] = []
    current = start
    while current <= end:
        dates.append(current)
        current += timedelta(days=1)
    connection.executemany(
        "INSERT INTO dim_date VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                int(value.strftime("%Y%m%d")),
                value.isoformat(),
                value.year,
                (value.month - 1) // 3 + 1,
                value.month,
                value.strftime("%B"),
                value.isocalendar().week,
                value.strftime("%A"),
                int(value.weekday() >= 5),
            )
            for value in dates
        ],
    )
    return dates


def _insert_studios(connection: sqlite3.Connection) -> list[int]:
    countries = ("Japan", "South Korea", "China", "United States")
    tiers = ("Indie", "Growth", "Major")
    rows = [
        (
            studio_id,
            f"Studio {chr(64 + ((studio_id - 1) % 26) + 1)}-{studio_id:02d}",
            countries[(studio_id - 1) % len(countries)],
            1965 + (studio_id * 7) % 56,
            tiers[(studio_id - 1) % len(tiers)],
        )
        for studio_id in range(1, 49)
    ]
    connection.executemany("INSERT INTO dim_studio VALUES (?, ?, ?, ?, ?)", rows)
    return [row[0] for row in rows]


def _insert_genres(connection: sqlite3.Connection) -> list[int]:
    groups = {
        "Action": "High Energy",
        "Adventure": "High Energy",
        "Comedy": "Light",
        "Drama": "Narrative",
        "Fantasy": "Speculative",
        "Romance": "Narrative",
        "Sci-Fi": "Speculative",
        "Slice of Life": "Light",
        "Mystery": "Suspense",
        "Thriller": "Suspense",
        "Sports": "High Energy",
        "Music": "Light",
        "Historical": "Narrative",
        "Supernatural": "Speculative",
        "Mecha": "Speculative",
        "Iyashikei": "Light",
        "Isekai": "Speculative",
        "Cyberpunk": "Suspense",
    }
    rows = [
        (
            genre_id,
            genre,
            groups[genre],
            int(genre in {"Thriller", "Cyberpunk"}),
        )
        for genre_id, genre in enumerate(GENRES, start=1)
    ]
    connection.executemany("INSERT INTO dim_genre VALUES (?, ?, ?, ?)", rows)
    return [row[0] for row in rows]


def _insert_anime(
    connection: sqlite3.Connection, rng: random.Random, studios: list[int]
) -> list[int]:
    adjectives = (
        "Azure",
        "Crimson",
        "Silent",
        "Neon",
        "Celestial",
        "Clockwork",
        "Paper",
        "Golden",
        "Midnight",
        "Electric",
        "Falling",
        "Hidden",
    )
    nouns = (
        "Voyager",
        "Garden",
        "Requiem",
        "Chronicle",
        "Signal",
        "Frontier",
        "Paradox",
        "Melody",
        "Guardian",
        "Archive",
        "Horizon",
        "Lantern",
        "Odyssey",
        "Protocol",
        "Kingdom",
    )
    formats = ("Series", "Movie", "OVA", "ONA")
    sources = ("Manga", "Light Novel", "Original", "Game", "Web Novel")
    age_ratings = ("G", "PG", "PG-13", "R")
    statuses = ("Completed", "Ongoing", "Announced")
    rows = []
    for anime_id in range(1, 361):
        title = (
            f"{adjectives[(anime_id - 1) % len(adjectives)]} "
            f"{nouns[((anime_id - 1) // len(adjectives)) % len(nouns)]} "
            f"{anime_id:03d}"
        )
        sequel = anime_id - 36 if anime_id > 72 and anime_id % 5 == 0 else None
        rows.append(
            (
                anime_id,
                title,
                f"Original Title {anime_id:03d}",
                studios[(anime_id * 11) % len(studios)],
                sequel,
                formats[(anime_id - 1) % len(formats)],
                sources[(anime_id * 3) % len(sources)],
                1998 + (anime_id * 7) % 28,
                age_ratings[(anime_id * 5) % len(age_ratings)],
                statuses[(anime_id * 13) % len(statuses)],
                16,
                round(rng.uniform(220_000, 8_500_000), 2),
            )
        )
    connection.executemany(
        "INSERT INTO dim_anime VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    return [row[0] for row in rows]


def _insert_anime_genres(
    connection: sqlite3.Connection, anime: list[int], genres: list[int]
) -> None:
    rows = []
    for anime_id in anime:
        selected = [
            genres[(anime_id * 5) % len(genres)],
            genres[(anime_id * 7 + 3) % len(genres)],
            genres[(anime_id * 11 + 5) % len(genres)],
        ]
        if len(set(selected)) < 3:
            selected = [genres[(anime_id + offset * 5) % len(genres)] for offset in range(3)]
        unique = list(dict.fromkeys(selected))
        while len(unique) < 3:
            candidate = genres[(anime_id + len(unique) * 7) % len(genres)]
            if candidate not in unique:
                unique.append(candidate)
        for rank, genre_id in enumerate(unique[:3]):
            rows.append((anime_id, genre_id, (0.5, 0.3, 0.2)[rank], int(rank == 0)))
    connection.executemany(
        "INSERT INTO bridge_anime_genre VALUES (?, ?, ?, ?)", rows
    )


def _insert_episodes(
    connection: sqlite3.Connection, anime: list[int]
) -> list[tuple[int, int, int]]:
    rows = []
    episode_id = 1
    start = date(2023, 1, 1)
    for anime_id in anime:
        first_release = start + timedelta(days=(anime_id * 17) % 900)
        for number in range(1, 17):
            release = first_release + timedelta(days=(number - 1) * 7)
            if release > date(2025, 12, 31):
                release = date(2025, 12, 31)
            duration = 1320 + (anime_id * 13 + number * 17) % 360
            rows.append(
                (
                    episode_id,
                    anime_id,
                    1 if number <= 12 else 2,
                    number if number <= 12 else number - 12,
                    f"Episode {number:02d}: Chapter {(anime_id + number) % 31 + 1}",
                    int(release.strftime("%Y%m%d")),
                    duration,
                    int((anime_id + number) % 17 == 0),
                )
            )
            episode_id += 1
    connection.executemany(
        "INSERT INTO dim_episode VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    return [(row[0], row[1], row[6]) for row in rows]


def _insert_users(
    connection: sqlite3.Connection, rng: random.Random
) -> list[int]:
    countries = (
        ("Japan", "APAC"),
        ("United States", "North America"),
        ("Brazil", "Latin America"),
        ("Germany", "Europe"),
        ("France", "Europe"),
        ("Indonesia", "APAC"),
        ("Mexico", "Latin America"),
        ("Canada", "North America"),
    )
    age_bands = ("13-17", "18-24", "25-34", "35-44", "45+")
    channels = ("Organic", "Creator", "Paid Social", "Search", "Referral")
    languages = ("Japanese", "English", "Spanish", "Portuguese", "German", "French")
    rows = []
    for user_id in range(1, 6001):
        country, region = countries[(user_id * 7) % len(countries)]
        signup = date(2023, 1, 1) + timedelta(days=rng.randrange(0, 1096))
        referred_by = rng.randrange(1, user_id) if user_id > 50 and user_id % 7 == 0 else None
        rows.append(
            (
                user_id,
                f"viewer_{user_id:05d}",
                f"viewer{user_id:05d}@example.invalid",
                referred_by,
                int(signup.strftime("%Y%m%d")),
                country,
                region,
                age_bands[(user_id * 3) % len(age_bands)],
                channels[(user_id * 11) % len(channels)],
                languages[(user_id * 5) % len(languages)],
                int(user_id % 3 != 0),
            )
        )
    connection.executemany(
        "INSERT INTO dim_user VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    return [row[0] for row in rows]


def _insert_subscriptions(
    connection: sqlite3.Connection,
    rng: random.Random,
    users: list[int],
    dates: list[date],
) -> list[tuple[int, int, date, date | None]]:
    plans = {
        "Fan": 5.99,
        "Premium": 9.99,
        "Family": 14.99,
    }
    rows = []
    subscriptions = []
    for subscription_id in range(1, 9001):
        user_id = users[(subscription_id * 37) % len(users)]
        start = dates[rng.randrange(0, len(dates) - 45)]
        duration = rng.choice((30, 90, 180, 365, 730))
        raw_end = start + timedelta(days=duration)
        active = raw_end > dates[-1] or subscription_id % 5 == 0
        end = None if active else min(raw_end, dates[-1])
        plan = tuple(plans)[(subscription_id * 5) % len(plans)]
        price = plans[plan]
        discount = round(price * (0.2 if subscription_id % 9 == 0 else 0), 2)
        months = max(1, min(duration, (dates[-1] - start).days + 1) // 30)
        recognized = round((price - discount) * months, 2)
        status = "Active" if end is None else "Cancelled"
        reason = (
            None
            if end is None
            else ("Price" if subscription_id % 3 == 0 else "Low Usage")
        )
        rows.append(
            (
                subscription_id,
                user_id,
                int(start.strftime("%Y%m%d")),
                int(end.strftime("%Y%m%d")) if end else None,
                plan,
                "Monthly" if subscription_id % 4 else "Annual",
                status,
                price,
                discount,
                recognized,
                reason,
            )
        )
        subscriptions.append((subscription_id, user_id, start, end))
    connection.executemany(
        "INSERT INTO fact_subscription VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return subscriptions


def _insert_watch_sessions(
    connection: sqlite3.Connection,
    rng: random.Random,
    users: list[int],
    episodes: list[tuple[int, int, int]],
    subscriptions: list[tuple[int, int, date, date | None]],
    dates: list[date],
) -> list[tuple[int, int, int]]:
    subscription_by_user: dict[int, list[tuple[int, date, date | None]]] = {}
    for subscription_id, user_id, start, end in subscriptions:
        subscription_by_user.setdefault(user_id, []).append((subscription_id, start, end))
    devices = ("Mobile", "Web", "TV", "Tablet", "Console")
    regions = ("APAC", "North America", "Europe", "Latin America")
    rows = []
    compact = []
    for session_id in range(1, 150001):
        user_id = users[(session_id * 73 + rng.randrange(len(users))) % len(users)]
        episode_id, anime_id, duration = episodes[
            (session_id * 97 + rng.randrange(len(episodes))) % len(episodes)
        ]
        watch_date = dates[(session_id * 29 + rng.randrange(len(dates))) % len(dates)]
        available = [
            item
            for item in subscription_by_user.get(user_id, [])
            if item[1] <= watch_date and (item[2] is None or watch_date <= item[2])
        ]
        subscription_id = available[0][0] if available and session_id % 4 else None
        completion = min(1.0, max(0.03, rng.betavariate(2.8, 1.25)))
        watch_seconds = min(duration, max(30, int(duration * completion)))
        started = datetime.combine(watch_date, datetime.min.time()) + timedelta(
            seconds=(session_id * 137) % 86400
        )
        rows.append(
            (
                session_id,
                user_id,
                episode_id,
                subscription_id,
                int(watch_date.strftime("%Y%m%d")),
                started.isoformat(timespec="seconds"),
                devices[(session_id * 7) % len(devices)],
                regions[(user_id * 3) % len(regions)],
                watch_seconds,
                round(watch_seconds / duration, 4),
                int(watch_seconds / duration >= 0.9),
                int(session_id % 11 == 0),
                int(rng.expovariate(1 / 8)),
            )
        )
        compact.append((session_id, user_id, anime_id))
        if len(rows) == 10_000:
            connection.executemany(
                "INSERT INTO fact_watch_session VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            rows.clear()
    return compact


def _insert_ratings(
    connection: sqlite3.Connection,
    rng: random.Random,
    users: list[int],
    anime: list[int],
    dates: list[date],
) -> None:
    pairs: set[tuple[int, int]] = set()
    rows = []
    rating_id = 1
    while rating_id <= 40000:
        user_id = users[rng.randrange(len(users))]
        anime_id = anime[rng.randrange(len(anime))]
        if (user_id, anime_id) in pairs:
            continue
        pairs.add((user_id, anime_id))
        score = round(min(10, max(1, rng.gauss(7.4, 1.5))), 1)
        rating_date = dates[rng.randrange(len(dates))]
        rows.append(
            (
                rating_id,
                user_id,
                anime_id,
                int(rating_date.strftime("%Y%m%d")),
                score,
                rng.randrange(0, 1200),
                int(rng.expovariate(1 / 6)),
                int(rating_id % 13 == 0),
            )
        )
        rating_id += 1
    connection.executemany(
        "INSERT INTO fact_rating VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
    )


def _insert_ad_impressions(
    connection: sqlite3.Connection,
    rng: random.Random,
    users: list[int],
    anime: list[int],
    watches: list[tuple[int, int, int]],
    dates: list[date],
) -> None:
    formats = ("Pre-roll", "Mid-roll", "Display", "Sponsored Card")
    industries = ("Gaming", "Technology", "Food", "Entertainment", "Retail")
    placements = ("Player", "Home Feed", "Search", "Anime Detail")
    rows = []
    for impression_id in range(1, 80001):
        session_id, session_user, session_anime = watches[
            (impression_id * 41) % len(watches)
        ]
        anonymous = impression_id % 17 == 0
        impression_date = dates[(impression_id * 19) % len(dates)]
        ad_format = formats[(impression_id * 7) % len(formats)]
        completed = int(ad_format == "Display" or rng.random() < 0.72)
        clicked = int(rng.random() < (0.045 if completed else 0.008))
        rows.append(
            (
                impression_id,
                None if anonymous else session_user,
                session_anime if impression_id % 9 else anime[impression_id % len(anime)],
                None if impression_id % 9 == 0 else session_id,
                int(impression_date.strftime("%Y%m%d")),
                ad_format,
                industries[(impression_id * 3) % len(industries)],
                placements[(impression_id * 11) % len(placements)],
                completed,
                clicked,
                round(rng.uniform(0.002, 0.085), 4),
            )
        )
    connection.executemany(
        "INSERT INTO fact_ad_impression VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _insert_user_follows(
    connection: sqlite3.Connection,
    rng: random.Random,
    users: list[int],
    dates: list[date],
) -> None:
    pairs: set[tuple[int, int]] = set()
    rows = []
    follow_id = 1
    surfaces = ("Profile", "Review", "Watch Party", "Recommendation")
    while follow_id <= 24000:
        follower = users[rng.randrange(len(users))]
        followed = users[rng.randrange(len(users))]
        if follower == followed or (follower, followed) in pairs:
            continue
        pairs.add((follower, followed))
        follow_date = dates[rng.randrange(len(dates))]
        rows.append(
            (
                follow_id,
                follower,
                followed,
                int(follow_date.strftime("%Y%m%d")),
                surfaces[(follow_id * 7) % len(surfaces)],
            )
        )
        follow_id += 1
    connection.executemany(
        "INSERT INTO fact_user_follow VALUES (?, ?, ?, ?, ?)", rows
    )


def _insert_merch_products(
    connection: sqlite3.Connection,
    rng: random.Random,
    anime: list[int],
) -> list[tuple[int, float]]:
    categories = ("Figure", "Apparel", "Poster", "Blu-ray", "Accessory")
    licenses = ("Exclusive", "Standard", "Limited")
    rows = []
    products = []
    for product_id in range(1, 901):
        anime_id = None if product_id % 20 == 0 else anime[(product_id * 17) % len(anime)]
        cost = round(rng.uniform(2.5, 90), 2)
        price = round(cost * rng.uniform(1.35, 2.6), 2)
        category = categories[(product_id * 3) % len(categories)]
        rows.append(
            (
                product_id,
                anime_id,
                f"ANI-{product_id:05d}",
                f"{category} Collection {product_id:04d}",
                category,
                licenses[(product_id * 5) % len(licenses)],
                cost,
                price,
            )
        )
        products.append((product_id, price))
    connection.executemany(
        "INSERT INTO dim_merch_product VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    return products


def _insert_merch_orders(
    connection: sqlite3.Connection,
    rng: random.Random,
    users: list[int],
    products: list[tuple[int, float]],
    dates: list[date],
) -> None:
    channels = ("Web", "Mobile", "Convention", "Partner")
    regions = ("APAC", "North America", "Europe", "Latin America")
    statuses = ("Completed", "Cancelled", "Refunded")
    order_rows = []
    item_rows = []
    order_item_id = 1
    for order_id in range(1, 15001):
        user_id = users[(order_id * 43 + rng.randrange(len(users))) % len(users)]
        order_date = dates[(order_id * 31 + rng.randrange(len(dates))) % len(dates)]
        selected = rng.sample(products, 2 if order_id % 2 else 3)
        gross = 0.0
        discount_total = 0.0
        pending_items = []
        for product_id, list_price in selected:
            quantity = 1 + int((order_id + product_id) % 11 == 0)
            discount = round(
                list_price * quantity * (0.15 if order_item_id % 8 == 0 else 0), 2
            )
            net = round(list_price * quantity - discount, 2)
            gross += list_price * quantity
            discount_total += discount
            pending_items.append(
                (
                    order_item_id,
                    order_id,
                    product_id,
                    quantity,
                    list_price,
                    discount,
                    net,
                )
            )
            order_item_id += 1
        shipping = 0.0 if gross >= 75 else round(rng.uniform(4.99, 12.99), 2)
        status = rng.choices(statuses, weights=(91, 6, 3), k=1)[0]
        order_rows.append(
            (
                order_id,
                user_id,
                int(order_date.strftime("%Y%m%d")),
                channels[(order_id * 7) % len(channels)],
                regions[(user_id * 3) % len(regions)],
                status,
                round(gross, 2),
                round(discount_total, 2),
                shipping,
                round(gross - discount_total + shipping, 2),
            )
        )
        item_rows.extend(pending_items)
    connection.executemany(
        "INSERT INTO fact_merch_order VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        order_rows,
    )
    connection.executemany(
        "INSERT INTO fact_merch_order_item VALUES (?, ?, ?, ?, ?, ?, ?)",
        item_rows,
    )


def main() -> int:
    counts = build_database()
    export_csv()
    semantic_result = SemanticModelBuilder(
        DATABASE,
        owner="data-platform",
    ).build(
        SEMANTIC_MODEL,
        existing_model_path=SEMANTIC_MODEL if SEMANTIC_MODEL.is_file() else None,
        report_path=SEMANTIC_BUILD_REPORT,
    )
    print(f"Generated anime streaming database: {DATABASE}")
    for table in TABLES:
        print(f"- {table}: {counts[table]:,} rows")
    print(f"CSV exports: {CSV_ROOT}")
    print(
        "Semantic model: "
        f"{SEMANTIC_MODEL} ({len(semantic_result.model.entities)} entities, "
        f"{len(semantic_result.model.metrics)} metrics)"
    )
    print(f"Semantic build report: {SEMANTIC_BUILD_REPORT}")
    print(f"Total rows: {sum(counts.values()):,}")
    return 0 if semantic_result.published else 1


if __name__ == "__main__":
    raise SystemExit(main())
