import unittest

from collection_import import CollectionImportError, parse_collection_csv, vault_create_payloads


class CollectionImportTests(unittest.TestCase):
    def test_normalizes_common_columns_and_returns_confirmation_preview(self):
        preview = parse_collection_csv(
            "Athlete,Release Year,Product,Card #,Variation,Grading,Card Condition,Paid,Date Purchased,Qty,Comments\n"
            "Mike Trout,2011,Bowman Chrome,175,Refractor,PSA 10,NM-MT,\" C$1,234.50 \",09/07/2026,2,Personal collection\n",
            filename="vault.csv",
        )

        self.assertEqual(1, preview["summary"]["ready_rows"])
        self.assertTrue(preview["summary"]["requires_confirmation"])
        record = preview["records"][0]
        self.assertEqual("Mike Trout", record["player"])
        self.assertEqual(2011, record["year"])
        self.assertEqual("Bowman Chrome", record["set_name"])
        self.assertEqual("175", record["card_number"])
        self.assertEqual("Refractor", record["parallel"])
        self.assertEqual("PSA 10", record["grade"])
        self.assertEqual("NM-MT", record["condition"])
        self.assertEqual(1234.5, record["purchase_price_cad"])
        self.assertEqual("2026-09-07", record["purchase_date"])
        self.assertEqual(2, record["quantity"])
        self.assertIn("interpreted as MM/DD/YYYY", preview["rows"][0]["warnings"][0])

    def test_formula_like_text_is_neutralized_and_price_formula_is_invalid(self):
        preview = parse_collection_csv(
            "Player,Set,Notes,Paid\n"
            "=HYPERLINK(\"https://bad.example\"),Bowman,@danger,10\n"
            "Normal Player,Chrome,okay,=1+1\n"
        )

        self.assertEqual(1, preview["summary"]["ready_rows"])
        record = preview["records"][0]
        self.assertEqual("'=HYPERLINK(\"https://bad.example\")", record["player"])
        self.assertEqual("'@danger", record["notes"])
        self.assertEqual("invalid", preview["rows"][1]["status"])
        self.assertIn("Purchase price must be a non-negative number", preview["rows"][1]["errors"][0])

    def test_invalid_rows_are_reported_without_discarding_valid_rows(self):
        preview = parse_collection_csv(
            "Player,Year,Qty,Purchase Date\n"
            "Valid Player,2024,1,2024-01-02\n"
            ",2024,1,2024-01-02\n"
            "Bad Quantity,2024,0,2024-01-02\n"
        )

        self.assertEqual(1, preview["summary"]["ready_rows"])
        self.assertEqual(2, preview["summary"]["invalid_rows"])
        self.assertEqual("Player is required.", preview["rows"][1]["errors"][0])
        self.assertIn("Quantity must be between", preview["rows"][2]["errors"][0])

    def test_utf8_bom_and_semicolon_export_are_supported(self):
        preview = parse_collection_csv(
            b"\xef\xbb\xbfPlayer;Year;Set;Number;Quantity\nJulio Rodriguez;2022;Topps Chrome;87;3\n"
        )

        self.assertEqual(1, preview["summary"]["ready_rows"])
        self.assertEqual("Julio Rodriguez", preview["records"][0]["player"])
        self.assertEqual(3, preview["records"][0]["quantity"])

    def test_rejects_unsafe_upload_sizes_row_caps_and_non_csv_filenames(self):
        with self.assertRaises(CollectionImportError):
            parse_collection_csv("Player\nMike Trout\n", filename="collection.txt")

        with self.assertRaises(CollectionImportError):
            parse_collection_csv("Player\nA\nB\n", max_rows=1)

        with self.assertRaises(CollectionImportError):
            parse_collection_csv("Player\n" + ("A" * (5 * 1024 * 1024)))

    def test_confirmed_records_expand_safely_into_vault_payloads(self):
        preview = parse_collection_csv(
            "Player,Set,Condition,Qty,Notes\n"
            "Elly De La Cruz,Bowman Chrome,NM,2,Keep together\n"
        )

        payloads = vault_create_payloads(preview["records"])
        self.assertEqual(2, len(payloads))
        self.assertEqual("Elly De La Cruz", payloads[0]["player"])
        self.assertEqual("Bowman Chrome", payloads[0]["set_name"])
        self.assertEqual("Keep together\nCondition: NM", payloads[0]["notes"])
        self.assertNotIn("quantity", payloads[0])


if __name__ == "__main__":
    unittest.main()
