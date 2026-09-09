"""Stored-sale access for Prospectr.

Prospectr must distinguish observed transactions from fixture data.  This
module deliberately excludes rows marked sample, demo, mock, or test so those
rows can never appear in a customer-facing valuation.
"""

import csv
import hashlib
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlparse


USD_TO_CAD = 1.36
NON_PRODUCTION_SOURCES = {"sample", "demo", "mock", "placeholder", "test", "fixture"}


def normalize_text(value: object) -> str:
    """Return a stable comparison key without changing the original display text."""
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())


def clean_display(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def safe_url(value: object) -> str:
    candidate = clean_display(value)
    parsed = urlparse(candidate)
    return candidate if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def parse_price(value: object) -> Optional[float]:
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return None
    price = float(match.group())
    return price if price > 0 else None


def parse_date(value: object) -> Optional[date]:
    text = clean_display(value)
    if not text or normalize_text(text) in NON_PRODUCTION_SOURCES:
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate).date()
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _grade_from_title(title: object) -> str:
    """Recover a slab grade when a legacy import labelled it as raw."""
    match = re.search(
        r"\b(PSA|BGS|SGC|CGC|CSG|HGA|AGS)\s*(10|[1-9](?:\.5)?)\b",
        str(title or ""),
        flags=re.IGNORECASE,
    )
    return "{} {}".format(match.group(1).upper(), match.group(2)) if match else ""


def _card_number_from_title(title: object) -> str:
    match = re.search(r"#\s*([A-Z]{0,4}(?:[- ]?[A-Z])?[- ]?\d{1,4})\b", str(title or ""), re.I)
    return match.group(1).upper().replace(" ", "") if match else ""


def _first(row: Dict[str, object], names: Iterable[str]) -> str:
    normalized = {normalize_text(key): value for key, value in row.items()}
    for name in names:
        value = normalized.get(normalize_text(name))
        if value not in (None, ""):
            return clean_display(value)
    return ""


def _csv_price_in_cad(row: Dict[str, object]) -> Optional[float]:
    cad = _first(row, ("sale_price_cad", "price_cad", "cad_price"))
    if cad:
        return parse_price(cad)

    usd = _first(row, ("sale_price_usd", "price_usd", "usd_price"))
    if usd:
        price = parse_price(usd)
        return round(price * USD_TO_CAD, 2) if price else None

    raw_price = _first(row, ("sale_price", "price", "sold_price", "amount", "value"))
    price = parse_price(raw_price)
    if price is None:
        return None
    currency = normalize_text(_first(row, ("currency", "sale_currency")))
    return price if currency == "cad" else round(price * USD_TO_CAD, 2)


@dataclass(frozen=True)
class Sale:
    id: str
    player: str
    year: str
    set_name: str
    card_type: str
    card_number: str
    parallel: str
    grade: str
    price_cad: float
    sale_date: Optional[date]
    platform: str
    listing_url: str
    title: str
    image_url: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "player": self.player,
            "year": self.year,
            "set_name": self.set_name,
            "card_type": self.card_type,
            "card_number": self.card_number,
            "parallel": self.parallel,
            "grade": self.grade,
            "price_cad": round(self.price_cad, 2),
            "sale_date": self.sale_date.isoformat() if self.sale_date else None,
            "platform": self.platform or "Imported sale",
            "listing_url": self.listing_url,
            "title": self.title or "Imported card sale",
            "image_url": self.image_url,
        }


def _sale_from_csv(row: Dict[str, object], line_number: int) -> Optional[Sale]:
    source = _first(row, ("source", "platform", "marketplace"))
    if normalize_text(source) in NON_PRODUCTION_SOURCES:
        return None

    # A source and a parsable completed-sale date are the minimum provenance
    # needed before a CSV row may be displayed as an observed transaction.
    sold_on = parse_date(_first(row, ("sale_date", "sold_at", "date")))
    price_cad = _csv_price_in_cad(row)
    player = _first(row, ("player", "player_name", "athlete", "name"))
    if not source or not sold_on or price_cad is None or not player:
        return None

    title = _first(row, ("title", "listing_title", "card_name"))
    identifier = _first(row, ("id", "sale_id", "listing_id"))
    if not identifier:
        raw = "|".join((str(line_number), player, title, sold_on.isoformat(), str(price_cad)))
        identifier = "csv-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    return Sale(
        id=identifier,
        player=player,
        year=_first(row, ("year", "card_year")),
        set_name=_first(row, ("set_name", "set", "product", "card_set")),
        card_type=_first(row, ("card_type", "type", "subset")),
        card_number=_first(row, ("card_number", "number", "card_no")) or _card_number_from_title(title),
        parallel=_first(row, ("parallel", "variant", "refractor")),
        grade=_first(row, ("grade", "condition", "grading")),
        price_cad=price_cad,
        sale_date=sold_on,
        platform=source,
        listing_url=safe_url(_first(row, ("listing_url", "url", "link"))),
        title=title,
        image_url=safe_url(_first(row, ("image_url", "thumbnail_url", "image"))),
    )


