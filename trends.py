
from collections import defaultdict
from datetime import datetime
from statistics import mean, median


def get_month(
    date_string
):

    if not date_string:
        return None

    try:

        parsed = datetime.fromisoformat(
            str(date_string)
            .replace(
                "Z",
                "+00:00"
            )
        )

        return parsed.strftime(
            "%Y-%m"
        )

    except (
        ValueError,
        TypeError
    ):

        text = str(
            date_string
        )

        if len(text) >= 7:
            return text[:7]

        return None


def build_monthly_history(
    sales
):

    months = defaultdict(
        list
    )

    for sale in sales or []:

        month = get_month(
            sale.get(
                "sale_date"
            )
        )

        price = sale.get(
            "price_cad"
        )

        if (
            month is None
            or
            price is None
        ):

            continue

        try:

            price = float(
                price
            )

        except (
            TypeError,
            ValueError
        ):

            continue

        if price <= 0:
            continue

        months[
            month
        ].append(
            price
        )

    history = []

    for month in sorted(
        months
    ):

        prices = months[
            month
        ]

        history.append({
            "month": month,
            "sales_count": len(
                prices
            ),
            "low": round(
                min(prices),
                2
            ),
            "high": round(
                max(prices),
                2
            ),
            "average": round(
                mean(prices),
                2
            ),
            "median": round(
                median(prices),
                2
            )
        })

    return history


def calculate_trend(
    history
):

    if len(
        history or []
    ) < 2:

        return {
            "trend": "unknown",
            "trend_percent": 0,
            "momentum": "insufficient_data"
        }

    current = history[
        -1
    ][
        "median"
    ]

    previous = history[
        -2
    ][
        "median"
    ]

    if previous <= 0:

        return {
            "trend": "unknown",
            "trend_percent": 0,
            "momentum": "unknown"
        }

    change = (
        (
            current
            -
            previous
        )
        /
        previous
    ) * 100

    if change >= 10:
        direction = "strong_up"

    elif change >= 3:
        direction = "up"

    elif change <= -10:
        direction = "strong_down"

    elif change <= -3:
        direction = "down"

    else:
        direction = "neutral"

    return {
        "trend": direction,
        "trend_percent": round(
            change,
            2
        ),
        "momentum": direction
    }


def calculate_market_signal(
    history
):

    if len(
        history or []
    ) < 2:

        return {
            "signal": "neutral",
            "score": 50
        }

    recent = history[
        -3:
    ]

    changes = []

    for index in range(
        1,
        len(recent)
    ):

        previous = recent[
            index - 1
        ][
            "median"
        ]

        current = recent[
            index
        ][
            "median"
        ]

        if previous > 0:

            changes.append(
                (
                    (
                        current
                        -
                        previous
                    )
                    /
                    previous
                ) * 100
            )

    if not changes:

        return {
            "signal": "neutral",
            "score": 50
        }

    average_change = mean(
        changes
    )

    score = (
        50
        +
        average_change * 2
    )

    score = max(
        0,
        min(
            100,
            score
        )
    )

    if score >= 70:
        signal = "bullish"

    elif score >= 55:
        signal = "positive"

    elif score <= 30:
        signal = "bearish"

    elif score <= 45:
        signal = "negative"

    else:
        signal = "neutral"

    return {
        "signal": signal,
        "score": round(
            score,
            1
        )
    }

