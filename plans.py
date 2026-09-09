"""Public CARDR plan definitions.

This module is deliberately separate from billing and authentication.  It
keeps the product limits and website copy consistent now, while a future
account/billing layer can enforce the same identifiers and caps server-side.
"""

from typing import Dict, List


PLAN_CATALOG: List[Dict[str, object]] = [
    {
        "id": "free",
        "name": "Free",
        "price_cad_monthly": 0,
        "trial_days": 0,
        "limits": {
            "analyses_per_month": 10,
            "vault_cards": 25,
            "watchlist_cards": 10,
            "predictive_valuations_per_month": 0,
        },
        "features": [
            "CARDR Score and observed-sale price history",
            "Portfolio and daily Pulse",
            "Observed-market valuations",
        ],
    },
    {
        "id": "pro",
        "name": "Pro",
        "price_cad_monthly": 15.99,
        "trial_days": 7,
        "limits": {
            "analyses_per_month": 250,
            "vault_cards": 1_000,
            "watchlist_cards": 250,
            "predictive_valuations_per_month": 50,
        },
        "features": [
            "Everything in Free",
            "Prospectr rare-card and no-exact-sale predictions",
            "Expanded Vault, Watchlist, and portfolio workflow",
        ],
    },
    {
        "id": "ultimate",
        "name": "Ultimate",
        "price_cad_monthly": 45.99,
        "trial_days": 7,
        "limits": {
            "analyses_per_month": 2_000,
            "vault_cards": 10_000,
            "watchlist_cards": 2_000,
            "predictive_valuations_per_month": 500,
        },
        "features": [
            "Everything in Pro",
            "Priority refresh access coming soon",
            "Highest workflow and collection limits",
        ],
    },
]


def public_plan_catalog() -> List[Dict[str, object]]:
    """Return a copy that routes and templates can safely serialize."""
    return [
        {
            **plan,
            "limits": dict(plan["limits"]),
            "features": list(plan["features"]),
        }
        for plan in PLAN_CATALOG
    ]
