"""Evidence-only market-pulse summaries for Cardr.

The module is deliberately independent of FastAPI, SQLite, and live provider
calls.  It accepts only already-vetted :class:`market_data.Sale` records and
will not create a trend unless a single, strictly identified card has enough
observed CAD sales in two consecutive comparison months.
"""

from collections import defaultdict
from datetime import date
import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from market_data import Sale, clean_display, normalize_text
from trends import build_monthly_history, calculate_market_signal, calculate_trend


MINIMUM_SALES_PER_COMPARED_MONTH = 2

# A blank parallel is meaningful for a base card.  Every other field below is
# needed before sales can safely be treated as one card rather than a broad
# player/product segment.
IDENTITY_FIELDS = (
    "player",
    "year",
    "set_name",
    "card_type",
    "card_number",
    "parallel",
    "grade",
)
REQUIRED_IDENTITY_FIELDS = tuple(field for field in IDENTITY_FIELDS if field != "parallel")


def _identity_for_sale(sale: Sale) -> Tuple[Optional[Dict[str, str]], Tuple[str, ...]]:
    """Return the display identity and any required fields that are absent."""
    identity = {
        field: clean_display(getattr(sale, field, ""))
        for field in IDENTITY_FIELDS
    }
    missing = tuple(
        field
        for field in REQUIRED_IDENTITY_FIELDS
        if not normalize_text(identity[field])
    )
    return (None, missing) if missing else (identity, ())


def _identity_key(identity: Dict[str, str]) -> Tuple[str, ...]:
    """Use normalized equality only; this intentionally does no fuzzy matching."""
    return tuple(normalize_text(identity[field]) for field in IDENTITY_FIELDS)


def _usable_sale(sale: object) -> bool:
    """Defend this pure layer against incomplete records supplied by a caller."""
    if not isinstance(sale, Sale) or not isinstance(sale.sale_date, date):
        return False
    try:
        return math.isfinite(float(sale.price_cad)) and float(sale.price_cad) > 0
    except (TypeError, ValueError):
        return False


def _cad_history(history: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    """Make the currency explicit instead of leaking generic trend helper names."""
    return [
        {
            "month": item["month"],
            "sales_count": item["sales_count"],
            "low_cad": item["low"],
            "high_cad": item["high"],
            "average_cad": item["average"],
            "median_cad": item["median"],
        }
        for item in history
    ]


def _insufficient_group(
    identity: Dict[str, str],
    history: Sequence[Dict[str, object]],
    reason: str,
) -> Dict[str, object]:
    """Return a group without an implied flat trend or neutral score."""
    return {
        "status": "insufficient_data",
        "card": identity,
        "currency": "CAD",
        "observed_sale_count": sum(int(item["sales_count"]) for item in history),
        "monthly_history": _cad_history(history),
        "reason": reason,
    }


def _pulse_for_group(identity: Dict[str, str], sales: Sequence[Sale]) -> Dict[str, object]:
    """Build one card's pulse, comparing only its two most recent months."""
    history = build_monthly_history([sale.as_dict() for sale in sales])

    if len(history) < 2:
        return _insufficient_group(
            identity,
            history,
            "At least two different months of observed sales are required to calculate a trend.",
        )

    # Never cherry-pick older months to manufacture a favourable comparison.
    # The latest two observed months are the only months used for the signal.
    compared_history = history[-2:]
    if any(item["sales_count"] < MINIMUM_SALES_PER_COMPARED_MONTH for item in compared_history):
        month_counts = ", ".join(
            "{}: {}".format(item["month"], item["sales_count"])
            for item in compared_history
        )
        return _insufficient_group(
            identity,
            history,
            "Each compared month needs at least {} observed sales ({}).".format(
                MINIMUM_SALES_PER_COMPARED_MONTH,
                month_counts,
            ),
        )

    # `trends` works from monthly median prices.  It is only invoked after the
    # evidence thresholds above, so its neutral fallback can never masquerade
    # as a genuine market score.
    trend = calculate_trend(compared_history)
    signal = calculate_market_signal(compared_history)

    return {
        "status": "success",
        "card": identity,
        "currency": "CAD",
        "observed_sale_count": sum(int(item["sales_count"]) for item in history),
        "monthly_history": _cad_history(history),
        "compared_months": [item["month"] for item in compared_history],
        "trend": {
            "direction": trend["trend"],
            "change_percent": trend["trend_percent"],
            "momentum": trend["momentum"],
            "basis": "Change between the two most recent monthly median CAD sale prices.",
        },
        "market_signal": {
            "label": signal["signal"],
            "score": signal["score"],
            "basis": "Derived from the compared monthly median CAD sale prices only.",
        },
    }


def build_market_pulse(observed_sales: Iterable[Sale]) -> Dict[str, object]:
    """Return transparent, strict-card market pulses from observed CAD sales.

    The top-level payload is ``insufficient_data`` when no strict card group
    meets the minimum evidence threshold.  Individual groups always carry
    their own status, allowing an interface to show an honest empty state next
    to any cards that do have enough history.
    """
    supplied_sales = list(observed_sales or [])
    groups: Dict[Tuple[str, ...], List[Sale]] = defaultdict(list)
    identities: Dict[Tuple[str, ...], Dict[str, str]] = {}
    excluded_invalid = 0
    excluded_incomplete_identity = 0

    for sale in supplied_sales:
        if not _usable_sale(sale):
            excluded_invalid += 1
            continue
        identity, missing = _identity_for_sale(sale)
        if identity is None:
            excluded_incomplete_identity += 1
            continue
        key = _identity_key(identity)
        groups[key].append(sale)
        identities.setdefault(key, identity)

    pulses = [
        _pulse_for_group(identities[key], groups[key])
        for key in sorted(groups)
    ]
    valid_count = sum(pulse["status"] == "success" for pulse in pulses)

    payload: Dict[str, object] = {
        "status": "success" if valid_count else "insufficient_data",
        "currency": "CAD",
        "input_sale_count": len(supplied_sales),
        "usable_sale_count": sum(len(group) for group in groups.values()),
        "excluded_observations": {
            "invalid_sale": excluded_invalid,
            "incomplete_card_identity": excluded_incomplete_identity,
        },
        "minimum_sales_per_compared_month": MINIMUM_SALES_PER_COMPARED_MONTH,
        "groups": pulses,
    }
    if not valid_count:
        payload["reason"] = (
            "No strictly identified card has at least two observed sales in each of two compared months."
        )
    return payload
