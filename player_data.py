import json
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


# ============================================================
# PROSPECTR PLAYER DATA ENGINE
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

CACHE_DIR = BASE_DIR / "player_cache"
CACHE_DIR.mkdir(exist_ok=True)

CACHE_SECONDS = 60 * 30


# ============================================================
# HTTP
# ============================================================

def fetch_json(url, params=None, timeout=20):
    """
    Fetch JSON from a public data endpoint.
    """

    if params:
        url = url + "?" + urlencode(params)

    request = Request(
        url,
        headers={
            "User-Agent": "Prospectr/1.0"
        }
    )

    try:
        with urlopen(
            request,
            timeout=timeout
        ) as response:

            raw = response.read()

            return json.loads(
                raw.decode("utf-8")
            )

    except HTTPError as exc:

        print(
            "PLAYER DATA HTTP ERROR:",
            exc.code,
            url
        )

    except URLError as exc:

        print(
            "PLAYER DATA URL ERROR:",
            exc.reason
        )

    except Exception as exc:

        print(
            "PLAYER DATA ERROR:",
            repr(exc)
        )

    return None


# ============================================================
# CACHE
# ============================================================

def cache_file(player_id):
    return CACHE_DIR / f"{player_id}.json"


def load_cache(player_id):
    """
    Load cached player data if it is still fresh.
    """

    path = cache_file(player_id)

    if not path.exists():
        return None

    try:

        age = (
            time.time()
            -
            path.stat().st_mtime
        )

        if age > CACHE_SECONDS:
            return None

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as file:

            return json.load(file)

    except Exception:

        return None


def save_cache(player_id, data):
    """
    Save player data locally.
    """

    try:

        path = cache_file(player_id)

        with open(
            path,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                data,
                file,
                indent=2
            )

    except Exception as exc:

        print(
            "CACHE SAVE ERROR:",
            repr(exc)
        )


# ============================================================
# MLB PLAYER SEARCH
# ============================================================

def search_mlb_player(player_name):
    """
    Search MLB's public stats API for a player.
    """

    if not player_name:
        return None

    data = fetch_json(
        "https://statsapi.mlb.com/api/v1/people/search",
        {
            "names": player_name
        }
    )

    if not data:
        return None

    people = data.get(
        "people",
        []
    )

    if not people:
        return None

    # Prefer an exact full-name match.
    normalized = player_name.strip().lower()

    for person in people:

        full_name = person.get(
            "fullName",
            ""
        ).strip().lower()

        if full_name == normalized:
            return person

    return people[0]


# ============================================================
# PLAYER PROFILE
# ============================================================

def get_player_profile(player_id):
    """
    Retrieve the MLB player profile.
    """

    return fetch_json(
        f"https://statsapi.mlb.com/api/v1/people/{player_id}"
    )


# ============================================================
# SEASON STATS
# ============================================================

def get_player_stats(
    player_id,
    season=2026
):
    """
    Retrieve MLB hitting statistics.
    """

    data = fetch_json(
        f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats",
        {
            "stats": "season",
            "group": "hitting",
            "season": season
        }
    )

    if not data:
        return None

    stats = data.get(
        "stats",
        []
    )

    if not stats:
        return None

    splits = stats[0].get(
        "splits",
        []
    )

    if not splits:
        return None

    return splits[0].get(
        "stat",
        {}
    )


# ============================================================
# NORMALIZE MLB STATS
# ============================================================

def normalize_stats(stats):
    """
    Convert MLB API statistics into clean numeric fields.
    """

    if not stats:
        return {}

    numeric_fields = [
        "gamesPlayed",
        "atBats",
        "runs",
        "hits",
        "doubles",
        "triples",
        "homeRuns",
        "rbi",
        "stolenBases",
        "baseOnBalls",
        "strikeOuts",
        "avg",
        "obp",
        "slg",
        "ops"
    ]

    result = {}

    for field in numeric_fields:

        value = stats.get(field)

        if value is None:
            continue

        try:

            if field in {
                "avg",
                "obp",
                "slg",
                "ops"
            }:

                result[field] = float(
                    value
                )

            else:

                result[field] = int(
                    float(value)
                )

        except (
            TypeError,
            ValueError
        ):

            result[field] = value

    return result


