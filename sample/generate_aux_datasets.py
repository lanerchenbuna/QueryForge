"""Generate the deterministic auxiliary benchmark datasets.

Two mutually independent small schemas are produced, each with its own SQLite
database, governed semantic model, SQL policy, and README:

* ``sample_data/retail_orders`` — multi-store retail order analytics;
* ``sample_data/support_tickets`` — customer support operations analytics.

Every value is derived from a fixed seed and an explicit 2024 calendar, so
``python sample/generate_aux_datasets.py`` is idempotent byte-for-byte and the
databases can be regenerated at any time without changing a gold value.

The datasets deliberately carry the situations the agent benchmark needs:

* ``support_tickets.fact_ticket_sla_breach`` is intentionally **empty**, so a
  dimensioned governed query returns no rows at all (``empty_result``);
* ``support_tickets.fact_csat_response.csat_score`` is missing for a large share
  of responses, so the governed ``null_rate`` quality check fails on that table
  (``data_fault``);
* the SQL policies withhold identifiers two different ways: the retail customer
  name/e-mail columns and the support survey verbatim column are outside the
  policy allowlist (``policy_rejection``).
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "sample_data"

RETAIL_DATASET = "retail_orders"
SUPPORT_DATASET = "support_tickets"

#: Independent seeds: changing one dataset never shifts the other one.
RETAIL_SEED = 20260816
SUPPORT_SEED = 20260817

RETAIL_TABLES = (
    "dim_date",
    "dim_store",
    "dim_product",
    "dim_customer",
    "fact_order",
    "fact_order_item",
)

SUPPORT_TABLES = (
    "dim_date",
    "dim_agent",
    "dim_queue",
    "fact_ticket",
    "fact_ticket_sla_breach",
    "fact_csat_response",
)

#: ``fact_ticket_sla_breach`` stays empty on purpose (see module docstring).
SUPPORT_EMPTY_TABLES = ("fact_ticket_sla_breach",)

CALENDAR_START = date(2024, 1, 1)
CALENDAR_DAYS = 366

MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

#: Orders per month for 2024 — sums to 480 and gives a Q4 seasonal lift.
RETAIL_MONTH_COUNTS = (30, 32, 34, 36, 38, 40, 42, 44, 42, 46, 48, 48)

#: Tickets per month for 2024 — sums to 420.
SUPPORT_MONTH_COUNTS = (30, 31, 32, 33, 34, 35, 36, 35, 34, 36, 40, 44)

#: A concentrated support incident: 24 tickets inside one week of March 2024.
SUPPORT_SPIKE_START = date(2024, 3, 11)
SUPPORT_SPIKE_END = date(2024, 3, 17)
SUPPORT_SPIKE_TICKETS = 24

#: Outlet stores only opened in time for the November 2024 holiday season, so the
#: Outlet format has no Q1/2024 trading rows at all.
OUTLET_FIRST_TRADING_DAY = date(2024, 11, 1)

#: Share of survey responses whose score was never submitted (the quality trap).
CSAT_MISSING_RATE = 0.45

RETAIL_STORES = (
    ("Seattle Flagship", "West", "Seattle", "Flagship"),
    ("Portland Standard", "West", "Portland", "Standard"),
    ("Denver Express", "West", "Denver", "Express"),
    ("Phoenix Outlet", "West", "Phoenix", "Outlet"),
    ("Austin Flagship", "South", "Austin", "Flagship"),
    ("Dallas Standard", "South", "Dallas", "Standard"),
    ("Atlanta Express", "South", "Atlanta", "Express"),
    ("Miami Outlet", "South", "Miami", "Outlet"),
    ("Boston Flagship", "East", "Boston", "Flagship"),
    ("New York Standard", "East", "New York", "Standard"),
    ("Philadelphia Express", "East", "Philadelphia", "Express"),
    ("Raleigh Outlet", "East", "Raleigh", "Outlet"),
    ("Chicago Flagship", "Central", "Chicago", "Flagship"),
    ("Detroit Standard", "Central", "Detroit", "Standard"),
    ("Minneapolis Express", "Central", "Minneapolis", "Express"),
    ("Columbus Outlet", "Central", "Columbus", "Outlet"),
    ("San Diego Standard", "West", "San Diego", "Standard"),
    ("Salt Lake City Express", "West", "Salt Lake City", "Express"),
    ("Houston Flagship", "South", "Houston", "Flagship"),
    ("Charlotte Express", "South", "Charlotte", "Express"),
    ("Nashville Standard", "South", "Nashville", "Standard"),
    ("Pittsburgh Standard", "East", "Pittsburgh", "Standard"),
    ("Cleveland Express", "Central", "Cleveland", "Express"),
    ("St. Louis Standard", "Central", "St. Louis", "Standard"),
)

RETAIL_PRODUCT_TEMPLATES = (
    ("Apparel", "T-Shirt", 24.99, 9.50),
    ("Apparel", "Hoodie", 59.99, 22.00),
    ("Apparel", "Cap", 19.99, 6.25),
    ("Footwear", "Running Shoe", 109.99, 44.00),
    ("Footwear", "Sandal", 49.99, 18.50),
    ("Footwear", "Boot", 139.99, 58.00),
    ("Accessories", "Backpack", 79.99, 28.00),
    ("Accessories", "Water Bottle", 17.99, 5.50),
    ("Accessories", "Field Watch", 129.99, 52.00),
    ("Home", "Throw Blanket", 39.99, 14.00),
    ("Home", "Stoneware Mug", 12.99, 3.75),
    ("Electronics", "Wireless Earbuds", 89.99, 36.00),
)

RETAIL_BRANDS = ("Alder", "Brook", "Cedar", "Delta", "Ember")

FIRST_NAMES = (
    "Ari",
    "Bela",
    "Cleo",
    "Dana",
    "Eli",
    "Fern",
    "Gale",
    "Hana",
    "Iris",
    "Jonah",
    "Kai",
    "Lena",
)

LAST_NAMES = (
    "Aoki",
    "Barros",
    "Chen",
    "Dubois",
    "Eze",
    "Ferrari",
    "Gallo",
    "Haddad",
    "Ibrahim",
    "Jensen",
    "Kimura",
    "Lopez",
)

LOYALTY_TIERS = ("Bronze", "Silver", "Gold", "Platinum")

SALES_CHANNELS = ("In-Store", "Web", "App")

ORDER_STATUSES = ("Completed", "Returned", "Cancelled", "Pending")

PAYMENT_METHODS = ("Card", "Cash", "Gift Card", "Digital Wallet")

SUPPORT_QUEUES = (
    ("Payments Email", "Payments", "Email", "P1", 4),
    ("Payments Chat", "Payments", "Chat", "P2", 8),
    ("Billing Phone", "Billing", "Phone", "P1", 4),
    ("Billing Email", "Billing", "Email", "P2", 8),
    ("Subscriptions Chat", "Subscriptions", "Chat", "P2", 8),
    ("Subscriptions Email", "Subscriptions", "Email", "P3", 24),
    ("Devices Chat", "Devices", "Chat", "P1", 4),
    ("Devices Phone", "Devices", "Phone", "P2", 8),
    ("Devices Email", "Devices", "Email", "P3", 24),
    ("Content Email", "Content", "Email", "P3", 24),
    ("Account Chat", "Account", "Chat", "P2", 8),
    ("Account Phone", "Account", "Phone", "P3", 24),
)

SUPPORT_TEAMS = ("Tier1", "Tier2", "Retention", "Billing")

SUPPORT_TICKET_STATUSES = ("Resolved", "Closed", "Pending", "Escalated")

SUPPORT_RESPONSE_CHANNELS = ("Survey Email", "In-App Survey", "Phone Follow-up")

VERBATIM_COMMENTS = (
    "Handled end to end by the support team.",
    "Response was clear and the case stayed open until it was fixed.",
    "Follow-up arrived later than the promised window.",
    "Asked twice for the same context before the case moved forward.",
    "Resolved on the first contact without a transfer.",
)


RETAIL_DDL = """
CREATE TABLE dim_date (
    date_key INTEGER PRIMARY KEY,
    full_date TEXT NOT NULL,
    year INTEGER NOT NULL,
    quarter INTEGER NOT NULL,
    month_number INTEGER NOT NULL,
    month_name TEXT NOT NULL,
    week_of_year INTEGER NOT NULL,
    day_of_week TEXT NOT NULL,
    is_weekend INTEGER NOT NULL,
    is_holiday_season INTEGER NOT NULL
);
CREATE TABLE dim_store (
    store_id INTEGER PRIMARY KEY,
    store_name TEXT NOT NULL,
    region TEXT NOT NULL,
    city TEXT NOT NULL,
    store_format TEXT NOT NULL,
    opened_year INTEGER NOT NULL,
    floor_area_sqm INTEGER NOT NULL
);
CREATE TABLE dim_product (
    product_id INTEGER PRIMARY KEY,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL,
    subcategory TEXT NOT NULL,
    list_price_usd REAL NOT NULL,
    unit_cost_usd REAL NOT NULL,
    is_seasonal INTEGER NOT NULL
);
CREATE TABLE dim_customer (
    customer_id INTEGER PRIMARY KEY,
    customer_name TEXT NOT NULL,
    customer_email TEXT NOT NULL,
    loyalty_tier TEXT NOT NULL,
    signup_date_key INTEGER NOT NULL REFERENCES dim_date(date_key),
    home_region TEXT NOT NULL
);
CREATE TABLE fact_order (
    order_id INTEGER PRIMARY KEY,
    order_date_key INTEGER NOT NULL REFERENCES dim_date(date_key),
    store_id INTEGER NOT NULL REFERENCES dim_store(store_id),
    customer_id INTEGER NOT NULL REFERENCES dim_customer(customer_id),
    sales_channel TEXT NOT NULL,
    order_status TEXT NOT NULL,
    payment_method TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    gross_amount_usd REAL NOT NULL,
    discount_usd REAL NOT NULL,
    shipping_usd REAL NOT NULL,
    net_amount_usd REAL NOT NULL,
    returned_flag INTEGER NOT NULL
);
CREATE TABLE fact_order_item (
    order_item_id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES fact_order(order_id),
    product_id INTEGER NOT NULL REFERENCES dim_product(product_id),
    quantity INTEGER NOT NULL,
    unit_price_usd REAL NOT NULL,
    discount_usd REAL NOT NULL,
    net_amount_usd REAL NOT NULL
);
"""

SUPPORT_DDL = """
CREATE TABLE dim_date (
    date_key INTEGER PRIMARY KEY,
    full_date TEXT NOT NULL,
    year INTEGER NOT NULL,
    quarter INTEGER NOT NULL,
    month_number INTEGER NOT NULL,
    month_name TEXT NOT NULL,
    week_of_year INTEGER NOT NULL,
    day_of_week TEXT NOT NULL,
    is_weekend INTEGER NOT NULL
);
CREATE TABLE dim_agent (
    agent_id INTEGER PRIMARY KEY,
    agent_name TEXT NOT NULL,
    team TEXT NOT NULL,
    region TEXT NOT NULL,
    seniority TEXT NOT NULL,
    active_flag INTEGER NOT NULL,
    hire_year INTEGER NOT NULL
);
CREATE TABLE dim_queue (
    queue_id INTEGER PRIMARY KEY,
    queue_name TEXT NOT NULL,
    product_area TEXT NOT NULL,
    channel_type TEXT NOT NULL,
    priority_class TEXT NOT NULL,
    sla_hours INTEGER NOT NULL
);
CREATE TABLE fact_ticket (
    ticket_id INTEGER PRIMARY KEY,
    created_date_key INTEGER NOT NULL REFERENCES dim_date(date_key),
    queue_id INTEGER NOT NULL REFERENCES dim_queue(queue_id),
    agent_id INTEGER NOT NULL REFERENCES dim_agent(agent_id),
    priority TEXT NOT NULL,
    status TEXT NOT NULL,
    first_response_minutes INTEGER NOT NULL,
    handle_minutes INTEGER NOT NULL,
    resolution_minutes INTEGER NOT NULL,
    reopened_flag INTEGER NOT NULL,
    escalated_flag INTEGER NOT NULL,
    satisfaction_sent_flag INTEGER NOT NULL
);
CREATE TABLE fact_ticket_sla_breach (
    breach_id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES fact_ticket(ticket_id),
    breach_date_key INTEGER NOT NULL REFERENCES dim_date(date_key),
    breached_target_minutes INTEGER NOT NULL,
    breach_minutes INTEGER NOT NULL
);
CREATE TABLE fact_csat_response (
    response_id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES fact_ticket(ticket_id),
    response_date_key INTEGER NOT NULL REFERENCES dim_date(date_key),
    agent_id INTEGER NOT NULL REFERENCES dim_agent(agent_id),
    csat_score INTEGER,
    response_channel TEXT NOT NULL,
    escalation_reason TEXT,
    verbatim_comment TEXT NOT NULL
);
"""


def calendar_rows(days: int = CALENDAR_DAYS, *, holiday_season: bool) -> list[tuple]:
    """Deterministic 2024 calendar rows (locale-independent weekday names)."""
    rows: list[tuple] = []
    for offset in range(days):
        day = CALENDAR_START + timedelta(days=offset)
        base = (
            day.year,
            (day.month - 1) // 3 + 1,
            day.month,
            MONTH_NAMES[day.month - 1],
            day.isocalendar()[1],
            WEEKDAY_NAMES[day.weekday()],
            int(day.weekday() >= 5),
        )
        if holiday_season:
            rows.append(
                (
                    int(day.strftime("%Y%m%d")),
                    day.isoformat(),
                    *base,
                    int(day.month in (11, 12)),
                )
            )
        else:
            rows.append((int(day.strftime("%Y%m%d")), day.isoformat(), *base))
    return rows


def _month_days(month: int) -> list[date]:
    start = date(2024, month, 1)
    next_month = date(2024, month + 1, 1) if month < 12 else date(2025, 1, 1)
    return [
        start + timedelta(days=offset)
        for offset in range((next_month - start).days)
    ]


def build_retail_database(database: Path) -> dict[str, int]:
    """Write ``retail_orders.sqlite`` and return the row count per table."""
    rng = random.Random(RETAIL_SEED)
    database.parent.mkdir(parents=True, exist_ok=True)
    database.unlink(missing_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.executescript(RETAIL_DDL)

        date_rows = calendar_rows(holiday_season=True)
        connection.executemany(
            "INSERT INTO dim_date VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", date_rows
        )

        store_rows = []
        for index, (name, region, city, store_format) in enumerate(RETAIL_STORES):
            store_id = index + 1
            store_rows.append(
                (
                    store_id,
                    name,
                    region,
                    city,
                    store_format,
                    2015 + (store_id * 7) % 9,
                    180 + (store_id * 37) % 620,
                )
            )
        connection.executemany(
            "INSERT INTO dim_store VALUES (?, ?, ?, ?, ?, ?, ?)", store_rows
        )

        product_rows = []
        product_id = 0
        for variant, brand in enumerate(RETAIL_BRANDS):
            for category, subcategory, list_price, unit_cost in RETAIL_PRODUCT_TEMPLATES:
                product_id += 1
                product_rows.append(
                    (
                        product_id,
                        f"{brand} {subcategory}",
                        category,
                        subcategory,
                        round(list_price * (1.0 + 0.05 * variant), 2),
                        round(unit_cost * (1.0 + 0.04 * variant), 2),
                        int(category == "Apparel" and subcategory in {"Hoodie", "Cap"}),
                    )
                )
        connection.executemany(
            "INSERT INTO dim_product VALUES (?, ?, ?, ?, ?, ?, ?)", product_rows
        )

        start_dates = calendar_rows(holiday_season=True)
        customer_rows = []
        for customer_id in range(1, 121):
            name = (
                f"{FIRST_NAMES[customer_id % len(FIRST_NAMES)]} "
                f"{LAST_NAMES[(customer_id * 5) % len(LAST_NAMES)]}"
            )
            signup_row = start_dates[(customer_id * 11) % 300]
            customer_rows.append(
                (
                    customer_id,
                    name,
                    f"customer{customer_id:04d}@example.com",
                    LOYALTY_TIERS[customer_id % len(LOYALTY_TIERS)],
                    signup_row[0],
                    RETAIL_STORES[customer_id % len(RETAIL_STORES)][1],
                )
            )
        connection.executemany(
            "INSERT INTO dim_customer VALUES (?, ?, ?, ?, ?, ?)", customer_rows
        )

        prices = {row[0]: row[4] for row in product_rows}
        outlet_ids_sorted = sorted(
            row[0] for row in store_rows if row[4] == "Outlet"
        )
        standard_ids = [row[0] for row in store_rows if row[4] != "Outlet"]

        order_rows: list[tuple] = []
        item_rows: list[tuple] = []
        order_id = 0
        item_id = 0
        for month, month_count in enumerate(RETAIL_MONTH_COUNTS, start=1):
            days = _month_days(month)
            for _ in range(month_count):
                order_id += 1
                order_day = days[rng.randrange(len(days))]
                if order_day >= OUTLET_FIRST_TRADING_DAY:
                    store_id = rng.choice(sorted(standard_ids + outlet_ids_sorted))
                else:
                    store_id = rng.choice(standard_ids)
                customer_id = rng.randint(1, 120)
                item_count = rng.randint(1, 4)
                gross = 0.0
                pending: list[tuple] = []
                for _ in range(item_count):
                    item_id += 1
                    product_id = rng.randint(1, len(product_rows))
                    quantity = rng.randint(1, 3)
                    unit_price = prices[product_id]
                    line_gross = round(unit_price * quantity, 2)
                    line_discount = round(line_gross * 0.1, 2) if rng.random() < 0.3 else 0.0
                    line_net = round(line_gross - line_discount, 2)
                    gross = round(gross + line_gross, 2)
                    pending.append(
                        (
                            item_id,
                            order_id,
                            product_id,
                            quantity,
                            unit_price,
                            line_discount,
                            line_net,
                        )
                    )
                discount = round(sum(row[5] for row in pending), 2)
                shipping = 0.0 if gross >= 75.0 else round(rng.uniform(4.99, 12.99), 2)
                status = rng.choices(
                    ORDER_STATUSES, weights=(78, 12, 6, 4), k=1
                )[0]
                order_rows.append(
                    (
                        order_id,
                        int(order_day.strftime("%Y%m%d")),
                        store_id,
                        customer_id,
                        SALES_CHANNELS[(order_id * 7) % len(SALES_CHANNELS)],
                        status,
                        PAYMENT_METHODS[(order_id * 5) % len(PAYMENT_METHODS)],
                        item_count,
                        gross,
                        discount,
                        shipping,
                        round(gross - discount + shipping, 2),
                        int(status == "Returned"),
                    )
                )
                item_rows.extend(pending)
        connection.executemany(
            "INSERT INTO fact_order VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            order_rows,
        )
        connection.executemany(
            "INSERT INTO fact_order_item VALUES (?, ?, ?, ?, ?, ?, ?)", item_rows
        )
        connection.commit()
        counts = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in RETAIL_TABLES
        }
    finally:
        connection.close()
    return counts


def build_support_database(database: Path) -> tuple[dict[str, int], dict[str, int]]:
    """Write ``support_tickets.sqlite``; return row counts and survey fault stats."""
    rng = random.Random(SUPPORT_SEED)
    database.parent.mkdir(parents=True, exist_ok=True)
    database.unlink(missing_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.executescript(SUPPORT_DDL)

        date_rows = calendar_rows(holiday_season=False)
        connection.executemany(
            "INSERT INTO dim_date VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", date_rows
        )

        agent_rows = []
        for index in range(24):
            agent_id = index + 1
            agent_rows.append(
                (
                    agent_id,
                    f"{FIRST_NAMES[index % len(FIRST_NAMES)]} "
                    f"{LAST_NAMES[(index * 7 + index // 12) % len(LAST_NAMES)]}",
                    SUPPORT_TEAMS[index % len(SUPPORT_TEAMS)],
                    ("North", "South", "East", "West")[index % 4],
                    ("Junior", "Mid", "Senior")[index % 3],
                    int(index % 8 != 7),
                    2016 + index % 8,
                )
            )
        if len({row[1] for row in agent_rows}) != len(agent_rows):
            raise AssertionError("generated agent names must stay unique")
        connection.executemany(
            "INSERT INTO dim_agent VALUES (?, ?, ?, ?, ?, ?, ?)", agent_rows
        )

        queue_rows = [
            (index + 1, *queue) for index, queue in enumerate(SUPPORT_QUEUES)
        ]
        connection.executemany(
            "INSERT INTO dim_queue VALUES (?, ?, ?, ?, ?, ?)", queue_rows
        )

        priority_response = {
            "P1": (2, 60),
            "P2": (5, 180),
            "P3": (10, 360),
        }
        ticket_rows: list[tuple] = []
        ticket_created: dict[int, date] = {}
        ticket_id = 0
        for month, month_count in enumerate(SUPPORT_MONTH_COUNTS, start=1):
            days = _month_days(month)
            outside = [
                day
                for day in days
                if not (SUPPORT_SPIKE_START <= day <= SUPPORT_SPIKE_END)
            ]
            inside = [
                day for day in days if SUPPORT_SPIKE_START <= day <= SUPPORT_SPIKE_END
            ]
            if inside:
                plan = [outside] * (month_count - SUPPORT_SPIKE_TICKETS) + [
                    inside
                ] * SUPPORT_SPIKE_TICKETS
            else:
                plan = [days] * month_count
            for pool in plan:
                ticket_id += 1
                created = pool[rng.randrange(len(pool))]
                ticket_created[ticket_id] = created
                priority = rng.choices(
                    ("P1", "P2", "P3"), weights=(15, 45, 40), k=1
                )[0]
                low, high = priority_response[priority]
                first_response = rng.randint(low, high)
                handle = rng.randint(5, 180)
                resolution = handle + rng.randint(30, 2200)
                status = rng.choices(
                    SUPPORT_TICKET_STATUSES, weights=(45, 40, 10, 5), k=1
                )[0]
                ticket_rows.append(
                    (
                        ticket_id,
                        int(created.strftime("%Y%m%d")),
                        rng.randint(1, len(queue_rows)),
                        rng.randint(1, len(agent_rows)),
                        priority,
                        status,
                        first_response,
                        handle,
                        resolution,
                        rng.choices((0, 1), weights=(93, 7), k=1)[0],
                        int(
                            status == "Escalated"
                            or rng.choices((0, 1), weights=(96, 4), k=1)[0] == 1
                        ),
                        rng.choices((0, 1), weights=(80, 20), k=1)[0],
                    )
                )
        connection.executemany(
            "INSERT INTO fact_ticket VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ticket_rows,
        )

        # Deliberately empty: no SLA breach has been recorded yet.
        connection.executemany(
            "INSERT INTO fact_ticket_sla_breach VALUES (?, ?, ?, ?, ?)", []
        )

        answered = {
            row[0]: row
            for row in ticket_rows
            if row[5] in {"Resolved", "Closed"}
        }
        answered_ids = sorted(answered)
        csat_rows: list[tuple] = []
        for response_id in range(1, 261):
            ticket_id_value = answered_ids[rng.randrange(len(answered_ids))]
            ticket = answered[ticket_id_value]
            response_day = min(
                ticket_created[ticket_id_value] + timedelta(days=rng.randint(1, 10)),
                CALENDAR_START + timedelta(days=CALENDAR_DAYS - 1),
            )
            missing = rng.random() < CSAT_MISSING_RATE
            csat_rows.append(
                (
                    response_id,
                    ticket_id_value,
                    int(response_day.strftime("%Y%m%d")),
                    ticket[3],
                    None if missing else rng.randint(1, 5),
                    SUPPORT_RESPONSE_CHANNELS[
                        rng.randrange(len(SUPPORT_RESPONSE_CHANNELS))
                    ],
                    # The escalation-reason field never arrived from the survey
                    # export: it is NULL for every single response on purpose.
                    None,
                    VERBATIM_COMMENTS[rng.randrange(len(VERBATIM_COMMENTS))],
                )
            )
        connection.executemany(
            "INSERT INTO fact_csat_response VALUES (?, ?, ?, ?, ?, ?, ?, ?)", csat_rows
        )
        connection.commit()
        counts = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in SUPPORT_TABLES
        }
    finally:
        connection.close()
    stats = {
        "csat_responses": len(csat_rows),
        "csat_missing_scores": sum(1 for row in csat_rows if row[4] is None),
    }
    stats["csat_missing_pct"] = int(
        round(100 * stats["csat_missing_scores"] / max(stats["csat_responses"], 1))
    )
    return counts, stats


RETAIL_SEMANTIC_MODEL = """version: 1
name: retail_orders
description: Governed semantics for a synthetic multi-store retail order dataset.
entities:
- name: calendar
  table: dim_date
  description: Conformed retail calendar shared by orders and customer signups.
  entity_type: dimension
  synonyms: [date, calendar, day]
  primary_key: [date_key]
  grain: [date_key]
  expected_columns: [date_key, full_date, year, quarter, month_number, month_name, week_of_year,
    day_of_week, is_weekend, is_holiday_season]
  dimensions:
  - name: date
    column: full_date
    synonyms: [full date, order date]
  - name: year
    column: year
    synonyms: [calendar year]
  - name: quarter
    column: quarter
    synonyms: [calendar quarter]
  - name: month
    column: month_name
    synonyms: [calendar month]
  - name: month_number
    column: month_number
    synonyms: [month index]
  - name: week_of_year
    column: week_of_year
    synonyms: [week]
  - name: day_of_week
    column: day_of_week
    synonyms: [weekday]
  - name: weekend
    column: is_weekend
  - name: holiday_season
    column: is_holiday_season
