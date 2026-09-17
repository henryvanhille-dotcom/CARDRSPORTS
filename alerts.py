"""Pure, evidence-first in-app alert evaluation for CARDR.

This module deliberately has no database, web, email, or FastAPI dependency.
The application supplies a user's saved rules plus already-vetted valuation,
sale, and market-pulse data.  The evaluator only creates notifications when
the supplied payload carries observed-sale evidence; a predictive estimate is
never treated as an observed card price.

Persisting rules, delivered ``dedup_key`` values, and notification records is
the responsibility of the route/storage layer.  ``active_condition_keys`` is
returned so that layer can clear a target/move/signal condition after it stops
being true and allow a future re-crossing to notify the collector again.
"""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from market_data import clean_display, normalize_text


ALERT_TYPES = frozenset({"target_price", "percent_move", "new_comp", "hot_signal"})
CARD_IDENTITY_FIELDS = (
    "player",
    "year",
    "set_name",
    "card_number",
    "card_type",
    "parallel",
    "grade",
)
MAX_RULES_PER_EVALUATION = 100
MAX_NOTIFICATIONS_PER_EVALUATION = 50
MAX_OBSERVED_COMPS_PER_CONTEXT = 500
MAX_DELIVERED_KEYS = 10_000
MAX_TEXT_LENGTH = 200
MAX_RULE_ID_LENGTH = 128
MAX_TARGET_PRICE_CAD = 10_000_000.0
MAX_MOVE_PERCENT = 1_000.0


class AlertValidationError(ValueError):
    """Raised when a saved alert rule or evaluation input is malformed."""


def _clean_required_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise AlertValidationError("{} must be text.".format(field))
    text = clean_display(value)
    if not text:
        raise AlertValidationError("{} is required.".format(field))
    if len(text) > maximum:
        raise AlertValidationError("{} must be {} characters or fewer.".format(field, maximum))
    return text


def _clean_optional_text(value: Any, field: str, maximum: int) -> str:
    if value in (None, ""):
        return ""
    return _clean_required_text(value, field, maximum)


def _finite_number(
    value: Any,
    field: str,
    *,
    minimum: float = 0.0,
    maximum: float,
    minimum_inclusive: bool = True,
) -> float:
    if isinstance(value, bool):
        raise AlertValidationError("{} must be a number.".format(field))
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise AlertValidationError("{} must be a number.".format(field))
    if not math.isfinite(number):
        raise AlertValidationError("{} must be finite.".format(field))
    under_minimum = number < minimum if minimum_inclusive else number <= minimum
    if under_minimum or number > maximum:
        lower = "at least" if minimum_inclusive else "greater than"
        raise AlertValidationError(
            "{} must be {} {} and no more than {}.".format(field, lower, minimum, maximum)
        )
    return round(number, 2)


def _normalize_card_identity(value: Any) -> Dict[str, str]:
    if not isinstance(value, Mapping):
        raise AlertValidationError("Card identity must be an object.")
    identity = {
        field: _clean_optional_text(value.get(field), field.replace("_", " ").title(), MAX_TEXT_LENGTH)
        for field in CARD_IDENTITY_FIELDS
    }
    if not identity["player"]:
        raise AlertValidationError("Card identity needs a player.")
    return identity


