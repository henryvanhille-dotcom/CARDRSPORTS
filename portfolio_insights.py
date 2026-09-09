"""Evidence-first portfolio summaries and CARDR Pulse building blocks.

This module deliberately has no database or web-framework dependency.  It
accepts the existing Vault card dictionaries and vetted ``Sale`` records, so
routes can reuse it without introducing a second card model or silently
writing historical data.  In particular, it distinguishes a current gain/loss
from a *mover*: no mover is claimed until a real prior valuation is recorded.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from market_data import Sale, clean_display, normalize_text
from market_pulse import build_market_pulse


CURRENCY = "CAD"
COMPOSITION_FIELDS: Tuple[str, ...] = (
    "player",
    "year",
    "set_name",
    "grade",
    "card_type",
)


def _finite_nonnegative(value: object) -> Optional[float]:
    """Return a usable monetary amount without converting absent data to zero."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 2) if math.isfinite(number) and number >= 0 else None


def _safe_date(value: object) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None


def _card_identity(card: Mapping[str, Any]) -> Dict[str, Any]:
    """Return public card metadata only; private costs and notes stay out."""
    return {
        "id": card.get("id"),
        "player": clean_display(card.get("player")),
        "year": card.get("year"),
        "set_name": clean_display(card.get("set_name")),
        "card_type": clean_display(card.get("card_type")),
        "card_number": clean_display(card.get("card_number")),
        "parallel": clean_display(card.get("parallel")),
        "grade": clean_display(card.get("grade")),
        "image_url": clean_display(card.get("image_url")),
    }


