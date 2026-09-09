import math
import unittest
from datetime import date

from cardr_score import build_card_price_history, calculate_cardr_score
from market_data import Sale


CARD = {
    "player": "Mike Trout",
    "year": "2011",
    "set_name": "Bowman Chrome",
    "card_type": "RC",
    "card_number": "175",
    "grade": "PSA 9",
}


def sale(identifier, price, sold_on, *, card_number="175"):
    return Sale(
        id=identifier,
        player="Mike Trout",
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
        title="2011 Bowman Chrome Mike Trout #{} PSA 9".format(card_number),
    )


class CardrScoreTests(unittest.TestCase):
    def test_score_is_withheld_without_multi_month_evidence(self):
        result = calculate_cardr_score(
            CARD,
            [
                sale("one", 100, date(2026, 9, 1)),
                sale("two", 110, date(2026, 9, 2)),
                sale("three", 120, date(2026, 9, 3)),
            ],
            today=date(2026, 9, 7),
        )

        self.assertEqual("insufficient_data", result["status"])
        self.assertIsNone(result["score"])
        self.assertIsNone(result["subscores"]["momentum"]["score"])
        self.assertIn("at least 2 observed sale months", result["reason"])
        self.assertIn("not investment advice", result["disclaimer"])

    def test_score_uses_only_strict_matching_chronological_observed_sales(self):
        result = calculate_cardr_score(
            CARD,
            [
                sale("august", 100, date(2026, 8, 15)),
                sale("september-one", 120, date(2026, 9, 1)),
                sale("september-two", 140, date(2026, 9, 2)),
                sale("other-card", 1000, date(2026, 9, 3), card_number="176"),
            ],
            print_run=25,
            today=date(2026, 9, 7),
        )

        self.assertEqual("available", result["status"])
        self.assertIsInstance(result["score"], int)
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)
        self.assertEqual(3, len(result["history"]["points"]))
        self.assertEqual(["2026-08-15", "2026-09-01", "2026-09-02"], [point["date"] for point in result["history"]["points"]])
        self.assertEqual(82, result["subscores"]["rarity"]["score"])
        self.assertEqual(30.0, result["subscores"]["momentum"]["change_percent"])

    def test_invalid_and_duplicate_observations_cannot_create_a_score(self):
        invalid = sale("invalid", math.nan, date(2026, 8, 1))
        duplicate = sale("same-listing", 100, date(2026, 8, 1))
        result = calculate_cardr_score(
            CARD,
            [duplicate, duplicate, invalid, sale("later", 120, date(2026, 9, 1))],
            today=date(2026, 9, 7),
        )

        self.assertEqual("insufficient_data", result["status"])
        self.assertEqual(2, len(result["history"]["points"]))
        self.assertEqual(1, result["history"]["evidence"]["duplicates"])
        self.assertEqual(1, result["history"]["evidence"]["invalid_sales"])

    def test_history_never_manufactures_missing_points_or_matches_broad_identity(self):
        history = build_card_price_history(
            {"player": "Mike Trout", "year": "2011", "set_name": "Bowman Chrome", "card_number": "175"},
            [sale("late", 120, date(2026, 9, 1)), sale("early", 100, date(2026, 8, 1)), sale("wrong", 500, date(2026, 9, 2), card_number="176")],
        )

        self.assertEqual("available", history["status"])
        self.assertTrue(history["enough_for_chart"])
        self.assertEqual(
            [
                {"date": "2026-08-01", "price": 100.0, "currency": "CAD", "sale_id": "early", "platform": "eBay"},
                {"date": "2026-09-01", "price": 120.0, "currency": "CAD", "sale_id": "late", "platform": "eBay"},
            ],
            history["points"],
        )

    def test_score_needs_a_specific_card_identity(self):
        result = calculate_cardr_score(
            {"player": "Mike Trout", "set_name": "Bowman Chrome"},
            [sale("one", 100, date(2026, 8, 1)), sale("two", 120, date(2026, 9, 1)), sale("three", 140, date(2026, 9, 2))],
            today=date(2026, 9, 7),
        )

        self.assertEqual("insufficient_data", result["status"])
        self.assertIsNone(result["score"])
        self.assertIn("too broad", result["reason"])


if __name__ == "__main__":
    unittest.main()