def canonical_card_key(card: Mapping[str, Any]) -> str:
    """Build a stable identity key without exposing database IDs."""
    values = [normalize_text(card.get(field, "")) for field in CARD_IDENTITY_FIELDS]
    raw = "|".join(values).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _stable_key(*parts: object) -> str:
    raw = json.dumps(parts, separators=(",", ":"), ensure_ascii=True, sort_keys=True)
    return "cardr-alert-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def validate_alert_rule(rule: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and normalize one JSON-safe alert rule.

    The supported schema is intentionally compact:

    * ``target_price``: ``target_price_cad`` and ``direction`` (``at_or_above``
      or ``at_or_below``).
    * ``percent_move``: ``threshold_percent`` and ``direction`` (``up``,
      ``down``, or ``either``).
    * ``new_comp``: optional ``minimum_sale_price_cad``.
    * ``hot_signal``: optional ``minimum_score`` from 0 to 100.

    Every rule is in-app only.  There is no silent email/push delivery path.
    """
    if not isinstance(rule, Mapping):
        raise AlertValidationError("Alert rule must be an object.")

    rule_id = _clean_required_text(rule.get("id"), "Rule id", MAX_RULE_ID_LENGTH)
    alert_type = _clean_required_text(rule.get("type"), "Alert type", 40).lower()
    if alert_type not in ALERT_TYPES:
        raise AlertValidationError(
            "Alert type must be one of: {}.".format(", ".join(sorted(ALERT_TYPES)))
        )

    channel = rule.get("channel", "in_app")
    if channel != "in_app":
        raise AlertValidationError("Only the in_app alert channel is available.")

    normalized: Dict[str, Any] = {
        "id": rule_id,
        "type": alert_type,
        "card": _normalize_card_identity(rule.get("card")),
        "enabled": bool(rule.get("enabled", True)),
        "channel": "in_app",
    }

    if alert_type == "target_price":
        normalized["target_price_cad"] = _finite_number(
            rule.get("target_price_cad"),
            "Target price",
            maximum=MAX_TARGET_PRICE_CAD,
            minimum_inclusive=False,
        )
        direction = rule.get("direction", "at_or_above")
        if direction not in {"at_or_above", "at_or_below"}:
            raise AlertValidationError(
                "Target-price direction must be at_or_above or at_or_below."
            )
        normalized["direction"] = direction

    elif alert_type == "percent_move":
        normalized["threshold_percent"] = _finite_number(
            rule.get("threshold_percent"),
            "Percent-move threshold",
            maximum=MAX_MOVE_PERCENT,
            minimum=0.01,
        )
        direction = rule.get("direction", "either")
        if direction not in {"up", "down", "either"}:
            raise AlertValidationError("Percent-move direction must be up, down, or either.")
        normalized["direction"] = direction

    elif alert_type == "new_comp":
        if rule.get("minimum_sale_price_cad") not in (None, ""):
            normalized["minimum_sale_price_cad"] = _finite_number(
                rule.get("minimum_sale_price_cad"),
                "Minimum sale price",
                maximum=MAX_TARGET_PRICE_CAD,
            )

    elif alert_type == "hot_signal":
        normalized["minimum_score"] = _finite_number(
            rule.get("minimum_score", 60),
            "Minimum hot-signal score",
            maximum=100.0,
        )

    return normalized


def validate_alert_rules(rules: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Validate a bounded rule collection before a storage layer saves it."""
    if isinstance(rules, (str, bytes)):
        raise AlertValidationError("Alert rules must be a list of objects.")
    try:
        items = list(rules)
    except TypeError:
        raise AlertValidationError("Alert rules must be a list of objects.")
    if len(items) > MAX_RULES_PER_EVALUATION:
        raise AlertValidationError(
            "A maximum of {} alert rules can be evaluated at once.".format(MAX_RULES_PER_EVALUATION)
        )

    normalized = [validate_alert_rule(item) for item in items]
    ids = [item["id"] for item in normalized]
    if len(ids) != len(set(ids)):
        raise AlertValidationError("Alert rule ids must be unique.")
    return normalized


