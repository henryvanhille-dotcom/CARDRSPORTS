"""Private, evidence-only CARDR alert endpoints.

Rules are stored per account and evaluated on the server against the current
observed-sales repository.  A browser never supplies the price or sales data
that can trigger an alert, and Prospectr fallback estimates never trigger one.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from accounts import add_notification, initialize_accounts, require_user
from alerts import AlertValidationError, evaluate_alert_rules, validate_alert_rule
from database import DATABASE, get_connection
from market_data import SalesRepository
from market_pulse import build_market_pulse
from valuation import CardQuery, calculate_valuation


router = APIRouter()
BASE_DIR = Path(__file__).resolve().parents[1]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_rule(row: Any) -> Optional[Dict[str, Any]]:
    try:
        rule = json.loads(row["rule_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    return rule if isinstance(rule, dict) else None


def _load_valuation(value: Any) -> Optional[Dict[str, Any]]:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _stored_rules(owner_id: str) -> List[Dict[str, Any]]:
    initialize_accounts()
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, rule_json, last_valuation_json, created_at, updated_at
            FROM account_alert_rules
            WHERE owner_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (owner_id,),
        ).fetchall()
    records = []
    for row in rows:
        rule = _load_rule(row)
        if rule is not None:
            records.append(
                {
                    "id": row["id"],
                    "rule": rule,
                    "last_valuation": _load_valuation(row["last_valuation_json"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
    return records


def _valuation_for_rule(rule: Dict[str, Any], sales: list[Any]) -> Dict[str, Any]:
    return calculate_valuation(CardQuery.from_mapping(rule["card"]), sales)


@router.get("/api/alerts")
async def get_alerts(request: Request) -> Dict[str, Any]:
    user = require_user(request)
    records = _stored_rules(user["id"])
    return {
        "success": True,
        "alerts": [
            {
                "id": record["id"],
                "rule": record["rule"],
                "created_at": record["created_at"],
                "updated_at": record["updated_at"],
                "has_observed_baseline": bool(record["last_valuation"] and record["last_valuation"].get("status") == "estimated"),
            }
            for record in records
        ],
        "delivery": "in_app_only",
        "evidence_policy": "Price alerts require direct observed-sale valuations. Prospectr predictions do not trigger alerts.",
    }


@router.post("/api/alerts")
async def create_alert(data: Dict[str, Any], request: Request) -> Dict[str, Any]:
    user = require_user(request)
    raw_rule = dict(data.get("rule") if isinstance(data.get("rule"), dict) else data)
    raw_rule["id"] = uuid.uuid4().hex
    raw_rule["channel"] = "in_app"
    try:
        rule = validate_alert_rule(raw_rule)
    except AlertValidationError as error:
        raise HTTPException(status_code=422, detail=str(error))

    sales = SalesRepository(BASE_DIR, db_path=DATABASE).all_observed_sales()
    baseline = _valuation_for_rule(rule, sales)
    now = _now()
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO account_alert_rules (
                id, owner_id, rule_json, last_valuation_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (rule["id"], user["id"], json.dumps(rule, separators=(",", ":")), json.dumps(baseline, separators=(",", ":")), now, now),
        )
        connection.commit()
    return {
        "success": True,
        "alert": {"id": rule["id"], "rule": rule, "created_at": now, "updated_at": now},
        "baseline": {
            "status": baseline.get("status"),
            "has_direct_observed_sales": baseline.get("status") == "estimated",
            "message": "This rule will watch future observed evidence. Existing sales were used only as its baseline.",
        },
    }


@router.put("/api/alerts/{alert_id}")
async def update_alert(alert_id: str, data: Dict[str, Any], request: Request) -> Dict[str, Any]:
    user = require_user(request)
    raw_rule = dict(data.get("rule") if isinstance(data.get("rule"), dict) else data)
    raw_rule["id"] = alert_id
    raw_rule["channel"] = "in_app"
    try:
        rule = validate_alert_rule(raw_rule)
    except AlertValidationError as error:
        raise HTTPException(status_code=422, detail=str(error))
    now = _now()
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE account_alert_rules
            SET rule_json = ?, last_valuation_json = NULL, updated_at = ?
            WHERE id = ? AND owner_id = ?
            """,
            (json.dumps(rule, separators=(",", ":")), now, alert_id, user["id"]),
        )
        connection.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Alert rule not found.")
    return {"success": True, "alert": {"id": alert_id, "rule": rule, "updated_at": now}}