- name: store
  table: dim_store
  description: Retail store with region, format, and physical attributes.
  entity_type: dimension
  synonyms: [store, shop, branch, location]
  primary_key: [store_id]
  grain: [store_id]
  expected_columns: [store_id, store_name, region, city, store_format, opened_year, floor_area_sqm]
  dimensions:
  - name: name
    column: store_name
    synonyms: [store name, shop name]
  - name: region
    column: region
    synonyms: [sales region]
  - name: city
    column: city
    synonyms: [town]
  - name: store_format
    column: store_format
    synonyms: [format, store type]
  - name: floor_area_sqm
    column: floor_area_sqm
- name: product
  table: dim_product
  description: Retail product catalog with merchandising attributes and cost.
  entity_type: dimension
  synonyms: [product, item, sku, merchandise]
  primary_key: [product_id]
  grain: [product_id]
  expected_columns: [product_id, product_name, category, subcategory, list_price_usd, unit_cost_usd,
    is_seasonal]
  dimensions:
  - name: name
    column: product_name
    synonyms: [product name]
  - name: category
    column: category
    synonyms: [product category]
  - name: subcategory
    column: subcategory
  - name: seasonal
    column: is_seasonal
  - name: list_price
    column: list_price_usd
    synonyms: [price]
- name: customer
  table: dim_customer
  description: Retail customer with loyalty tier and home region; contact data is withheld.
  entity_type: dimension
  synonyms: [customer, shopper, buyer]
  primary_key: [customer_id]
  grain: [customer_id]
  hidden_columns: [customer_name, customer_email]
  expected_columns: [customer_id, customer_name, customer_email, loyalty_tier, signup_date_key,
    home_region]
  dimensions:
  - name: loyalty_tier
    column: loyalty_tier
    synonyms: [tier, loyalty level]
  - name: home_region
    column: home_region
    synonyms: [customer region]
