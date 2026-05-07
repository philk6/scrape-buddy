import unittest

from strategies import structured_extractor


class StructuredExtractorTests(unittest.TestCase):
    def test_jsonld_nested_itemlist_product(self):
        html = """
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "ItemList",
          "itemListElement": [
            {"@type": "ListItem", "item": {
              "@type": "Product",
              "name": "Case of Fancy Pretzels",
              "sku": "PRETZ-24",
              "gtin13": "1234567890123",
              "image": "/img/pretzels.jpg",
              "offers": {"@type": "Offer", "price": "24.99", "priceCurrency": "USD"}
            }}
          ]
        }
        </script>
        """

        products = structured_extractor.extract(html, "https://example.test/catalog")

        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["product_name"], "Case of Fancy Pretzels")
        self.assertEqual(products[0]["sku"], "PRETZ-24")
        self.assertEqual(products[0]["upc"], "1234567890123")
        self.assertEqual(products[0]["price"], "$24.99")

    def test_opengraph_non_usd_price_compile_regression(self):
        html = """
        <meta property="og:type" content="product">
        <meta property="og:title" content="Imported Candy">
        <meta property="product:price:amount" content="9.50">
        <meta property="product:price:currency" content="CAD">
        """

        products = structured_extractor.extract(html, "https://example.test/item")

        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["price"], "9.50 CAD")


if __name__ == "__main__":
    unittest.main()
