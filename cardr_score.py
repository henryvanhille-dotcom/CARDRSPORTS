"""Evidence-only CARDR Score and card price-history helpers.

This module has no database, HTTP, or template dependencies.  Callers supply
already-vetted :class:`market_data.Sale` objects (normally from
``SalesRepository.all_observed_sales()``), plus the card metadata being viewed.
It deliberately withholds an overall score when the card is not sufficiently
identified or when recorded sales cannot support a multi-month comparison.

The CARDR Score describes recorded market signals only.  It is not investment
advice and it does not predict future prices or returns.
"""

from collections import defaultdict
from datetime import date, timedelta
import math
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from market_data import Sale, clean_display, normalize_text


SCORE_DISCLAIMER = (
    "CARDR Score summarizes recorded market signals. It is not investment advice "
    "and does not predict future prices or returns."
)
MINIMUM_SALES_FOR_SCORE = 3
MINIMUM_MONTHS_FOR_SCORE = 2

IDENTITY_FIELDS = (
    "player",
    "year",
    "set_name",
    "card_type",
    "card_number",
    "parallel",
    "grade",
)


def _card_mapping(card: object) -> Dict[str, str]:
    """Accept CardQuery-like objects or mappings without coupling to FastAPI."""
    if hasattr(card, "as_dict"):
        raw = getattr(card, "as_dict")()
    elif isinstance(card, Mapping):
        raw = card
    else:
        raw = {}
    return {
        "player": clean_display(raw.get("player", "")),
        "year": clean_display(raw.get("year", "")),
        "set_name": clean_display(raw.get("set_name", raw.get("set", ""))),
        "card_type": clean_display(raw.get("card_type", raw.get("type", ""))),
        "card_number": clean_display(raw.get("card_number", raw.get("number", ""))),
        "parallel": clean_display(raw.get("parallel", "")),
        "grade": clean_display(raw.get("grade", "")),
    }


def _field_matches(expected: str, actual: str) -> bool:
    """Match only supplied fields, with the existing RC/Rookie equivalence."""
    expected_key = normalize_text(expected)
    actual_key = normalize_text(actual)
    if not expected_key:
        return True
    if not actual_key:
        return False
    return expected_key == actual_key or {expected_key, actual_key} <= {"rc", "rookie"}


def _identity_is_specific(identity: Mapping[str, str]) -> bool:
    """Require a concrete card, not a broad player/product segment, for a score."""
    return all(
        normalize_text(identity.get(field, ""))
        for field in ("player", "year", "set_name", "card_number")
    )


def _identity_can_match(identity: Mapping[str, str]) -> bool:
    """Require enough metadata before presenting any matching-sale history."""
    if not normalize_text(identity.get("player", "")):
        return False
    return any(
        normalize_text(identity.get(field, ""))
        for field in ("year", "set_name", "card_number")
    )


def _valid_sale(sale: object) -> bool:
    if not isinstance(sale, Sale) or not isinstance(sale.sale_date, date):
        return False
    try:
        return math.isfinite(float(sale.price_cad)) and float(sale.price_cad) > 0
    except (TypeError, ValueError):
        return False


def _matching_sales(
    identity: Mapping[str, str], observed_sales: Iterable[Sale]
) -> Tuple[List[Sale], Dict[str, int]]:
    """Return deduplicated, exact-on-supplied-fields, dated observed sales."""
    counts = {"input_sales": 0, "invalid_sales": 0, "non_matching_sales": 0, "duplicates": 0}
    matches: List[Sale] = []
    seen = set()
    for sale in observed_sales or []:
        counts["input_sales"] += 1
        if not _valid_sale(sale):
            counts["invalid_sales"] += 1
            continue
        if not all(
            _field_matches(identity[field], getattr(sale, field, ""))
            for field in IDENTITY_FIELDS
        ):
            counts["non_matching_sales"] += 1
            continue
        # A repeated listing ID must not make liquidity or confidence look better.
        key = clean_display(sale.id) or "{}|{}|{}".format(
            sale.sale_date.isoformat(), sale.price_cad, clean_display(sale.title)
        )
        if key in seen:
            counts["duplicates"] += 1
            continue
        seen.add(key)
        matches.append(sale)
    return sorted(matches, key=lambda item: (item.sale_date, item.id)), counts


def _history_points(sales: Sequence[Sale]) -> List[Dict[str, object]]:
    """Expose each observed transaction chronologically; never synthesize points."""
    return [
        {
            "date": sale.sale_date.isoformat(),
            "price": round(float(sale.price_cad), 2),
            "currency": "CAD",
            "sale_id": sale.id,
            "platform": sale.platform or "Imported sale",
        }
        for sale in sales
    ]