- name: order
  table: fact_order
  description: Retail order fact at one row per placed order.
  entity_type: fact
  sla: PT4H
  refresh_frequency: hourly
  synonyms: [order, purchase, transaction, sale]
  primary_key: [order_id]
  grain: [order_id]
  expected_columns: [order_id, order_date_key, store_id, customer_id, sales_channel, order_status,
    payment_method, item_count, gross_amount_usd, discount_usd, shipping_usd, net_amount_usd,
    returned_flag]
  dimensions:
  - name: sales_channel
    column: sales_channel
    synonyms: [channel, sales channel]
  - name: order_status
    column: order_status
    synonyms: [status]
  - name: payment_method
    column: payment_method
    synonyms: [payment type]
  - name: item_count
    column: item_count
  - name: returned
    column: returned_flag
    synonyms: [returned order]
- name: order_item
  table: fact_order_item
  description: Retail order line fact at product-within-order grain.
  entity_type: fact
  synonyms: [order line, order item, basket line]
  primary_key: [order_item_id]
  grain: [order_item_id]
  expected_columns: [order_item_id, order_id, product_id, quantity, unit_price_usd, discount_usd,
    net_amount_usd]
  dimensions:
  - name: quantity
    column: quantity
  - name: unit_price
    column: unit_price_usd