@router.delete("/api/alerts/{alert_id}")
async def delete_alert(alert_id: str, request: Request) -> Dict[str, Any]:
    user = require_user(request)
    with get_connection() as connection:
        cursor = connection.execute(
            "DELETE FROM account_alert_rules WHERE id = ? AND owner_id = ?", (alert_id, user["id"])
        )
        connection.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Alert rule not found.")
    return {"success": True}


@router.post("/api/alerts/evaluate")
async def evaluate_alerts(request: Request) -> Dict[str, Any]:
    """Check saved rules using server-side sales evidence only."""

    user = require_user(request)
    records = _stored_rules(user["id"])
    sales = SalesRepository(BASE_DIR, db_path=DATABASE).all_observed_sales()
    market_pulse = build_market_pulse(sales)
    with get_connection() as connection:
        existing_state = {
            row["dedupe_key"]
            for row in connection.execute(
                "SELECT dedupe_key FROM account_alert_delivery_state WHERE owner_id = ? AND active = 1",
                (user["id"],),
            ).fetchall()
        }

    all_notifications: List[Dict[str, Any]] = []
    all_active_keys: set[str] = set()
    skipped: List[Dict[str, str]] = []
    invalid: List[Dict[str, str]] = []
    now = _now()
    with get_connection() as connection:
        for record in records:
            rule = record["rule"]
            current = _valuation_for_rule(rule, sales)
            context = {
                "valuation": current,
                "previous_valuation": record["last_valuation"],
                "market_pulse": market_pulse,
                "evaluated_at": now,
            }
            try:
                result = evaluate_alert_rules([rule], context, delivered_keys=existing_state)
            except AlertValidationError as error:
                invalid.append({"rule_id": rule.get("id", record["id"]), "error": str(error)})
                continue
            skipped.extend(result.get("skipped_rules", []))
            invalid.extend(result.get("invalid_rules", []))
            all_active_keys.update(result.get("active_condition_keys", []))
            for event in result.get("notifications", []):
                saved = add_notification(
                    user["id"],
                    kind="alert." + str(event.get("type", "market")),
                    title=str(event.get("title", "Cardr alert")),
                    body=str(event.get("body", "")),
                    payload={
                        "rule_id": event.get("rule_id"),
                        "card": event.get("card"),
                        "severity": event.get("severity"),
                        "evidence": event.get("evidence"),
                    },
                    dedupe_key=str(event.get("dedup_key", "")) or None,
                )
                if saved is not None:
                    all_notifications.append(saved)
            connection.execute(
                """
                UPDATE account_alert_rules
                SET last_valuation_json = ?, updated_at = ?
                WHERE id = ? AND owner_id = ?
                """,
                (json.dumps(current, separators=(",", ":")), now, record["id"], user["id"]),
            )

        # Alert states are active only while their direct-evidence condition
        # is still present.  This permits a genuine future re-crossing while
        # suppressing repeated checks of the same condition.
        connection.execute(
            "UPDATE account_alert_delivery_state SET active = 0, updated_at = ? WHERE owner_id = ?",
            (now, user["id"]),
        )
        for dedupe_key in all_active_keys:
            connection.execute(
                """
                INSERT INTO account_alert_delivery_state (owner_id, dedupe_key, active, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(owner_id, dedupe_key) DO UPDATE SET active = 1, updated_at = excluded.updated_at
                """,
                (user["id"], dedupe_key, now, now),
            )
        connection.commit()

    return {
        "success": True,
        "evaluated_rule_count": len(records),
        "notifications": all_notifications,
        "notification_count": len(all_notifications),
        "skipped_rules": skipped,
        "invalid_rules": invalid,
        "delivery": "in_app_only",
        "evidence_policy": "Only direct valuations with completed-sale provenance can trigger price alerts. Predictive estimates never trigger alerts.",
    }