class SalesRepository:
    """Read observed transactions from the existing SQLite store and CSV import."""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.db_path = self.base_dir / "prospectr.db"
        self.csv_path = self.base_dir / "sales_data.csv"

    def _database_sales(self) -> List[Sale]:
        if not self.db_path.exists():
            return []
        try:
            with sqlite3.connect(str(self.db_path)) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT id, title, sale_date, price_cad, price_usd, currency,
                           platform, listing_url, image_url, thumbnail_url,
                           player, year, set_name, card_type, parallel, grade,
                           price_confirmed, match_confidence
                    FROM sales
                    WHERE COALESCE(match_confidence, 'unknown') != 'rejected'
                    """
                ).fetchall()
        except sqlite3.Error:
            return []

        sales: List[Sale] = []
        for row in rows:
            platform = clean_display(row["platform"])
            # Legacy records with no platform may exist, but a rejected record
            # or an explicitly unconfirmed price can never be used.
            if row["price_confirmed"] == 0 or normalize_text(platform) in NON_PRODUCTION_SOURCES:
                continue
            price_cad = parse_price(row["price_cad"])
            if price_cad is None:
                usd = parse_price(row["price_usd"])
                price_cad = round(usd * USD_TO_CAD, 2) if usd else None
            sold_on = parse_date(row["sale_date"])
            if price_cad is None or sold_on is None or not clean_display(row["player"]):
                continue
            sales.append(
                Sale(
                    id="db-" + clean_display(row["id"]),
                    player=clean_display(row["player"]),
                    year=clean_display(row["year"]),
                    set_name=clean_display(row["set_name"]),
                    card_type=clean_display(row["card_type"]),
                    card_number=_card_number_from_title(row["title"]),
                    parallel=clean_display(row["parallel"]),
                    grade=(
                        _grade_from_title(row["title"])
                        if normalize_text(row["grade"]) in {"", "raw", "ungraded"}
                        and _grade_from_title(row["title"])
                        else clean_display(row["grade"])
                    ),
                    price_cad=price_cad,
                    sale_date=sold_on,
                    platform=platform or "Imported sale",
                    listing_url=safe_url(row["listing_url"]),
                    title=clean_display(row["title"]),
                    image_url=safe_url(row["image_url"] or row["thumbnail_url"]),
                )
            )
        return sales

    def _csv_sales(self) -> List[Sale]:
        if not self.csv_path.exists():
            return []
        try:
            with self.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                return [
                    sale
                    for line_number, row in enumerate(reader, start=2)
                    for sale in [_sale_from_csv(row, line_number)]
                    if sale is not None
                ]
        except (OSError, csv.Error):
            return []

    def all_observed_sales(self) -> List[Sale]:
        # Prefer database records if the same original listing exists in both
        # places.  A stable URL makes the best de-duplication key.
        seen = set()
        sales: List[Sale] = []
        for sale in self._database_sales() + self._csv_sales():
            key = normalize_text(sale.listing_url) or sale.id
            if key in seen:
                continue
            seen.add(key)
            sales.append(sale)
        return sorted(sales, key=lambda sale: sale.sale_date or date.min, reverse=True)

    def recent_sales(self, limit: int = 12) -> List[Sale]:
        return self.all_observed_sales()[: max(1, min(limit, 100))]

    def source_summary(self) -> Dict[str, object]:
        sales = self.all_observed_sales()
        return {
            "stored_sales_count": len(sales),
            "sources": sorted({sale.platform for sale in sales if sale.platform}),
            "sample_rows_excluded": self._sample_row_count(),
        }

    def _sample_row_count(self) -> int:
        if not self.csv_path.exists():
            return 0
        try:
            with self.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                return sum(
                    1
                    for row in csv.DictReader(handle)
                    if normalize_text(_first(row, ("source", "platform", "marketplace")))
                    in NON_PRODUCTION_SOURCES
                )
        except (OSError, csv.Error):
            return 0
