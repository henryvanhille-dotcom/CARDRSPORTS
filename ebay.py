import os
import re
import hashlib
import requests
from typing import Optional


BASE_URL = "https://thecardapi.com/api/v1/market"
# Store the source currency and conversion rate alongside every import.  The
# display layer may use a current preference, but historical provenance should
# never silently overwrite the rate used at import time.
USD_TO_CAD = 1.36
MAX_PROVIDER_PAGE_SIZE = 1000
DEFAULT_IMPORT_PAGE_LIMIT = 10


# ============================================================
# HELPERS
# ============================================================

def normalize_text(value):
    if value is None:
        return ""

    text = str(value).lower()

    text = text.replace("’", "'")
    text = text.replace("–", "-")
    text = text.replace("—", "-")
    text = text.replace("&", " and ")

    text = re.sub(r"[^a-z0-9#./ -]", " ", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


# ============================================================
# CARD DETECTION
# ============================================================

def detect_grade(title):
    text = normalize_text(title)

    patterns = [
        r"\b(psa|bgs|sgc|cgc|csg|hga|ags)\s*([0-9]{1,2}(?:\.[0-9])?)\b",
        r"\bgrade\s*([0-9]{1,2}(?:\.[0-9])?)\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            groups = match.groups()

            if len(groups) == 2:
                return groups[1], groups[0].upper()

            return groups[0], None

    return None, None


def detect_parallel(title):
    text = normalize_text(title)

    parallels = [
        "superfractor",
        "snakeskin",
        "gold vinyl",
        "gold wave",
        "gold shimmer",
        "gold refractor",
        "orange refractor",
        "red refractor",
        "blue refractor",
        "green refractor",
        "purple refractor",
        "pink refractor",
        "aqua refractor",
        "silver refractor",
        "black refractor",
        "white refractor",
        "refractor",
        "xfractor",
        "speckle",
        "shimmer",
        "atomic",
        "cosmic",
        "mojo",
        "wave",
        "velocity",
        "ice",
        "prizm",
        "gold",
        "orange",
        "red",
        "blue",
        "green",
        "purple",
        "pink",
        "aqua",
        "silver",
        "black",
        "white",
    ]

    for parallel in parallels:
        if parallel in text:
            return parallel

    return None


def detect_numbering(title):
    text = normalize_text(title)

    match = re.search(
        r"\b(\d{1,5})\s*/\s*(\d{1,5})\b",
        text
    )

    if match:
        return f"{match.group(1)}/{match.group(2)}"

    return None


def detect_card_number(title):
    """Extract a listing's card number without confusing serial numbering."""
    text = str(title or "")
    match = re.search(r"#\s*([A-Z]{0,5}(?:[- ]?[A-Z])?[- ]?\d{1,5}[A-Z]?)\b", text, re.I)
    return match.group(1).upper().replace(" ", "") if match else None


def detect_rookie(title):
    text = normalize_text(title)

    return bool(
        re.search(r"\brc\b", text)
        or re.search(r"\brookie\b", text)
    )


def detect_autograph(title):
    text = normalize_text(title)

    negative = [
        r"\bno auto\b",
        r"\bnon auto\b",
        r"\bunsigned\b",
    ]

    for pattern in negative:
        if re.search(pattern, text):
            return False

    positive = [
        r"\bauto\b",
        r"\bautograph\b",
        r"\bautographed\b",
        r"\bsigned\b",
        r"\bsignature\b",
    ]

    return any(
        re.search(pattern, text)
        for pattern in positive
    )


def is_raw_sale(sale):
    title = normalize_text(
        sale.get("title", "")
    )

    if sale.get("grade"):
        return False

    if sale.get("grading_company"):
        return False

    if sale.get("grader"):
        return False

    grading_patterns = [
        r"\bpsa\s*[0-9]+\b",
        r"\bbgs\s*[0-9]+\b",
        r"\bsgc\s*[0-9]+\b",
        r"\bcgc\s*[0-9]+\b",
        r"\bcsg\s*[0-9]+\b",
        r"\bhga\s*[0-9]+\b",
        r"\bags\s*[0-9]+\b",
    ]

    return not any(
        re.search(pattern, title)
        for pattern in grading_patterns
    )


# ============================================================
# API
# ============================================================

def _request_sales_page(query, limit=MAX_PROVIDER_PAGE_SIZE, cursor=None, graded=None):
    """Fetch one provider page and preserve its cursor metadata."""

    api_key = os.getenv("CARD_API_KEY")

    if not api_key:
        raise RuntimeError(
            "CARD_API_KEY is not set."
        )

    params = {
        "q": query,
        "limit": max(1, min(int(limit), MAX_PROVIDER_PAGE_SIZE)),
    }
    if cursor:
        params["cursor"] = cursor

    headers = {
        "x-market-api-key": api_key,
        "Accept": "application/json",
    }

    response = requests.get(
        f"{BASE_URL}/sales",
        params=params,
        headers=headers,
        timeout=20,
    )

    response.raise_for_status()

    data = response.json()

    if isinstance(data, dict):
        records = data.get("data") or data.get("sales") or data.get("results") or []
        pagination = data.get("pagination") or {}
        return records, pagination.get("next_cursor"), pagination, data.get("meta") or {}
    if isinstance(data, list):
        return data, None, {}, {}
    return [], None, {}, {}


def _request_sales(query, limit=50, graded=None):
    """Backward-compatible one-page provider request."""
    records, _, _, _ = _request_sales_page(query=query, limit=limit, graded=graded)
    return records


# ============================================================
# NORMALIZE SALES
# ============================================================

def normalize_sale(sale):

    title = sale.get("title") or ""

    price = sale.get("price")

    if price is None:
        price = sale.get("sale_price")

    if price is None:
        price = sale.get("original_price")

    try:
        price = float(price)
    except (TypeError, ValueError):
        price = None

    currency = str(
        sale.get("currency")
        or "USD"
    ).upper()

    if price is not None:

        if currency == "CAD":
            price_cad = price
            price_usd = price / USD_TO_CAD

        else:
            price_usd = price
            price_cad = price * USD_TO_CAD

    else:
        price_usd = None
        price_cad = None

    detected_grade, detected_company = (
        detect_grade(title)
    )

    grade = (
        sale.get("grade")
        or detected_grade
    )

    grading_company = (
        sale.get("grading_company")
        or sale.get("grader")
        or detected_company
    )

    sale_id = sale.get("id")

    if not sale_id:
        sale_id = hashlib.sha256(
            (
                f"{title}|"
                f"{sale.get('sale_date')}|"
                f"{price}"
            ).encode()
        ).hexdigest()[:24]

    source_player = sale.get("player") or sale.get("player_name") or ""
    source_set_name = sale.get("card_set") or sale.get("set_name") or sale.get("parent_set_name") or ""
    source_year = sale.get("year") or sale.get("card_year") or ""
    source_card_type = sale.get("card_type") or sale.get("subset") or ""
    source_card_number = sale.get("card_number") or detect_card_number(title)
    source_parallel = sale.get("parallel") or sale.get("variant") or detect_parallel(title)

    return {
        "id": str(sale_id),
        "source_sale_id": str(sale.get("id") or sale_id),

        "title": title,

        "platform": sale.get(
            "platform"
        ),

        "listing_type": sale.get(
            "listing_type"
        ),

        "sale_date": sale.get(
            "sale_date"
        ),

        "sold_at": sale.get(
            "sold_at"
        ),

        "price_original": price,

        "currency": currency,
        "source_currency": currency,
        "fx_rate_to_cad": 1.0 if currency == "CAD" else USD_TO_CAD,

        "price_usd": price_usd,

        "price_cad": price_cad,

        "price_confirmed": sale.get(
            "price_confirmed"
        ),

        "bids": sale.get(
            "bids"
        ),

        "image_url": sale.get(
            "image_url"
        ),

        "thumbnail_url": sale.get(
            "thumbnail_url"
        ),

        "listing_url": sale.get(
            "listing_url"
        ),

        "cert": sale.get(
            "cert"
        ),

        "condition": sale.get(
            "condition"
        ),

        "grade": (
            str(grade)
            if grade is not None
            else None
        ),

        # Keep provider-native identity when it is available.  The importer
        # uses it rather than overwriting all rows with the search query.
        "source_player": str(source_player).strip(),
        "source_year": str(source_year).strip(),
        "source_set_name": str(source_set_name).strip(),
        "source_card_type": str(source_card_type).strip(),
        "card_number": str(source_card_number).strip() if source_card_number else None,
        "source_parallel": str(source_parallel).strip() if source_parallel else "",
        "identity_status": "source_native" if any((source_player, source_set_name, source_card_number)) else "query_verified",

        "grader": sale.get(
            "grader"
        ),

        "grading_company":
            grading_company,

        "is_autograph":
            detect_autograph(title),

        "is_rookie":
            detect_rookie(title),

        "is_numbered":
            bool(
                detect_numbering(title)
            ),

        "serial_number":
            detect_numbering(title),

        "detected_parallel":
            detect_parallel(title),

        "match_score": 0,

        "match_confidence":
            "unmatched",

        "match_reason":
            "",

        "raw": sale,
    }


# ============================================================
# RAW API SEARCH
# ============================================================

def search_real_sales(
    query,
    limit=50,
    graded=None,
    cursor=None,
):

    raw_sales, _, _, _ = _request_sales_page(
        query=query,
        limit=limit,
        cursor=cursor,
        graded=graded,
    )

    sales = []

    for raw in raw_sales:

        try:
            sales.append(
                normalize_sale(raw)
            )

        except Exception as exc:
            print(
                "NORMALIZATION ERROR:",
                repr(exc)
            )

    return sales


# ============================================================
# PLAYER MATCH
# ============================================================

def player_match(player, title):

    player = normalize_text(player)
    title = normalize_text(title)

    if not player:
        return True

    # Exact player phrase.
    if player in title:
        return True

    # First + last name fallback.
    parts = player.split()

    if len(parts) >= 2:

        first = parts[0]
        last = parts[-1]

        if (
            re.search(
                rf"\b{re.escape(first)}\b",
                title
            )
            and
            re.search(
                rf"\b{re.escape(last)}\b",
                title
            )
        ):
            return True

    return False


# ============================================================
# YEAR MATCH
# ============================================================

def year_match(year, title):

    if not year:
        return True

    return bool(
        re.search(
            rf"\b{int(year)}\b",
            normalize_text(title)
        )
    )


# ============================================================
# SET / PRODUCT MATCHING
# ============================================================

# Products that contain "Topps" but are NOT the flagship
# Topps base product.
TOPPS_VARIANTS = [
    "chrome",
    "heritage",
    "tribute",
    "update",
    "allen ginter",
    "allen and ginter",
    "gypsy queen",
    "finest",
    "museum",
    "archives",
    "opening day",
    "stadium club",
    "fire",
    "gallery",
    "inception",
    "tier one",
    "dynasty",
    "luminaries",
    "gold label",
    "big league",
    "pro debut",
    "holiday",
    "heritage high number",
    "heritage minors",
    "total",
    "living",
    "brooklyn",
    "jumbo",
    "mini",
    "minis",
    "3d",
    "diamond anniversary",
]


def is_topps_flagship_base(
    title,
    year=0,
    parallel=None,
):

    text = normalize_text(title)

    # We need "Topps" in the title.
    if not re.search(
        r"\btopps\b",
        text
    ):
        return False

    # If year is supplied, prefer "YEAR Topps".
    if year:

        year_topps = (
            rf"\b{int(year)}\s+topps\b"
        )

        if not re.search(
            year_topps,
            text
        ):
            return False

    # If a specific parallel is requested,
    # variants may be valid depending on the card.
    # Otherwise we're looking specifically for flagship base.
    if parallel:
        return True

    # Reject known Topps sub-products.
    for variant in TOPPS_VARIANTS:

        if re.search(
            rf"\btopps\s+{re.escape(variant)}\b",
            text
        ):
            return False

        # Handles titles like:
        # "2012 Topps - 1987 Topps Minis"
        if re.search(
            rf"\b{re.escape(variant)}\b",
            text
        ):
            return False

    return True


def set_match(
    set_name,
    title,
    year=0,
    parallel=None,
):

    if not set_name:
        return True

    set_name = normalize_text(
        set_name
    )

    title = normalize_text(
        title
    )

    # Special handling for the generic
    # "Topps" flagship product.
    if set_name == "topps":

        return is_topps_flagship_base(
            title,
            year=year,
            parallel=parallel,
        )

    # Multi-word set names.
    words = set_name.split()

    if len(words) >= 2:

        # Prefer exact phrase.
        if set_name in title:
            return True

        return all(
            word in title
            for word in words
        )

    return bool(
        re.search(
            rf"\b{re.escape(set_name)}\b",
            title
        )
    )


# ============================================================
# CARD TYPE MATCH
# ============================================================

def card_type_match(
    card_type,
    sale,
):

    requested = normalize_text(
        card_type or "base"
    )

    title = normalize_text(
        sale.get("title", "")
    )

    # --------------------------------------------------------
    # BASE
    # --------------------------------------------------------

    if requested in {
        "base",
        "base card",
        "standard",
    }:

        # No autograph.
        if sale.get(
            "is_autograph"
        ):
            return False

        # No parallel.
        if sale.get(
            "detected_parallel"
        ):
            return False

        # No serial numbering.
        if sale.get(
            "is_numbered"
        ):
            return False

        return True

    # --------------------------------------------------------
    # ROOKIE
    # --------------------------------------------------------

    if requested in {
        "rookie",
        "rc",
        "rookie card",
    }:

        return sale.get(
            "is_rookie",
            False
        )

    # --------------------------------------------------------
    # AUTOGRAPH
    # --------------------------------------------------------

    if requested in {
        "auto",
        "autograph",
        "signed",
        "signature",
    }:

        return sale.get(
            "is_autograph",
            False
        )

    # --------------------------------------------------------
    # OTHER CARD TYPES
    # --------------------------------------------------------

    return requested in title


# ============================================================
# PARALLEL MATCH
# ============================================================

def parallel_match(
    parallel,
    sale,
):

    # No requested parallel means
    # base/standard card only.
    if not parallel:
        return True

    requested = normalize_text(
        parallel
    )

    title = normalize_text(
        sale.get("title", "")
    )

    detected = normalize_text(
        sale.get(
            "detected_parallel"
        ) or ""
    )

    return (
        requested in title
        or requested == detected
    )


# ============================================================
# GRADE MATCH
# ============================================================

def grade_match(
    grade,
    sale,
):

    if not grade:
        return True

    requested = normalize_text(
        grade
    )

    # Raw.
    if requested in {
        "raw",
        "ungraded",
    }:

        return is_raw_sale(
            sale
        )

    sale_grade = normalize_text(
        sale.get("grade")
        or ""
    )

    sale_company = normalize_text(
        sale.get(
            "grading_company"
        )
        or sale.get("grader")
        or ""
    )

    grade_number = re.search(
        r"([0-9]{1,2}(?:\.[0-9])?)",
        requested
    )

    if grade_number:

        requested_number = (
            grade_number.group(1)
        )

        if sale_grade != requested_number:
            return False

    for company in [
        "psa",
        "bgs",
        "sgc",
        "cgc",
        "csg",
        "hga",
        "ags",
    ]:

        if company in requested:

            if company not in sale_company:
                return False

    return True


def card_number_match(card_number, sale):
    """Require an explicit listing number to agree when the user supplied one."""
    requested = normalize_text(card_number or "").replace(" ", "")
    if not requested:
        return True
    observed = normalize_text(sale.get("card_number") or detect_card_number(sale.get("title", ""))).replace(" ", "")
    # A number omitted from the listing is too ambiguous for a strict refresh.
    return bool(observed and observed == requested)


# ============================================================
# BAD LISTINGS
# ============================================================

def obvious_bad_match(sale):

    title = normalize_text(
        sale.get("title", "")
    )

    bad_patterns = [
        r"\blot\b",
        r"\blots\b",
        r"\bcollection\b",
        r"\bcomplete set\b",
        r"\bteam set\b",
        r"\bbox\b",
        r"\bpack\b",
        r"\bbundle\b",
        r"\bcase\b",
    ]

    return any(
        re.search(
            pattern,
            title
        )
        for pattern in bad_patterns
    )


# ============================================================
# SCORE
# ============================================================

def score_sale(
    sale,
    player,
    year,
    set_name,
    card_type,
    parallel,
    grade,
    card_number=None,
):

    title = sale.get(
        "title",
        ""
    )

    # Player.
    if not player_match(
        player,
        title,
    ):
        return -1, "wrong player"

    # Year.
    if not year_match(
        year,
        title,
    ):
        return -1, "wrong year"

    # Set/product.
    if not set_match(
        set_name,
        title,
        year=year,
        parallel=parallel,
    ):
        return -1, "wrong set/product"

    if not card_number_match(card_number, sale):
        return -1, "wrong or missing card number"

    # Card type.
    if not card_type_match(
        card_type,
        sale,
    ):
        return -1, "wrong card type"

    # Parallel.
    if not parallel_match(
        parallel,
        sale,
    ):
        return -1, "wrong parallel"

    # Grade.
    if not grade_match(
        grade,
        sale,
    ):
        return -1, "wrong grade"

    # Lots/bundles.
    if obvious_bad_match(
        sale
    ):
        return -1, "lot/bundle"

    score = 50
    reasons = [
        "player"
    ]

    if year:
        score += 20
        reasons.append(
            "year"
        )

    if set_name:
        score += 20
        reasons.append(
            "set"
        )

    if parallel:
        score += 10
        reasons.append(
            "parallel"
        )

    if grade:
        score += 10
        reasons.append(
            "grade"
        )

    if card_number:
        score += 20
        reasons.append("card number")

    if sale.get(
        "image_url"
    ):
        score += 2

    if sale.get(
        "listing_url"
    ):
        score += 2

    return (
        score,
        ", ".join(reasons)
    )


# ============================================================
# MAIN PROSPECTR SEARCH
# ============================================================

def search_card_sales(
    player,
    year=0,
    set_name="",
    card_type="Base",
    parallel=None,
    grade=None,
    card_number=None,
    days=None,
    max_pages=None,
    page_limit=MAX_PROVIDER_PAGE_SIZE,
):
    """Search every available provider page up to a controlled import cap.

    Provider cursor pagination is used instead of a fixed 50-sale snapshot.
    A host can raise ``CARDR_MARKET_IMPORT_MAX_PAGES`` for a long-running
    historical backfill.  The return value reports when a continuation is
    needed; it never pretends a capped response is all history.
    """
    if max_pages is None:
        try:
            max_pages = int(os.getenv("CARDR_MARKET_IMPORT_MAX_PAGES", str(DEFAULT_IMPORT_PAGE_LIMIT)))
        except ValueError:
            max_pages = DEFAULT_IMPORT_PAGE_LIMIT
    max_pages = max(1, min(int(max_pages), 100))
    page_limit = max(1, min(int(page_limit), MAX_PROVIDER_PAGE_SIZE))

    queries = []

    # Most specific search.
    specific = [
        player,
        str(year) if year else "",
        set_name,
        card_number or "",
        parallel or "",
    ]

    specific = [
        x.strip()
        for x in specific
        if x
    ]

    if specific:
        queries.append(
            " ".join(specific)
        )

    # Player + year.
    if player and year:
        queries.append(
            f"{player} {year}"
        )

    # Player only.
    if player:
        queries.append(
            player
        )

    # Remove duplicates.
    unique_queries = []

    for query in queries:

        if query not in unique_queries:
            unique_queries.append(
                query
            )

    all_sales = {}
    pages_scanned = 0
    provider_total = 0
    coverage = []
    needs_continuation = False

    for level, query in enumerate(
        unique_queries,
        start=1
    ):

        try:

            cursor = None
            for page_number in range(1, max_pages + 1):
                raw_results, next_cursor, pagination, meta = _request_sales_page(
                    query=query,
                    limit=page_limit,
                    cursor=cursor,
                )
                pages_scanned += 1
                provider_total = max(provider_total, int(pagination.get("total") or 0))
                if meta.get("coverage_date_from") or meta.get("coverage_date_to"):
                    coverage.append({
                        "from": meta.get("coverage_date_from"),
                        "to": meta.get("coverage_date_to"),
                    })

                for sale in raw_results:

                    score, reason = score_sale(
                        sale=sale,
                        player=player,
                        year=year,
                        set_name=set_name,
                        card_type=card_type,
                        parallel=parallel,
                        grade=grade,
                        card_number=card_number,
                    )

                    if score < 0:
                        continue

                    sale[
                        "match_score"
                    ] = score

                    sale[
                        "match_reason"
                    ] = reason

                    if score >= 90:
                        sale[
                            "match_confidence"
                        ] = "high"

                    elif score >= 70:
                        sale[
                            "match_confidence"
                        ] = "medium"

                    else:
                        sale[
                            "match_confidence"
                        ] = "low"

                    sale_id = sale[
                        "id"
                    ]

                    existing = all_sales.get(
                        sale_id
                    )

                    if (
                        existing is None
                        or score >
                        existing.get(
                            "match_score",
                            0
                        )
                    ):

                        all_sales[
                            sale_id
                        ] = sale

                if not next_cursor:
                    break
                cursor = next_cursor
            else:
                # We ended because of our safety cap, not because the source
                # ran out of history.  Surface that fact to the caller.
                needs_continuation = True

        except Exception as exc:

            print(
                "SEARCH ERROR:",
                repr(exc)
            )

    sales = list(
        all_sales.values()
    )

    sales.sort(
        key=lambda x: (
            x.get(
                "match_score",
                0
            ),
            x.get(
                "sale_date"
            ) or ""
        ),
        reverse=True,
    )

    return {
        "sales": sales,

        "query_used": (
            unique_queries[0]
            if unique_queries
            else ""
        ),

        "search_level": (
            1
            if sales
            else 0
        ),
        "import_summary": {
            "pages_scanned": pages_scanned,
            "provider_total": provider_total or None,
            "coverage": coverage,
            "needs_continuation": needs_continuation,
            "max_pages_per_query": max_pages,
        },
    }


# ============================================================
# LATEST MARKET SALES
# ============================================================

def get_latest_market_sales(
    query="",
    limit=12,
):

    if query:

        return search_real_sales(
            query=query,
            limit=limit,
        )

    return []