def build_card_price_history(card: object, observed_sales: Iterable[Sale]) -> Dict[str, object]:
    """Build actual chronological matching-sale history for a card-chart API.

    A result with one point remains ``available`` because it is a true observed
    transaction.  The frontend should use ``enough_for_chart`` to choose its
    single-sale empty state rather than fabricating a line.
    """
    identity = _card_mapping(card)
    if not _identity_can_match(identity):
        return {
            "status": "insufficient_data",
            "currency": "CAD",
            "card": identity,
            "points": [],
            "enough_for_chart": False,
            "reason": "Add a player and at least one of year, set, or card number to match recorded sales.",
        }

    sales, counts = _matching_sales(identity, observed_sales)
    points = _history_points(sales)
    if not points:
        return {
            "status": "insufficient_data",
            "currency": "CAD",
            "card": identity,
            "points": [],
            "enough_for_chart": False,
            "matching_basis": "Observed sales matching every supplied card field.",
            "evidence": counts,
            "reason": "No observed sales match the supplied card details yet.",
        }

    return {
        "status": "available",
        "currency": "CAD",
        "card": identity,
        "points": points,
        "enough_for_chart": len(points) >= 2,
        "matching_basis": "Observed sales matching every supplied card field.",
        "evidence": {**counts, "matching_sales": len(points)},
    }


def _monthly_medians(points: Sequence[Mapping[str, object]]) -> List[Dict[str, object]]:
    prices = defaultdict(list)
    for point in points:
        prices[str(point["date"])[:7]].append(float(point["price"]))
    return [
        {"month": month, "sales_count": len(values), "median_cad": round(median(values), 2)}
        for month, values in sorted(prices.items())
    ]


def _unavailable(label: str, reason: str) -> Dict[str, object]:
    return {"status": "insufficient_data", "score": None, "label": label, "explanation": reason}


