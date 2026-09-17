"""Safe, provider-free CSV normalization for CARDR collection imports.

The importer deliberately stops at a preview of normalized records.  It does
not write to the Vault itself: callers can show the preview, require a user
confirmation, and then pass each record to the appropriate account-scoped
Vault service.
"""

from __future__ import annotations

import csv
import io
import math
import re
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union


MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 5_000
MAX_CELL_CHARS = 16_000
MAX_NOTES_CHARS = 4_000
MAX_TEXT_CHARS = 500
MAX_PLAYER_CHARS = 160
MAX_SET_CHARS = 200
MAX_CARD_NUMBER_CHARS = 80
MAX_GRADE_CHARS = 80
MAX_QUANTITY = 10_000
MAX_PURCHASE_PRICE_CAD = 10_000_000


class CollectionImportError(ValueError):
    """Raised when a file cannot safely be parsed as a collection CSV."""


FIELD_ALIASES: Mapping[str, Tuple[str, ...]] = {
    "player": (
        "player", "player_name", "athlete", "athlete_name", "name",
        "card_player",
    ),
    "year": ("year", "card_year", "release_year", "season"),
    "set_name": (
        "set", "set_name", "card_set", "cardset", "product", "brand",
        "series",
    ),
    "card_number": (
        "card_number", "card_no", "card_num", "cardnumber", "number",
        "no", "card", "card_id",
    ),
    "parallel": (
        "parallel", "variation", "insert", "insert_parallel", "variant",
    ),
    "grade": ("grade", "grading", "slab_grade", "graded"),
    "condition": ("condition", "card_condition", "raw_condition"),
    "purchase_price_cad": (
        "purchase_price", "purchase_price_cad", "paid", "price_paid", "cost",
        "purchase_cost", "buy_price", "acquisition_cost",
    ),
    "purchase_date": (
        "purchase_date", "date_purchased", "purchased_on", "acquisition_date",
        "bought_date", "date_bought",
    ),
    "quantity": ("quantity", "qty", "count", "units", "number_owned"),
    "notes": ("notes", "note", "comments", "comment", "description", "details"),
}


def _header_key(value: Any) -> str:
    """Make common header spellings comparable without executing any content."""
    text = str(value or "").replace("\ufeff", "").strip().lower()
    text = text.replace("#", " number ")
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _strip_text(value: Any, *, limit: int, field: str) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if len(text) > limit:
        raise ValueError("{} is longer than {} characters.".format(field, limit))
    return text


def _neutralize_formula(value: str) -> Tuple[str, bool]:
    """Ensure a later CSV export cannot turn a text cell into a spreadsheet formula."""
    if value and value[0] in {"=", "+", "-", "@"}:
        return "'" + value, True
    return value, False


def _text_field(
    value: Any,
    *,
    field: str,
    limit: int,
    warnings: List[str],
) -> str:
    text = _strip_text(value, limit=limit, field=field)
    safe_text, neutralized = _neutralize_formula(text)
    if neutralized:
        warnings.append("{} started with a spreadsheet formula character and was saved as text.".format(field))
    return safe_text


def _parse_year(value: Any) -> Optional[int]:
    text = _strip_text(value, limit=16, field="Year")
    if not text:
        return None
    if not re.fullmatch(r"\d{4}", text):
        raise ValueError("Year must be a four-digit number.")
    year = int(text)
    if year < 1800 or year > date.today().year + 1:
        raise ValueError("Year must be between 1800 and {}.".format(date.today().year + 1))
    return year


def _parse_price(value: Any) -> Optional[float]:
    text = _strip_text(value, limit=64, field="Purchase price")
    if not text:
        return None
    # The text stays text until it satisfies this narrow numeric grammar; a
    # formula is never evaluated or sent to an external parser.
    normalized = re.sub(r"(?i)\b(?:cad|usd)\b", "", text)
    normalized = normalized.replace("C$", "").replace("US$", "").replace("$", "")
    normalized = normalized.replace(",", "").strip()
    if not re.fullmatch(r"(?:\d+(?:\.\d{1,2})?|\.\d{1,2})", normalized):
        raise ValueError("Purchase price must be a non-negative number.")
    price = float(normalized)
    if not math.isfinite(price) or price > MAX_PURCHASE_PRICE_CAD:
        raise ValueError("Purchase price must be at most ${:,.0f}.".format(MAX_PURCHASE_PRICE_CAD))
    return round(price, 2)


def _parse_quantity(value: Any) -> int:
    text = _strip_text(value, limit=32, field="Quantity")
    if not text:
        return 1
    if not re.fullmatch(r"\d+", text):
        raise ValueError("Quantity must be a whole number.")
    quantity = int(text)
    if quantity < 1 or quantity > MAX_QUANTITY:
        raise ValueError("Quantity must be between 1 and {:,}.".format(MAX_QUANTITY))
    return quantity


