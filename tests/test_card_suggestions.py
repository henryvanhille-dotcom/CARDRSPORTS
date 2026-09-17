import json
import unittest

from card_suggestions import MAX_INPUT_CHARS, suggest_card_details


class CardSuggestionTests(unittest.TestCase):
    def test_explicit_text_fields_produce_confirm_before_save_suggestions(self):
        result = suggest_card_details(
            """Player: Paul Skenes
Year: 2024
Set: Bowman Chrome
Card Number: CPA-1
Type: 1st Bowman Auto
Parallel: Gold Refractor
Grade: PSA 10"""
        )

        self.assertEqual("suggestions_available", result["status"])
        self.assertEqual("text_only", result["recognition_mode"])
        self.assertFalse(result["photo_recognition"]["available"])
        self.assertTrue(result["non_authoritative"])
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual("Paul Skenes", result["suggestions"]["player"])
        self.assertEqual("2024", result["suggestions"]["year"])
        self.assertEqual("Bowman Chrome", result["suggestions"]["set_name"])
        self.assertEqual("CPA-1", result["suggestions"]["card_number"])
        self.assertEqual("1st Bowman Auto", result["suggestions"]["card_type"])
        self.assertEqual("Gold Refractor", result["suggestions"]["parallel"])
        self.assertEqual("PSA 10", result["suggestions"]["grade"])
        self.assertEqual("high", result["fields"]["player"]["confidence"])
        self.assertIn("text-only", result["notice"])

    def test_unlabelled_name_is_low_confidence_candidate_not_photo_recognition(self):
        result = suggest_card_details("2024 BOWMAN CHROME PAUL SKENES #CPA-1")

        self.assertEqual("Paul Skenes", result["fields"]["player"]["value"])
        self.assertEqual("low", result["fields"]["player"]["confidence"])
        self.assertEqual("Bowman Chrome", result["suggestions"]["set_name"])
        self.assertEqual("CPA-1", result["suggestions"]["card_number"])
        self.assertEqual(["Paul Skenes"], [candidate["value"] for candidate in result["fields"]["player"]["candidates"]])
        self.assertTrue(result["photo_recognition"]["available"] is False)
        self.assertIn("unlabeled", " ".join(result["warnings"]).casefold())

    def test_ambiguous_text_does_not_hallucinate_a_card_identity(self):
        result = suggest_card_details("Mystery box pull, nice card, maybe from 2024.")

        self.assertEqual("suggestions_available", result["status"])
        self.assertEqual("2024", result["suggestions"]["year"])
        self.assertEqual("", result["fields"]["player"]["value"])
        self.assertEqual("", result["fields"]["set_name"]["value"])
        self.assertEqual("", result["fields"]["card_number"]["value"])
        self.assertEqual("", result["fields"]["grade"]["value"])

    def test_markup_is_removed_and_never_treated_as_code(self):
        result = suggest_card_details("Player: Mike Trout <script>alert('nope')</script>\nSet: Topps Chrome")
        serialized = json.dumps(result)

        self.assertEqual("Mike Trout", result["suggestions"]["player"])
        self.assertTrue(result["input_summary"]["markup_removed"])
        self.assertNotIn("<script>", serialized.casefold())
        self.assertNotIn("</script>", serialized.casefold())
        self.assertIn("no markup was executed", " ".join(result["warnings"]).casefold())

    def test_oversized_and_non_string_input_are_rejected_before_parsing(self):
        oversized = suggest_card_details("x" * (MAX_INPUT_CHARS + 1))
        non_string = suggest_card_details({"player": "Mike Trout"})

        self.assertEqual("input_rejected", oversized["status"])
        self.assertEqual("input_too_long", oversized["error"]["code"])
        self.assertEqual({}, oversized["suggestions"])
        self.assertEqual("input_rejected", non_string["status"])
        self.assertEqual("invalid_input", non_string["error"]["code"])

    def test_one_of_one_is_not_overstated_as_a_known_parallel(self):
        result = suggest_card_details("2025 Bowman #1 1/1")

        self.assertEqual("1", result["suggestions"]["card_number"])
        self.assertEqual("1/1", result["suggestions"]["parallel"])
        self.assertEqual("medium", result["fields"]["parallel"]["confidence"])
        self.assertIn("confirm the exact parallel", " ".join(result["warnings"]).casefold())


if __name__ == "__main__":
    unittest.main()