relationships:
- name: orders_to_date
  from: fact_order.order_date_key
  to: dim_date.date_key
  relationship_type: many_to_one
  cardinality_contract: {source: many, target: one, enforcement: physical_fk}
- name: orders_to_store
  from: fact_order.store_id
  to: dim_store.store_id
  relationship_type: many_to_one
  cardinality_contract: {source: many, target: one, enforcement: physical_fk}
- name: orders_to_customer
  from: fact_order.customer_id
  to: dim_customer.customer_id
  relationship_type: many_to_one
  cardinality_contract: {source: many, target: one, enforcement: physical_fk}
- name: order_items_to_order
  from: fact_order_item.order_id
  to: fact_order.order_id
  relationship_type: many_to_one
  cardinality_contract: {source: many, target: one, enforcement: physical_fk}
- name: order_items_to_product
  from: fact_order_item.product_id
  to: dim_product.product_id
  relationship_type: many_to_one
  cardinality_contract: {source: many, target: one, enforcement: physical_fk}
- name: customers_to_signup_date
  from: dim_customer.signup_date_key
  to: dim_date.date_key
  relationship_type: many_to_one
  cardinality_contract: {source: many, target: one, enforcement: physical_fk}
join_paths:
- name: order_items_to_date_via_order
  from_entity: order_item
  to_entity: calendar
  relationships: [order_items_to_order, orders_to_date]
  description: Safe path from order lines to the order calendar.
