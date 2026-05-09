import os
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


if __name__ == "__main__":
    unittest.main()
