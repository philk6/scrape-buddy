"""
scraper.py — Core scraping logic for Scrape Buddy v1

How it works:
1. fetch_html() downloads the raw HTML of the given URL using requests,
   with a realistic browser User-Agent to avoid basic bot blocks.

2. extract_products() parses that HTML with BeautifulSoup and uses
   CSS class heuristics to find product cards on the page:
   - It looks for elements whose class names contain  keywords like
     "product", "item", "card" — common patterns across e-commerce sites,
    - From each candidate element it tries to pull: name, brand, price,
      product URL, image URL, and SKU using further keyword matching.
    - Missing fields default to empty string"".
    - Results are deduplicated by product_url and filtered to remove
       empty entries (no name AND no url).

This approach works across many sites without needing site-specific config.
Future versions can add: Playwright for JS-rendered pages, JSON-LD/microdata
parsing, site-specific extractors, and pagination support.
"""

import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from collections import Counter

# Browser-like headers to avoid basic bot detection
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Class name keywords used to identify product container elements
PRODUCT_KEYWORDS = ["product", "item", "card", "listing", "tile", "result"]

# Minimum number of matching product-like elements to consider a selector valid
MIN_PRODUCTS = 2
# URL path fragments that indicate a category, collection, or browse page
# rather than an individual product detail page.
# Applied in extract_products() to reject collection tiles masquerading as products.
_COLLECTEDWRL_PATTERNS = [
    "/collections/", "/collection/",
    "/category/", "/categories/",
    "/dept/", "/department/",
    "/c/", "/cat/",
    "/brand/", "/brands/",
    "/search", "/cart", "/checkout", "/account",
    "/login", "/register", "/wishlist", "/compare",
    "/pages/", "/blogs/", "/blog/",
    "/tag/", "/tags/",
    "/contact", "/about", "/faq", "/help",
]


# Name patterns that indicate a category or navigation title, not a product.
# Checked via _looks_like_category_name().
_CATEGORY_NAME_SEPARATORS = [" : ", " > ", " / ", " | "]
_CATEGORY_NAME_PREFIXES = [
    "shop all", "view all", "see all", "browse all",
    "shop", "browse ", "all ",
]

# HTML tag names that indicate site-chrome / navigation context.
# Elements inside these tags should not be treated as product cards.
_CHROME_TAGS = frozenset(["nav", "header", "footer", "aside", "menu"])

# Class/id fragments that indicate nav, breadcrumb, filter, or sidebar context.
# Substring-matched against the combined class+id string (lowercased).
_CHROME_CLASS_SIGNALS = [
    "breadcrumb", "crumb",
    "sidebar", "side-bar",
    "navbar", "nav-bar", "nav-menu", "nav-item", "navigation",
    "menu-item", "menuitem",
    "filter", "facet",
    "footer", "header",
    "banner", "announcement",
    "dropdown", "flyout", "mega-menu",
    "pagination", "pager",
]


def fetch_html(url: str) -> str:
    """   Fetch the HTML content of the given URL.
    Raises requests.RequestException on network/HTTP errors.
    """
    response = requests.get(url, headers=HEADERS, timeout=15)
    response.raise_for_status()
    return response.text

def fetch_html_playwright(url: str, wait_ms: int = 3000) -> str:
    """
    Fetch HTML using Playwright with full JavaScript rendering.
    Falls back to regular fetch_html() if Playwright is not available.

    Used for:
      - JS-rendered SPAs (React, Vue, Next.js)
      - Sites that require JS to load product data
      - Sites detected as 'js_app' by the page classifier
    """
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1920, "height": 1080},
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Wait for dynamic content to load
            page.wait_for_timeout(wait_ms)
            # Try to wait for common product selectors
            for selector in [
                "[class*='product']", "[class*='item']", "[class*='card']",
                "[data-testid*='product']", ".product-list", ".products-grid",
            ]:
                try:
                    page.wait_for_selector(selector, timeout=3000)
                    break
                except Exception:
                    continue
            html = page.content()
            browser.close()
            return html
    except ImportError:
        import logging
        logging.getLogger(__name__).warning(
            "[scraper] Playwright not available — falling back to requests"
        )
        return fetch_html(url)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"[scraper] Playwright fetch failed ({e}) — falling back to requests"
        )
        return fetch_html(url)
