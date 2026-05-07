import unittest

from strategies.playwright_catalog import (
    _api_object_to_product,
    _candidate_page_urls,
    _detail_enrichment_count_cap,
    _looks_like_product_detail_url,
    _looks_like_aggregate_product,
    _navigation_exhausted_reason,
    _select_detail_enrichment_links,
    _walk_api_payload,
)


class PlaywrightCatalogLinkTests(unittest.TestCase):
    def test_accepts_common_detail_route_shapes(self):
        self.assertTrue(_looks_like_product_detail_url("https://example.com/product/666785"))
        self.assertTrue(_looks_like_product_detail_url("https://example.com/products/widget"))
        self.assertTrue(_looks_like_product_detail_url("https://example.com/item/ABC123"))
        self.assertTrue(_looks_like_product_detail_url("https://example.com/p/ABC123"))
        self.assertTrue(
            _looks_like_product_detail_url(
                "https://example.com/scotch-box-lock-shipping-packaging-tape-mmm1956"
            )
        )

    def test_rejects_category_and_search_routes(self):
        self.assertFalse(_looks_like_product_detail_url("https://example.com/category/syrup"))
        self.assertFalse(_looks_like_product_detail_url("https://example.com/search?q=syrup"))
        self.assertFalse(_looks_like_product_detail_url("https://example.com/collections/syrup"))
        self.assertFalse(_looks_like_product_detail_url("https://example.com/basket/view"))
        self.assertFalse(_looks_like_product_detail_url("https://cdn.example.com/product-image-123.jpg"))

    def test_collected_product_links_outrank_weak_listing_rows(self):
        links = [
            "https://example.com/product/1",
            "https://example.com/product/2",
            "https://example.com/product/3",
        ]
        listing_urls = {"https://example.com/product/1"}

        self.assertEqual(_select_detail_enrichment_links(links, listing_urls), links)

    def test_rejects_search_summary_as_product(self):
        self.assertTrue(
            _looks_like_aggregate_product(
                {
                    "product_name": '205 results for "Syrup"',
                    "product_url": "https://example.com/product/666785",
                }
            )
        )
        self.assertFalse(
            _looks_like_aggregate_product(
                {
                    "product_name": "Syrup Pump 12 oz",
                    "sku": "ABC123",
                    "product_url": "https://example.com/product/666785",
                }
            )
        )

    def test_api_object_to_product_normalizes_common_fields(self):
        product = _api_object_to_product(
            {
                "productName": "Maple Syrup",
                "brandName": "Acme",
                "itemNumber": "SYR-12",
                "gtin13": "0123456789012",
                "unitPrice": "12.99",
                "pdpUrl": "/product/123",
                "imageUrl": "/images/123.jpg",
            },
            "https://example.com/category/syrup",
        )

        self.assertEqual(product["product_name"], "Maple Syrup")
        self.assertEqual(product["sku"], "SYR-12")
        self.assertEqual(product["upc"], "0123456789012")
        self.assertEqual(product["ean"], "0123456789012")
        self.assertEqual(product["identifier_type"], "ean13")
        self.assertEqual(product["price"], "$12.99")
        self.assertEqual(product["product_url"], "https://example.com/product/123")

    def test_api_object_to_product_rejects_media_only_objects(self):
        product = _api_object_to_product(
            {
                "name": "promo image",
                "url": "https://cdn.example.com/promo-123.png",
            },
            "https://example.com/catalog",
        )

        self.assertEqual(product, {})

    def test_walk_api_payload_finds_nested_product_objects_and_urls(self):
        products = []
        urls = []
        _walk_api_payload(
            {
                "data": {
                    "items": [
                        {
                            "name": "Chocolate",
                            "sku": "CHOC-1",
                            "price": "5.50",
                            "url": "/item/choc-1",
                        },
                        {"href": "/category/not-a-product"},
                    ]
                }
            },
            "https://example.com/search?q=chocolate",
            products,
            urls,
        )

        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["product_url"], "https://example.com/item/choc-1")
        self.assertIn("https://example.com/item/choc-1", urls)
        self.assertNotIn("https://example.com/category/not-a-product", urls)

    def test_candidate_page_urls_preserve_existing_filters(self):
        urls = _candidate_page_urls(
            "https://example.com/catalog?q=syrup&sort=name",
            2,
        )

        self.assertIn("https://example.com/catalog?q=syrup&sort=name&page=2", urls)
        self.assertIn("https://example.com/catalog?q=syrup&sort=name&p=2", urls)

    def test_candidate_page_urls_update_existing_page_param(self):
        urls = _candidate_page_urls(
            "https://example.com/catalog?page=1&q=syrup",
            3,
        )

        self.assertIn("https://example.com/catalog?q=syrup&page=3", urls)
        self.assertNotIn("https://example.com/catalog?page=1&q=syrup&page=3", urls)

    def test_navigation_exhausted_reason_distinguishes_page_estimates(self):
        self.assertEqual(
            _navigation_exhausted_reason(3, 1),
            "no navigation path found after page 1",
        )
        self.assertEqual(
            _navigation_exhausted_reason(1, 1),
            "all 1 expected page(s) visited; no further navigation found",
        )
        self.assertEqual(
            _navigation_exhausted_reason(None, 4),
            "navigation exhausted after page 4",
        )
        self.assertEqual(
            _navigation_exhausted_reason(1, 2),
            "navigation exhausted after 2 page(s); detected estimate was 1",
        )

    def test_detail_enrichment_cap_ignores_undercounted_pagination(self):
        self.assertEqual(_detail_enrichment_count_cap(24, 2, 2, 40), 24)
        self.assertIsNone(_detail_enrichment_count_cap(24, 1, 2, 40))
        self.assertIsNone(_detail_enrichment_count_cap(None, None, 3, 40))
        self.assertIsNone(_detail_enrichment_count_cap(50, 3, 3, 40))


if __name__ == "__main__":
    unittest.main()