def _parse_purchase_date(value: Any, warnings: List[str]) -> Optional[str]:
    text = _strip_text(value, limit=32, field="Purchase date")
    if not text:
        return None

    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        pass

    for format_string in ("%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, format_string).date().isoformat()
        except ValueError:
            continue

    # Support common exports while making the US-vs-Canadian ambiguity visible
    # instead of silently corrupting a purchase date.
    for format_string, interpretation in (("%m/%d/%Y", "MM/DD/YYYY"), ("%m/%d/%y", "MM/DD/YY")):
        try:
            parsed = datetime.strptime(text, format_string).date()
        except ValueError:
            continue
        parts = text.split("/")
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit() and int(parts[0]) <= 12 and int(parts[1]) <= 12:
            warnings.append("Purchase date '{}' was interpreted as {}; use YYYY-MM-DD to avoid ambiguity.".format(text, interpretation))
        return parsed.isoformat()

    # A day greater than 12 makes the intended DD/MM/YYYY format unambiguous.
    try:
        parsed = datetime.strptime(text, "%d/%m/%Y").date()
        warnings.append("Purchase date '{}' was normalized from DD/MM/YYYY.".format(text))
        return parsed.isoformat()
    except ValueError:
        raise ValueError("Purchase date must use YYYY-MM-DD or a recognized numeric date format.")


def _decode_payload(payload: Union[str, bytes]) -> str:
    if isinstance(payload, str):
        encoded = payload.encode("utf-8")
        text = payload
    elif isinstance(payload, bytes):
        encoded = payload
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise CollectionImportError("CSV must be UTF-8 encoded.") from error
    else:
        raise CollectionImportError("CSV upload must be text or bytes.")

    if len(encoded) > MAX_UPLOAD_BYTES:
        raise CollectionImportError("CSV is larger than the 5 MB import limit.")
    if "\x00" in text:
        raise CollectionImportError("CSV contains unsupported null characters.")
    return text


def _detect_dialect(text: str) -> csv.Dialect:
    sample = text[:8_192]
    if not sample.strip():
        raise CollectionImportError("CSV is empty.")
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def _mapped_headers(headers: Iterable[str]) -> Tuple[Dict[str, str], List[str]]:
    lookup: Dict[str, str] = {}
    for header in headers:
        normalized = _header_key(header)
        if normalized and normalized not in lookup:
            lookup[normalized] = header

    mapped: Dict[str, str] = {}
    for target, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                mapped[target] = lookup[alias]
                break

    warnings: List[str] = []
    if "player" not in mapped:
        warnings.append("No player column was recognized; rows without a player will be skipped.")
    return mapped, warnings


def _row_value(row: Mapping[str, Any], source_header: Optional[str]) -> Any:
    if not source_header:
        return ""
    return row.get(source_header, "")


def _is_empty_row(row: Mapping[str, Any]) -> bool:
    return not any(str(value or "").strip() for value in row.values())


def _normalize_row(row: Mapping[str, Any], mapped: Mapping[str, str]) -> Tuple[Dict[str, Any], List[str]]:
    warnings: List[str] = []
    player = _text_field(
        _row_value(row, mapped.get("player")), field="Player", limit=MAX_PLAYER_CHARS, warnings=warnings
    )
    if not player:
        raise ValueError("Player is required.")

    record = {
        "player": player,
        "year": _parse_year(_row_value(row, mapped.get("year"))),
        "set_name": _text_field(_row_value(row, mapped.get("set_name")), field="Set name", limit=MAX_SET_CHARS, warnings=warnings),
        "card_number": _text_field(_row_value(row, mapped.get("card_number")), field="Card number", limit=MAX_CARD_NUMBER_CHARS, warnings=warnings),
        "parallel": _text_field(_row_value(row, mapped.get("parallel")), field="Parallel", limit=MAX_TEXT_CHARS, warnings=warnings),
        "grade": _text_field(_row_value(row, mapped.get("grade")), field="Grade", limit=MAX_GRADE_CHARS, warnings=warnings),
        "condition": _text_field(_row_value(row, mapped.get("condition")), field="Condition", limit=MAX_TEXT_CHARS, warnings=warnings),
        "purchase_price_cad": _parse_price(_row_value(row, mapped.get("purchase_price_cad"))),
        "purchase_date": _parse_purchase_date(_row_value(row, mapped.get("purchase_date")), warnings),
        "quantity": _parse_quantity(_row_value(row, mapped.get("quantity"))),
        "notes": _text_field(_row_value(row, mapped.get("notes")), field="Notes", limit=MAX_NOTES_CHARS, warnings=warnings),
    }
    return record, warnings


def vault_create_payloads(records: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Convert confirmed preview records into ``vault.add_vault_card`` inputs.

    Vault currently has no separate condition column, so condition is retained
    in a labeled note rather than silently discarded.  Quantity is expanded to
    one Vault payload per physical card, which keeps later valuations and
    portfolio totals per-card.
    """
    payloads: List[Dict[str, Any]] = []
    for record in records:
        quantity = _parse_quantity(record.get("quantity", 1))
        notes = _strip_text(record.get("notes", ""), limit=MAX_NOTES_CHARS, field="Notes")
        condition = _strip_text(record.get("condition", ""), limit=MAX_TEXT_CHARS, field="Condition")
        if condition:
            condition_note = "Condition: {}".format(condition)
            notes = "{}\n{}".format(notes, condition_note).strip() if notes else condition_note
        # This is intentionally an allow-list: extra CSV properties never
        # become surprising parameters on the Vault write path.
        payload = {
            "player": _strip_text(record.get("player", ""), limit=MAX_PLAYER_CHARS, field="Player"),
            "year": _parse_year(record.get("year")),
            "set_name": _strip_text(record.get("set_name", ""), limit=MAX_SET_CHARS, field="Set name"),
            "card_type": "",
            "card_number": _strip_text(record.get("card_number", ""), limit=MAX_CARD_NUMBER_CHARS, field="Card number"),
            "parallel": _strip_text(record.get("parallel", ""), limit=MAX_TEXT_CHARS, field="Parallel"),
            "grade": _strip_text(record.get("grade", ""), limit=MAX_GRADE_CHARS, field="Grade"),
            "purchase_price_cad": _parse_price(record.get("purchase_price_cad")),
            "purchase_date": _parse_purchase_date(record.get("purchase_date"), []),
            "notes": notes,
        }
        if not payload["player"]:
            raise ValueError("Player is required.")
        payloads.extend(dict(payload) for _ in range(quantity))
    return payloads


def parse_collection_csv(
    payload: Union[str, bytes],
    *,
    filename: str = "collection.csv",
    max_rows: int = MAX_IMPORT_ROWS,
) -> Dict[str, Any]:
    """Return a safe preview of a collection CSV without persisting anything.

    `records` contains only valid normalized rows.  `rows` retains the
    row-by-row diagnostics a UI needs before a user confirms an import.  No
    CSV value is evaluated as a spreadsheet formula.
    """
    if not isinstance(max_rows, int) or max_rows < 1 or max_rows > MAX_IMPORT_ROWS:
        raise ValueError("max_rows must be between 1 and {:,}.".format(MAX_IMPORT_ROWS))
    if filename and not str(filename).lower().endswith(".csv"):
        raise CollectionImportError("Collection import accepts .csv files only.")

    text = _decode_payload(payload)
    dialect = _detect_dialect(text)
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(MAX_CELL_CHARS)
    try:
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if not reader.fieldnames:
            raise CollectionImportError("CSV needs a header row.")
        headers = [str(header or "") for header in reader.fieldnames]
        mapped, import_warnings = _mapped_headers(headers)
        rows: List[Dict[str, Any]] = []
        records: List[Dict[str, Any]] = []
        processed_rows = 0

        try:
            source_rows = iter(reader)
            for row_number, raw_row in enumerate(source_rows, start=2):
                if processed_rows >= max_rows:
                    raise CollectionImportError("CSV exceeds the {:,}-row import limit.".format(max_rows))
                processed_rows += 1
                extra_cells = raw_row.get(None)
                if extra_cells:
                    rows.append({
                        "row_number": row_number,
                        "status": "invalid",
                        "warnings": [],
                        "errors": ["Row has more cells than its header. Quote values that contain a delimiter."],
                    })
                    continue
                row = {str(key or ""): value for key, value in raw_row.items() if key is not None}
                if _is_empty_row(row):
                    rows.append({"row_number": row_number, "status": "skipped", "warnings": ["Empty row skipped."], "errors": []})
                    continue
                try:
                    record, warnings = _normalize_row(row, mapped)
                except ValueError as error:
                    rows.append({"row_number": row_number, "status": "invalid", "warnings": [], "errors": [str(error)]})
                    continue
                records.append(record)
                rows.append({"row_number": row_number, "status": "ready", "record": record, "warnings": warnings, "errors": []})
        except csv.Error as error:
            raise CollectionImportError("CSV could not be parsed: {}".format(error)) from error
    finally:
        csv.field_size_limit(previous_limit)

    invalid_rows = sum(item["status"] == "invalid" for item in rows)
    skipped_rows = sum(item["status"] == "skipped" for item in rows)
    warning_count = len(import_warnings) + sum(len(item["warnings"]) for item in rows)
    return {
        "filename": _strip_text(filename, limit=255, field="Filename") or "collection.csv",
        "headers": headers,
        "mapped_columns": mapped,
        "warnings": import_warnings,
        "rows": rows,
        "records": records,
        "summary": {
            "total_rows": processed_rows,
            "ready_rows": len(records),
            "invalid_rows": invalid_rows,
            "skipped_rows": skipped_rows,
            "warning_count": warning_count,
            "requires_confirmation": bool(records),
        },
    }
