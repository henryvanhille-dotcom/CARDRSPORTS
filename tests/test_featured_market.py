import unittest
from datetime import date

from featured_market import build_featured_market
from market_data import Sale


def sale(identifier, player, price, sold_on):
    return Sale(
        id=identifier,
        player=player,
        year="2024",
        set_name="Bowman Chrome",
        card_type="RC",
        card_number=identifier,
        parallel="Base",
        grade="Raw",
        price_cad=price,
        sale_date=sold_on,
        platform="eBay",
        listing_url="https://example.test/{}".format(identifier),
        title="2024 Bowman Chrome {} #{}".format(player, identifier),
    )


class FeaturedMarketTests(unittest.TestCase):
    def test_empty_market_never_creates_placeholder_featured_cards(self):
        result = build_featured_market([])
        self.assertEqual("insufficient_data", result["status"])
        self.assertEqual([], result["featured"])

    def test_rising_signal_is_based_on_two_observed_windows(self):
        sales = [
            sale("one", "Rising Player", 100, date(2025, 1, 1)),
            sale("two", "Rising Player", 110, date(2025, 1, 20)),
            sale("three", "Rising Player", 200, date(2025, 5, 3)),
            sale("four", "Rising Player", 220, date(2025, 5, 20)),
        ]
        result = build_featured_market(sales)
        self.assertEqual("available", result["status"])
        featured = result["featured"][0]
        self.assertEqual("Rising", featured["signal"])
        self.assertEqual("📈", featured["emoji"])
        self.assertGreater(featured["change_percent"], 0)
        self.assertEqual(4, featured["observed_sale_count"])


if __name__ == "__main__":
    unittest.main()