def _valid_sale_date(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _observed_comp(value: Any) -> Optional[Dict[str, Any]]:
    """Return a minimal provenance-bearing comp, or reject it silently.

    An alert cannot be based on a bare price.  It needs a source identifier,
    completed-sale date, platform, and a positive CAD price.  This makes it
    safe to pass raw valuation ``comps`` into this pure layer without letting
    a predictive fallback masquerade as a completed transaction.
    """
    if not isinstance(value, Mapping):
        return None
    sale_id = clean_display(value.get("id", ""))
    platform = clean_display(value.get("platform", ""))
    sold_on = _valid_sale_date(value.get("sale_date"))
    if not sale_id or not platform or not sold_on:
        return None
    try:
        price = float(value.get("price_cad"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    identity = {
        field: clean_display(value.get(field, ""))
        for field in CARD_IDENTITY_FIELDS
    }
    return {
        "id": sale_id,
        "price_cad": round(price, 2),
        "sale_date": sold_on,
        "platform": platform,
        "listing_url": clean_display(value.get("listing_url", "")),
        "title": clean_display(value.get("title", "")),
        "card": identity,
        "match_level": clean_display(value.get("match_level", "")),
        "match_score": value.get("match_score"),
    }


def _observed_comps(values: Any) -> List[Dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    if len(values) > MAX_OBSERVED_COMPS_PER_CONTEXT:
        raise AlertValidationError(
            "A maximum of {} observed comps can be evaluated at once.".format(
                MAX_OBSERVED_COMPS_PER_CONTEXT
            )
        )
    result: List[Dict[str, Any]] = []
    seen = set()
    for value in values:
        comp = _observed_comp(value)
        if comp is None or comp["id"] in seen:
            continue
        seen.add(comp["id"])
        result.append(comp)
    return result


def _observed_valuation(value: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    """Normalize an evidence-backed direct valuation or explain why it is unusable."""
    if not isinstance(value, Mapping):
        return None, "No valuation was supplied."
    if value.get("status") != "estimated" or value.get("is_predictive") is True:
        return None, "A direct observed-sale valuation is required; predictive estimates do not trigger alerts."
    try:
        estimate = float(value.get("estimated_value_cad"))
    except (TypeError, ValueError):
        return None, "The supplied valuation has no usable CAD estimate."
    if not math.isfinite(estimate) or estimate <= 0:
        return None, "The supplied valuation has no usable CAD estimate."
    comps = _observed_comps(value.get("comps", []))
    if not comps:
        return None, "The valuation has no provenance-bearing observed comps."
    try:
        exact_count = int(value.get("exact_comp_count", 0) or 0)
    except (TypeError, ValueError):
        exact_count = 0
    try:
        confidence = float(value.get("confidence_percent", 0) or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "estimated_value_cad": round(estimate, 2),
        "comp_count": len(comps),
        "exact_comp_count": max(0, exact_count),
        "confidence_percent": round(max(0.0, min(100.0, confidence)), 2),
        "comps": comps,
    }, ""


def _same_card(rule_card: Mapping[str, Any], evidence_card: Mapping[str, Any]) -> bool:
    """Require every supplied rule identity detail to match exactly."""
    for field in CARD_IDENTITY_FIELDS:
        expected = normalize_text(rule_card.get(field, ""))
        if expected and expected != normalize_text(evidence_card.get(field, "")):
            return False
    return True


def _valuation_matches_rule_card(valuation: Mapping[str, Any], rule_card: Mapping[str, Any]) -> bool:
    """Reject a valuation accidentally supplied for a different card.

    A valuation is often computed immediately before alert evaluation, but the
    evaluator stays defensive because a stale client payload should never
    cause a target notification for the wrong player, set, or card number.
    """
    comps = valuation.get("comps", [])
    return bool(comps) and all(_same_card(rule_card, comp["card"]) for comp in comps)


def _comp_evidence(comps: Sequence[Mapping[str, Any]], maximum: int = 12) -> List[Dict[str, Any]]:
    """Keep notification evidence compact and safe for rendering."""
    return [
        {
            "id": comp["id"],
            "price_cad": comp["price_cad"],
            "sale_date": comp["sale_date"],
            "platform": comp["platform"],
            "listing_url": comp["listing_url"],
            "title": comp["title"],
            "match_level": comp["match_level"],
        }
        for comp in list(comps)[:maximum]
    ]


def _card_label(card: Mapping[str, Any]) -> str:
    parts = [
        clean_display(card.get("year", "")),
        clean_display(card.get("player", "")),
        clean_display(card.get("set_name", "")),
        ("#" + clean_display(card.get("card_number", ""))) if card.get("card_number") else "",
    ]
    return " ".join(part for part in parts if part) or "your card"


def _notification(
    rule: Mapping[str, Any],
    *,
    event_type: str,
    title: str,
    body: str,
    severity: str,
    dedup_key: str,
    evidence: Mapping[str, Any],
    evaluated_at: Optional[str],
) -> Dict[str, Any]:
    return {
        "id": _stable_key("notification", dedup_key),
        "type": event_type,
        "channel": "in_app",
        "severity": severity,
        "title": title,
        "body": body,
        "rule_id": rule["id"],
        "card": dict(rule["card"]),
        "dedup_key": dedup_key,
        "created_at": evaluated_at,
        "evidence": dict(evidence),
    }


def _target_event(
    rule: Mapping[str, Any],
    current: Mapping[str, Any],
    evaluated_at: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    estimate = float(current["estimated_value_cad"])
    target = float(rule["target_price_cad"])
    direction = rule["direction"]
    active = estimate >= target if direction == "at_or_above" else estimate <= target
    if not active:
        return None, None
    dedup_key = _stable_key("target_price", rule["id"], direction, target)
    relation = "reached" if direction == "at_or_above" else "fell to or below"
    title = "Target {} for {}".format(relation, _card_label(rule["card"]))
    body = "Observed comparable-sale estimate: CA${:,.2f}; your target: CA${:,.2f}.".format(
        estimate, target
    )
    return _notification(
        rule,
        event_type="target_price",
        title=title,
        body=body,
        severity="positive" if direction == "at_or_above" else "neutral",
        dedup_key=dedup_key,
        evidence={
            "currency": "CAD",
            "valuation": {
                "estimated_value_cad": estimate,
                "comp_count": current["comp_count"],
                "exact_comp_count": current["exact_comp_count"],
                "confidence_percent": current["confidence_percent"],
            },
            "observed_comps": _comp_evidence(current["comps"]),
        },
        evaluated_at=evaluated_at,
    ), dedup_key


def _percent_move_event(
    rule: Mapping[str, Any],
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    evaluated_at: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    previous_value = float(previous["estimated_value_cad"])
    current_value = float(current["estimated_value_cad"])
    if previous_value <= 0:
        return None, None
    prior_ids = {comp["id"] for comp in previous["comps"]}
    current_ids = {comp["id"] for comp in current["comps"]}
    new_ids = sorted(current_ids - prior_ids)
    # Re-running a model against an identical evidence set must not create a
    # price-move story. There needs to be a new observed sale in the current
    # evidence set.
    if not new_ids:
        return None, None
    change = round((current_value - previous_value) / previous_value * 100, 2)
    threshold = float(rule["threshold_percent"])
    direction = rule["direction"]
    active = (
        (direction in {"up", "either"} and change >= threshold)
        or (direction in {"down", "either"} and change <= -threshold)
    )
    if not active:
        return None, None
    move_direction = "up" if change > 0 else "down" if change < 0 else "flat"
    evidence_marker = _stable_key("new_evidence", new_ids)
    dedup_key = _stable_key("percent_move", rule["id"], move_direction, evidence_marker)
    title = "Observed estimate moved {} for {}".format(move_direction, _card_label(rule["card"]))
    body = "Estimate moved {:+.2f}% (CA${:,.2f} to CA${:,.2f}) after {} new observed comp{}.".format(
        change,
        previous_value,
        current_value,
        len(new_ids),
        "s" if len(new_ids) != 1 else "",
    )
    return _notification(
        rule,
        event_type="percent_move",
        title=title,
        body=body,
        severity="positive" if change > 0 else "neutral",
        dedup_key=dedup_key,
        evidence={
            "currency": "CAD",
            "previous_estimate_cad": previous_value,
            "current_estimate_cad": current_value,
            "change_percent": change,
            "new_observed_comp_ids": new_ids,
            "previous_observed_comps": _comp_evidence(previous["comps"]),
            "current_observed_comps": _comp_evidence(current["comps"]),
        },
        evaluated_at=evaluated_at,
    ), dedup_key


def _new_comp_candidates(
    context: Mapping[str, Any],
    current: Optional[Mapping[str, Any]],
    previous: Optional[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    explicit = context.get("new_observed_comps")
    if explicit is not None:
        return _observed_comps(explicit)
    if current is None or previous is None:
        return []
    prior_ids = {comp["id"] for comp in previous["comps"]}
    return [comp for comp in current["comps"] if comp["id"] not in prior_ids]


def _new_comp_events(
    rule: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    evaluated_at: Optional[str],
) -> List[Tuple[Dict[str, Any], str]]:
    minimum = rule.get("minimum_sale_price_cad")
    notifications: List[Tuple[Dict[str, Any], str]] = []
    for comp in candidates:
        if not _same_card(rule["card"], comp["card"]):
            continue
        if minimum is not None and float(comp["price_cad"]) < float(minimum):
            continue
        dedup_key = _stable_key("new_comp", rule["id"], comp["id"])
        title = "New observed comp for {}".format(_card_label(rule["card"]))
        body = "{} recorded a completed sale at CA${:,.2f} on {}.".format(
            comp["platform"], float(comp["price_cad"]), comp["sale_date"]
        )
        notifications.append((
            _notification(
                rule,
                event_type="new_comp",
                title=title,
                body=body,
                severity="neutral",
                dedup_key=dedup_key,
                evidence={"currency": "CAD", "observed_comp": _comp_evidence([comp], 1)[0]},
                evaluated_at=evaluated_at,
            ),
            dedup_key,
        ))
    return notifications


def _hot_signal_candidates(context: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    direct = context.get("hot_signal")
    if isinstance(direct, Mapping):
        return [direct]
    market_pulse = context.get("market_pulse")
    if not isinstance(market_pulse, Mapping):
        return []
    groups = market_pulse.get("groups", [])
    if not isinstance(groups, Sequence) or isinstance(groups, (str, bytes)):
        return []
    return [group for group in groups if isinstance(group, Mapping)]


def _hot_signal_event(
    rule: Mapping[str, Any],
    candidate: Mapping[str, Any],
    evaluated_at: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if candidate.get("status") != "success":
        return None, None
    card = candidate.get("card")
    signal = candidate.get("market_signal")
    compared_months = candidate.get("compared_months")
    if not isinstance(card, Mapping) or not _same_card(rule["card"], card):
        return None, None
    if not isinstance(signal, Mapping) or not isinstance(compared_months, Sequence) or isinstance(compared_months, (str, bytes)):
        return None, None
    if len(compared_months) < 2 or not all(isinstance(item, str) and item for item in compared_months[-2:]):
        return None, None
    try:
        score = float(signal.get("score"))
        observed_sale_count = int(candidate.get("observed_sale_count", 0))
    except (TypeError, ValueError):
        return None, None
    if not math.isfinite(score) or not 0 <= score <= 100 or observed_sale_count < 4:
        return None, None
    if score < float(rule["minimum_score"]):
        return None, None
    label = clean_display(signal.get("label", "Market signal")) or "Market signal"
    month_marker = tuple(compared_months[-2:])
    dedup_key = _stable_key("hot_signal", rule["id"], month_marker, label, score)
    title = "{} signal for {}".format(label, _card_label(rule["card"]))
    body = "Score: {:.0f}/100 from {} observed sales across {} and {}.".format(
        score, observed_sale_count, month_marker[0], month_marker[1]
    )
    return _notification(
        rule,
        event_type="hot_signal",
        title=title,
        body=body,
        severity="positive",
        dedup_key=dedup_key,
        evidence={
            "currency": "CAD",
            "market_signal": {"label": label, "score": round(score, 2)},
            "trend": dict(candidate.get("trend", {})) if isinstance(candidate.get("trend"), Mapping) else None,
            "observed_sale_count": observed_sale_count,
            "compared_months": list(month_marker),
            "basis": clean_display(signal.get("basis", "")),
        },
        evaluated_at=evaluated_at,
    ), dedup_key


def _evaluated_at(context: Mapping[str, Any]) -> Optional[str]:
    value = context.get("evaluated_at")
    if value in (None, ""):
        return None
    normalized = _valid_sale_date(value)
    if normalized is None:
        raise AlertValidationError("evaluated_at must be an ISO-8601 date or timestamp.")
    # Preserve an explicitly supplied timestamp for the persisted notification.
    return str(value)


def evaluate_alert_rules(
    rules: Iterable[Mapping[str, Any]],
    context: Mapping[str, Any],
    *,
    delivered_keys: Iterable[str] = (),
) -> Dict[str, Any]:
    """Evaluate in-app alert rules against supplied observed evidence.

    ``context`` may contain a direct ``valuation``, a prior direct
    ``previous_valuation``, explicitly detected ``new_observed_comps``, and a
    ``market_pulse`` payload from :func:`market_pulse.build_market_pulse`.
    Invalid rules are reported individually so one stale saved rule cannot
    suppress another.  Invalid evidence simply results in a documented skip.
    """
    if not isinstance(context, Mapping):
        raise AlertValidationError("Alert evaluation context must be an object.")
    normalized_rules: List[Dict[str, Any]] = []
    invalid_rules: List[Dict[str, str]] = []
    if isinstance(rules, (str, bytes)):
        raise AlertValidationError("Alert rules must be a list of objects.")
    try:
        raw_rules = list(rules)
    except TypeError:
        raise AlertValidationError("Alert rules must be a list of objects.")
    if len(raw_rules) > MAX_RULES_PER_EVALUATION:
        raise AlertValidationError(
            "A maximum of {} alert rules can be evaluated at once.".format(MAX_RULES_PER_EVALUATION)
        )
    seen_rule_ids = set()
    for index, raw_rule in enumerate(raw_rules):
        try:
            rule = validate_alert_rule(raw_rule)
            if rule["id"] in seen_rule_ids:
                raise AlertValidationError("Alert rule ids must be unique.")
            seen_rule_ids.add(rule["id"])
            normalized_rules.append(rule)
        except AlertValidationError as error:
            raw_id = raw_rule.get("id") if isinstance(raw_rule, Mapping) else None
            invalid_rules.append({
                "index": str(index),
                "rule_id": clean_display(raw_id) if isinstance(raw_id, str) else "",
                "error": str(error),
            })

    if isinstance(delivered_keys, (str, bytes)):
        raise AlertValidationError("Delivered alert keys must be a list of strings.")
    try:
        delivered = list(delivered_keys)
    except TypeError:
        raise AlertValidationError("Delivered alert keys must be a list of strings.")
    if len(delivered) > MAX_DELIVERED_KEYS:
        raise AlertValidationError(
            "A maximum of {} delivered alert keys can be supplied.".format(MAX_DELIVERED_KEYS)
        )
    delivered_set = {item for item in delivered if isinstance(item, str) and item}
    evaluated_at = _evaluated_at(context)
    current, current_reason = _observed_valuation(context.get("valuation"))
    previous, previous_reason = _observed_valuation(context.get("previous_valuation"))
    new_candidates = _new_comp_candidates(context, current, previous)

    notifications: List[Dict[str, Any]] = []
    active_condition_keys: List[str] = []
    skipped_rules: List[Dict[str, str]] = []
    suppressed_count = 0
    notification_limit_reached = False

    def add(notification: Optional[Dict[str, Any]], dedup_key: Optional[str]) -> None:
        nonlocal suppressed_count, notification_limit_reached
        if dedup_key:
            active_condition_keys.append(dedup_key)
        if notification is None or dedup_key in delivered_set:
            if notification is not None and dedup_key in delivered_set:
                suppressed_count += 1
            return
        if len(notifications) >= MAX_NOTIFICATIONS_PER_EVALUATION:
            notification_limit_reached = True
            return
        notifications.append(notification)

    for rule in normalized_rules:
        if not rule["enabled"]:
            skipped_rules.append({"rule_id": rule["id"], "reason": "Rule is disabled."})
            continue
        if rule["type"] == "target_price":
            if current is None:
                skipped_rules.append({"rule_id": rule["id"], "reason": current_reason})
                continue
            if not _valuation_matches_rule_card(current, rule["card"]):
                skipped_rules.append({
                    "rule_id": rule["id"],
                    "reason": "The supplied observed valuation does not match this rule's card identity.",
                })
                continue
            notification, dedup_key = _target_event(rule, current, evaluated_at)
            add(notification, dedup_key)
        elif rule["type"] == "percent_move":
            if current is None or previous is None:
                skipped_rules.append({
                    "rule_id": rule["id"],
                    "reason": current_reason if current is None else previous_reason,
                })
                continue
            if not (
                _valuation_matches_rule_card(current, rule["card"])
                and _valuation_matches_rule_card(previous, rule["card"])
            ):
                skipped_rules.append({
                    "rule_id": rule["id"],
                    "reason": "The supplied observed valuation does not match this rule's card identity.",
                })
                continue
            notification, dedup_key = _percent_move_event(rule, previous, current, evaluated_at)
            add(notification, dedup_key)
        elif rule["type"] == "new_comp":
            if not new_candidates:
                skipped_rules.append({
                    "rule_id": rule["id"],
                    "reason": "No new provenance-bearing observed comps were supplied.",
                })
                continue
            for notification, dedup_key in _new_comp_events(rule, new_candidates, evaluated_at):
                add(notification, dedup_key)
        elif rule["type"] == "hot_signal":
            candidates = _hot_signal_candidates(context)
            if not candidates:
                skipped_rules.append({
                    "rule_id": rule["id"],
                    "reason": "No evidence-backed market-pulse signal was supplied.",
                })
                continue
            for candidate in candidates:
                notification, dedup_key = _hot_signal_event(rule, candidate, evaluated_at)
                add(notification, dedup_key)

    return {
        "notifications": notifications,
        "active_condition_keys": sorted(set(active_condition_keys)),
        "suppressed_deduplicated_count": suppressed_count,
        "notification_limit_reached": notification_limit_reached,
        "invalid_rules": invalid_rules,
        "skipped_rules": skipped_rules,
        "evidence_policy": "Only direct valuations with provenance-bearing observed comps can trigger price alerts. Predictive estimates never trigger alerts.",
    }
