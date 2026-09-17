import unittest

from alerts import (
    AlertValidationError,
    MAX_RULES_PER_EVALUATION,
    evaluate_alert_rules,
    validate_alert_rule,
    validate_alert_rules,
)


CARD = {
    "player": "Mike Trout",
    "year": "2011",
    "set_name": "Bowman Chrome",
    "card_number": "175",
    "card_type": "RC",
    "parallel": "",
    "grade": "PSA 9",
}


def comp(identifier, price, sold_on="2026-09-01"):
    return {
        "id": identifier,
        "price_cad": price,
        "sale_date": sold_on,
        "platform": "eBay",
        "listing_url": "https://example.test/{}".format(identifier),
        "title": "2011 Bowman Chrome Mike Trout #175 PSA 9",
        **CARD,
        "match_level": "exact",
        "match_score": 100,
    }


def observed_valuation(value, comps):
    return {
        "status": "estimated",
        "is_predictive": False,
        "estimated_value_cad": value,
        "comp_count": len(comps),
        "exact_comp_count": len(comps),
        "confidence_percent": 82,
        "comps": comps,
    }


class AlertRuleValidationTests(unittest.TestCase):
    def test_target_rule_is_normalized_and_in_app_only(self):
        rule = validate_alert_rule({
            "id": " trout-target ",
            "type": "target_price",
            "card": CARD,
            "target_price_cad": "200",
            "direction": "at_or_above",
        })
        self.assertEqual("trout-target", rule["id"])
        self.assertEqual(200.0, rule["target_price_cad"])
        self.assertEqual("in_app", rule["channel"])

    def test_invalid_rules_have_clear_errors_and_collection_has_caps(self):
        with self.assertRaisesRegex(AlertValidationError, "Only the in_app"):
            validate_alert_rule({
                "id": "email-attempt", "type": "target_price", "card": CARD,
                "target_price_cad": 100, "channel": "email",
            })
        with self.assertRaisesRegex(AlertValidationError, "needs a player"):
            validate_alert_rule({
                "id": "missing-card", "type": "new_comp", "card": {"set_name": "Bowman"},
            })
        with self.assertRaisesRegex(AlertValidationError, "maximum"):
            validate_alert_rules([
                {"id": "rule-{}".format(index), "type": "new_comp", "card": CARD}
                for index in range(MAX_RULES_PER_EVALUATION + 1)
            ])


class AlertEvaluationTests(unittest.TestCase):
    def test_target_alert_needs_direct_observed_evidence_and_deduplicates(self):
        rule = {
            "id": "target", "type": "target_price", "card": CARD,
            "target_price_cad": 200, "direction": "at_or_above",
        }
        predictive = {
            "status": "predictive",
            "is_predictive": True,
            "estimated_value_cad": 500,
            "comps": [comp("not-enough", 500)],
        }
        skipped = evaluate_alert_rules([rule], {"valuation": predictive})
        self.assertEqual([], skipped["notifications"])
        self.assertIn("predictive estimates", skipped["skipped_rules"][0]["reason"])

        wrong_card = dict(CARD, card_number="176")
        mismatched = evaluate_alert_rules([rule], {
            "valuation": observed_valuation(220, [dict(comp("wrong-card", 220), **wrong_card)]),
        })
        self.assertEqual([], mismatched["notifications"])
        self.assertIn("does not match", mismatched["skipped_rules"][0]["reason"])

        result = evaluate_alert_rules(
            [rule],
            {"valuation": observed_valuation(220, [comp("one", 220)]), "evaluated_at": "2026-09-02T10:00:00Z"},
        )
        self.assertEqual(1, len(result["notifications"]))
        notification = result["notifications"][0]
        self.assertEqual("target_price", notification["type"])
        self.assertEqual("in_app", notification["channel"])
        self.assertEqual(220.0, notification["evidence"]["valuation"]["estimated_value_cad"])
        self.assertEqual([notification["dedup_key"]], result["active_condition_keys"])

        repeat = evaluate_alert_rules(
            [rule],
            {"valuation": observed_valuation(220, [comp("one", 220)])},
            delivered_keys=[notification["dedup_key"]],
        )
        self.assertEqual([], repeat["notifications"])
        self.assertEqual(1, repeat["suppressed_deduplicated_count"])

    def test_percent_move_requires_new_observed_comp_and_persists_its_evidence(self):
        rule = {
            "id": "move", "type": "percent_move", "card": CARD,
            "threshold_percent": 10, "direction": "up",
        }
        prior = observed_valuation(100, [comp("old", 100, "2026-08-01")])
        current_without_new_evidence = observed_valuation(125, [comp("old", 100, "2026-08-01")])
        no_event = evaluate_alert_rules(
            [rule], {"valuation": current_without_new_evidence, "previous_valuation": prior}
        )
        self.assertEqual([], no_event["notifications"])

        current = observed_valuation(125, [
            comp("old", 100, "2026-08-01"),
            comp("new", 150, "2026-09-01"),
        ])
        result = evaluate_alert_rules([rule], {"valuation": current, "previous_valuation": prior})
        self.assertEqual(1, len(result["notifications"]))
        event = result["notifications"][0]
        self.assertEqual("percent_move", event["type"])
        self.assertEqual(25.0, event["evidence"]["change_percent"])
        self.assertEqual(["new"], event["evidence"]["new_observed_comp_ids"])

    def test_new_comp_uses_provenance_and_sale_specific_deduplication(self):
        rule = {"id": "comp", "type": "new_comp", "card": CARD}
        invalid_comp = {"id": "no-date", "price_cad": 200, "platform": "eBay", **CARD}
        result = evaluate_alert_rules(
            [rule], {"new_observed_comps": [invalid_comp, comp("real-sale", 210)]}
        )
        self.assertEqual(1, len(result["notifications"]))
        event = result["notifications"][0]
        self.assertEqual("new_comp", event["type"])
        self.assertEqual("real-sale", event["evidence"]["observed_comp"]["id"])

        repeated = evaluate_alert_rules(
            [rule], {"new_observed_comps": [comp("real-sale", 210)]},
            delivered_keys=[event["dedup_key"]],
        )
        self.assertEqual([], repeated["notifications"])

    def test_hot_signal_requires_verified_market_pulse_and_matching_card(self):
        rule = {"id": "hot", "type": "hot_signal", "card": CARD, "minimum_score": 70}
        no_event = evaluate_alert_rules([rule], {
            "market_pulse": {"groups": [{
                "status": "insufficient_data", "card": CARD,
                "market_signal": {"label": "Strong up", "score": 90},
            }]},
        })
        self.assertEqual([], no_event["notifications"])

        pulse_card = dict(CARD)
        result = evaluate_alert_rules([rule], {
            "market_pulse": {"groups": [{
                "status": "success",
                "card": pulse_card,
                "observed_sale_count": 4,
                "compared_months": ["2026-08", "2026-09"],
                "market_signal": {"label": "Strong up", "score": 86, "basis": "Observed medians"},
                "trend": {"direction": "strong_up", "change_percent": 21},
            }]},
        })
        self.assertEqual(1, len(result["notifications"]))
        self.assertEqual("hot_signal", result["notifications"][0]["type"])
        self.assertEqual(86.0, result["notifications"][0]["evidence"]["market_signal"]["score"])


if __name__ == "__main__":
    unittest.main()
