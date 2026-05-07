import unittest
from unittest.mock import patch

import app


class PublicBrowserRoutingTests(unittest.TestCase):
    def test_empty_or_js_html_uses_browser_crawler(self):
        self.assertTrue(app._html_needs_browser_crawler(None))
        self.assertTrue(app._html_needs_browser_crawler(""))
        self.assertTrue(app._html_needs_browser_crawler(
            "<html><body><script id='__NEXT_DATA__' type='application/json'>{}</script></body></html>"
        ))

    def test_static_product_html_can_stay_on_html_pipeline(self):
        html = """
        <html><body>
          <div class="product-card"><span>Alpha Widget</span><span>$1.00</span></div>
          <div class="product-card"><span>Beta Widget</span><span>$2.00</span></div>
          <div class="product-card"><span>Gamma Widget</span><span>$3.00</span></div>
        </body></html>
        """

        self.assertFalse(app._html_needs_browser_crawler(html))

    def test_product_shell_without_prices_uses_browser_crawler(self):
        html = """
        <html><body>
          <div class="product-card">Alpha Widget</div>
          <div class="product-card">Beta Widget</div>
          <div class="product-card">Gamma Widget</div>
        </body></html>
        """

        self.assertTrue(app._html_needs_browser_crawler(html))

    def test_public_browser_result_keeps_crawl_diagnostics(self):
        products = [{"product_name": "Alpha Widget", "product_url": "https://example.com/product/1"}]
        diagnostics = {
            "expected_pages": 3,
            "pages_visited": 1,
            "stop_reason": "no navigation path found after page 1",
        }

        with patch.object(app.playwright_catalog, "run_public", return_value=products):
            with patch.object(app.playwright_catalog, "LAST_CRAWL_DIAGNOSTICS", diagnostics):
                result, crawl = app._run_public_browser_catalog("https://example.com/catalog")

        self.assertIsNotNone(result)
        self.assertEqual(result["products"], products)
        self.assertEqual(result["_crawl_diagnostics"], diagnostics)
        self.assertEqual(crawl, diagnostics)


if __name__ == "__main__":
    unittest.main()
