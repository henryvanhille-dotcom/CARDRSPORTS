import csv
import tempfile
import unittest
from pathlib import Path

import database
import watchlist


class WatchlistTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temporary_directory.name)
        self.original_database = database.DATABASE
        self.original_base_dir = watchlist.BASE_DIR
        database.DATABASE = self.base_dir / "prospectr.db"
        watchlist.BASE_DIR = self.base_dir

    def tearDown(self):
        database.DATABASE = self.original_database
        watchlist.BASE_DIR = self.original_base_dir
        self.temporary_directory.cleanup()

    def test_watchlist_requires_a_player_and_prevents_duplicate_identity(self):
        with self.assertRaisesRegex(ValueError, "Player is required"):
            watchlist.add_watchlist_card(set_name="Bowman Chrome")

        card = watchlist.add_watchlist_card(
            player="Mike Trout", year=2011, set_name="Bowman Chrome",
            card_number="175", grade="PSA 9", priority=True,
        )
        self.assertEqual(1, card["priority"])
        with self.assertRaisesRegex(ValueError, "already on the Watchlist"):
            watchlist.add_watchlist_card(
                player=" mike   trout ", year="2011", set_name="bowman chrome",
                card_number="175", grade="psa 9",
            )

    def test_target_comparison_uses_a_disclosed_starter_value_without_observed_evidence(self):
        card = watchlist.add_watchlist_card(
            player="No Evidence", year=2025, set_name="Unknown", target_price_cad="170",
        )
        self.assertEqual(25.0, card["current_estimate_cad"])
        self.assertEqual(-145.0, card["target_comparison"]["estimated_minus_target_cad"])
        self.assertEqual("predictive", card["market_estimate"]["status"])
        self.assertEqual("low", card["market_estimate"]["confidence_level"])
        self.assertIn("Low-confidence", card["market_estimate"]["message"])

    def test_observed_sales_produce_an_explicit_target_vs_estimate_calculation(self):
        with (self.base_dir / "sales_data.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "player", "year", "set_name", "card_type", "card_number",
                "grade", "sale_price_cad", "sale_date", "source",
            ])
            writer.writeheader()
            for amount in (180, 200, 220):
                writer.writerow({
                    "player": "Mike Trout", "year": "2011", "set_name": "Bowman Chrome",
                    "card_type": "RC", "card_number": "175", "grade": "PSA 9",
                    "sale_price_cad": amount, "sale_date": "2026-09-01", "source": "eBay",
                })

        card = watchlist.add_watchlist_card(
            player="Mike Trout", year=2011, set_name="Bowman Chrome", card_type="RC",
            card_number="175", grade="PSA 9", target_price_cad=170,
        )
        self.assertEqual("estimated", card["market_estimate"]["status"])
        self.assertEqual(200.0, card["current_estimate_cad"])
        self.assertEqual(30.0, card["target_comparison"]["estimated_minus_target_cad"])
        self.assertEqual("above_target", card["target_comparison"]["relation"])

    def test_update_rekeys_identity_and_filters_priority(self):
        standard = watchlist.add_watchlist_card(player="One Player", year=2024)
        priority = watchlist.add_watchlist_card(player="Two Player", year=2024, priority="high")
        self.assertEqual(2, priority["priority"])
        self.assertEqual([priority["id"]], [card["id"] for card in watchlist.get_watchlist_cards(priority_only=True)])

        updated = watchlist.update_watchlist_card(standard["id"], card_number="42", priority=1)
        self.assertEqual("42", updated["card_number"])
        self.assertEqual(1, updated["priority"])
        self.assertTrue(watchlist.delete_watchlist_card(updated["id"]))
        self.assertIsNone(watchlist.get_watchlist_card(updated["id"]))


if __name__ == "__main__":
    unittest.main()
