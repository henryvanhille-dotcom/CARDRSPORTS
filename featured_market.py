"""Evidence-backed featured market signals for CARDR's home screen."""

from collections import defaultdict
from datetime import date, timedelta
from statistics import median
from typing import Dict, Iterable, List, Tuple

from market_data import Sale, clean_display, normalize_text


def _group_key(sale: Sale) -> Tuple[str, str, str]:
    return (
        normalize_text(sale.player),
        normalize_text(sale.set_name),
        normalize_text(sale.card_type),
    )


def _label(sale: Sale) -> str:
    return clean_display(sale.player) or clean_display(sale.set_name) or "Observed market"


def build_featured_market(sales: Iterable[Sale], limit: int = 8) -> Dict[str, object]:
    """Rank genuinely active card groups without manufacturing a trend.

    Signals use the most recent 120-day window available in the stored data,
    not the current calendar date.  That keeps an imported historical archive
    useful while making the evidence window explicit in the response.
    """
    observed = [sale for sale in sales if sale.price_cad > 0 and sale.sale_date]
    if not observed:
        return {
            "status": "insufficient_data",
            "featured": [],
            "as_of": None,
            "all_time_sales_count": 0,
            "note": "Featured signals appear after CARDR has observed completed sales.",
        }

    as_of = max(sale.sale_date for sale in observed if sale.sale_date)
    recent_start = as_of - timedelta(days=119)
    prior_start = recent_start - timedelta(days=120)
    groups: Dict[Tuple[str, str, str], List[Sale]] = defaultdict(list)
    for sale in observed:
        key = _group_key(sale)
        if any(key):
            groups[key].append(sale)

    featured = []
    for grouped_sales in groups.values():
        grouped_sales.sort(key=lambda sale: sale.sale_date or date.min, reverse=True)
        recent = [sale for sale in grouped_sales if sale.sale_date and sale.sale_date >= recent_start]
        previous = [
            sale
            for sale in grouped_sales
            if sale.sale_date and prior_start <= sale.sale_date < recent_start
        ]
        if not recent:
            continue

        change = None
        if len(recent) >= 2 and len(previous) >= 2:
            prior_median = median(sale.price_cad for sale in previous)
            if prior_median > 0:
                change = round((median(sale.price_cad for sale in recent) - prior_median) / prior_median * 100, 1)

        if change is not None and change >= 8:
            emoji, signal = "📈", "Rising"
        elif change is not None and change <= -8:
            emoji, signal = "📉", "Falling"
        elif len(recent) >= 3:
            emoji, signal = "🔥", "Active"
        else:
            emoji, signal = "🧭", "Observed"

        latest = grouped_sales[0]
        featured.append({
            "label": _label(latest),
            "subtitle": " · ".join(filter(None, (clean_display(latest.set_name), clean_display(latest.card_type)))) or "Completed-sale activity",
            "emoji": emoji,
            "signal": signal,
            "change_percent": change,
            "observed_sale_count": len(grouped_sales),
            "recent_sale_count": len(recent),
            "last_sale_date": latest.sale_date.isoformat() if latest.sale_date else None,
            "evidence_note": "Signal is based on observed completed sales; it is not a price forecast.",
            "activity_score": (len(recent) * 10) + (abs(change) if change is not None else 0),
        })

    featured.sort(
        key=lambda item: (
            item["activity_score"],
            item["observed_sale_count"],
            item["last_sale_date"] or "",
        ),
        reverse=True,
    )
    for item in featured:
        item.pop("activity_score", None)
    return {
        "status": "available" if featured else "insufficient_data",
        "featured": featured[:max(1, min(limit, 16))],
        "as_of": as_of.isoformat(),
        "all_time_sales_count": len(observed),
        "note": "Featured signals use all-time stored history and the latest available 120-day activity window.",
    }
