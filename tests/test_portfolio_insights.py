import unittest
from datetime import date, datetime, timezone

from market_data import Sale
from portfolio_insights import (
    build_cardr_pulse,
    build_portfolio_composition,
    build_portfolio_overview,
    make_portfolio_snapshot,
)


def sale(identifier, price, sold_on):
    return Sale(
        id=identifier,
        player="Mike Trout",
        year="2011",
        set_name="Bowman Chrome",
        card_type="RC",
        card_number="175",
        parallel="",
        grade="PSA 9",
        price_cad=price,
        sale_date=sold_on,
        platform="eBay",
        listing_url="https://example.test/{}".format(identifier),
        title="Mike Trout #175",
    )


class PortfolioInsightTests(unittest.TestCase):
    def test_overview_only_calls_winners_and_losers_with_valid_cost_basis(self):
        result = build_portfolio_overview([
            {"id": 1, "player": "Winner", "purchase_price_cad": 100, "estimated_value_cad": 160},
            {"id": 2, "player": "Loser", "purchase_price_cad": 200, "estimated_value_cad": 150},
            {"id": 3, "player": "No basis", "purchase_price_cad": 0, "estimated_value_cad": 999},
            {"id": 4, "player": "Unvalued", "purchase_price_cad": 75},
        ])

        self.assertEqual(4, result["card_count"])
        self.assertEqual(3, result["valued_card_count"])
        self.assertEqual("partial", result["estimated_value_status"])
        # Only the +$60 and -$50 positions have a valid positive cost basis.
        self.assertEqual(10.0, result["tracked_profit_loss_cad"])
        self.assertEqual(3.33, result["tracked_return_percent"])
        self.assertEqual("Winner", result["biggest_winner"]["player"])
        self.assertEqual("Loser", result["biggest_loser"]["player"])

        no_change = build_portfolio_overview([
            {"id": 5, "player": "Flat", "purchase_price_cad": 100, "estimated_value_cad": 100},
        ])
        self.assertIsNone(no_change["biggest_winner"])
        self.assertIsNone(no_change["biggest_loser"])

    def test_composition_does_not_create_an_unknown_category_and_snapshot_is_current_only(self):
        cards = [
            {"id": 1, "player": "Mike Trout", "year": 2011, "set_name": "Bowman", "grade": "PSA 9", "card_type": "RC", "estimated_value_cad": 100},
            {"id": 2, "player": "", "estimated_value_cad": None},
        ]
        composition = build_portfolio_composition(cards)
        players = composition["dimensions"]["player"]
        self.assertEqual(["Mike Trout"], [group["label"] for group in players["groups"]])
        self.assertEqual(1, players["unclassified_card_count"])

        snapshot = make_portfolio_snapshot(
            cards,
            captured_at=datetime(2026, 9, 8, 12, tzinfo=timezone.utc),
        )
        self.assertEqual("2026-09-08T12:00:00+00:00", snapshot["captured_at"])
        self.assertEqual(1, snapshot["schema_version"])
        self.assertEqual("partial", snapshot["estimated_value_status"])

    def test_pulse_never_fabricates_movers_or_trends_and_uses_real_recent_sales(self):
        result = build_cardr_pulse(
            [{"id": 1, "player": "Mike Trout", "created_at": "2026-09-07T14:00:00+00:00"}],
            [sale("one", 350, date(2026, 9, 8))],
            as_of=date(2026, 9, 8),
        )
        self.assertEqual("insufficient_data", result["your_vault"]["movers"]["status"])
        self.assertEqual("available", result["recent_market_activity"]["status"])
        self.assertEqual(350.0, result["recent_market_activity"]["largest_recent_sale"]["price_cad"])
        self.assertEqual("insufficient_data", result["trending"]["status"])
        self.assertEqual("Not enough market data yet.", result["trending"]["reason"])

    def test_pulse_exposes_a_trend_only_when_market_pulse_evidence_threshold_is_met(self):
        result = build_cardr_pulse(
            [],
            [
                sale("july-1", 100, date(2026, 7, 1)),
                sale("july-2", 200, date(2026, 7, 2)),
                sale("august-1", 200, date(2026, 8, 1)),
                sale("august-2", 300, date(2026, 8, 2)),
            ],
            as_of=date(2026, 8, 31),
            recent_days=90,
        )
        self.assertEqual("available", result["trending"]["status"])
        self.assertEqual("success", result["trending"]["cards"][0]["status"])
        self.assertEqual(4, result["recent_market_activity"]["observed_sale_count"])


if __name__ == "__main__":
    unittest.main()