def _momentum(months: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    if len(months) < MINIMUM_MONTHS_FOR_SCORE:
        return _unavailable(
            "Momentum unavailable",
            "At least two observed sale months are needed to compare monthly median prices.",
        )
    previous, current = months[-2], months[-1]
    previous_price = float(previous["median_cad"])
    current_price = float(current["median_cad"])
    if previous_price <= 0:
        return _unavailable("Momentum unavailable", "The earlier monthly median is not usable.")
    change = ((current_price - previous_price) / previous_price) * 100
    score = int(round(max(0, min(100, 50 + max(-40, min(40, change)) * 1.25))))
    if change > 2:
        explanation = "Latest monthly median CAD sale price is {:.1f}% higher than the prior observed month.".format(change)
    elif change < -2:
        explanation = "Latest monthly median CAD sale price is {:.1f}% lower than the prior observed month.".format(abs(change))
    else:
        explanation = "Latest two observed monthly median CAD sale prices are broadly unchanged ({:+.1f}%).".format(change)
    return {
        "status": "available",
        "score": score,
        "label": "Momentum",
        "change_percent": round(change, 2),
        "compared_months": [previous["month"], current["month"]],
        "explanation": explanation,
    }


def _liquidity(sale_count: int) -> Dict[str, object]:
    score = int(round(min(100, sale_count / 10 * 100)))
    if sale_count >= 8:
        detail = "Strong recorded matching-sale activity ({} observed sales).".format(sale_count)
    elif sale_count >= 4:
        detail = "Moderate recorded matching-sale activity ({} observed sales).".format(sale_count)
    else:
        detail = "Limited recorded matching-sale activity ({} observed sales).".format(sale_count)
    return {"status": "available", "score": score, "label": "Liquidity", "explanation": detail}


def _rarity(one_of_one: bool, print_run: Optional[object]) -> Dict[str, object]:
    if one_of_one:
        return {"status": "available", "score": 100, "label": "Rarity", "explanation": "Supplied metadata identifies this card as a 1-of-1."}
    try:
        run = int(print_run) if print_run not in (None, "") else None
    except (TypeError, ValueError):
        run = None
    if run is None or run < 1:
        return {"status": "neutral", "score": 50, "label": "Rarity", "explanation": "No verified print-run metadata was supplied, so rarity is neutral."}
    if run <= 5:
        score = 95
    elif run <= 10:
        score = 90
    elif run <= 25:
        score = 82
    elif run <= 99:
        score = 70
    elif run <= 250:
        score = 60
    else:
        score = 45
    return {"status": "available", "score": score, "label": "Rarity", "print_run": run, "explanation": "Supplied print run of {} informs the rarity signal.".format(run)}


def _data_confidence(sale_count: int, month_count: int, latest_date: date, today: date) -> Dict[str, object]:
    count_component = min(100, sale_count / 10 * 100)
    month_component = min(100, month_count / 6 * 100)
    age_days = max(0, (today - latest_date).days)
    recency_component = 100 if age_days <= 30 else 75 if age_days <= 90 else 50 if age_days <= 180 else 25 if age_days <= 365 else 10
    score = int(round(count_component * 0.45 + month_component * 0.35 + recency_component * 0.20))
    return {
        "status": "available",
        "score": score,
        "label": "Data confidence",
        "explanation": "Based on {} observed matching sales across {} month{}; latest sale is {} days old.".format(sale_count, month_count, "s" if month_count != 1 else "", age_days),
    }


def _market_activity(sales: Sequence[Sale], today: date) -> Dict[str, object]:
    latest = sales[-1].sale_date
    age_days = max(0, (today - latest).days)
    recent = sum(sale.sale_date >= today - timedelta(days=90) for sale in sales)
    recency = 100 if age_days <= 30 else 70 if age_days <= 90 else 40 if age_days <= 180 else 15
    volume = min(100, recent / 6 * 100)
    score = int(round(recency * 0.55 + volume * 0.45))
    return {
        "status": "available",
        "score": score,
        "label": "Market activity",
        "recent_sale_count": recent,
        "explanation": "{} matching observed sale{} occurred in the latest 90 days; latest sale is {} days old.".format(recent, "s" if recent != 1 else "", age_days),
    }


def _score_label(score: int) -> str:
    if score >= 75:
        return "Strong recorded signals"
    if score >= 60:
        return "Positive recorded signals"
    if score >= 40:
        return "Mixed recorded signals"
    return "Weaker recorded signals"


def calculate_cardr_score(
    card: object,
    observed_sales: Iterable[Sale],
    *,
    print_run: Optional[object] = None,
    one_of_one: bool = False,
    today: Optional[date] = None,
) -> Dict[str, object]:
    """Calculate a transparent 0–100 CARDR Score only when evidence supports it.

    ``observed_sales`` must already exclude rejected, demo, or unconfirmed sales.
    The function rejects malformed records and deduplicates sale IDs again as a
    defensive measure, so caller mistakes cannot inflate the score.
    """
    today = today or date.today()
    # A caller may provide a generator; retain the observations so history and
    # scoring inspect the identical evidence set.
    supplied_sales = list(observed_sales or [])
    identity = _card_mapping(card)
    history = build_card_price_history(identity, supplied_sales)
    base = {
        "currency": "CAD",
        "card": identity,
        "disclaimer": SCORE_DISCLAIMER,
        "history": history,
    }
    if not _identity_is_specific(identity):
        return {
            **base,
            "status": "insufficient_data",
            "score": None,
            "label": "Score unavailable",
            "subscores": {},
            "explanations": ["Add player, year, set, and card number before CARDR can score a specific card."],
            "reason": "The supplied card identity is too broad for a card-specific score.",
        }
    if history["status"] != "available":
        return {
            **base,
            "status": "insufficient_data",
            "score": None,
            "label": "Score unavailable",
            "subscores": {},
            "explanations": [history["reason"]],
            "reason": history["reason"],
        }

    points = history["points"]
    sales, _ = _matching_sales(identity, supplied_sales)
    months = _monthly_medians(points)
    subscores = {
        "momentum": _momentum(months),
        "liquidity": _liquidity(len(sales)),
        "rarity": _rarity(bool(one_of_one), print_run),
        "data_confidence": _data_confidence(len(sales), len(months), sales[-1].sale_date, today),
        "market_activity": _market_activity(sales, today),
    }
    explanations = [
        "Based on {} observed matching sales across {} month{}.".format(len(sales), len(months), "s" if len(months) != 1 else ""),
        *(item["explanation"] for item in subscores.values()),
    ]
    if len(sales) < MINIMUM_SALES_FOR_SCORE or len(months) < MINIMUM_MONTHS_FOR_SCORE:
        missing = []
        if len(sales) < MINIMUM_SALES_FOR_SCORE:
            missing.append("at least {} observed matching sales".format(MINIMUM_SALES_FOR_SCORE))
        if len(months) < MINIMUM_MONTHS_FOR_SCORE:
            missing.append("at least {} observed sale months".format(MINIMUM_MONTHS_FOR_SCORE))
        return {
            **base,
            "status": "insufficient_data",
            "score": None,
            "label": "Score unavailable",
            "subscores": subscores,
            "monthly_history": months,
            "explanations": explanations,
            "reason": "CARDR needs {} before displaying an overall score.".format(" and ".join(missing)),
        }

    score = int(round(
        subscores["momentum"]["score"] * 0.30
        + subscores["liquidity"]["score"] * 0.25
        + subscores["rarity"]["score"] * 0.20
        + subscores["data_confidence"]["score"] * 0.15
        + subscores["market_activity"]["score"] * 0.10
    ))
    return {
        **base,
        "status": "available",
        "score": max(0, min(100, score)),
        "label": _score_label(score),
        "subscores": subscores,
        "monthly_history": months,
        "explanations": explanations,
        "weights": {
            "momentum": 0.30,
            "liquidity": 0.25,
            "rarity": 0.20,
            "data_confidence": 0.15,
            "market_activity": 0.10,
        },
    }