def _tracked_position(card: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a valid cost-basis comparison, or ``None`` when one is missing."""
    purchase = _finite_nonnegative(card.get("purchase_price_cad"))
    value = _finite_nonnegative(card.get("estimated_value_cad"))
    if purchase is None or purchase <= 0 or value is None:
        return None
    change = round(value - purchase, 2)
    return {
        **_card_identity(card),
        "purchase_price_cad": purchase,
        "estimated_value_cad": value,
        "change_cad": change,
        "return_percent": round((change / purchase) * 100, 2),
    }


def _sum_or_none(values: Iterable[Optional[float]]) -> Optional[float]:
    present = [value for value in values if value is not None]
    return round(sum(present), 2) if present else None


def build_portfolio_overview(cards: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Build truthful current portfolio totals and current-gain leaders.

    A missing estimate never becomes a $0 estimate.  Profit/loss and percentage
    return include only cards with both a positive purchase basis and a current
    estimate.  ``biggest_winner`` / ``biggest_loser`` are absent rather than
    treating an unchanged card as a winner or loser.
    """
    card_list = [card for card in (cards or []) if isinstance(card, Mapping)]
    estimates = [_finite_nonnegative(card.get("estimated_value_cad")) for card in card_list]
    purchases = [_finite_nonnegative(card.get("purchase_price_cad")) for card in card_list]
    positions = [position for card in card_list if (position := _tracked_position(card))]
    winners = [position for position in positions if position["change_cad"] > 0]
    losers = [position for position in positions if position["change_cad"] < 0]

    estimated_value = _sum_or_none(estimates)
    total_invested = _sum_or_none(purchases)
    tracked_basis = round(sum(position["purchase_price_cad"] for position in positions), 2) if positions else None
    tracked_change = round(sum(position["change_cad"] for position in positions), 2) if positions else None

    return {
        "currency": CURRENCY,
        "card_count": len(card_list),
        "valued_card_count": sum(value is not None for value in estimates),
        "unvalued_card_count": sum(value is None for value in estimates),
        "estimated_value_cad": estimated_value,
        "estimated_value_status": (
            "unavailable" if estimated_value is None else
            "complete" if all(value is not None for value in estimates) else "partial"
        ),
        "total_invested_cad": total_invested,
        "invested_card_count": sum(value is not None for value in purchases),
        "tracked_cost_basis_cad": tracked_basis,
        "tracked_profit_loss_cad": tracked_change,
        "tracked_return_percent": (
            round((tracked_change / tracked_basis) * 100, 2)
            if tracked_basis is not None and tracked_basis > 0 and tracked_change is not None
            else None
        ),
        "positions_with_valid_basis": len(positions),
        "biggest_winner": max(winners, key=lambda item: item["change_cad"], default=None),
        "biggest_loser": min(losers, key=lambda item: item["change_cad"], default=None),
    }


def build_portfolio_composition(cards: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Group the current Vault by supported identity fields without fake labels."""
    card_list = [card for card in (cards or []) if isinstance(card, Mapping)]
    dimensions: Dict[str, Dict[str, Any]] = {}
    for field in COMPOSITION_FIELDS:
        groups: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
        unclassified = 0
        for card in card_list:
            label = clean_display(card.get(field))
            if not normalize_text(label):
                unclassified += 1
                continue
            groups[label].append(card)
        rows = []
        for label, grouped_cards in groups.items():
            overview = build_portfolio_overview(grouped_cards)
            rows.append({
                "label": label,
                "card_count": overview["card_count"],
                "valued_card_count": overview["valued_card_count"],
                "estimated_value_cad": overview["estimated_value_cad"],
                "estimated_value_status": overview["estimated_value_status"],
                "tracked_profit_loss_cad": overview["tracked_profit_loss_cad"],
            })
        rows.sort(key=lambda item: (item["estimated_value_cad"] is None, -(item["estimated_value_cad"] or 0), item["label"].lower()))
        dimensions[field] = {"groups": rows, "unclassified_card_count": unclassified}
    return {"currency": CURRENCY, "card_count": len(card_list), "dimensions": dimensions}


def make_portfolio_snapshot(
    cards: Iterable[Mapping[str, Any]],
    captured_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Make (but never persist) a real, future portfolio snapshot record.

    Callers can store this exact shape once per day after wiring a snapshot
    table.  No backfill is attempted, so a new graph starts at its first real
    observation rather than a fabricated history.
    """
    overview = build_portfolio_overview(cards)
    timestamp = captured_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return {
        "schema_version": 1,
        "captured_at": timestamp.astimezone(timezone.utc).isoformat(),
        "currency": CURRENCY,
        "card_count": overview["card_count"],
        "valued_card_count": overview["valued_card_count"],
        "unvalued_card_count": overview["unvalued_card_count"],
        "estimated_value_cad": overview["estimated_value_cad"],
        "estimated_value_status": overview["estimated_value_status"],
        "total_invested_cad": overview["total_invested_cad"],
        "tracked_cost_basis_cad": overview["tracked_cost_basis_cad"],
        "tracked_profit_loss_cad": overview["tracked_profit_loss_cad"],
        "tracked_return_percent": overview["tracked_return_percent"],
    }


def _usable_sale(sale: object) -> bool:
    if not isinstance(sale, Sale) or sale.sale_date is None:
        return False
    return _finite_nonnegative(sale.price_cad) not in (None, 0.0)


def _sale_identity(sale: Sale) -> Dict[str, Any]:
    return {
        "id": sale.id,
        "player": sale.player,
        "year": sale.year,
        "set_name": sale.set_name,
        "card_type": sale.card_type,
        "card_number": sale.card_number,
        "parallel": sale.parallel,
        "grade": sale.grade,
        "price_cad": round(float(sale.price_cad), 2),
        "sale_date": sale.sale_date.isoformat() if sale.sale_date else None,
        "platform": sale.platform,
        "listing_url": sale.listing_url,
        "image_url": sale.image_url,
    }


def _strict_sale_key(sale: Sale) -> Optional[Tuple[str, ...]]:
    fields = ("player", "year", "set_name", "card_type", "card_number", "grade")
    values = tuple(normalize_text(getattr(sale, field, "")) for field in fields)
    return values if all(values) else None


def _recent_additions(cards: Sequence[Mapping[str, Any]], limit: int = 3) -> List[Dict[str, Any]]:
    dated = []
    for card in cards:
        added_on = _safe_date(card.get("created_at"))
        if added_on is not None:
            dated.append((added_on, card))
    dated.sort(key=lambda item: item[0], reverse=True)
    return [{**_card_identity(card), "added_at": added_on.isoformat()} for added_on, card in dated[:limit]]


def build_cardr_pulse(
    vault_cards: Iterable[Mapping[str, Any]],
    observed_sales: Iterable[Sale],
    *,
    as_of: Optional[date] = None,
    recent_days: int = 30,
) -> Dict[str, Any]:
    """Build CARDR's concise briefing from current data only.

    ``trending`` delegates to the strict existing market-pulse evidence rules.
    ``movers`` is explicitly unavailable until CARDR records past valuations;
    purchase-to-current gain/loss is exposed separately and never relabelled as
    a period move.
    """
    if recent_days < 1:
        raise ValueError("recent_days must be at least 1.")
    cards = [card for card in (vault_cards or []) if isinstance(card, Mapping)]
    usable_sales = [sale for sale in (observed_sales or []) if _usable_sale(sale)]
    reference_date = as_of or date.today()
    window_start = reference_date - timedelta(days=recent_days - 1)
    recent_sales = [sale for sale in usable_sales if window_start <= sale.sale_date <= reference_date]
    overview = build_portfolio_overview(cards)

    player_groups: Dict[str, List[Sale]] = defaultdict(list)
    card_groups: Dict[Tuple[str, ...], List[Sale]] = defaultdict(list)
    for sale in recent_sales:
        player = clean_display(sale.player)
        if normalize_text(player):
            player_groups[player].append(sale)
        key = _strict_sale_key(sale)
        if key:
            card_groups[key].append(sale)

    active_players = [
        {"player": player, "observed_sale_count": len(group)}
        for player, group in player_groups.items()
        if len(group) >= 2
    ]
    active_players.sort(key=lambda item: (-item["observed_sale_count"], item["player"].lower()))
    active_cards = [
        {"card": _sale_identity(group[0]) | {"price_cad": None, "sale_date": None, "platform": "", "listing_url": "", "image_url": ""}, "observed_sale_count": len(group)}
        for group in card_groups.values()
        if len(group) >= 2
    ]
    active_cards.sort(key=lambda item: (-item["observed_sale_count"], str(item["card"]["player"]).lower()))

    strict_market_pulse = build_market_pulse(usable_sales)
    supported_trends = [group for group in strict_market_pulse["groups"] if group["status"] == "success"]
    return {
        "currency": CURRENCY,
        "as_of": reference_date.isoformat(),
        "your_vault": {
            "status": "available" if cards else "empty",
            "card_count": overview["card_count"],
            "recent_additions": _recent_additions(cards),
            "biggest_current_gain": overview["biggest_winner"],
            "biggest_current_loss": overview["biggest_loser"],
            "movers": {
                "status": "insufficient_data",
                "reason": "CARDR has not recorded a prior portfolio valuation to compare yet.",
            },
        },
        "recent_market_activity": {
            "status": "available" if recent_sales else "insufficient_data",
            "window": {"start_date": window_start.isoformat(), "end_date": reference_date.isoformat(), "days": recent_days},
            "observed_sale_count": len(recent_sales),
            "largest_recent_sale": _sale_identity(max(recent_sales, key=lambda sale: sale.price_cad)) if recent_sales else None,
            "active_players": active_players,
            "active_cards": active_cards,
            "reason": None if recent_sales else "No observed sales fall inside this recent activity window.",
        },
        "trending": {
            "status": "available" if supported_trends else "insufficient_data",
            "cards": supported_trends,
            "reason": None if supported_trends else "Not enough market data yet.",
            "evidence_rule": "A trend requires two observed sales in each of two compared months for one strictly identified card.",
        },
    }
