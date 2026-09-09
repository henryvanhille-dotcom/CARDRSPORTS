import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

import app
import database
import vault
from market_data import Sale, SalesRepository, safe_url
from valuation import CardQuery, calculate_valuation


def sale(identifier, price, card_number="175", sold_on=date(2026, 9, 1)):
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
        listing_url="https://example.test/" + identifier,
        title="2011 Bowman Chrome Mike Trout #{} PSA 9".format(card_number),
    )


class SalesRepositoryTests(unittest.TestCase):
    def test_image_signature_detection(self):
        self.assertEqual(".jpg", app._image_extension(b"\xff\xd8\xff\xe0JFIF"))
        self.assertEqual(".png", app._image_extension(b"\x89PNG\r\n\x1a\nrest"))
        self.assertEqual(".webp", app._image_extension(b"RIFF\x00\x00\x00\x00WEBPVP8 "))
        self.assertIsNone(app._image_extension(b"not an image"))

    def test_database_value_helpers_reject_non_finite_numbers_and_parse_flags(self):
        self.assertIsNone(database.safe_float("NaN"))
        self.assertIsNone(database.safe_float("Infinity"))
        self.assertEqual(12.5, database.safe_float("12.5"))
        self.assertEqual(0, database.optional_bool("false"))
        self.assertEqual(1, database.optional_bool("yes"))
        self.assertIsNone(database.optional_bool("pending"))

    def test_only_http_links_are_exposed(self):
        self.assertEqual("", safe_url("javascript:alert(1)"))
        self.assertEqual("https://example.test/sale", safe_url("https://example.test/sale"))

    def test_demo_rows_never_become_market_sales(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base_dir = Path(temporary_directory)
            with (base_dir / "sales_data.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["player", "year", "set_name", "sale_price_usd", "sale_date", "source"],
                )
                writer.writeheader()
                writer.writerow({"player": "Demo Player", "year": "2024", "set_name": "Demo", "sale_price_usd": "100", "sale_date": "SAMPLE", "source": "sample"})
                writer.writerow({"player": "Mike Trout", "year": "2011", "set_name": "Bowman Chrome", "sale_price_usd": "200", "sale_date": "2026-09-01", "source": "eBay"})

            repository = SalesRepository(base_dir)
            loaded = repository.all_observed_sales()
            self.assertEqual(1, len(loaded))
            self.assertEqual("Mike Trout", loaded[0].player)
            self.assertEqual(1, repository.source_summary()["sample_rows_excluded"])


class ValuationTests(unittest.TestCase):
    def setUp(self):
        self.card = CardQuery(
            player="Mike Trout",
            year="2011",
            set_name="Bowman Chrome",
            card_type="RC",
            card_number="175",
            grade="PSA 9",
        )

    def test_no_stored_sales_means_no_dollar_estimate(self):
        result = calculate_valuation(self.card, [], today=date(2026, 9, 7))
        self.assertEqual("insufficient_data", result["status"])
        self.assertIsNone(result["estimated_value_cad"])
        self.assertEqual(0, result["comp_count"])

    def test_exact_card_sales_produce_a_transparent_estimate(self):
        result = calculate_valuation(
            self.card,
            [sale("one", 280), sale("two", 300), sale("three", 320)],
            today=date(2026, 9, 7),
        )
        self.assertEqual("estimated", result["status"])
        self.assertEqual(3, result["exact_comp_count"])
        self.assertEqual(300.0, result["estimated_value_cad"])
        self.assertGreater(result["confidence_percent"], 0)
        self.assertTrue(all(comp["match_level"] == "exact" for comp in result["comps"]))

    def test_different_card_number_is_never_called_exact(self):
        result = calculate_valuation(
            self.card,
            [sale("other-card", 475, card_number="101")],
            today=date(2026, 9, 7),
        )
        self.assertEqual("estimated", result["status"])
        self.assertEqual(0, result["exact_comp_count"])
        self.assertNotEqual("exact", result["comps"][0]["match_level"])

    def test_one_of_one_without_an_exact_sale_uses_a_clearly_labelled_prediction(self):
        one_of_one = CardQuery(
            player="Mike Trout",
            year="2011",
            set_name="Bowman Chrome",
            card_type="RC",
            card_number="1",
            parallel="Superfractor 1/1",
            grade="PSA 9",
        )
        result = calculate_valuation(
            one_of_one,
            [sale("one", 280), sale("two", 300), sale("three", 320)],
            today=date(2026, 9, 7),
            predictive_mode=True,
            one_of_one=True,
        )
        self.assertEqual("predictive", result["status"])
        self.assertTrue(result["is_predictive"])
        self.assertEqual(0, result["exact_comp_count"])
        self.assertEqual(3, result["related_comp_count"])
        self.assertEqual(3.0, result["scarcity_multiplier"])
        self.assertEqual(900.0, result["estimated_value_cad"])
        self.assertLessEqual(result["confidence_percent"], 48)

    def test_predictive_mode_requires_an_anchor_when_no_related_sales_exist(self):
        unknown_card = CardQuery(player="No Evidence", year="2024", set_name="Unknown Set")
        result = calculate_valuation(
            unknown_card,
            [],
            predictive_mode=True,
            one_of_one=True,
        )
        self.assertEqual("insufficient_data", result["status"])
        self.assertIsNone(result["estimated_value_cad"])

        anchored = calculate_valuation(
            unknown_card,
            [],
            predictive_mode=True,
            one_of_one=True,
            reference_value_cad=100,
        )
        self.assertEqual("predictive", anchored["status"])
        self.assertEqual(300.0, anchored["estimated_value_cad"])
        self.assertEqual("user-supplied reference value", anchored["prediction_basis"])

    def test_first_bowman_auto_segment_can_anchor_a_no_sale_prediction(self):
        segment_sales = [
            Sale(
                id="segment-{}".format(price),
                player="Other Prospect",
                year="2025",
                set_name="Bowman Chrome",
                card_type="1st Bowman Auto",
                card_number="CPA-1",
                parallel="Base",
                grade="Raw",
                price_cad=price,
                sale_date=date(2026, 9, 1),
                platform="eBay",
                listing_url="https://example.test/segment-{}".format(price),
                title="2025 Bowman Chrome 1st Bowman Auto Other Prospect",
            )
            for price in (100, 200, 300)
        ]
        target = CardQuery(
            player="No Sale Prospect",
            year="2025",
            set_name="Bowman Chrome",
            card_type="1st Bowman Auto",
        )
        result = calculate_valuation(
            target,
            segment_sales,
            today=date(2026, 9, 7),
            predictive_mode=True,
            first_bowman_auto=True,
        )
        self.assertEqual("predictive", result["status"])
        self.assertEqual("first_bowman_auto_segment", result["anchor_kind"])
        self.assertEqual("recent first-Bowman autograph market segment", result["prediction_basis"])
        self.assertEqual(200.0, result["estimated_value_cad"])
        self.assertEqual(3, result["related_comp_count"])
        self.assertEqual(1, len(result["sales_chart"]))


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temporary_directory.name)
        self.original_database = database.DATABASE
        self.original_base_dir = vault.BASE_DIR
        database.DATABASE = self.base_dir / "prospectr.db"
        vault.BASE_DIR = self.base_dir

    def tearDown(self):
        database.DATABASE = self.original_database
        vault.BASE_DIR = self.original_base_dir
        self.temporary_directory.cleanup()

    def test_vault_card_is_validated_and_profit_is_recalculated(self):
        card = vault.add_vault_card(
            player="Mike Trout",
            year="2011",
            set_name="Bowman Chrome",
            purchase_price_cad="200",
            estimated_value_cad="250",
            favorite="false",
        )
        self.assertEqual(2011, card["year"])
        self.assertEqual(50.0, card["profit_loss_cad"])
        self.assertEqual(0, card["favorite"])

        updated = vault.update_vault_card(
            card["id"],
            purchase_price_cad="300",
            favorite="true",
        )
        self.assertEqual(-50.0, updated["profit_loss_cad"])
        self.assertEqual(1, updated["favorite"])

        cleared = vault.update_vault_card(card["id"], estimated_value_cad=None)
        self.assertIsNone(cleared["profit_loss_cad"])
        self.assertEqual("not_analyzed", cleared["prospectr_status"])
        self.assertEqual(1, vault.get_vault_summary()["unvalued_cards"])

        with self.assertRaisesRegex(ValueError, "Purchase price"):
            vault.add_vault_card(player="Mike Trout", purchase_price_cad="not-a-number")
        with self.assertRaisesRegex(ValueError, "Image URL"):
            vault.add_vault_card(player="Mike Trout", image_url="javascript:alert(1)")

    def test_vault_summary_return_uses_only_cards_with_a_meaningful_valued_basis(self):
        zero_basis_card = vault.add_vault_card(
            player="Zero Cost Card",
            purchase_price_cad=0,
            estimated_value_cad=100,
        )
        self.assertIsNone(zero_basis_card["profit_loss_cad"])

        no_meaningful_basis = vault.get_vault_summary()
        self.assertEqual(0.0, no_meaningful_basis["valued_cost_basis_cad"])
        self.assertIsNone(no_meaningful_basis["total_profit_loss_percent"])

        vault.add_vault_card(
            player="Tracked Card",
            purchase_price_cad=100,
            estimated_value_cad=125,
        )
        vault.add_vault_card(
            player="Unvalued Card",
            purchase_price_cad=800,
        )
        vault.add_vault_card(
            player="No Cost Card",
            estimated_value_cad=300,
        )

        summary = vault.get_vault_summary()
        self.assertEqual(900.0, summary["total_purchase_price_cad"])
        self.assertEqual(525.0, summary["total_estimated_value_cad"])
        self.assertEqual(25.0, summary["total_profit_loss_cad"])
        self.assertEqual(100.0, summary["valued_cost_basis_cad"])
        self.assertEqual(25.0, summary["total_profit_loss_percent"])

    def test_vault_analysis_uses_stored_sales_and_never_invents_a_value(self):
        with (self.base_dir / "sales_data.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "player", "year", "set_name", "card_type", "card_number",
                    "grade", "sale_price_cad", "sale_date", "source",
                ],
            )
            writer.writeheader()
            for price in (280, 300, 320):
                writer.writerow({
                    "player": "Mike Trout", "year": "2011", "set_name": "Bowman Chrome",
                    "card_type": "RC", "card_number": "175", "grade": "PSA 9",
                    "sale_price_cad": price, "sale_date": "2026-09-01", "source": "eBay",
                })

        card = vault.add_vault_card(
            player="Mike Trout", year=2011, set_name="Bowman Chrome",
            card_type="RC", card_number="175", grade="PSA 9",
        )
        result = vault.analyze_vault_card(card["id"])
        self.assertEqual("estimated", result["valuation"]["status"])
        self.assertEqual(300.0, result["card"]["estimated_value_cad"])
        self.assertEqual("analyzed", result["card"]["prospectr_status"])

        no_data = vault.add_vault_card(player="No Evidence", year=2024, set_name="Test Set")
        no_data_result = vault.analyze_vault_card(no_data["id"])
        self.assertEqual("insufficient_data", no_data_result["valuation"]["status"])
        self.assertIsNone(no_data_result["card"]["estimated_value_cad"])
        self.assertEqual("insufficient_data", no_data_result["card"]["prospectr_status"])


if __name__ == "__main__":
    unittest.main()
