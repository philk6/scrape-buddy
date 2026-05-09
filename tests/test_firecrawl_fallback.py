import os
import json
import tempfile
import unittest

from strategies import firecrawl_fallback


class FirecrawlFallbackTests(unittest.TestCase):
    def test_enabled_requires_api_key(self):
        old = os.environ.pop("FIRECRAWL_API_KEY", None)
        try:
            self.assertFalse(firecrawl_fallback.enabled())
        finally:
            if old is not None:
                os.environ["FIRECRAWL_API_KEY"] = old

    def test_enabled_allows_primary_mode_with_api_key(self):
        old_key = os.environ.get("FIRECRAWL_API_KEY")
        old_disabled = os.environ.pop("SCRAPEBUDDY_FIRECRAWL_DISABLED", None)
        try:
            os.environ["FIRECRAWL_API_KEY"] = "fc-test"
            self.assertTrue(firecrawl_fallback.enabled())
        finally:
            if old_key is None:
                os.environ.pop("FIRECRAWL_API_KEY", None)
            else:
                os.environ["FIRECRAWL_API_KEY"] = old_key
            if old_disabled is not None:
                os.environ["SCRAPEBUDDY_FIRECRAWL_DISABLED"] = old_disabled

    def test_extract_products_from_nested_response(self):
        payload = {
            "success": True,
            "data": {
                "json": {
                    "products": [
                        {
                            "product_name": "Syrup Bottle",
                            "sku": "SYR-1",
                            "gtin": "1234567890123",
                            "price": "$8.99",
                            "product_url": "/product/syrup",
                        },
                        {
                            "product_name": '205 results for "syrup"',
                            "product_url": "/search/syrup",
                        },
                    ]
                }
            },
        }

        products = firecrawl_fallback._extract_products_from_response(
            payload,
            "https://example.com/catalog",
        )

        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["product_name"], "Syrup Bottle")
        self.assertEqual(products[0]["product_url"], "https://example.com/product/syrup")
        self.assertEqual(products[0]["upc"], "1234567890123")
        self.assertEqual(products[0]["identifier_type"], "ean13")

    def test_cookie_header_from_state_file_filters_to_target_domain(self):
        state = {
            "cookies": [
                {"name": "session", "value": "abc", "domain": ".example.com"},
                {"name": "other", "value": "def", "domain": ".other.com"},
                {"name": "pref", "value": "light", "domain": "shop.example.com"},
            ]
        }
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f)

            header = firecrawl_fallback._cookie_header_from_state_file(
                path,
                "https://shop.example.com/catalog",
            )
        finally:
            os.remove(path)

        self.assertIn("session=abc", header)
        self.assertIn("pref=light", header)
        self.assertNotIn("other=def", header)


if __name__ == "__main__":
    unittest.main()
