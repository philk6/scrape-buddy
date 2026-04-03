"""
strategies/router.py — Strategy selection and fallback logic

Decision flow:
  1. Scan the listing page HTML for /products/ links (fast, no extra requests).
     - If found → go straight to Strategy 2 (Detail Page Crawl). No point
       running Strategy 1 first; we already know the better path.
     - If not found → try Strategy 1 (Listing Page Heuristics) first, then
       fall back to Strategy 2 if results are sparse.

  2. If Strategy 1 runs and returns fewer than STRATEGY_1_MIN_RESULTS products,
     escalate to Strategy 2.

  3. If both strategies return nothing, return whatever Strategy 1 found
     (possibly empty) with a diagnostic message.

Adding a new strategy:
  1. Create strategies/my_strategy.py with ID, NAME, and run(html, url) -> list
  2. Import it here
  3. Add routing logic below

Planned future strategies:
  3. Playwright / JS rendering — for React/Vue/Next.js SPAs
  4. Login / session-based — for supplier portals behind auth
  5. Per-site custom rules — for specific high-value suppliers
"""

import logging
import re
from . import listing, detail
from .detail import count_product_links, enrich_from_detail_pages
from .page_classifier import classify as classify_page

logger = logging.getLogger(__name__)


def _static_html_has_product_content(html: str) -> bool:
    """
    Quick heuristic: does the static HTML contain actual product data?

    Returns True if the page has price-like text ($X.XX) outside of <script>
    tags. Returns False if the page is a JS shell — templates exist but no
    rendered product data (prices, SKUs) is present.

    This is the key signal for deciding whether to escalate to Playwright:
    a page might have thousands of chars of navigation chrome but zero
    product content if all products are rendered by JavaScript.
    """
    # Strip <script> and <style> content so we don't match JS template literals
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    visible_text = soup.get_text(separator=" ")

    # Check for price patterns — the most reliable signal that product data
    # has been rendered into the DOM (not just templated)
    has_prices = bool(re.search(r"\$\d+\.\d{2}", visible_text))
    if has_prices:
        return True

    # Check for SKU patterns as a secondary signal
    has_skus = bool(re.search(
        r"\b(?:SKU|Item\s*#|Part\s*#|UPC)\s*[:\s]?\s*[A-Z0-9]{3,}",
        visible_text, re.IGNORECASE
    ))
    return has_skus