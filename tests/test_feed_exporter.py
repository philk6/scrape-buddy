import unittest

from strategies import feed_exporter


class FeedExporterTests(unittest.TestCase):
    def test_shopify_rows_expand_variants_with_upc_and_spreadsheet_fields(self):
        products = [
            {
                "id": 100,
                "title": "Reusable Respirator",
                "vendor": "ReGo Trading Inc",
                "handle": "reusable-respirator",
                "body_html": "<p>Respirator description</p>",
                "tags": ["Brand_3M", "case/1", "Health Care", "RegInv"],
                "images": [{"id": 10, "src": "https://cdn.example.com/item.jpg"}],
                "variants": [
                    {
                        "id": 200,
                        "name": "3M Reusable Respirator Half Facepiece 7502 - 1pk",
                        "sku": "0711-51131-70784/37082",
                        "barcode": "051131370821",
                        "price": "13.97",
                        "available": False,
                        "image_id": 10,
                    }
                ],
            }
        ]

        rows = feed_exporter._shopify_rows(products, "https://regowholesale.com")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["product_name"], "3M Reusable Respirator Half Facepiece 7502 - 1pk")
        self.assertEqual(rows[0]["brand"], "3M")
        self.assertEqual(rows[0]["sku"], "0711-51131-70784/37082")
        self.assertEqual(rows[0]["upc"], "051131370821")
        self.assertEqual(rows[0]["price"], "$13.97")
        self.assertEqual(rows[0]["case_pack"], "1")
        self.assertEqual(rows[0]["category"], "Health Care; RegInv")
        self.assertEqual(rows[0]["description"], "Respirator description")
        self.assertEqual(rows[0]["availability"], "Unavailable")
        self.assertEqual(rows[0]["source_product_id"], "100")
        self.assertEqual(rows[0]["source_variant_id"], "200")
        self.assertEqual(rows[0]["source_platform"], "Product feed")

    def test_enrich_shopify_product_from_html_reads_selected_variant_barcode(self):
        product = {
            "variants": [{"id": 200, "sku": "", "barcode": ""}],
        }
        page_html = '''
        <script>
        window.meta = {"selected_variant_drop":{"id":200,"sku":"ABC-1","barcode":"012345678905"}};
        </script>
        '''

        feed_exporter._enrich_shopify_product_from_html(product, page_html)

        self.assertEqual(product["variants"][0]["sku"], "ABC-1")
        self.assertEqual(product["variants"][0]["barcode"], "012345678905")

    def test_woocommerce_rows_extract_feed_fields_and_identifier_attribute(self):
        products = [
            {
                "id": 300,
                "name": "Wholesale Coffee Case",
                "slug": "wholesale-coffee-case",
                "sku": "COFFEE-CASE",
                "short_description": "<p>Dark roast case.</p>",
                "is_in_stock": True,
                "prices": {
                    "currency_symbol": "$",
                    "currency_minor_unit": 2,
                    "price": "1999",
                },
                "permalink": "https://shop.example.com/product/wholesale-coffee-case/",
                "categories": [{"name": "Grocery"}],
                "tags": [{"name": "Case"}],
                "images": [{"src": "https://shop.example.com/coffee.jpg"}],
                "attributes": [
                    {"name": "Brand", "terms": [{"name": "Acme"}]},
                    {"name": "UPC", "terms": [{"name": "012345678905"}]},
                ],
            }
        ]

        rows = feed_exporter._woocommerce_rows(products, "https://shop.example.com")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["product_name"], "Wholesale Coffee Case")
        self.assertEqual(rows[0]["brand"], "Acme")
        self.assertEqual(rows[0]["sku"], "COFFEE-CASE")
        self.assertEqual(rows[0]["upc"], "012345678905")
        self.assertEqual(rows[0]["price"], "$19.99")
        self.assertEqual(rows[0]["category"], "Grocery")
        self.assertEqual(rows[0]["description"], "Dark roast case.")
        self.assertEqual(rows[0]["availability"], "Available")
        self.assertEqual(rows[0]["source_product_id"], "300")
        self.assertEqual(rows[0]["source_platform"], "Product feed")


if __name__ == "__main__":
    unittest.main()
