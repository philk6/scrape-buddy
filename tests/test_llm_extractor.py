import os
import unittest

from strategies import llm_extractor


class LlmExtractorTests(unittest.TestCase):
    def test_enabled_requires_openai_key(self):
        old = os.environ.pop("OPENAI_API_KEY", None)
        try:
            self.assertFalse(llm_extractor.enabled())
        finally:
            if old is not None:
                os.environ["OPENAI_API_KEY"] = old

    def test_parse_structured_response_normalizes_urls_and_identifiers(self):
        response = """
        {
          "products": [
            {
              "product_name": "Chocolate Bar Case",
              "sku": "CHOCO-12",
              "gtin": "12345678901234",
              "price": "$24.99",
              "image_url": "/images/choco.jpg",
              "product_url": "/products/chocolate-bar-case"
            },
            {
              "product_name": "12 results for chocolate",
              "product_url": "/search/chocolate"
            }
          ]
        }
        """

        products = llm_extractor._parse_llm_response(
            response,
            "https://example.com/catalog",
        )

        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["product_name"], "Chocolate Bar Case")
        self.assertEqual(products[0]["product_url"], "https://example.com/products/chocolate-bar-case")
        self.assertEqual(products[0]["image_url"], "https://example.com/images/choco.jpg")
        self.assertEqual(products[0]["upc"], "12345678901234")
        self.assertEqual(products[0]["identifier_type"], "gtin14")

    def test_extract_from_text_skips_short_content_without_api_call(self):
        self.assertEqual(
            llm_extractor.extract_from_text("tiny", "https://example.com"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