- name: order_items_to_store_via_order
  from_entity: order_item
  to_entity: store
  relationships: [order_items_to_order, orders_to_store]
  description: Safe path from order lines to the fulfilling store.
- name: order_items_to_customer_via_order
  from_entity: order_item
  to_entity: customer
  relationships: [order_items_to_order, orders_to_customer]
  description: Safe path from order lines to the purchasing customer.
metrics:
- name: net_revenue
  description: Net order value after line discounts and including shipping.
  entity: order
  aggregation: sum
  expression: SUM(fact_order.net_amount_usd)
  synonyms: [net revenue, revenue, net sales, sales]
  allowed_dimensions: [order.sales_channel, order.order_status, order.payment_method, store.name,
    store.region, store.city, store.store_format, customer.loyalty_tier, customer.home_region,
    calendar.year, calendar.quarter, calendar.month, calendar.month_number]
  time_field: fact_order.order_date_key
- name: order_count
  description: Count of distinct retail orders.
  entity: order
  aggregation: count
  expression: COUNT(DISTINCT fact_order.order_id)
  synonyms: [order count, orders, number of orders]
  allowed_dimensions: [order.sales_channel, order.order_status, order.payment_method, store.name,
    store.region, store.city, store.store_format, customer.loyalty_tier, customer.home_region,
    calendar.year, calendar.quarter, calendar.month, calendar.month_number]
  time_field: fact_order.order_date_key
- name: average_order_value
  description: Net order value divided by the number of orders.
  entity: order
  aggregation: ratio
  expression: CAST(SUM(fact_order.net_amount_usd) AS REAL) / NULLIF(COUNT(DISTINCT fact_order.order_id),
    0)
  synonyms: [average order value, AOV, average basket]
  allowed_dimensions: [order.sales_channel, store.region, store.store_format, customer.loyalty_tier,
    calendar.year, calendar.month]
  time_field: fact_order.order_date_key
- name: discount_amount
  description: Total discount granted on retail orders.
  entity: order
  aggregation: sum
  expression: SUM(fact_order.discount_usd)
  synonyms: [discount amount, discounts, markdown]
  allowed_dimensions: [order.sales_channel, order.order_status, store.region, store.store_format,
    calendar.year, calendar.month]
  time_field: fact_order.order_date_key
- name: returned_order_rate
  description: Returned orders divided by all placed orders.
  entity: order
  aggregation: ratio
  expression: CAST(SUM(CASE WHEN fact_order.returned_flag = 1 THEN 1 ELSE 0 END) AS REAL) / NULLIF(COUNT(*),
    0)
  synonyms: [returned order rate, return rate, returns]
  allowed_dimensions: [order.sales_channel, store.region, store.store_format, customer.loyalty_tier,
    calendar.year, calendar.month]
  time_field: fact_order.order_date_key
- name: units_sold
  description: Total product units sold at order-line grain.
  entity: order_item
  aggregation: sum
  expression: SUM(fact_order_item.quantity)
  synonyms: [units sold, unit sales, quantity sold]
  allowed_dimensions: [product.name, product.category, product.subcategory, store.region, store.store_format,
    calendar.year, calendar.month, calendar.month_number]
