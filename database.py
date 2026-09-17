import hashlib
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


# Local development keeps data beside the app. A managed host can set
# CARDR_DATA_DIR (for example, to a mounted persistent disk) so SQLite data
# survives deployments and restarts.
DEFAULT_DATA_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("CARDR_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser()
DATABASE = DATA_DIR / "prospectr.db"


def get_connection():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(DATABASE),
        timeout=15
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 15000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize_database():
    with get_connection() as connection:
        cursor = connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sales (
                id TEXT PRIMARY KEY,
                title TEXT,
                sale_date TEXT,

                price_usd REAL,
                price_cad REAL,
                price_original REAL,
                currency TEXT,

                price_confirmed INTEGER,

                platform TEXT,
                listing_type TEXT,
                listing_url TEXT,

                image_url TEXT,
                thumbnail_url TEXT,

                bids INTEGER,

                cert TEXT,
                condition TEXT,
                grader TEXT,
                grading_company TEXT,

                player TEXT,
                year INTEGER,
                set_name TEXT,
                card_type TEXT,
                card_number TEXT,
                parallel TEXT,
                grade TEXT,

                is_autograph INTEGER DEFAULT 0,
                autograph_type TEXT,

                match_confidence TEXT DEFAULT 'unknown',

                created_at TEXT
            )
        """)

        existing_columns = {
            row[1]
            for row in cursor.execute(
                "PRAGMA table_info(sales)"
            ).fetchall()
        }

        migrations = {
            "price_original": "REAL",
            "currency": "TEXT",
            "price_confirmed": "INTEGER",
            "bids": "INTEGER",
            "cert": "TEXT",
            "condition": "TEXT",
            "grader": "TEXT",
            "grading_company": "TEXT",
            "is_autograph": "INTEGER DEFAULT 0",
            "autograph_type": "TEXT",
            "match_confidence": "TEXT DEFAULT 'unknown'",
            # Source-aware identity fields make historical imports auditable
            # instead of relying solely on a query label or title parsing.
            "card_number": "TEXT",
            "source_sale_id": "TEXT",
            "source_currency": "TEXT",
            "fx_rate_to_cad": "REAL",
            "identity_status": "TEXT DEFAULT 'legacy_unverified'",
        }

        for column, definition in migrations.items():

            if column not in existing_columns:

                cursor.execute(
                    f"""
                    ALTER TABLE sales
                    ADD COLUMN {column} {definition}
                    """
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

        # Portfolio history starts collecting from the moment CARDR is
        # upgraded.  The app never backfills or fabricates earlier values.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_date TEXT NOT NULL UNIQUE,
                total_value_cad REAL NOT NULL,
                total_invested_cad REAL NOT NULL,
                profit_loss_cad REAL NOT NULL,
                card_count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sales_card
            ON sales(
                player,
                year,
                set_name,
                card_type,
                card_number,
                parallel,
                grade
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sales_date
            ON sales(sale_date)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sales_identity
            ON sales(player, year, set_name, card_number, parallel, grade)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_date
            ON portfolio_snapshots(snapshot_date)
        """)

        connection.commit()


def safe_float(value):

    try:
        number = float(value)

        if not math.isfinite(number) or number <= 0:
            return None

        return number

    except (TypeError, ValueError):

        return None


