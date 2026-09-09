
import re
from statistics import median


AUTOGRAPH_PATTERNS = [
    r"\bauto\b",
    r"\bautograph\b",
    r"\bautographed\b",
    r"\bautograph(ed)?\b",
    r"\bsigned\b",
    r"\bsignature\b",
    r"\bon[- ]card auto\b",
    r"\bon[- ]card autograph\b",
    r"\bsticker auto\b",
    r"\bsticker autograph\b",
    r"\bcertified autograph\b",
]


NON_AUTOGRAPH_PATTERNS = [
    r"\bno auto\b",
    r"\bnon[- ]auto\b",
    r"\bnon auto\b",
    r"\bunsigned\b",
    r"\bwithout auto\b",
]


def normalize_text(value):

    text = str(value or "").lower()

    text = re.sub(
        r"[^a-z0-9]+",
        " ",
        text
    )

    return " ".join(
        text.split()
    )


def detect_autograph(
    title,
    card_type="",
    parallel=""
):

    text = normalize_text(
        " ".join([
            str(title or ""),
            str(card_type or ""),
            str(parallel or "")
        ])
    )

    # Explicit negatives win.
    for pattern in NON_AUTOGRAPH_PATTERNS:

        if re.search(
            pattern,
            text
        ):

            return False, None

    if re.search(
        r"\bon card auto\b",
        text
    ):

        return True, "on_card"

    if re.search(
        r"\bon card autograph\b",
        text
    ):

        return True, "on_card"

    if re.search(
        r"\bsticker auto\b",
        text
    ):

        return True, "sticker"

    if re.search(
        r"\bsticker autograph\b",
        text
    ):

        return True, "sticker"

    for pattern in AUTOGRAPH_PATTERNS:

        if re.search(
            pattern,
            text
        ):

            return True, "autograph"

    return False, None


def analyze_sales(
    sales
):

    valid = []

    for sale in sales or []:

        try:

            price = float(
                sale
            )

            if price > 0:
                valid.append(
                    price
                )

        except (
            TypeError,
            ValueError
        ):

            continue

    if not valid:

        return {
            "status": "no_sales",
            "sales_count": 0
        }

    valid.sort()

    med = median(
        valid
    )

    # Conservative outlier filter.
    filtered = [
        price
        for price in valid
        if (
            price >= med / 3
            and
            price <= med * 3
        )
    ]

    if len(filtered) < 2:

        filtered = valid

    return {
        "status": "success",
        "sales_count": len(filtered),
        "raw_sales_count": len(valid),

        "low_sale_usd": round(
            min(filtered),
            2
        ),

        "high_sale_usd": round(
            max(filtered),
            2
        ),

        "average_sale_usd": round(
            sum(filtered)
            /
            len(filtered),
            2
        ),

        "median_sale_usd": round(
            median(filtered),
            2
        )
    }