- name: order_item_revenue
  description: Net revenue recognised at order-line grain.
  entity: order_item
  aggregation: sum
  expression: SUM(fact_order_item.net_amount_usd)
  synonyms: [line revenue, order item revenue]
  allowed_dimensions: [product.name, product.category, product.subcategory, store.region, store.store_format,
    calendar.year, calendar.month, calendar.month_number]
- name: average_unit_price
  description: Revenue-weighted average price per unit sold.
  entity: order_item
  aggregation: ratio
  expression: CAST(SUM(fact_order_item.unit_price_usd * fact_order_item.quantity) AS REAL) / NULLIF(SUM(fact_order_item.quantity),
    0)
  synonyms: [average unit price, average price, price per unit]
  allowed_dimensions: [product.category, product.subcategory, store.region, calendar.month]
"""

RETAIL_SQL_POLICY = """version: 1
name: retail_orders_analyst

allowed_tables:
  - dim_date
  - dim_store
  - dim_product
  - dim_customer
  - fact_order
  - fact_order_item

# Customer contact data (name and e-mail) is withheld while every analytical
# customer attribute (loyalty tier, home region, signup date) stays available.
allowed_columns:
  dim_customer:
    - customer_id
    - loyalty_tier
    - signup_date_key
    - home_region

dangerous_functions: [randomblob]
require_limit: false
max_limit: 1000
max_tables: 8
max_joins: 7
allow_cross_join: false
"""

SUPPORT_SEMANTIC_MODEL = """version: 1
name: support_tickets
description: Governed semantics for a synthetic customer support operations dataset.
entities:
- name: calendar
  table: dim_date
  description: Conformed support calendar shared by tickets and survey responses.
  entity_type: dimension
  synonyms: [date, calendar, day]
  primary_key: [date_key]
  grain: [date_key]
  expected_columns: [date_key, full_date, year, quarter, month_number, month_name, week_of_year,
    day_of_week, is_weekend]
  dimensions:
  - name: date
    column: full_date
    synonyms: [full date, ticket date]
  - name: year
    column: year
    synonyms: [calendar year]
  - name: quarter
    column: quarter
    synonyms: [calendar quarter]
  - name: month
    column: month_name
    synonyms: [calendar month]
  - name: month_number
    column: month_number
    synonyms: [month index]
  - name: week_of_year
    column: week_of_year
    synonyms: [week]
  - name: day_of_week
    column: day_of_week
    synonyms: [weekday]
  - name: weekend
    column: is_weekend
- name: agent
  table: dim_agent
  description: Support agent with team, region, and seniority attributes.
  entity_type: dimension
  synonyms: [agent, advisor, representative]
  primary_key: [agent_id]
  grain: [agent_id]
  expected_columns: [agent_id, agent_name, team, region, seniority, active_flag, hire_year]
  dimensions:
  - name: name
    column: agent_name
    synonyms: [agent name]
  - name: team
    column: team
    synonyms: [support team, queue team]
  - name: region
    column: region
    synonyms: [agent region]
  - name: seniority
    column: seniority
  - name: active
    column: active_flag
- name: queue
  table: dim_queue
  description: Support queue with product area, channel, and target service level.
  entity_type: dimension
  synonyms: [queue, inbox, channel group]
  primary_key: [queue_id]
  grain: [queue_id]
  expected_columns: [queue_id, queue_name, product_area, channel_type, priority_class, sla_hours]
  dimensions:
  - name: name
    column: queue_name
    synonyms: [queue name]
  - name: product_area
    column: product_area
    synonyms: [product, area]
  - name: channel_type
    column: channel_type
    synonyms: [channel, contact channel]
  - name: priority_class
    column: priority_class
    synonyms: [queue priority]
  - name: sla_hours
    column: sla_hours
- name: ticket
  table: fact_ticket
  description: Support ticket fact at one row per created ticket.
  entity_type: fact
  sla: PT2H
  refresh_frequency: hourly
  synonyms: [ticket, case, contact, issue]
  primary_key: [ticket_id]
  grain: [ticket_id]
  expected_columns: [ticket_id, created_date_key, queue_id, agent_id, priority, status, first_response_minutes,
    handle_minutes, resolution_minutes, reopened_flag, escalated_flag, satisfaction_sent_flag]
  dimensions:
  - name: priority
    column: priority
    synonyms: [ticket priority, severity]
  - name: status
    column: status
    synonyms: [ticket status]
  - name: reopened
    column: reopened_flag
    synonyms: [reopened ticket]
  - name: escalated
    column: escalated_flag
    synonyms: [escalated ticket]
  - name: satisfaction_sent
    column: satisfaction_sent_flag
- name: sla_breach
  table: fact_ticket_sla_breach
  description: Recorded service-level breaches; no breach has been recorded yet, so this fact
    is intentionally empty.
  entity_type: fact
  synonyms: [sla breach, breach, missed target]
  primary_key: [breach_id]
  grain: [breach_id]
  expected_columns: [breach_id, ticket_id, breach_date_key, breached_target_minutes, breach_minutes]
  dimensions:
  - name: breached_target_minutes
    column: breached_target_minutes
  - name: breach_minutes
    column: breach_minutes
- name: csat_response
  table: fact_csat_response
  description: Customer satisfaction survey response; free-text verbatims are withheld, the
    escalation reason is entirely missing, and many responses never received a score.
  entity_type: fact
  synonyms: [csat response, survey response, satisfaction survey]
  primary_key: [response_id]
  grain: [response_id]
  hidden_columns: [verbatim_comment]
  quality_rules:
  - rule: null_rate
    column: escalation_reason
    max_null_rate: 0.0
    severity: error
  expected_columns: [response_id, ticket_id, response_date_key, agent_id, csat_score, response_channel,
    escalation_reason, verbatim_comment]
  dimensions:
  - name: response_channel
    column: response_channel
    synonyms: [survey channel]
  - name: score
    column: csat_score
    synonyms: [csat score, satisfaction score]
relationships:
- name: tickets_to_date
  from: fact_ticket.created_date_key
  to: dim_date.date_key
  relationship_type: many_to_one
  cardinality_contract: {source: many, target: one, enforcement: physical_fk}
- name: tickets_to_queue
  from: fact_ticket.queue_id
  to: dim_queue.queue_id
  relationship_type: many_to_one
  cardinality_contract: {source: many, target: one, enforcement: physical_fk}
- name: tickets_to_agent
  from: fact_ticket.agent_id
  to: dim_agent.agent_id
  relationship_type: many_to_one
  cardinality_contract: {source: many, target: one, enforcement: physical_fk}
