import unittest

from strategies.detail import _extract_from_detail_page


class DetailIdentifierTests(unittest.TestCase):
    def test_extracts_gtin14_from_json_ld(self):
        html = """
        <html><head>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Product",
          "name": "Case Product",
          "sku": "ABC123",
          "gtin14": "10012345678905"
        }
        </script>
        </head><body></body></html>
        """

        product = _extract_from_detail_page(html, "https://example.com/p/1")

        self.assertEqual(product["upc"], "10012345678905")
        self.assertEqual(product["gtin"], "10012345678905")
        self.assertEqual(product["identifier_type"], "gtin14")

    def test_extracts_ean13_from_meta(self):
        html = """
        <html><head>
        <meta itemprop="name" content="Meta Product">
        <meta itemprop="gtin13" content="4006381333931">
        </head><body><h1>Meta Product</h1></body></html>
        """

        product = _extract_from_detail_page(html, "https://example.com/p/2")

        self.assertEqual(product["upc"], "4006381333931")
        self.assertEqual(product["ean"], "4006381333931")
        self.assertEqual(product["identifier_type"], "ean13")

    def test_extracts_script_product_id(self):
        html = """
        <html><body>
        <h1>Script Product</h1>
        <script>
        window.__PRODUCT__ = { productID: "012345678905", salePrice: "12.99" };
        </script>
        </body></html>
        """

        product = _extract_from_detail_page(html, "https://example.com/p/3")

        self.assertEqual(product["upc"], "012345678905")
        self.assertEqual(product["identifier_type"], "upc")


if __name__ == "__main__":
    unittest.main()
