"""
CARDr Vault

Personal baseball-card collection storage and tracking.
Vault data is separate from Prospectr's public market-sales database.
"""

import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from database import get_connection, initialize_database
from market_data import SalesRepository
from valuation import CardQuery, calculate_valuation


BASE_DIR = Path(__file__).resolve().parent
MAX_TEXT_LENGTH = 500
VALID_PROSPECTR_STATUSES = {
    "not_analyzed",
    "analyzed",
    "insufficient_data",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any, field: str, max_length: int = MAX_TEXT_LENGTH) -> str:
    """Normalize a text field and keep unexpectedly large submissions out of SQLite."""
    text = str(value or "").strip()
    if len(text) > max_length:
        raise ValueError("{} must be {} characters or fewer.".format(field, max_length))
    return text


def _optional_number(
    value: Any,
    field: str,
    *,
    minimum: float = 0,
    maximum: Optional[float] = None,
) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("{} must be a number.".format(field))
    if not math.isfinite(number) or number < minimum:
        raise ValueError("{} must be at least {}.".format(field, minimum))
    if maximum is not None and number > maximum:
        raise ValueError("{} must be at most {}.".format(field, maximum))
    return round(number, 2)


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


def _optional_date(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text = _clean_text(value, "Purchase date", 32)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ValueError("Purchase date must use YYYY-MM-DD format.")


def _optional_image_url(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    candidate = _clean_text(value, "Image URL", 2000)
    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return candidate
    if candidate.startswith("/uploads/") and "/" not in candidate[len("/uploads/"):]:
        return candidate
    raise ValueError("Image URL must be an HTTPS, HTTP, or Prospectr upload URL.")


def _coerce_bool(value: Any, field: str = "Favorite") -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    raise ValueError("{} must be true or false.".format(field))


def _normalize_card_values(values: Dict[str, Any], *, require_player: bool) -> Dict[str, Any]:
    """Apply the same validation on create and update requests."""
    cleaned: Dict[str, Any] = {}
    text_fields = {
        "player": ("Player", 160),
        "set_name": ("Set name", 200),
        "card_type": ("Card type", 160),
        "card_number": ("Card number", 80),
        "parallel": ("Parallel", 160),
        "grade": ("Grade", 80),
        "notes": ("Notes", 4000),
    }
    for key, (label, max_length) in text_fields.items():
        if key in values:
            cleaned[key] = _clean_text(values[key], label, max_length)

    if require_player and not cleaned.get("player", ""):
        raise ValueError("Player is required.")
    if "player" in cleaned and not cleaned["player"]:
        raise ValueError("Player is required.")

    if "year" in values:
        cleaned["year"] = _optional_year(values["year"])
    if "image_url" in values:
        cleaned["image_url"] = _optional_image_url(values["image_url"])
    if "purchase_price_cad" in values:
        cleaned["purchase_price_cad"] = _optional_number(
            values["purchase_price_cad"], "Purchase price"
        )
    if "estimated_value_cad" in values:
        cleaned["estimated_value_cad"] = _optional_number(
            values["estimated_value_cad"], "Estimated value"
        )
    if "value_confidence" in values:
        cleaned["value_confidence"] = _optional_number(
            values["value_confidence"], "Value confidence", maximum=100
        )
    if "purchase_date" in values:
        cleaned["purchase_date"] = _optional_date(values["purchase_date"])
    if "favorite" in values:
        cleaned["favorite"] = int(_coerce_bool(values["favorite"]))
    if "prospectr_status" in values:
        status = _clean_text(values["prospectr_status"], "Prospectr status", 40)
        if status not in VALID_PROSPECTR_STATUSES:
            raise ValueError("Invalid Prospectr status.")
        cleaned["prospectr_status"] = status
    return cleaned


def initialize_vault():
    """Create the CARDr Vault table if it does not already exist."""

    initialize_database()

    with get_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS vault_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                player TEXT NOT NULL,
                year INTEGER,
                set_name TEXT,
                card_type TEXT,
                card_number TEXT,
                parallel TEXT,
                grade TEXT,

                image_url TEXT,

                purchase_price_cad REAL,
                purchase_date TEXT,

                estimated_value_cad REAL,
                value_confidence REAL,

                profit_loss_cad REAL,
                profit_loss_percent REAL,

                prospectr_status TEXT DEFAULT 'not_analyzed',

                notes TEXT,
                favorite INTEGER DEFAULT 0,

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_vault_player
            ON vault_cards(player)
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_vault_created
            ON vault_cards(created_at)
        """)

        connection.commit()


def _calculate_profit_loss(
    purchase_price: Optional[float],
    estimated_value: Optional[float],
):
    """Calculate current value change."""

    if purchase_price is None or estimated_value is None:
        return None, None

    if purchase_price <= 0:
        return None, None

    profit_loss = estimated_value - purchase_price
    profit_loss_percent = (profit_loss / purchase_price) * 100

    return (
        round(profit_loss, 2),
        round(profit_loss_percent, 2),
    )


def add_vault_card(
    player: str,
    year: Optional[int] = None,
    set_name: str = "",
    card_type: str = "",
    card_number: str = "",
    parallel: str = "",
    grade: str = "",
    image_url: Optional[str] = None,
    purchase_price_cad: Optional[float] = None,
    purchase_date: Optional[str] = None,
    estimated_value_cad: Optional[float] = None,
    value_confidence: Optional[float] = None,
    notes: str = "",
    favorite: bool = False,
) -> Dict[str, Any]:
    """Add a card to the user's Vault."""

    initialize_vault()

    values = _normalize_card_values(
        {
            "player": player,
            "year": year,
            "set_name": set_name,
            "card_type": card_type,
            "card_number": card_number,
            "parallel": parallel,
            "grade": grade,
            "image_url": image_url,
            "purchase_price_cad": purchase_price_cad,
            "purchase_date": purchase_date,
            "estimated_value_cad": estimated_value_cad,
            "value_confidence": value_confidence,
            "notes": notes,
            "favorite": favorite,
        },
        require_player=True,
    )
    purchase_price = values["purchase_price_cad"]
    estimated_value = values["estimated_value_cad"]

    profit_loss, profit_loss_percent = _calculate_profit_loss(
        purchase_price,
        estimated_value,
    )

    now = _now()

    with get_connection() as connection:
        cursor = connection.execute("""
            INSERT INTO vault_cards (
                player,
                year,
                set_name,
                card_type,
                card_number,
                parallel,
                grade,

                image_url,

                purchase_price_cad,
                purchase_date,

                estimated_value_cad,
                value_confidence,

                profit_loss_cad,
                profit_loss_percent,

                prospectr_status,

                notes,
                favorite,

                created_at,
                updated_at
            )

            VALUES (
                ?, ?, ?, ?, ?, ?, ?,
                ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?,
                ?, ?,
                ?, ?
            )
        """, (
            values["player"],
            values["year"],
            values["set_name"],
            values["card_type"],
            values["card_number"],
            values["parallel"],
            values["grade"],

            values["image_url"],

            purchase_price,
            values["purchase_date"],

            estimated_value,
            values["value_confidence"],

            profit_loss,
            profit_loss_percent,

            "analyzed" if estimated_value is not None else "not_analyzed",

            values["notes"],
            values["favorite"],

            now,
            now,
        ))

        card_id = cursor.lastrowid
        connection.commit()

    return get_vault_card(card_id)


def get_vault_card(card_id: int) -> Optional[Dict[str, Any]]:
    """Get one Vault card by ID."""

    initialize_vault()

    with get_connection() as connection:
        row = connection.execute("""
            SELECT *
            FROM vault_cards
            WHERE id = ?
        """, (card_id,)).fetchone()

    if row is None:
        return None

    return dict(row)


def get_vault_cards(
    search: str = "",
    favorite_only: bool = False,
) -> List[Dict[str, Any]]:
    """Return Vault cards, newest first."""

    initialize_vault()

    query = """
        SELECT *
        FROM vault_cards
        WHERE 1 = 1
    """

    params = []

    if search.strip():
        query += """
            AND (
                player LIKE ?
                OR set_name LIKE ?
                OR card_number LIKE ?
                OR parallel LIKE ?
            )
        """

        search_value = "%" + search.strip() + "%"

        params.extend([
            search_value,
            search_value,
            search_value,
            search_value,
        ])

    if favorite_only:
        query += " AND favorite = 1"

    query += """
        ORDER BY
            created_at DESC,
            id DESC
    """

    with get_connection() as connection:
        rows = connection.execute(
            query,
            params,
        ).fetchall()

    return [dict(row) for row in rows]


def update_vault_card(
    card_id: int,
    **updates,
) -> Optional[Dict[str, Any]]:
    """Update allowed Vault card fields."""

    initialize_vault()

    allowed_fields = {
        "player",
        "year",
        "set_name",
        "card_type",
        "card_number",
        "parallel",
        "grade",
        "image_url",
        "purchase_price_cad",
        "purchase_date",
        "estimated_value_cad",
        "value_confidence",
        "notes",
        "favorite",
        "prospectr_status",
    }

    requested_updates = {
        key: value
        for key, value in updates.items()
        if key in allowed_fields
    }

    if not requested_updates:
        return get_vault_card(card_id)

    current = get_vault_card(card_id)

    if current is None:
        return None

    clean_updates = _normalize_card_values(requested_updates, require_player=False)

    purchase_price = clean_updates.get(
        "purchase_price_cad",
        current.get("purchase_price_cad"),
    )

    estimated_value = clean_updates.get(
        "estimated_value_cad",
        current.get("estimated_value_cad"),
    )

    profit_loss, profit_loss_percent = _calculate_profit_loss(
        purchase_price,
        estimated_value,
    )

    clean_updates["profit_loss_cad"] = profit_loss
    clean_updates["profit_loss_percent"] = profit_loss_percent

    if "estimated_value_cad" in clean_updates and "prospectr_status" not in clean_updates:
        clean_updates["prospectr_status"] = (
            "analyzed" if estimated_value is not None else "not_analyzed"
        )

    clean_updates["updated_at"] = _now()

    set_parts = []
    values = []

    for key, value in clean_updates.items():

        if key not in {
            "profit_loss_cad",
            "profit_loss_percent",
            "updated_at",
        } and key not in allowed_fields:
            continue

        set_parts.append(f"{key} = ?")
        values.append(value)

    values.append(card_id)

    with get_connection() as connection:
        connection.execute(
            f"""
            UPDATE vault_cards
            SET {", ".join(set_parts)}
            WHERE id = ?
            """,
            values,
        )

        connection.commit()

    return get_vault_card(card_id)


def delete_vault_card(card_id: int) -> bool:
    """Delete a card from the Vault."""

    initialize_vault()

    with get_connection() as connection:
        cursor = connection.execute("""
            DELETE FROM vault_cards
            WHERE id = ?
        """, (card_id,))

        connection.commit()

    return cursor.rowcount > 0


def toggle_favorite(card_id: int) -> Optional[Dict[str, Any]]:
    """Toggle a card's favorite status."""

    card = get_vault_card(card_id)

    if card is None:
        return None

    new_value = not bool(card.get("favorite"))

    return update_vault_card(
        card_id,
        favorite=new_value,
    )


def analyze_vault_card(card_id: int) -> Optional[Dict[str, Any]]:
    """Refresh a Vault card from stored market evidence without fabricating a value."""
    card = get_vault_card(card_id)
    if card is None:
        return None

    query = CardQuery.from_mapping(card)
    valuation = calculate_valuation(
        query,
        SalesRepository(BASE_DIR).all_observed_sales(),
    )
    status = "analyzed" if valuation["status"] == "estimated" else "insufficient_data"
    updated = update_vault_card(
        card_id,
        estimated_value_cad=valuation["estimated_value_cad"],
        value_confidence=valuation["confidence_percent"],
        prospectr_status=status,
    )
    return {"card": updated, "valuation": valuation}


def get_vault_summary() -> Dict[str, Any]:
    """Return collection-level Vault statistics."""

    initialize_vault()

    with get_connection() as connection:

        row = connection.execute("""
            SELECT
                COUNT(*) AS card_count,

                COALESCE(
                    SUM(purchase_price_cad),
                    0
                ) AS total_purchase_price,

                COALESCE(
                    SUM(estimated_value_cad),
                    0
                ) AS total_estimated_value,

                COALESCE(
                    SUM(profit_loss_cad),
                    0
                ) AS total_profit_loss,

                COALESCE(
                    SUM(
                        CASE
                        WHEN purchase_price_cad IS NOT NULL
                             AND purchase_price_cad > 0
                             AND estimated_value_cad IS NOT NULL
                            THEN purchase_price_cad
                        ELSE 0
                        END
                    ),
                    0
                ) AS valued_cost_basis,

                COALESCE(
                    SUM(
                        CASE
                        WHEN purchase_price_cad IS NOT NULL
                             AND purchase_price_cad > 0
                             AND estimated_value_cad IS NOT NULL
                            THEN profit_loss_cad
                        ELSE 0
                        END
                    ),
                    0
                ) AS valued_profit_loss,

                SUM(
                    CASE
                    WHEN estimated_value_cad IS NOT NULL
                        THEN 1
                        ELSE 0
                    END
                ) AS analyzed_cards,

                SUM(
                    CASE
                        WHEN estimated_value_cad IS NULL
                        THEN 1
                        ELSE 0
                    END
                ) AS unvalued_cards,

                SUM(
                    CASE
                        WHEN favorite = 1
                        THEN 1
                        ELSE 0
                    END
                ) AS favorite_cards

            FROM vault_cards
        """).fetchone()

    return {
        "card_count": int(row["card_count"] or 0),
        "total_purchase_price_cad": round(
            float(row["total_purchase_price"] or 0),
            2,
        ),
        "total_estimated_value_cad": round(
            float(row["total_estimated_value"] or 0),
            2,
        ),
        "total_profit_loss_cad": round(
            float(row["total_profit_loss"] or 0),
            2,
        ),
        "valued_cost_basis_cad": round(
            float(row["valued_cost_basis"] or 0),
            2,
        ),
        "total_profit_loss_percent": (
            round(
                (float(row["valued_profit_loss"] or 0) /
                 float(row["valued_cost_basis"] or 0)) * 100,
                2,
            )
            if float(row["valued_cost_basis"] or 0) > 0
            else None
        ),
        "analyzed_cards": int(
            row["analyzed_cards"] or 0
        ),
        "unvalued_cards": int(
            row["unvalued_cards"] or 0
        ),
        "favorite_cards": int(
            row["favorite_cards"] or 0
        ),
    }