# ============================================================
# PLAYER DATA
# ============================================================

def get_player_data(
    player_name,
    season=2026
):
    """
    Main player-data function.

    Returns:
        player identity
        profile
        season stats
    """

    player = search_mlb_player(
        player_name
    )

    if not player:

        return {
            "status": "not_found",
            "player": player_name
        }

    player_id = player.get(
        "id"
    )

    if not player_id:

        return {
            "status": "no_player_id",
            "player": player_name
        }

    cached = load_cache(
        f"{player_id}_{season}"
    )

    if cached:

        return cached

    profile_data = get_player_profile(
        player_id
    )

    profile = {}

    if profile_data:

        people = profile_data.get(
            "people",
            []
        )

        if people:

            profile = people[0]

    stats = get_player_stats(
        player_id,
        season
    )

    normalized_stats = normalize_stats(
        stats
    )

    result = {
        "status": "success",

        "player": {
            "id": player_id,
            "name": player.get(
                "fullName"
            ),
            "position": (
                player.get("primaryPosition", {})
                .get("name")
            ),
            "team": (
                player.get("currentTeam", {})
                .get("name")
            ),
            "birth_date": player.get(
                "birthDate"
            ),
            "height": player.get(
                "height"
            ),
            "weight": player.get(
                "weight"
            )
        },

        "profile": profile,

        "season": season,

        "stats": normalized_stats,

        "data_source": "MLB Stats API"
    }

    save_cache(
        f"{player_id}_{season}",
        result
    )

    return result


# ============================================================
# SIMPLE PLAYER FORM
# ============================================================

def calculate_basic_form(stats):
    """
    Create a basic performance score from traditional stats.

    This is intentionally separate from the future Statcast
    score so we can see exactly what each layer contributes.
    """

    if not stats:

        return {
            "score": None,
            "status": "no_data"
        }

    components = []

    avg = stats.get("avg")

    if avg is not None:

        components.append(
            (
                "batting_average",
                max(
                    0,
                    min(
                        100,
                        ((avg - 0.200) / 0.120) * 100
                    )
                ),
                0.20
            )
        )

    obp = stats.get("obp")

    if obp is not None:

        components.append(
            (
                "on_base_percentage",
                max(
                    0,
                    min(
                        100,
                        ((obp - 0.280) / 0.150) * 100
                    )
                ),
                0.25
            )
        )

    slg = stats.get("slg")

    if slg is not None:

        components.append(
            (
                "slugging",
                max(
                    0,
                    min(
                        100,
                        ((slg - 0.300) / 0.400) * 100
                    )
                ),
                0.25
            )
        )

    ops = stats.get("ops")

    if ops is not None:

        components.append(
            (
                "ops",
                max(
                    0,
                    min(
                        100,
                        ((ops - 0.550) / 0.450) * 100
                    )
                ),
                0.30
            )
        )

    if not components:

        return {
            "score": None,
            "status": "no_data"
        }

    weighted = sum(
        value * weight
        for _, value, weight in components
    )

    weights = sum(
        weight
        for _, _, weight in components
    )

    score = (
        weighted / weights
        if weights
        else 50
    )

    return {
        "score": round(
            score,
            1
        ),
        "status": "success",
        "components": {
            name: round(
                value,
                1
            )
            for name, value, _ in components
        }
    }


# ============================================================
# TEST / DEMO
# ============================================================

if __name__ == "__main__":

    player = get_player_data(
        "Mike Trout"
    )

    print()
    print(
        "=========================================="
    )
    print(
        "PROSPECTR PLAYER DATA TEST"
    )
    print(
        "=========================================="
    )

    print(
        json.dumps(
            player,
            indent=2
        )
    )

    if player.get("status") == "success":

        form = calculate_basic_form(
            player.get("stats")
        )

        print()
        print(
            "BASIC PERFORMANCE SCORE:",
            form
        )