def optional_bool(value):
    """Preserve unknown provider flags without treating the string 'false' as true."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)) and value in {0, 1}:
        return int(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return 1
        if normalized in {"false", "0", "no", "off", ""}:
            return 0
    return None


def generate_sale_id(
    sale,
    player,
    year,
    set_name,
    card_type,
    parallel,
    grade
):

    real_id = sale.get("id")

    if real_id:
        return str(real_id)

    raw = "|".join([
        str(player or ""),
        str(year or ""),
        str(set_name or ""),
        str(card_type or ""),
        str(parallel or ""),
        str(grade or ""),
        str(sale.get("sale_date") or ""),
        str(
            sale.get("price_original")
            or sale.get("price_usd")
            or ""
        ),
        str(sale.get("title") or ""),
        str(sale.get("listing_url") or "")
    ])

    digest = hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()

    return "generated-" + digest


def save_sales(
    sales,
    player,
    year,
    set_name,
    card_type,
    parallel,
    grade
):

    initialize_database()

    now = datetime.now(
        timezone.utc
    ).isoformat()

    with get_connection() as connection:

        cursor = connection.cursor()

        for sale in sales or []:

            sale_id = generate_sale_id(
                sale,
                player,
                year,
                set_name,
                card_type,
                parallel,
                grade
            )

            price_original = safe_float(
                sale.get("price_original")
                if sale.get("price_original") is not None
                else sale.get("price_usd")
            )

            price_usd = safe_float(
                sale.get("price_usd")
            )

            price_cad = safe_float(
                sale.get("price_cad")
            )

            # Never store a sale with no usable price.
            if (
                price_original is None
                and price_cad is None
            ):
                continue

            confirmed = optional_bool(sale.get("price_confirmed"))
            is_autograph = optional_bool(sale.get("is_autograph")) or 0
            # Prefer fields carried by the market provider.  Query values are
            # used only as a verified fallback after ebay.score_sale() has
            # accepted the listing, which prevents a broad search from
            # relabelling every returned row as the same card.
            stored_player = str(sale.get("source_player") or player or "").strip()
            stored_year = str(sale.get("source_year") or year or "").strip()
            stored_set_name = str(sale.get("source_set_name") or set_name or "").strip()
            stored_card_type = str(sale.get("source_card_type") or card_type or "").strip()
            stored_parallel = str(sale.get("source_parallel") or parallel or "").strip()

            cursor.execute("""
                INSERT INTO sales (
                    id,
                    title,
                    sale_date,

                    price_usd,
                    price_cad,
                    price_original,
                    currency,

                    price_confirmed,

                    platform,
                    listing_type,
                    listing_url,

                    image_url,
                    thumbnail_url,

                    bids,

                    cert,
                    condition,
                    grader,
                    grading_company,

                    player,
                    year,
                    set_name,
                    card_type,
                    card_number,
                    parallel,
                    grade,
                    source_sale_id,
                    source_currency,
                    fx_rate_to_cad,
                    identity_status,

                    is_autograph,
                    autograph_type,

                    match_confidence,

                    created_at
                )

                VALUES (
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?,
                    ?, ?, ?,
                    ?, ?,
                    ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?,
                    ?,
                    ?
                )

                ON CONFLICT(id)
                DO UPDATE SET

                    title = excluded.title,
                    sale_date = excluded.sale_date,

                    price_usd = excluded.price_usd,
                    price_cad = excluded.price_cad,
                    price_original = excluded.price_original,
                    currency = excluded.currency,

                    price_confirmed =
                        excluded.price_confirmed,

                    platform = excluded.platform,
                    listing_type = excluded.listing_type,
                    listing_url = excluded.listing_url,

                    image_url =
                        COALESCE(
                            excluded.image_url,
                            sales.image_url
                        ),

                    thumbnail_url =
                        COALESCE(
                            excluded.thumbnail_url,
                            sales.thumbnail_url
                        ),

                    bids = excluded.bids,

                    cert = excluded.cert,
                    condition = excluded.condition,
                    grader = excluded.grader,
                    grading_company =
                        excluded.grading_company,

                    card_number = COALESCE(excluded.card_number, sales.card_number),

                    source_sale_id = COALESCE(excluded.source_sale_id, sales.source_sale_id),
                    source_currency = COALESCE(excluded.source_currency, sales.source_currency),
                    fx_rate_to_cad = COALESCE(excluded.fx_rate_to_cad, sales.fx_rate_to_cad),
                    identity_status = excluded.identity_status,

                    is_autograph =
                        excluded.is_autograph,

                    autograph_type =
                        excluded.autograph_type,

                    match_confidence =
                        excluded.match_confidence
            """, (

                sale_id,
                sale.get("title"),
                sale.get("sale_date"),

                price_usd,
                price_cad,
                price_original,
                sale.get("currency"),

                confirmed,

                sale.get("platform"),
                sale.get("listing_type"),
                sale.get("listing_url"),

                sale.get("image_url"),
                sale.get("thumbnail_url"),

                sale.get("bids"),

                sale.get("cert"),
                sale.get("condition"),
                sale.get("grader"),
                sale.get("grading_company"),

                stored_player,
                stored_year,
                stored_set_name,
                stored_card_type,
                sale.get("card_number") or None,
                stored_parallel,
                sale.get("grade") or grade,
                sale.get("source_sale_id") or sale_id,
                sale.get("source_currency") or sale.get("currency"),
                safe_float(sale.get("fx_rate_to_cad")),
                sale.get("identity_status") or "query_verified",

                is_autograph,
                sale.get("autograph_type"),

                sale.get(
                    "match_confidence",
                    "unknown"
                ),

                now
            ))

        connection.commit()


def get_card_sales(
    player,
    year,
    set_name,
    card_type,
    parallel,
    grade
):

    initialize_database()

    with get_connection() as connection:

        rows = connection.execute("""
            SELECT *
            FROM sales

            WHERE player = ?
              AND year = ?
              AND set_name = ?
              AND card_type = ?

              AND COALESCE(
                    parallel,
                    ''
                  )
                  =
                  COALESCE(
                    ?,
                    ''
                  )

              AND COALESCE(
                    grade,
                    ''
                  )
                  =
                  COALESCE(
                    ?,
                    ''
                  )

              AND COALESCE(
                    match_confidence,
                    'unknown'
                  )
                  !=
                  'rejected'

            ORDER BY
                sale_date DESC,
                created_at DESC
        """, (
            player,
            year,
            set_name,
            card_type,
            parallel,
            grade
        )).fetchall()

    return [
        dict(row)
        for row in rows
    ]


def get_recent_sales(
    limit=12
):

    initialize_database()

    with get_connection() as connection:

        rows = connection.execute("""
            SELECT *
            FROM sales

            WHERE price_cad IS NOT NULL
              AND price_cad > 0

            ORDER BY
                sale_date DESC,
                created_at DESC

            LIMIT ?
        """, (
            limit,
        )).fetchall()

    return [
        dict(row)
        for row in rows
    ]
