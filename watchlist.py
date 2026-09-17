"""Private, evidence-aware Watchlist storage for CARDr.

The Watchlist intentionally stores a card identity rather than a copy of a
Vault card.  A user can therefore track a card (or a player-level interest)
without implying that they own it.  Any displayed market estimate is derived
fresh from the observed-sales repository and is never persisted as a made-up
watchlist value.
"""

import hashlib
import math
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import database
from database import get_connection, initialize_database
from market_data import SalesRepository, normalize_text
from valuation import CardQuery, calculate_valuation


BASE_DIR = Path(__file__).resolve().parent
MAX_TEXT_LENGTH = 500
MAX_NOTES_LENGTH = 4000
MAX_MONEY_CAD = 10_000_000
PRIORITY_LEVELS = {0, 1, 2}


def _owner_clause(owner_id: Optional[str]) -> tuple[str, List[Any]]:
    """Scope Watchlist queries to one authenticated collector or local guest."""

    if owner_id is None:
        return "owner_id IS NULL", []
    return "owner_id = ?", [owner_id]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any, field: str, maximum: int = MAX_TEXT_LENGTH) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) > maximum:
        raise ValueError("{} must be {} characters or fewer.".format(field, maximum))
    return text


def _optional_year(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        year = int(value)
    except (TypeError, ValueError):
        raise ValueError("Year must be a whole number.")
    if year < 1800 or year > date.today().year + 1:
        raise ValueError("Year must be between 1800 and {}.".format(date.today().year + 1))
    return year


def _optional_serial_total(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        serial_total = int(value)
    except (TypeError, ValueError):
        raise ValueError("Serial total must be a whole number.")
    if serial_total < 1 or serial_total > 1_000_000:
        raise ValueError("Serial total must be between 1 and 1,000,000.")
    return serial_total


def _optional_money(value: Any, field: str) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ValueError("{} must be a number.".format(field))
    if not math.isfinite(amount) or amount < 0 or amount > MAX_MONEY_CAD:
        raise ValueError("{} must be between 0 and ${:,.0f} CAD.".format(field, MAX_MONEY_CAD))
    return round(amount, 2)


def _optional_image_url(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    candidate = _clean_text(value, "Image URL", 2000)
    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return candidate
    if candidate.startswith("/uploads/") and "/" not in candidate[len("/uploads/"):]:
        return candidate
    raise ValueError("Image URL must be an HTTPS, HTTP, or CARDr upload URL.")


def _priority(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)) and value in PRIORITY_LEVELS:
        return int(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        aliases = {
            "false": 0, "normal": 0, "none": 0,
            "true": 1, "priority": 1, "favorite": 1,
            "high": 2, "high_priority": 2, "high-priority": 2,
        }
        if normalized in aliases:
            return aliases[normalized]
    raise ValueError("Priority must be normal (0), priority (1), or high (2).")


def _normalize_values(values: Dict[str, Any], *, require_player: bool) -> Dict[str, Any]:
    fields = {
        "player": ("Player", 160),
        "set_name": ("Set name", 200),
        "card_number": ("Card number", 80),
        "card_type": ("Card type", 160),
        "parallel": ("Parallel", 160),
        "grade": ("Grade", 80),
        "grading_company": ("Grading company", 80),
        "notes": ("Notes", MAX_NOTES_LENGTH),
    }
    clean: Dict[str, Any] = {}
    for key, (label, limit) in fields.items():
        if key in values:
            clean[key] = _clean_text(values[key], label, limit)
    if require_player and not clean.get("player"):
        raise ValueError("Player is required.")
    if "player" in clean and not clean["player"]:
        raise ValueError("Player is required.")
    if "year" in values:
        clean["year"] = _optional_year(values["year"])
    if "serial_total" in values:
        clean["serial_total"] = _optional_serial_total(values["serial_total"])
    if "target_price_cad" in values:
        clean["target_price_cad"] = _optional_money(values["target_price_cad"], "Target price")
    if "image_url" in values:
        clean["image_url"] = _optional_image_url(values["image_url"])
    if "priority" in values:
        clean["priority"] = _priority(values["priority"])
    return clean


def canonical_card_key(values: Dict[str, Any]) -> str:
    """Return a stable identity key without exposing a database-specific ID."""
    identity = (
        "player", "year", "set_name", "card_number", "card_type",
        "parallel", "serial_total", "grade", "grading_company",
    )
    raw = "|".join(normalize_text(values.get(field, "")) for field in identity)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _scoped_card_key(values: Dict[str, Any], owner_id: Optional[str]) -> str:
    """Preserve legacy guest keys while allowing collectors to watch the same card."""

    card_key = canonical_card_key(values)
    if owner_id is None:
        return card_key
    return hashlib.sha256("{}|{}".format(owner_id, card_key).encode("utf-8")).hexdigest()


def initialize_watchlist() -> None:
    """Create the Watchlist table and indexes without touching existing tables."""
    initialize_database()
    with get_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS watchlist_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT,
                card_key TEXT NOT NULL UNIQUE,
                player TEXT NOT NULL,
                year INTEGER,
                set_name TEXT,
                card_number TEXT,
                card_type TEXT,
                parallel TEXT,
                serial_total INTEGER,
                grade TEXT,
                grading_company TEXT,
                image_url TEXT,
                target_price_cad REAL,
                notes TEXT,
                priority INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        existing_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(watchlist_cards)").fetchall()
        }
        if "owner_id" not in existing_columns:
            connection.execute("ALTER TABLE watchlist_cards ADD COLUMN owner_id TEXT")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_created ON watchlist_cards(created_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_priority ON watchlist_cards(priority, created_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_player ON watchlist_cards(player)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_owner_created ON watchlist_cards(owner_id, created_at)")
        connection.commit()


def _row(watchlist_id: int, owner_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    owner_sql, owner_params = _owner_clause(owner_id)
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM watchlist_cards WHERE id = ? AND {}".format(owner_sql),
            [watchlist_id, *owner_params],
        ).fetchone()
    return dict(row) if row is not None else None


def _market_context(card: Dict[str, Any], observed_sales: Iterable[Any]) -> Dict[str, Any]:
    """Attach a direct estimate or visibly disclosed low-confidence fallback."""
    valuation = calculate_valuation(CardQuery.from_mapping(card), observed_sales)
    estimate = (
        valuation.get("estimated_value_cad")
        if valuation.get("status") in {"estimated", "predictive"}
        else None
    )
    market = {
        "status": valuation.get("status", "insufficient_data"),
        "estimated_value_cad": estimate,
        "confidence_percent": valuation.get("confidence_percent", 0) if estimate is not None else 0,
        "comp_count": valuation.get("comp_count", 0) if estimate is not None else 0,
        "exact_comp_count": valuation.get("exact_comp_count", 0) if estimate is not None else 0,
        "confidence_level": valuation.get("confidence_level", "low") if estimate is not None else None,
        "fallback_tier": valuation.get("fallback_tier") if estimate is not None else None,
    }
    if estimate is None:
        market["message"] = "Market data isn't available for this card yet."
    elif market["confidence_level"] == "low":
        market["message"] = "Low-confidence Prospectr estimate based on broader market signals, not an exact-card sale."
    return market


def _enrich(card: Dict[str, Any], observed_sales: Iterable[Any]) -> Dict[str, Any]:
    result = dict(card)
    market = _market_context(result, observed_sales)
    result["market_estimate"] = market
    result["current_estimate_cad"] = market["estimated_value_cad"]
    result["target_comparison"] = None
    target = result.get("target_price_cad")
    estimate = market["estimated_value_cad"]
    if target is not None and estimate is not None:
        difference = round(estimate - target, 2)
        result["target_comparison"] = {
            "estimated_minus_target_cad": difference,
            "relation": "above_target" if difference > 0 else "below_target" if difference < 0 else "at_target",
        }
    return result


def add_watchlist_card(owner_id: Optional[str] = None, **values: Any) -> Dict[str, Any]:
    initialize_watchlist()
    clean = _normalize_values(values, require_player=True)
    # Populate omitted optional fields so the persisted metadata is stable.
    for field in ("year", "set_name", "card_number", "card_type", "parallel", "serial_total", "grade", "grading_company", "image_url", "target_price_cad", "notes", "priority"):
        clean.setdefault(field, None if field not in {"set_name", "card_number", "card_type", "parallel", "grade", "grading_company", "notes", "priority"} else (0 if field == "priority" else ""))
    clean["card_key"] = _scoped_card_key(clean, owner_id)
    now = _now()
    try:
        with get_connection() as connection:
            cursor = connection.execute("""
                INSERT INTO watchlist_cards (
                    owner_id, card_key, player, year, set_name, card_number, card_type,
                    parallel, serial_total, grade, grading_company, image_url,
                    target_price_cad, notes, priority, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                owner_id, clean["card_key"], clean["player"], clean["year"], clean["set_name"],
                clean["card_number"], clean["card_type"], clean["parallel"],
                clean["serial_total"], clean["grade"], clean["grading_company"],
                clean["image_url"], clean["target_price_cad"], clean["notes"],
                clean["priority"], now, now,
            ))
            watchlist_id = cursor.lastrowid
            connection.commit()
    except sqlite3.IntegrityError:
        raise ValueError("This card is already on the Watchlist.")
    return get_watchlist_card(watchlist_id, owner_id=owner_id)  # type: ignore[return-value]


def get_watchlist_card(watchlist_id: int, owner_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    initialize_watchlist()
    card = _row(watchlist_id, owner_id=owner_id)
    if card is None:
        return None
    return _enrich(card, SalesRepository(BASE_DIR, db_path=database.DATABASE).all_observed_sales())


def get_watchlist_cards(
    search: str = "",
    priority_only: bool = False,
    owner_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    initialize_watchlist()
    owner_sql, owner_params = _owner_clause(owner_id)
    query = "SELECT * FROM watchlist_cards WHERE {}".format(owner_sql)
    params: List[Any] = list(owner_params)
    if search.strip():
        query += " AND (player LIKE ? OR set_name LIKE ? OR card_number LIKE ? OR parallel LIKE ?)"
        pattern = "%{}%".format(search.strip())
        params.extend([pattern, pattern, pattern, pattern])
    if priority_only:
        query += " AND priority > 0"
    query += " ORDER BY priority DESC, created_at DESC, id DESC"
    with get_connection() as connection:
        cards = [dict(row) for row in connection.execute(query, params).fetchall()]
    sales = SalesRepository(BASE_DIR, db_path=database.DATABASE).all_observed_sales()
    return [_enrich(card, sales) for card in cards]


def update_watchlist_card(
    watchlist_id: int,
    owner_id: Optional[str] = None,
    **updates: Any,
) -> Optional[Dict[str, Any]]:
    initialize_watchlist()
    allowed = {"player", "year", "set_name", "card_number", "card_type", "parallel", "serial_total", "grade", "grading_company", "image_url", "target_price_cad", "notes", "priority"}
    requested = {key: value for key, value in updates.items() if key in allowed}
    current = _row(watchlist_id, owner_id=owner_id)
    if current is None:
        return None
    if not requested:
        return get_watchlist_card(watchlist_id, owner_id=owner_id)
    clean = _normalize_values(requested, require_player=False)
    merged = dict(current)
    merged.update(clean)
    if not merged.get("player"):
        raise ValueError("Player is required.")
    clean["card_key"] = _scoped_card_key(merged, owner_id)
    clean["updated_at"] = _now()
    assignments = ", ".join("{} = ?".format(key) for key in clean)
    owner_sql, owner_params = _owner_clause(owner_id)
    try:
        with get_connection() as connection:
            connection.execute(
                "UPDATE watchlist_cards SET {} WHERE id = ? AND {}".format(assignments, owner_sql),
                [*clean.values(), watchlist_id, *owner_params],
            )
            connection.commit()
    except sqlite3.IntegrityError:
        raise ValueError("This card is already on the Watchlist.")
    return get_watchlist_card(watchlist_id, owner_id=owner_id)


def delete_watchlist_card(watchlist_id: int, owner_id: Optional[str] = None) -> bool:
    initialize_watchlist()
    owner_sql, owner_params = _owner_clause(owner_id)
    with get_connection() as connection:
        cursor = connection.execute(
            "DELETE FROM watchlist_cards WHERE id = ? AND {}".format(owner_sql),
            [watchlist_id, *owner_params],
        )
        connection.commit()
    return cursor.rowcount > 0


def get_watchlist_summary(owner_id: Optional[str] = None) -> Dict[str, int]:
    """Return counts only; value totals would be misleading without estimates."""
    initialize_watchlist()
    owner_sql, owner_params = _owner_clause(owner_id)
    with get_connection() as connection:
        row = connection.execute("""
            SELECT COUNT(*) AS card_count,
                   COALESCE(SUM(CASE WHEN priority > 0 THEN 1 ELSE 0 END), 0) AS priority_count,
                   COALESCE(SUM(CASE WHEN target_price_cad IS NOT NULL THEN 1 ELSE 0 END), 0) AS target_count
            FROM watchlist_cards
            WHERE {}
        """.format(owner_sql), owner_params).fetchone()
    return dict(row)
