from statistics import mean, median, stdev
from math import sqrt


# ============================================================
# PROSPECTR MARKET INTELLIGENCE ENGINE
# ============================================================

def clean_prices(prices):
    """
    Convert incoming prices into clean positive floats.
    """
    clean = []

    for price in prices or []:
        try:
            value = float(price)

            if value > 0:
                clean.append(value)

        except (TypeError, ValueError):
            continue

    return clean


def clamp(value, minimum=0, maximum=100):
    """
    Keep a score between minimum and maximum.
    """
    return max(minimum, min(maximum, value))


def safe_float(value):
    """
    Safely convert a value to float.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def percent_change(old_value, new_value):
    """
    Calculate percentage change.
    """
    old_value = safe_float(old_value)
    new_value = safe_float(new_value)

    if old_value is None or new_value is None:
        return None

    if old_value == 0:
        return None

    return ((new_value - old_value) / old_value) * 100


# ============================================================
# PRICE DISTRIBUTION
# ============================================================

def calculate_price_distribution(prices):
    """
    Calculate deeper statistics about the price distribution.
    """

    clean = clean_prices(prices)

    if not clean:
        return {
            "count": 0
        }

    clean.sort()

    result = {
        "count": len(clean),
        "min": round(min(clean), 2),
        "max": round(max(clean), 2),
        "mean": round(mean(clean), 2),
        "median": round(median(clean), 2),
    }

    if len(clean) >= 2:
        result["range"] = round(
            max(clean) - min(clean),
            2
        )
    else:
        result["range"] = 0

    if len(clean) >= 2:
        result["standard_deviation"] = round(
            stdev(clean),
            2
        )
    else:
        result["standard_deviation"] = 0

    med = median(clean)

    if med > 0:
        result["coefficient_of_variation"] = round(
            (result["standard_deviation"] / med) * 100,
            1
        )
    else:
        result["coefficient_of_variation"] = 0

    return result


# ============================================================
# OUTLIER FILTER
# ============================================================

def remove_price_outliers(prices):
    """
    Remove extreme observations without destroying
    small datasets.
    """

    clean = clean_prices(prices)

    if len(clean) < 5:
        return clean

    med = median(clean)

    if med <= 0:
        return clean

    filtered = [
        value
        for value in clean
        if (
            value >= med / 3
            and value <= med * 3
        )
    ]

    if len(filtered) < 3:
        return clean

    return filtered


# ============================================================
# LIQUIDITY
# ============================================================

def calculate_liquidity_score(sales_count):
    """
    Estimate market liquidity from the number of
    observed comparable sales.

    This is a Prospectr research metric, not a
    financial-market liquidity measure.
    """

    try:
        count = int(sales_count)
    except (TypeError, ValueError):
        count = 0

    if count >= 50:
        return 100

    if count >= 35:
        return 95

    if count >= 25:
        return 90

    if count >= 15:
        return 75

    if count >= 10:
        return 65

    if count >= 5:
        return 50

    if count >= 3:
        return 35

    if count >= 1:
        return 20

    return 0


# ============================================================
# MARKET CONFIDENCE
# ============================================================

def calculate_confidence(sales_count):
    """
    Determine confidence from sample size.
    """

    try:
        count = int(sales_count)
    except (TypeError, ValueError):
        count = 0

    if count >= 25:
        return "very_high"

    if count >= 15:
        return "high"

    if count >= 8:
        return "medium"

    if count >= 3:
        return "low"

    return "very_low"


# ============================================================
# VOLATILITY
# ============================================================

def calculate_volatility(prices):
    """
    Measure spread relative to the median.
    """

    clean = clean_prices(prices)

    if len(clean) < 2:
        return 0

    med = median(clean)

    if med <= 0:
        return 0

    low = min(clean)
    high = max(clean)

    return ((high - low) / med) * 100


# ============================================================
# MARKET MOMENTUM
# ============================================================

def calculate_market_momentum(
    recent_prices=None,
    previous_prices=None
):
    """
    Compare a recent price sample against an older sample.
    """

    recent = clean_prices(recent_prices)
    previous = clean_prices(previous_prices)

    if not recent or not previous:
        return {
            "status": "insufficient_data",
            "change_percent": None,
            "score": 50,
            "direction": "unknown"
        }

    recent_median = median(recent)
    previous_median = median(previous)

    if previous_median <= 0:
        return {
            "status": "insufficient_data",
            "change_percent": None,
            "score": 50,
            "direction": "unknown"
        }

    change = (
        (recent_median - previous_median)
        / previous_median
    ) * 100

    score = 50 + (change * 2)

    score = clamp(score)

    if change >= 10:
        direction = "strong_up"

    elif change >= 3:
        direction = "up"

    elif change <= -10:
        direction = "strong_down"

    elif change <= -3:
        direction = "down"

    else:
        direction = "flat"

    return {
        "status": "success",
        "recent_median": round(recent_median, 2),
        "previous_median": round(previous_median, 2),
        "change_percent": round(change, 1),
        "score": round(score, 1),
        "direction": direction
    }


# ============================================================
# SALES VELOCITY
# ============================================================

def calculate_sales_velocity(
    sales_count,
    days=30
):
    """
    Sales per day over the observed period.
    """

    try:
        count = float(sales_count)
        days = float(days)
    except (TypeError, ValueError):
        return 0

    if days <= 0:
        return 0

    return round(count / days, 3)


# ============================================================
# STATCAST PLAYER SCORE
# ============================================================

def calculate_statcast_score(metrics):
    """
    Convert available Statcast metrics into a normalized
    0-100 contact-quality score.

    Metrics can be partial. Missing values are ignored.
    """

    if not metrics:
        return {
            "score": None,
            "metrics_used": 0,
            "status": "no_data"
        }

    # These are intentionally broad ranges rather than
    # pretending every metric has the same distribution.
    metric_weights = {
        "xwoba": (0.250, 0.450, 0.25),
        "xba": (0.220, 0.330, 0.10),
        "xslg": (0.350, 0.650, 0.10),
        "barrel_percent": (3, 15, 0.20),
        "hard_hit_percent": (30, 55, 0.15),
        "exit_velocity": (85, 95, 0.10),
        "ev50": (88, 102, 0.05),
        "sweetspot_percent": (25, 45, 0.05)
    }

    weighted_scores = []
    total_weight = 0

    for metric, (
        low,
        high,
        weight
    ) in metric_weights.items():

        value = safe_float(
            metrics.get(metric)
        )

        if value is None:
            continue

        normalized = (
            (value - low)
            /
            (high - low)
        ) * 100

        normalized = clamp(normalized)

        weighted_scores.append(
            normalized * weight
        )

        total_weight += weight

    if not weighted_scores or total_weight == 0:
        return {
            "score": None,
            "metrics_used": 0,
            "status": "no_data"
        }

    score = sum(weighted_scores) / total_weight

    return {
        "score": round(score, 1),
        "metrics_used": len(weighted_scores),
        "status": "success"
    }


# ============================================================
# RECENT PLAYER FORM
# ============================================================

def calculate_recent_form(
    recent=None,
    season=None
):
    """
    Compare recent player performance against season performance.

    Expected values can include:

    batting_average
    obp
    slg
    ops
    woba
    xwoba
    barrel_percent
    hard_hit_percent
    strikeout_percent
    walk_percent
    """

    recent = recent or {}
    season = season or {}

    comparisons = []

    keys = [
        "batting_average",
        "obp",
        "slg",
        "ops",
        "woba",
        "xwoba",
        "barrel_percent",
        "hard_hit_percent",
        "walk_percent"
    ]

    for key in keys:

        recent_value = safe_float(
            recent.get(key)
        )

        season_value = safe_float(
            season.get(key)
        )

        if (
            recent_value is None
            or
            season_value is None
        ):
            continue

        if season_value == 0:
            continue

        change = (
            (recent_value - season_value)
            /
            abs(season_value)
        ) * 100

        comparisons.append(change)

    if not comparisons:
        return {
            "score": None,
            "direction": "unknown",
            "status": "no_data"
        }

    average_change = mean(comparisons)

    score = clamp(
        50 + (average_change * 1.5)
    )

    if average_change >= 8:
        direction = "hot"

    elif average_change >= 3:
        direction = "improving"

    elif average_change <= -8:
        direction = "cold"

    elif average_change <= -3:
        direction = "declining"

    else:
        direction = "stable"

    return {
        "score": round(score, 1),
        "average_change_percent": round(
            average_change,
            1
        ),
        "direction": direction,
        "status": "success"
    }


# ============================================================
# PROSPECTR PLAYER SIGNAL
# ============================================================

def calculate_player_signal(
    statcast_score=None,
    recent_form_score=None,
    market_momentum_score=None,
    award_bonus=0,
    prospect_bonus=0
):
    """
    Combine baseball performance and market information.

    This is a research/analytics signal, not a recommendation.
    """

    components = []

    if statcast_score is not None:
        components.append(
            ("statcast", statcast_score, 0.30)
        )

    if recent_form_score is not None:
        components.append(
            ("recent_form", recent_form_score, 0.25)
        )

    if market_momentum_score is not None:
        components.append(
            ("market_momentum", market_momentum_score, 0.25)
        )

    if components:

        weighted_total = sum(
            score * weight
            for _, score, weight in components
        )

        total_weight = sum(
            weight
            for _, _, weight in components
        )

        score = weighted_total / total_weight

    else:
        score = 50

    # Awards and prospect status should influence the
    # narrative but should never overwhelm actual performance.
    score += float(award_bonus or 0)
    score += float(prospect_bonus or 0)

    score = clamp(score)

    if score >= 85:
        tier = "elite"

    elif score >= 75:
        tier = "strong"

    elif score >= 65:
        tier = "positive"

    elif score >= 50:
        tier = "neutral"

    elif score >= 35:
        tier = "weak"

    else:
        tier = "declining"

    return {
        "score": round(score, 1),
        "tier": tier,
        "components": {
            name: round(score_value, 1)
            for name, score_value, _ in components
        }
    }


# ============================================================
# PROSPECTR NARRATIVE
# ============================================================

def build_player_narrative(
    player_name,
    signal,
    recent_form=None,
    statcast=None,
    market=None
):
    """
    Generate a human-readable explanation for the website.
    """

    recent_form = recent_form or {}
    statcast = statcast or {}
    market = market or {}

    score = signal.get("score")

    if score is None:
        score = 50

    direction = recent_form.get(
        "direction",
        "unknown"
    )

    market_direction = market.get(
        "direction",
        "unknown"
    )

    if direction == "hot":

        opening = (
            f"{player_name} is showing a strong recent "
            f"performance trend."
        )

    elif direction == "improving":

        opening = (
            f"{player_name} is showing signs of "
            f"improvement compared with the season baseline."
        )

    elif direction == "cold":

        opening = (
            f"{player_name}'s recent results have cooled "
            f"relative to the season baseline."
        )

    elif direction == "declining":

        opening = (
            f"{player_name} is showing a meaningful "
            f"downward performance trend."
        )

    else:

        opening = (
            f"{player_name} is currently showing a "
            f"mixed performance profile."
        )

    details = []

    xwoba = statcast.get("xwoba")

    if xwoba is not None:
        details.append(
            f"xwOBA is {xwoba:.3f}"
        )

    barrel = statcast.get(
        "barrel_percent"
    )

    if barrel is not None:
        details.append(
            f"barrel rate is {barrel:.1f}%"
        )

    hard_hit = statcast.get(
        "hard_hit_percent"
    )

    if hard_hit is not None:
        details.append(
            f"hard-hit rate is {hard_hit:.1f}%"
        )

    if details:

        baseball_sentence = (
            " Key Statcast indicators include "
            + ", ".join(details)
            + "."
        )

    else:

        baseball_sentence = ""

    if market_direction == "up":

        market_sentence = (
            " Market activity is also moving upward."
        )

    elif market_direction == "strong_up":

        market_sentence = (
            " Market activity is accelerating upward."
        )

    elif market_direction == "down":

        market_sentence = (
            " Market activity has weakened recently."
        )

    elif market_direction == "strong_down":

        market_sentence = (
            " Market activity is showing a significant decline."
        )

    else:

        market_sentence = ""

    return (
        opening
        + baseball_sentence
        + market_sentence
    )


# ============================================================
# FULL MARKET STATS
# ============================================================

def calculate_market_stats(
    prices,
    sales=None,
    recent_prices=None,
    previous_prices=None
):
    """
    Main market-statistics function.

    Backwards compatible with the old:

        calculate_market_stats(prices)

    but now supports sales and momentum analysis.
    """

    clean = clean_prices(prices)

    if not clean:

        return {
            "status": "no_data",
            "sales_count": 0,
            "raw_sales_count": 0,
            "confidence": "none",
            "market_momentum": {
                "status": "no_data"
            }
        }

    clean.sort()

    raw_count = len(clean)

    original_median = median(clean)

    filtered = remove_price_outliers(clean)

    average = mean(filtered)
    med = median(filtered)
    low = min(filtered)
    high = max(filtered)

    volatility = calculate_volatility(filtered)

    liquidity = calculate_liquidity_score(
        len(filtered)
    )

    confidence = calculate_confidence(
        len(filtered)
    )

    distribution = calculate_price_distribution(
        filtered
    )

    momentum = calculate_market_momentum(
        recent_prices=recent_prices,
        previous_prices=previous_prices
    )

    velocity = calculate_sales_velocity(
        raw_count,
        30
    )

    result = {
        "status": "success",

        "sales_count": len(filtered),

        "raw_sales_count": raw_count,

        "low": round(
            low,
            2
        ),

        "high": round(
            high,
            2
        ),

        "average": round(
            average,
            2
        ),

        "median": round(
            med,
            2
        ),

        "volatility_percent": round(
            volatility,
            1
        ),

        "liquidity_score": liquidity,

        "sales_velocity_30d": velocity,

        "confidence": confidence,

        "distribution": distribution,

        "market_momentum": momentum
    }

    # --------------------------------------------------------
    # Optional raw sale metadata
    # --------------------------------------------------------

    if sales:

        result["sale_metadata"] = {
            "confirmed_sales": sum(
                1
                for sale in sales
                if sale.get("price_confirmed")
            ),

            "auction_sales": sum(
                1
                for sale in sales
                if sale.get("listing_type") == "auction"
            ),

            "fixed_price_sales": sum(
                1
                for sale in sales
                if sale.get("listing_type") == "fixed_price"
            ),

            "graded_sales": sum(
                1
                for sale in sales
                if (
                    sale.get("grade") is not None
                    or sale.get("grader")
                )
            ),

            "raw_sales": sum(
                1
                for sale in sales
                if (
                    sale.get("grade") is None
                    and not sale.get("grader")
                )
            )
        }

    return result


# ============================================================
# MARKET SIGNAL
# ============================================================

def calculate_market_signal(
    market_stats
):
    """
    Convert market statistics into a simple 0-100
    research signal.
    """

    if not market_stats:
        return {
            "score": 50,
            "tier": "unknown"
        }

    liquidity = safe_float(
        market_stats.get(
            "liquidity_score"
        )
    )

    momentum = market_stats.get(
        "market_momentum",
        {}
    )

    momentum_score = safe_float(
        momentum.get("score")
    )

    if liquidity is None:
        liquidity = 50

    if momentum_score is None:
        momentum_score = 50

    volatility = safe_float(
        market_stats.get(
            "volatility_percent"
        )
    )

    if volatility is None:
        volatility = 0

    # Extremely high volatility reduces confidence,
    # rather than automatically meaning "bad".
    volatility_penalty = min(
        volatility * 0.10,
        15
    )

    score = (
        liquidity * 0.35
        +
        momentum_score * 0.65
        -
        volatility_penalty
    )

    score = clamp(score)

    if score >= 85:
        tier = "very_strong"

    elif score >= 70:
        tier = "strong"

    elif score >= 55:
        tier = "positive"

    elif score >= 45:
        tier = "neutral"

    elif score >= 30:
        tier = "weak"

    else:
        tier = "declining"

    return {
        "score": round(score, 1),
        "tier": tier,
        "liquidity_component": round(
            liquidity,
            1
        ),
        "momentum_component": round(
            momentum_score,
            1
        ),
        "volatility_penalty": round(
            volatility_penalty,
            1
        )
    }