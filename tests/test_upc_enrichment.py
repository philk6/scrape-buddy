import os
import unittest

import upc_enrichment


class FakeProvider:
    name = "upcdatabase"

    def __init__(self):
        self.calls = 0

    def lookup(self, **kwargs):
        self.calls += 1
        return {"status": "no_match_found"}


class UpcEnrichmentTests(unittest.TestCase):
    def test_external_lookup_row_budget_marks_remaining_rows(self):
        old_rows = os.environ.get("SCRAPEBUDDY_UPC_ENRICHMENT_MAX_ROWS")
        old_seconds = os.environ.get("SCRAPEBUDDY_UPC_ENRICHMENT_MAX_SECONDS")
        os.environ["SCRAPEBUDDY_UPC_ENRICHMENT_MAX_ROWS"] = "1"
        os.environ["SCRAPEBUDDY_UPC_ENRICHMENT_MAX_SECONDS"] = "60"
        provider = FakeProvider()
        products = [
            {"product_name": "Premium Chocolate Candy Bar Case", "brand": "Test Brand"},
            {"product_name": "Premium Vanilla Candy Bar Case", "brand": "Test Brand"},
        ]

        try:
            result = upc_enrichment.enrich_products_upc(products, providers=[provider])
        finally:
            if old_rows is None:
                os.environ.pop("SCRAPEBUDDY_UPC_ENRICHMENT_MAX_ROWS", None)
            else:
                os.environ["SCRAPEBUDDY_UPC_ENRICHMENT_MAX_ROWS"] = old_rows
            if old_seconds is None:
                os.environ.pop("SCRAPEBUDDY_UPC_ENRICHMENT_MAX_SECONDS", None)
            else:
                os.environ["SCRAPEBUDDY_UPC_ENRICHMENT_MAX_SECONDS"] = old_seconds

        self.assertEqual(provider.calls, 1)
        statuses = {p.get("resolution_status") for p in result}
        self.assertIn("external_lookup_skipped_budget", statuses)


if __name__ == "__main__":
    unittest.main()
