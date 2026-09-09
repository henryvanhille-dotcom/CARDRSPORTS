import unittest
from datetime import date

from market_data import Sale
from market_pulse import build_market_pulse


def sale(identifier, price, sold_on, *, card_number="175", player="Mike Trout"):
    return Sale(
        id=identifier,
        player=player,
        year="2011",
        set_name="Bowman Chrome",
        card_type="RC",
        card_number=card_number,
        parallel="",
        grade="PSA 9",
        price_cad=price,
        sale_date=sold_on,
        platform="eBay",
        listing_url="https://example.test/{}".format(identifier),
        title="2011 Bowman Chrome {} #{} PSA 9".format(player, card_number),
    )


class MarketPulseTests(unittest.TestCase):
    def test_one_month_never_returns_a_trend_or_score(self):
        result = build_market_pulse(
            [
                sale("one", 100, date(2026, 9, 1)),
                sale("two", 200, date(2026, 9, 2)),
            ]
        )

        self.assertEqual("insufficient_data", result["status"])
        self.assertEqual(1, len(result["groups"]))
        group = result["groups"][0]
        self.assertEqual("insufficient_data", group["status"])
        self.assertNotIn("trend", group)
        self.assertNotIn("market_signal", group)
        self.assertEqual("CAD", group["currency"])
        self.assertEqual("2026-09", group["monthly_history"][0]["month"])

    def test_each_of_the_latest_compared_months_needs_two_sales(self):
        result = build_market_pulse(
            [
                sale("may-one", 100, date(2026, 5, 1)),
                sale("june-one", 190, date(2026, 6, 1)),
                sale("june-two", 210, date(2026, 6, 2)),
            ]
        )

        group = result["groups"][0]
        self.assertEqual("insufficient_data", result["status"])
        self.assertEqual("insufficient_data", group["status"])
        self.assertIn("2026-05: 1", group["reason"])
        self.assertIn("2026-06: 2", group["reason"])
        self.assertNotIn("trend", group)

    def test_valid_group_uses_transparent_monthly_cad_medians(self):
        result = build_market_pulse(
            [
                sale("july-one", 100, date(2026, 7, 1)),
                sale("july-two", 200, date(2026, 7, 2)),
                sale("august-one", 200, date(2026, 8, 1)),
                sale("august-two", 300, date(2026, 8, 2)),
            ]
        )

        self.assertEqual("success", result["status"])
        group = result["groups"][0]
        self.assertEqual("success", group["status"])
        self.assertEqual(["2026-07", "2026-08"], group["compared_months"])
        self.assertEqual(
            {
                "month": "2026-07",
                "sales_count": 2,
                "low_cad": 100.0,
                "high_cad": 200.0,
                "average_cad": 150.0,
                "median_cad": 150.0,
            },
            group["monthly_history"][0],
        )
        self.assertEqual("strong_up", group["trend"]["direction"])
        self.assertEqual(66.67, group["trend"]["change_percent"])
        self.assertIn("score", group["market_signal"])

    def test_different_card_numbers_are_never_combined_to_create_history(self):
        result = build_market_pulse(
            [
                sale("a-one", 100, date(2026, 7, 1), card_number="175"),
                sale("a-two", 200, date(2026, 7, 2), card_number="175"),
                sale("b-one", 100, date(2026, 8, 1), card_number="176"),
                sale("b-two", 200, date(2026, 8, 2), card_number="176"),
            ]
        )

        self.assertEqual("insufficient_data", result["status"])
        self.assertEqual(2, len(result["groups"]))
        self.assertTrue(all(group["status"] == "insufficient_data" for group in result["groups"]))
        self.assertEqual(
            {"175", "176"},
            {group["card"]["card_number"] for group in result["groups"]},
        )


if __name__ == "__main__":
    unittest.main()
