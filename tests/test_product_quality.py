import unittest

from strategies.product_quality import (
    build_error_report,
    build_quality_report,
    classify_barcode,
    classify_scrape_error,
    normalize_barcode,
    normalize_product,
)


class ProductQualityTests(unittest.TestCase):
    def test_normalizes_upc_with_spaces_and_hyphens(self):
        product = normalize_product({"product_name": "A", "ean": "0 12345-67890 5"})

        self.assertEqual(product["upc"], "012345678905")
        self.assertEqual(product["barcode_raw"], "0 12345-67890 5")
        self.assertEqual(product["identifier_type"], "upc")

    def test_classifies_identifier_lengths(self):
        self.assertEqual(classify_barcode("12345678"), "ean8")
        self.assertEqual(classify_barcode("012345678905"), "upc")
        self.assertEqual(classify_barcode("1234567890123"), "ean13")
        self.assertEqual(classify_barcode("12345678901234"), "gtin14")

    def test_rejects_non_barcode_digits(self):
        self.assertEqual(normalize_barcode("12345"), "")
        self.assertEqual(normalize_barcode("abc"), "")

    def test_quality_report_flags_low_identifier_coverage(self):
        report = build_quality_report(
            [
                {"product_name": "A", "price": "$1.00"},
                {"product_name": "B", "price": "$2.00"},
                {"product_name": "C", "price": "$3.00"},
            ],
            strategy_name="fixture",
        )

        self.assertLess(report["score"], 1.0)
        self.assertTrue(any("UPC/EAN/GTIN" in w for w in report["warnings"]))

    def test_error_report_classifies_blocked_and_timeout_sites(self):
        self.assertEqual(classify_scrape_error("403 Forbidden"), "blocked")
        self.assertEqual(classify_scrape_error("Read timed out after 8 seconds"), "timeout")

        report = build_error_report("Read timed out after 8 seconds", stage="fetch")
        self.assertEqual(report["score"], 0.0)
        self.assertEqual(report["error_category"], "timeout")
        self.assertIn("fetch", report["stage"])

    def test_quality_report_flags_aggregate_summary_rows(self):
        report = build_quality_report(
            [{"product_name": '205 results for "Syrup"', "product_url": "https://example.com/product/1"}],
            strategy_name="fixture",
        )

        self.assertTrue(any("summaries" in w for w in report["warnings"]))
        self.assertLess(report["score"], 0.8)

    def test_quality_report_flags_incomplete_catalog_coverage(self):
        report = build_quality_report(
            [{"product_name": f"Product {i}", "upc": "012345678905"} for i in range(20)],
            expected_products=205,
            strategy_name="fixture",
        )

        self.assertTrue(any("captured 20/205" in w for w in report["warnings"]))
        self.assertLess(report["score"], 1.0)


if __name__ == "__main__":
    unittest.main()
