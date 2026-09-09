import sqlite3
from datetime import datetime

DATABASE = "prospectr.db"


def get_connection():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sales (
            id TEXT PRIMARY KEY,
            title TEXT,
            sale_date TEXT,
            price_usd REAL,
            price_cad REAL,
            platform TEXT,
            listing_type TEXT,
            listing_url TEXT,
            image_url TEXT,
            thumbnail_url TEXT,
            player TEXT,
            year INTEGER,
            set_name TEXT,
            card_type TEXT,
            parallel TEXT,
            grade TEXT,
            created_at TEXT
        )
    """)

    # Upgrade databases created by older versions
    columns = [
        row[1]
        for row in cursor.execute(
            "PRAGMA table_info(sales)"
        ).fetchall()
    ]

    if "image_url" not in columns:
        cursor.execute(
            "ALTER TABLE sales ADD COLUMN image_url TEXT"
        )

    if "thumbnail_url" not in columns:
        cursor.execute(
            "ALTER TABLE sales ADD COLUMN thumbnail_url TEXT"
        )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_key TEXT,
            snapshot_date TEXT,
            sales_count INTEGER,
            low_price_cad REAL,
            high_price_cad REAL,
            average_price_cad REAL,
            median_price_cad REAL,
            volatility_percent REAL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS player_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player TEXT,
            stat_date TEXT,
            season INTEGER,
            stats_json TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_key TEXT,
            forecast_date TEXT,
            horizon_months INTEGER,
            bear_value_cad REAL,
            base_value_cad REAL,
            bull_value_cad REAL,
            confidence REAL
        )
    """)

    connection.commit()
    connection.close()


def save_sales(
    sales,
    player,
    year,
    set_name,
    card_type,
    parallel,
    grade
):
    connection = get_connection()
    cursor = connection.cursor()

    for sale in sales:

        sale_id = sale.get("id")

        if not sale_id:
            sale_id = (
                f"{player}|{year}|{set_name}|"
                f"{card_type}|{parallel}|{grade}|"
                f"{sale.get('sale_date')}|"
                f"{sale.get('price_usd')}|"
                f"{sale.get('title')}"
            )

        cursor.execute("""
            INSERT OR REPLACE INTO sales (
                id,
                title,
                sale_date,
                price_usd,
                price_cad,
                platform,
                listing_type,
                listing_url,
                image_url,
                thumbnail_url,
                player,
                year,
                set_name,
                card_type,
                parallel,
                grade,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(sale_id),
            sale.get("title"),
            sale.get("sale_date"),
            sale.get("price_usd"),
            sale.get("price_cad"),
            sale.get("platform"),
            sale.get("listing_type"),
            sale.get("listing_url"),
            sale.get("image_url"),
            sale.get("thumbnail_url"),
            player,
            year,
            set_name,
            card_type,
            parallel,
            grade,
            datetime.utcnow().isoformat()
        ))

    connection.commit()
    connection.close()


def get_card_sales(
    player,
    year,
    set_name,
    card_type,
    parallel,
    grade
):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT *
        FROM sales
        WHERE player = ?
          AND year = ?
          AND set_name = ?
          AND card_type = ?
          AND COALESCE(parallel, '') = COALESCE(?, '')
          AND grade = ?
        ORDER BY sale_date DESC
    """, (
        player,
        year,
        set_name,
        card_type,
        parallel,
        grade
    ))

    rows = cursor.fetchall()

    connection.close()

    return [dict(row) for row in rows]


def get_recent_sales(limit=12):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT *
        FROM sales
        WHERE price_cad IS NOT NULL
        ORDER BY sale_date DESC, created_at DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()

    connection.close()

    return [dict(row) for row in rows]