- name: breaches_to_ticket
  from: fact_ticket_sla_breach.ticket_id
  to: fact_ticket.ticket_id
  relationship_type: many_to_one
  cardinality_contract: {source: many, target: one, enforcement: physical_fk}
- name: breaches_to_date
  from: fact_ticket_sla_breach.breach_date_key
  to: dim_date.date_key
  relationship_type: many_to_one
  cardinality_contract: {source: many, target: one, enforcement: physical_fk}
- name: csat_to_ticket
  from: fact_csat_response.ticket_id
  to: fact_ticket.ticket_id
  relationship_type: many_to_one
  cardinality_contract: {source: many, target: one, enforcement: physical_fk}
- name: csat_to_agent
  from: fact_csat_response.agent_id
  to: dim_agent.agent_id
  relationship_type: many_to_one
  cardinality_contract: {source: many, target: one, enforcement: physical_fk}
- name: csat_to_date
  from: fact_csat_response.response_date_key
  to: dim_date.date_key
  relationship_type: many_to_one
  cardinality_contract: {source: many, target: one, enforcement: physical_fk}
join_paths:
- name: breaches_to_queue_via_ticket
  from_entity: sla_breach
  to_entity: queue
  relationships: [breaches_to_ticket, tickets_to_queue]
  description: Safe path from a breach to the queue that owned the ticket.
- name: breaches_to_agent_via_ticket
  from_entity: sla_breach
  to_entity: agent
  relationships: [breaches_to_ticket, tickets_to_agent]
  description: Safe path from a breach to the agent that owned the ticket.
- name: csat_to_queue_via_ticket
  from_entity: csat_response
  to_entity: queue
  relationships: [csat_to_ticket, tickets_to_queue]
  description: Safe path from a survey response to the handling queue.
metrics:
- name: ticket_volume
  description: Count of distinct support tickets created.
  entity: ticket
  aggregation: count
  expression: COUNT(DISTINCT fact_ticket.ticket_id)
  synonyms: [ticket volume, tickets, contact volume, case volume]
  allowed_dimensions: [ticket.priority, ticket.status, ticket.reopened, ticket.escalated, queue.name,
    queue.product_area, queue.channel_type, queue.priority_class, agent.name, agent.team, agent.region,
    agent.seniority, calendar.year, calendar.quarter, calendar.month, calendar.month_number, calendar.week_of_year]
  time_field: fact_ticket.created_date_key
- name: average_first_response_minutes
  description: Mean minutes from ticket creation to the first agent response.
  entity: ticket
  aggregation: ratio
  expression: CAST(SUM(fact_ticket.first_response_minutes) AS REAL) / NULLIF(COUNT(*), 0)
  synonyms: [first response time, first reply minutes, average first response]
  allowed_dimensions: [ticket.priority, queue.name, queue.channel_type, agent.team, agent.region,
    agent.seniority, calendar.year, calendar.month]
  time_field: fact_ticket.created_date_key
- name: average_handle_minutes
  description: Mean agent handling minutes per ticket.
  entity: ticket
  aggregation: ratio
  expression: CAST(SUM(fact_ticket.handle_minutes) AS REAL) / NULLIF(COUNT(*), 0)
  synonyms: [handle time, handling minutes]
  allowed_dimensions: [ticket.priority, queue.name, queue.channel_type, agent.team, agent.seniority,
    calendar.year, calendar.month]
  time_field: fact_ticket.created_date_key
- name: reopened_ticket_rate
  description: Reopened tickets divided by all created tickets.
  entity: ticket
  aggregation: ratio
  expression: CAST(SUM(CASE WHEN fact_ticket.reopened_flag = 1 THEN 1 ELSE 0 END) AS REAL) / NULLIF(COUNT(*),
    0)
  synonyms: [reopened ticket rate, reopen rate, repeat contact rate]
  allowed_dimensions: [ticket.priority, queue.name, queue.product_area, agent.team, agent.region,
    calendar.year, calendar.month]
  time_field: fact_ticket.created_date_key
- name: escalation_rate
  description: Escalated tickets divided by all created tickets.
  entity: ticket
  aggregation: ratio
  expression: CAST(SUM(CASE WHEN fact_ticket.escalated_flag = 1 THEN 1 ELSE 0 END) AS REAL) / NULLIF(COUNT(*),
    0)
  synonyms: [escalation rate, escalated share]
  allowed_dimensions: [ticket.priority, queue.name, queue.product_area, agent.team, agent.region,
    calendar.year, calendar.month]
  time_field: fact_ticket.created_date_key
- name: sla_breach_count
  description: Count of recorded service-level breaches.
  entity: sla_breach
  aggregation: count
  expression: COUNT(DISTINCT fact_ticket_sla_breach.breach_id)
  synonyms: [sla breach count, breaches, missed service level]
  allowed_dimensions: [sla_breach.breached_target_minutes, queue.name, queue.product_area, agent.team,
    calendar.year, calendar.month, calendar.month_number]
  time_field: fact_ticket_sla_breach.breach_date_key
- name: csat_response_count
  description: Count of distinct customer satisfaction responses.
  entity: csat_response
  aggregation: count
  expression: COUNT(DISTINCT fact_csat_response.response_id)
  synonyms: [csat response count, survey responses, responses]
  allowed_dimensions: [csat_response.response_channel, agent.team, agent.region, calendar.year,
    calendar.month, calendar.month_number]
  time_field: fact_csat_response.response_date_key
- name: average_csat_score
  description: Mean submitted customer satisfaction score on a one-to-five scale.
  entity: csat_response
  aggregation: ratio
  expression: CAST(SUM(fact_csat_response.csat_score) AS REAL) / NULLIF(COUNT(fact_csat_response.csat_score),
    0)
  synonyms: [average csat score, csat, customer satisfaction score, satisfaction]
  allowed_dimensions: [csat_response.response_channel, agent.team, agent.region, calendar.year,
    calendar.month, calendar.month_number]
  time_field: fact_csat_response.response_date_key
"""

SUPPORT_SQL_POLICY = """version: 1
name: support_tickets_analyst

allowed_tables:
  - dim_date
  - dim_agent
  - dim_queue
  - fact_ticket
  - fact_ticket_sla_breach
  - fact_csat_response

# Free-text survey verbatims stay out of analytical reach; the structured survey
# fields (score, channel, dates) remain available.
allowed_columns:
  fact_csat_response:
    - response_id
    - ticket_id
    - response_date_key
    - agent_id
    - csat_score
    - response_channel
    - escalation_reason

dangerous_functions: [randomblob]
require_limit: false
max_limit: 1000
max_tables: 8
max_joins: 7
allow_cross_join: false
"""

RETAIL_README = """# Retail Orders Analytics Sample

This directory contains a fully synthetic, deterministic retail order dataset for
a multi-store retailer. No row is copied from a real retailer, catalog, customer
base, or third-party benchmark.

