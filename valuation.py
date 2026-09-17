"""Evidence-first card valuation.

Direct estimates use stored, non-demo completed sales.  When a specific card
has no direct match, Prospectr still returns a number using a clearly labelled
fallback ladder.  Those numbers are deliberately low confidence and never
presented as an observed sale or an exact-card value.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from statistics import median
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from market_data import Sale, normalize_text


# This only applies when CARDR has *zero* observed, non-demo sales to use as a
# market prior.  It makes the "every card gets an estimate" promise possible
# on a fresh installation while keeping the result visibly low confidence.
UNANCHORED_DEFAULT_CAD = 25.0


@dataclass(frozen=True)
class CardQuery:
    player: str = ""
    year: str = ""
    set_name: str = ""
    card_type: str = ""
    card_number: str = ""
    parallel: str = ""
    grade: str = ""

    @classmethod
    def from_mapping(cls, data: Dict[str, object]) -> "CardQuery":
        return cls(
            player=str(data.get("player", "")).strip(),
            year=str(data.get("year", "")).strip(),
            set_name=str(data.get("set_name", data.get("set", ""))).strip(),
            card_type=str(data.get("card_type", "")).strip(),
            card_number=str(data.get("card_number", data.get("number", ""))).strip(),
            parallel=str(data.get("parallel", "")).strip(),
            grade=str(data.get("grade", "")).strip(),
        )

    def as_dict(self) -> Dict[str, str]:
        return {
            "player": self.player,
            "year": self.year,
            "set_name": self.set_name,
            "card_type": self.card_type,
            "card_number": self.card_number,
            "parallel": self.parallel,
            "grade": self.grade,
        }


@dataclass(frozen=True)
class MatchedSale:
    sale: Sale
    score: int
    level: str
    reasons: Tuple[str, ...]

    def as_dict(self) -> Dict[str, object]:
        value = self.sale.as_dict()
        value.update(
            {
                "match_score": self.score,
                "match_level": self.level,
                "match_reasons": list(self.reasons),
            }
        )
        return value


def _field_match(expected: str, actual: str) -> bool:
    expected_key = normalize_text(expected)
    actual_key = normalize_text(actual)
    if not expected_key:
        return True
    if not actual_key:
        return False
    if expected_key == actual_key:
        return True
    # These two labels describe the same card feature. Product names are
    # intentionally *not* fuzzy-matched: "Topps Chrome" and "Topps Chrome
    # Update" are materially different products and must not be exact comps.
    return {expected_key, actual_key} <= {"rc", "rookie"}


def match_sale(card: CardQuery, sale: Sale) -> Optional[MatchedSale]:
    """Rank one observed sale without pretending a broad comp is an exact sale."""
    if not card.player or normalize_text(card.player) != normalize_text(sale.player):
        return None
    # A known, conflicting card number is a different card—not a weak direct
    # comp.  This prevents #145 or a Rookie Debut from being blended into an
    # HMT1-style exact-card estimate just because the player and set overlap.
    if (
        card.card_number
        and sale.card_number
        and normalize_text(card.card_number) != normalize_text(sale.card_number)
    ):
        return None

    score = 45
    reasons: List[str] = ["same player"]
    comparisons = (
        ("year", card.year, sale.year, 15),
        ("set", card.set_name, sale.set_name, 15),
        ("card type", card.card_type, sale.card_type, 5),
        ("card number", card.card_number, sale.card_number, 10),
        ("parallel", card.parallel, sale.parallel, 7),
        ("grade", card.grade, sale.grade, 3),
    )
    matched = {}
    for label, expected, actual, weight in comparisons:
        is_match = _field_match(expected, actual)
        matched[label] = is_match
        if expected and is_match:
            score += weight
            reasons.append("same " + label)

    # A player-only transaction is too broad to place a dollar figure on a
    # distinct card. Require at least product or year evidence.
    if score < 70:
        return None

    required = [label for label, expected, _, _ in comparisons if expected]
    if card.card_number and required and all(matched[label] for label in required):
        level = "exact"
    elif required and all(matched[label] for label in required):
        level = "matching_supplied_details"
    elif (
        matched["year"]
        and matched["set"]
        and matched["parallel"]
        and card.grade
        and not matched["grade"]
    ):
        level = "same_card_different_grade"
    elif matched["year"] and matched["set"]:
        level = "same_player_same_set"
    else:
        level = "same_player_similar_card"

    return MatchedSale(sale=sale, score=score, level=level, reasons=tuple(reasons))


def find_comps(card: CardQuery, sales: Iterable[Sale], limit: int = 20) -> List[MatchedSale]:
    matches = [match_sale(card, sale) for sale in sales]
    matches = [item for item in matches if item is not None]
    return sorted(
        matches,
        key=lambda item: (item.score, item.sale.sale_date or date.min),
        reverse=True,
    )[:limit]


def find_predictive_comps(
    card: CardQuery,
    sales: Iterable[Sale],
    limit: int = 12,
) -> List[MatchedSale]:
    """Find transparent same-player anchors for a no-sale prediction.

    These are intentionally broader than direct valuation comps, so callers
    must label any result as a model prediction rather than a market-backed
    sale estimate.
    """
    matches: List[MatchedSale] = []
    player_key = normalize_text(card.player)
    if not player_key:
        return matches

    for sale in sales:
        if player_key != normalize_text(sale.player):
            continue

        score = 45
        reasons = ["same player"]
        for label, expected, actual, weight in (
            ("year", card.year, sale.year, 16),
            ("set", card.set_name, sale.set_name, 22),
            ("card type", card.card_type, sale.card_type, 5),
            ("grade", card.grade, sale.grade, 4),
            ("parallel", card.parallel, sale.parallel, 5),
        ):
            if expected and _field_match(expected, actual):
                score += weight
                reasons.append("same " + label)

        # Require the same player and at least the same year or product.
        # Similar-looking cards from other seasons or products are not useful
        # enough to anchor a dollar prediction.
        if score < 65:
            continue
        matches.append(
            MatchedSale(
                sale=sale,
                score=score,
                level="predictive_related",
                reasons=tuple(reasons),
            )
        )

    return sorted(
        matches,
        key=lambda item: (item.score, item.sale.sale_date or date.min),
        reverse=True,
    )[:limit]


def _is_first_bowman_autograph(sale: Sale) -> bool:
    text = normalize_text(" ".join((sale.title, sale.set_name, sale.card_type, sale.parallel)))
    is_first_bowman = "1st bowman" in text or "first bowman" in text
    is_autograph = any(term in text for term in (" auto", " autograph", " signed", " signature"))
    return is_first_bowman and is_autograph


def find_first_bowman_auto_comps(
    sales: Iterable[Sale],
    today: date,
    limit: int = 12,
) -> List[MatchedSale]:
    """Return all observed first-Bowman auto sales for a segment fallback.

    A segment fallback is useful only for a user-confirmed first-Bowman auto.
    It is intentionally labelled as market-segment evidence, never as a
    player-specific comparable.
    """
    comps = []
    for sale in sales:
        if not _is_first_bowman_autograph(sale):
            continue
        comps.append(
            MatchedSale(
                sale=sale,
                score=60,
                level="first_bowman_auto_segment",
                reasons=("all-time first-Bowman autograph market segment",),
            )
        )
    return sorted(comps, key=lambda item: item.sale.sale_date or date.min, reverse=True)[:limit]


def find_broad_similarity_comps(
    card: CardQuery,
    sales: Iterable[Sale],
    limit: int = 24,
) -> List[MatchedSale]:
    """Find labelled, non-player-specific market-segment anchors.

    A broad comparable must share at least one real card attribute.  It is
    never returned from :func:`find_comps`, so callers cannot accidentally
    display it as an exact or direct sale.
    """
    matches: List[MatchedSale] = []
    for sale in sales:
        # Same-player sales belong in the stronger related-player tier.
        if card.player and normalize_text(card.player) == normalize_text(sale.player):
            continue

        score = 0
        reasons: List[str] = []
        exact_set_match = False
        comparisons = (
            ("same set", card.set_name, sale.set_name, 35),
            ("same year", card.year, sale.year, 10),
            ("same card type", card.card_type, sale.card_type, 10),
            ("same parallel", card.parallel, sale.parallel, 7),
            ("same grade", card.grade, sale.grade, 5),
        )
        for label, expected, actual, points in comparisons:
            if expected and _field_match(expected, actual):
                score += points
                reasons.append(label)
                if label == "same set":
                    exact_set_match = True

        # A shared product-family token is weaker than an exact product but is
        # still useful when the supplied set is sparse (for example, Bowman
        # Chrome across a release family).  It cannot make a match by itself.
        set_tokens = set(normalize_text(card.set_name).split())
        sale_tokens = set(normalize_text(sale.set_name).split())
        shared_tokens = set_tokens & sale_tokens
        if shared_tokens and normalize_text(card.set_name) != normalize_text(sale.set_name):
            score += 12
            reasons.append("shared product family")

        # A shared card type alone (for example, just "RC") is not a
        # meaningful similarity signal.  Require an exact product, or two
        # independent attributes, before calling a sale broadly similar.
        if not reasons or (not exact_set_match and len(reasons) < 2):
            continue
        matches.append(
            MatchedSale(
                sale=sale,
                score=min(70, score),
                level="broad_similar_card",
                reasons=tuple(reasons),
            )
        )

    return sorted(
        matches,
        key=lambda item: (item.score, item.sale.sale_date or date.min),
        reverse=True,
    )[:limit]


def _weighted_quantile(items: Sequence[Tuple[float, float]], quantile: float) -> float:
    ordered = sorted(items, key=lambda item: item[0])
    target = sum(weight for _, weight in ordered) * quantile
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= target:
            return value
    return ordered[-1][0]


def _remove_outliers(comps: Sequence[MatchedSale]) -> List[MatchedSale]:
    if len(comps) < 5:
        return list(comps)
    centre = median(comp.sale.price_cad for comp in comps)
    filtered = [
        comp
        for comp in comps
        if centre / 3 <= comp.sale.price_cad <= centre * 3
    ]
    return filtered if len(filtered) >= 3 else list(comps)


def _weight(comp: MatchedSale, today: date) -> float:
    quality = (comp.score / 100.0) ** 3
    if comp.sale.sale_date is None:
        recency = 0.55
    else:
        age_days = max(0, (today - comp.sale.sale_date).days)
        recency = 0.55 + 0.45 * math.exp(-age_days / 365.0)
    return max(0.01, quality * recency)


def _confidence(comps: Sequence[MatchedSale], today: date) -> int:
    if not comps:
        return 0
    count_points = min(38, len(comps) * 7)
    precision_points = round(sum(comp.score for comp in comps) / len(comps) * 0.34)
    dated = [comp.sale.sale_date for comp in comps if comp.sale.sale_date]
    if not dated:
        recency_points = 4
    else:
        median_age = median(max(0, (today - sold_on).days) for sold_on in dated)
        recency_points = max(3, round(18 * math.exp(-median_age / 540.0)))
    exact_bonus = 8 if any(comp.level == "exact" for comp in comps) else 0
    return min(94, max(0, count_points + precision_points + recency_points + exact_bonus))


def _rationale(comps: Sequence[MatchedSale], card: CardQuery, excluded_count: int) -> List[str]:
    exact = sum(comp.level == "exact" for comp in comps)
    direct = sum(comp.level in {"exact", "matching_supplied_details"} for comp in comps)
    lines = [
        "Based on {} stored, non-demo comparable sale{} for {}.".format(
            len(comps), "s" if len(comps) != 1 else "", card.player
        )
    ]
    if exact:
        lines.append("{} comp{} match{} every card detail supplied.".format(
            exact,
            "s" if exact != 1 else "",
            "" if exact != 1 else "es",
        ))
    elif direct:
        lines.append("{} comp{} match{} every supplied detail; add the card number to verify exact-card matches.".format(
            direct,
            "s" if direct != 1 else "",
            "" if direct != 1 else "es",
        ))
    else:
        lines.append("No exact match was stored; the estimate uses clearly labelled same-player comparables.")
    if excluded_count:
        lines.append("{} extreme price point{} were excluded from the calculation.".format(
            excluded_count, "s" if excluded_count != 1 else ""))
    lines.append("This is a market-evidence estimate, not investment advice or a future-price forecast.")
    return lines


def _scarcity_profile(one_of_one: bool, print_run: Optional[int]) -> Tuple[float, str]:
    """Return a conservative scarcity prior and a human-readable explanation.

    This is deliberately a model assumption, not a claim about an observed
    sale premium. The output is paired with a broad range and capped
    confidence by the predictive valuation path below.
    """
    if one_of_one or print_run == 1:
        return 3.0, "1-of-1 scarcity prior"
    if print_run is None:
        return 1.0, "no print-run premium supplied"
    if print_run <= 5:
        return 2.1, "print run of {} scarcity prior".format(print_run)
    if print_run <= 10:
        return 1.8, "print run of {} scarcity prior".format(print_run)
    if print_run <= 25:
        return 1.55, "print run of {} scarcity prior".format(print_run)
    if print_run <= 99:
        return 1.3, "print run of {} scarcity prior".format(print_run)
    if print_run <= 250:
        return 1.15, "print run of {} scarcity prior".format(print_run)
    return 1.0, "print run of {} has no model premium".format(print_run)


def _sales_chart(comps: Sequence[MatchedSale]) -> List[Dict[str, object]]:
    """Return a compact monthly sale series for the interface chart."""
    months = defaultdict(list)
    for comp in comps:
        if comp.sale.sale_date is not None:
            months[comp.sale.sale_date.strftime("%Y-%m")].append(comp.sale.price_cad)
    return [
        {
            "month": month,
            "sales_count": len(prices),
            "low_cad": round(min(prices), 2),
            "median_cad": round(median(prices), 2),
            "high_cad": round(max(prices), 2),
        }
        for month, prices in sorted(months.items())[-12:]
    ]


def _all_market_baseline(sales: Iterable[Sale]) -> Tuple[Optional[float], List[MatchedSale]]:
    """Return a robust all-market historical prior without inventing a sale.

    The prior intentionally uses every valid stored transaction as an equally
    weighted historical observation.  It is a last-resort market signal, not
    a comparable-card calculation, and callers cap its confidence accordingly.
    """
    anchors = [
        MatchedSale(
            sale=sale,
            score=20,
            level="global_market_baseline",
            reasons=("all-time observed market sale",),
        )
        for sale in sales
        if sale.price_cad > 0
    ]
    anchors = _remove_outliers(anchors)
    if not anchors:
        return None, []
    return median(comp.sale.price_cad for comp in anchors), anchors


def _predictive_valuation(
    card: CardQuery,
    sales: Iterable[Sale],
    today: date,
    *,
    one_of_one: bool,
    print_run: Optional[int],
    reference_value_cad: Optional[float],
    first_bowman_auto: bool,
) -> Dict[str, object]:
    """Estimate through a transparent, strongest-evidence-first fallback ladder."""
    sales = list(sales)
    related = _remove_outliers(find_predictive_comps(card, sales))
    segment: List[MatchedSale] = []
    broad: List[MatchedSale] = []
    global_anchors: List[MatchedSale] = []
    multiplier, scarcity_label = _scarcity_profile(one_of_one, print_run)
    baseline: Optional[float] = None
    basis = ""
    fallback_tier = ""
    anchor_kind = ""
    anchors: List[MatchedSale] = []
    confidence_cap = 48
    uncertainty = 0.5

    if reference_value_cad is not None:
        baseline = reference_value_cad
        basis = "user-supplied reference value"
        fallback_tier = "reference_value"
        anchor_kind = "reference_value"
        anchors = related
        if first_bowman_auto and not related:
            segment = _remove_outliers(find_first_bowman_auto_comps(sales, today))
    elif related:
        baseline = _weighted_quantile(
            [(comp.sale.price_cad, _weight(comp, today)) for comp in related],
            0.5,
        )
        basis = "related stored sales"
        fallback_tier = "related_player_sales"
        anchor_kind = "related_player_sales"
        anchors = related
    elif first_bowman_auto:
        segment = _remove_outliers(find_first_bowman_auto_comps(sales, today))
        if segment:
            baseline = _weighted_quantile(
                [(comp.sale.price_cad, _weight(comp, today)) for comp in segment],
                0.5,
            )
            basis = "all-time first-Bowman autograph market segment"
            fallback_tier = "first_bowman_auto_segment"
            anchor_kind = "first_bowman_auto_segment"
            anchors = segment

    if baseline is None:
        broad = _remove_outliers(find_broad_similarity_comps(card, sales))
        if broad:
            baseline = _weighted_quantile(
                [(comp.sale.price_cad, _weight(comp, today)) for comp in broad],
                0.5,
            )
            basis = "broader similar-card sales"
            fallback_tier = "broad_similar_cards"
            anchor_kind = "broad_similar_card_sales"
            anchors = broad
            confidence_cap = 12
            uncertainty = 0.65

    if baseline is None:
        baseline, global_anchors = _all_market_baseline(sales)
        if baseline is not None:
            basis = "all-time observed market baseline"
            fallback_tier = "global_market_baseline"
            anchor_kind = "global_market_baseline"
            anchors = global_anchors
            confidence_cap = 5
            uncertainty = 0.8

    if baseline is None:
        baseline = UNANCHORED_DEFAULT_CAD
        basis = "CARDR starter-value prior with no observed sales"
        fallback_tier = "unanchored_default"
        anchor_kind = "unanchored_default"
        anchors = []
        # Without a real market anchor, do not imply that scarcity math makes
        # a made-up prior more trustworthy.
        multiplier = 1.0
        scarcity_label = "no evidence-backed scarcity adjustment"
        confidence_cap = 2
        uncertainty = 0.9

    estimate = baseline * multiplier
    # Rarity has much wider uncertainty than a comp-backed price. One-of-ones
    # are intentionally shown with the broadest interval.
    if one_of_one or print_run == 1:
        uncertainty = max(uncertainty, 0.65)
    low = estimate * (1 - uncertainty)
    high = estimate * (1 + uncertainty)
    if fallback_tier == "unanchored_default":
        low, high = estimate * 0.1, estimate * 4.0
    confidence = min(15, confidence_cap)
    if related:
        confidence = min(confidence_cap, max(18, round(_confidence(related, today) * 0.52)))
    elif segment:
        # Segment-level demand is weaker evidence than a same-player comp.
        confidence = min(30, max(12, round(_confidence(segment, today) * 0.34)))
    elif broad:
        confidence = min(confidence_cap, max(8, round(_confidence(broad, today) * 0.20)))
    elif global_anchors:
        confidence = confidence_cap
    if reference_value_cad is not None:
        confidence = min(confidence_cap, confidence + 4)

    if related:
        anchor_statement = "{} related same-player sale{} anchor the model.".format(
            len(related), "s" if len(related) != 1 else ""
        )
    elif segment:
        anchor_statement = "{} all-time first-Bowman autograph market-segment sale{} anchor the model.".format(
            len(segment), "s" if len(segment) != 1 else ""
        )
    elif broad:
        anchor_statement = "{} broader similar-card sale{} provide a market segment, not an exact-card match.".format(
            len(broad), "s" if len(broad) != 1 else ""
        )
    elif global_anchors:
        anchor_statement = "{} all-time observed sales provide only a whole-market baseline, not card-specific evidence.".format(
            len(global_anchors), "s" if len(global_anchors) != 1 else ""
        )
    elif reference_value_cad is not None:
        anchor_statement = "The user-supplied reference value is the only model anchor."
    else:
        anchor_statement = "CARDR has no observed sales in this installation, so this is a generic starter-value prior."

    low_confidence = fallback_tier in {
        "broad_similar_cards",
        "global_market_baseline",
        "unanchored_default",
    }
    confidence_level = "low" if low_confidence else "medium"

    return {
        "status": "predictive",
        "estimated_value_cad": round(estimate, 2),
        "low_estimate_cad": round(low, 2),
        "high_estimate_cad": round(high, 2),
        "confidence_percent": confidence,
        "evidence_score": confidence,
        "comp_count": len(anchors),
        "exact_comp_count": 0,
        "related_comp_count": len(anchors),
        "anchor_kind": anchor_kind,
        "fallback_tier": fallback_tier,
        "confidence_level": confidence_level,
        "is_predictive": True,
        "baseline_value_cad": round(baseline, 2),
        "scarcity_multiplier": multiplier,
        "prediction_basis": basis,
        "valuation_method": "Predictive fallback model calibrated to {}.".format(basis),
        "rationale": [
            "No exact completed sale is stored for this card; this is a model prediction, not a confirmed market price.",
            "Baseline: {} of ${:.2f} CAD, adjusted by a {} ({:.2f}×).".format(
                basis, baseline, scarcity_label, multiplier
            ),
            anchor_statement,
            "LOW CONFIDENCE: the range is intentionally wide because rarity premiums and collector demand can move sharply."
            if low_confidence
            else "The range is intentionally wide because rarity premiums are uncertain and collector demand can move sharply.",
            "This is not investment advice or a future-price guarantee.",
        ],
        "comps": [comp.as_dict() for comp in anchors],
        "sales_chart": _sales_chart(anchors),
    }


def calculate_valuation(
    card: CardQuery,
    sales: Iterable[Sale],
    today: Optional[date] = None,
    *,
    predictive_mode: bool = False,
    one_of_one: bool = False,
    print_run: Optional[int] = None,
    reference_value_cad: Optional[float] = None,
    first_bowman_auto: bool = False,
) -> Dict[str, object]:
    """Return a direct estimate or an explicitly low-confidence fallback."""
    today = today or date.today()
    sales = list(sales)
    candidates = find_comps(card, sales)
    comps = _remove_outliers(candidates)

    # A sale of the exact card always wins over a scarcity-model prediction.
    # Any no-direct-comp path now follows the fallback ladder so every card
    # receives a clearly disclosed estimate, including on a fresh database.
    if predictive_mode and not any(comp.level == "exact" for comp in comps):
        return _predictive_valuation(
            card,
            sales,
            today,
            one_of_one=one_of_one,
            print_run=print_run,
            reference_value_cad=reference_value_cad,
            first_bowman_auto=first_bowman_auto,
        )

    if not comps:
        return _predictive_valuation(
            card,
            sales,
            today,
            one_of_one=one_of_one,
            print_run=print_run,
            reference_value_cad=reference_value_cad,
            first_bowman_auto=first_bowman_auto,
        )

    weighted_prices = [(comp.sale.price_cad, _weight(comp, today)) for comp in comps]
    estimate = _weighted_quantile(weighted_prices, 0.5)
    if len(comps) >= 4:
        low = _weighted_quantile(weighted_prices, 0.2)
        high = _weighted_quantile(weighted_prices, 0.8)
    elif len(comps) >= 2:
        low, high = estimate * 0.82, estimate * 1.18
    else:
        low, high = estimate * 0.75, estimate * 1.25

    # Avoid presenting a meaningless zero-width range for repeated sales.
    low = min(low, estimate * 0.94)
    high = max(high, estimate * 1.06)
    exact_count = sum(comp.level == "exact" for comp in comps)
    excluded_count = len(candidates) - len(comps)
    confidence = _confidence(comps, today)
    # Same-player/product evidence can be useful, but a supplied card number
    # that is absent from every listing should never yield an exact-card-level
    # confidence badge.
    if card.card_number and not exact_count:
        confidence = min(72, confidence)
    return {
        "status": "estimated",
        "estimated_value_cad": round(estimate, 2),
        "low_estimate_cad": round(low, 2),
        "high_estimate_cad": round(high, 2),
        "confidence_percent": confidence,
        "evidence_score": confidence,
        "comp_count": len(comps),
        "exact_comp_count": exact_count,
        "related_comp_count": 0,
        "anchor_kind": "exact_or_direct_comps",
        "fallback_tier": "direct_comparable_sales",
        "confidence_level": "high" if exact_count and confidence >= 65 else "medium",
        "is_predictive": False,
        "valuation_method": "Recency- and match-quality-weighted stored comparable sales.",
        "rationale": _rationale(comps, card, excluded_count),
        "comps": [comp.as_dict() for comp in comps],
        "sales_chart": _sales_chart(comps),
    }