## Scale

The generated SQLite database contains **{total:,} rows across {table_count} tables**:

| Table | Grain | Rows |
| --- | --- | --- |
{table_rows}

`fact_order` carries the order calendar (`order_date_key`), the fulfilling store,
the purchasing customer, the sales channel, and the order amounts; `fact_order_item`
holds one row per product within an order. `dim_customer` keeps contact data
(`customer_name`, `customer_email`) physically present but **outside the SQL
policy allowlist**, so an analytical request for it must be rejected rather than
answered.

Outlet-format stores only started trading on 2024-11-01, so the Outlet format has
no order rows at all in Q1/2024 — a real empty slice for the benchmark.

## Files

- `retail_orders.sqlite` — ready-to-query database;
- [semantic_model.yml](semantic_model.yml) — entities, dimensions, metrics, and safe Join Paths;
- [sql_policy.yml](sql_policy.yml) — table/column scope and query-shape budgets;
- [README.md](README.md) — this file.

Regenerate the database, semantic model, policy, and README from repository root
(with a fixed seed; regeneration is idempotent byte-for-byte):

```bash
python sample/generate_aux_datasets.py
```

The generator lives in [sample/generate_aux_datasets.py](../../sample/generate_aux_datasets.py).

Example:

```bash
python main.py \\
  --database sample_data/retail_orders/retail_orders.sqlite \\
  --semantic-model sample_data/retail_orders/semantic_model.yml \\
  --sql-policy sample_data/retail_orders/sql_policy.yml \\
  --question "What is net revenue by store region in 2024?"
```
"""

SUPPORT_README = """# Support Tickets Analytics Sample

This directory contains a fully synthetic, deterministic customer support
operations dataset. No row is copied from a real support desk, agent roster,
customer base, or third-party benchmark.

## Scale

The generated SQLite database contains **{total:,} rows across {table_count} tables**:

| Table | Grain | Rows |
| --- | --- | --- |
{table_rows}

Two properties of this dataset are deliberate, because the agent benchmark needs
them:

- **`fact_ticket_sla_breach` is intentionally empty.** No service-level breach has
  been recorded, so any dimensioned breach question returns no rows at all and the
  honest answer is "no breach records exist" (`empty_result`).
- **`fact_csat_response` fails its quality contract.** `escalation_reason` never
  arrived from the survey export and is NULL for every response (a `null_rate`
  error for the governed check), and `csat_score` is missing for
  {csat_missing_pct}% of responses on top of that. A question that depends on the
  response table must stop at the data fault instead of reporting a confident
  average (`data_fault`).

The `verbatim_comment` column is physically present but **outside the SQL policy
allowlist**, so a request for survey free text must be rejected rather than
answered. `fact_ticket` itself is fully populated, so ticket-level metrics are not
affected by either trap.

## Files

- `support_tickets.sqlite` — ready-to-query database;
- [semantic_model.yml](semantic_model.yml) — entities, dimensions, metrics, and safe Join Paths;
- [sql_policy.yml](sql_policy.yml) — table/column scope and query-shape budgets;
- [README.md](README.md) — this file.

Regenerate the database, semantic model, policy, and README from repository root
(with a fixed seed; regeneration is idempotent byte-for-byte):

```bash
python sample/generate_aux_datasets.py
```

The generator lives in [sample/generate_aux_datasets.py](../../sample/generate_aux_datasets.py).

Example:

```bash
python main.py \\
  --database sample_data/support_tickets/support_tickets.sqlite \\
  --semantic-model sample_data/support_tickets/semantic_model.yml \\
  --sql-policy sample_data/support_tickets/sql_policy.yml \\
  --question "How many tickets were created by queue in 2024?"
```
"""


def _table_rows_markdown(counts: dict[str, int], grains: dict[str, str]) -> str:
    return "\n".join(
        f"| `{table}` | {grains[table]} | {counts[table]:,} |" for table in counts
    )


RETAIL_GRAINS = {
    "dim_date": "calendar day",
    "dim_store": "store",
    "dim_product": "product",
    "dim_customer": "customer",
    "fact_order": "order",
    "fact_order_item": "order line",
}

SUPPORT_GRAINS = {
    "dim_date": "calendar day",
    "dim_agent": "agent",
    "dim_queue": "queue",
    "fact_ticket": "ticket",
    "fact_ticket_sla_breach": "recorded breach (none yet)",
    "fact_csat_response": "survey response",
}


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def generate(output_root: Path) -> dict[str, dict[str, int]]:
    """Generate both auxiliary datasets under ``output_root``."""
    retail_root = output_root / RETAIL_DATASET
    support_root = output_root / SUPPORT_DATASET

    retail_counts = build_retail_database(retail_root / f"{RETAIL_DATASET}.sqlite")
    support_counts, support_stats = build_support_database(
        support_root / f"{SUPPORT_DATASET}.sqlite"
    )

    _write_text(retail_root / "semantic_model.yml", RETAIL_SEMANTIC_MODEL)
    _write_text(retail_root / "sql_policy.yml", RETAIL_SQL_POLICY)
    _write_text(
        retail_root / "README.md",
        RETAIL_README.format(
            total=sum(retail_counts.values()),
            table_count=len(retail_counts),
            table_rows=_table_rows_markdown(retail_counts, RETAIL_GRAINS),
        ),
    )
    _write_text(support_root / "semantic_model.yml", SUPPORT_SEMANTIC_MODEL)
    _write_text(support_root / "sql_policy.yml", SUPPORT_SQL_POLICY)
    _write_text(
        support_root / "README.md",
        SUPPORT_README.format(
            total=sum(support_counts.values()),
            table_count=len(support_counts),
            table_rows=_table_rows_markdown(support_counts, SUPPORT_GRAINS),
            csat_missing_pct=support_stats["csat_missing_pct"],
        ),
    )
    return {RETAIL_DATASET: retail_counts, SUPPORT_DATASET: support_counts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help="directory that receives sample_data/<dataset_id>/ (default: repo sample_data)",
    )
    arguments = parser.parse_args(argv)
    output_root = Path(arguments.output_root).expanduser()
    counts = generate(output_root)
    print(f"Generated auxiliary datasets under {output_root}")
    for dataset_id in (RETAIL_DATASET, SUPPORT_DATASET):
        tables = counts[dataset_id]
        print(f"- {dataset_id}: {sum(tables.values()):,} rows across {len(tables)} tables")
        for table in tables:
            print(f"  - {table}: {tables[table]:,} rows")
    print(
        "Datasets: "
        + ", ".join(
            str(output_root / dataset_id / f"{dataset_id}.sqlite")
            for dataset_id in (RETAIL_DATASET, SUPPORT_DATASET)
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
