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
from . import listing, detail
from .detail import count_product_links, enrich_from_detail_pages
from .page_classifier import classify as classify_page

logger = logging.getLogger(__name__)

# Strategy 1 is considered successful if it returns at least this many products
STRATEGY_1_MIN_RESULTS = 3

# If the listing page has at least this many /products/ links, Strategy 2 is
# considered viable. We still avoid skipping Strategy 1 on low-confidence pages,
# because some public supplier sites expose a few /products/ links while their
# primary extraction value still lives in explicit row/card structure.
PRODUCTS_LINK_THRESHOLD = 1

# Minimum classifier confidence required before we allow page-type-driven fast
# routing. Low-confidence classifications should preserve layered fallback
# behaviour instead of forcing one path too early.
MIN_CLASSIFICATION_CONFIDENCE = 0.75


def run_best_strategy(html: str, url: str, use_playwright: bool = False) -> dict:
    """
    Choose and run the best scraping strategy for the given page.

    Always returns a structured dict — never raises, never returns None.

    Args:
        html: Raw HTML of the listing/category page (already fetched by app.py).
        url:  URL of that page.
        use_playwright: If True, the initial page was fetched with Playwright
                       (site blocks regular HTTP).  Passed through to pagination
                       so subsequent pages also use Playwright.

    Returns:
        {
            "strategy_id":   int,
            "strategy_name": str,
            "reason":        str,
            "products":      list,   # empty list if nothing found
        }
    """
    try:
        return _run(html, url, use_playwright=use_playwright)
    except Exception as e:
        logger.error(f"[Router] Unexpected error — returning empty result. Error: {e}")
        return {
            "strategy_id":   0,
            "strategy_name": "None",
            "reason":        f"Unexpected error during scraping: {e}",
            "products":      [],
        }


def _run(html: str, url: str, use_playwright: bool = False) -> dict:
    """Internal implementation — called by run_best_strategy inside a try/except."""
    logger.info(f"[Router] Starting strategy selection for: {url}")

    # ── Page classification ───────────────────────────────────────────────────
    page_type = classify_page(html, url)
    # page_type is a ClassificationResult — compares equal to its .type string
    # so all existing == comparisons below work unchanged.
    logger.info(
        f"[Router] Page type: {page_type.type} "
        f"(confidence={page_type.confidence:.0%}, reason: {page_type.reason})"
    )

    if page_type == "login_required":
        logger.warning("[Router] Page requires login — cannot scrape without authentication")
        return {
            "strategy_id":   0,
            "strategy_name": "None",
            "reason":        "Page requires login. Use the Login & Scrape feature for this supplier.",
            "products":      [],
        }

    if page_type == "js_app":
        logger.info("[Router] Page appears to be a JS-rendered SPA — re-fetching with Playwright")
        use_playwright = True  # Site needs JS rendering for all pages
        try:
            from scraper import fetch_html_playwright
            js_html = fetch_html_playwright(url)
            if len(js_html) > len(html) + 200:
                logger.info(
                    f"[Router] Playwright rendered {len(js_html)} chars "
                    f"(vs {len(html)} from requests) — using JS-rendered HTML"
                )
                html = js_html
                # Re-classify with the JS-rendered content
                page_type = classify_page(html, url)
                logger.info(
                    f"[Router] Re-classified after JS render: {page_type.type} "
                    f"(confidence={page_type.confidence:.0%})"
                )
                classification_confident = page_type.confidence >= MIN_CLASSIFICATION_CONFIDENCE
            else:
                logger.info("[Router] Playwright did not produce more content — continuing with original HTML")
        except Exception as e:
            logger.warning(f"[Router] Playwright re-fetch failed: {e} — continuing with original HTML")

    # Set classification_confident AFTER any Playwright re-classification
    # so it reflects the most up-to-date page_type.
    classification_confident = page_type.confidence >= MIN_CLASSIFICATION_CONFIDENCE

    if page_type == "detail_page":
        logger.info(
            f"[Router] URL classified as detail_page "
            f"(confidence={page_type.confidence:.0%}) — "
            f"trying Strategy 2 anyway"
        )

    # ── Pre-check: does the page have product detail links? ────────────────────
    # This is a cheap scan (no extra HTTP requests) that lets us skip Strategy 1
    # when we already know the page links to product detail pages.
    # Uses broad product path signals (/products/, /product/, /item/, /p/, etc.)
    #
    # Exception: row_catalog pages are always routed through Strategy 1 first,
    # because Strategy 1 uses the explicit column structure (row_extractor) to
    # extract images, SKUs, prices, and the correct product-detail links directly
    # from the structured layout.
    product_link_count = count_product_links(html, url)
    logger.info(f"[Router] Found {product_link_count} product detail link(s) on the listing page")

    should_fast_track_to_detail = (
        product_link_count >= PRODUCTS_LINK_THRESHOLD
        and page_type != "row_catalog"
        and (
            page_type == "detail_page"
            or (page_type == "listing_grid" and classification_confident)
        )
    )

    if should_fast_track_to_detail:
        logger.info(
            f"[Router] {product_link_count} product link(s) detected on a "
            f"confident {page_type.type} page — skipping Strategy 1 and going "
            f"straight to Strategy 2 (Detail Page Crawl)"
        )
        s2 = {"id": detail.ID, "name": detail.NAME, "run": detail.run}
        products = s2["run"](html, url)

        if products:
            reason = (
                f"Detected {product_link_count} product link(s) on the listing page; "
                f"Strategy 2 scraped {len(products)} product detail pages"
            )
            logger.info(f"[Router] Strategy 2 succeeded — {len(products)} products")
            return _result(s2, products, reason)

        reason = (
            f"Detected {product_link_count} product link(s) but Strategy 2 "
            f"could not extract data (bot protection or JS rendering likely). "
            f"Falling back to Strategy 1."
        )
        logger.warning(f"[Router] Strategy 2 found links but returned no products — falling back")
    elif product_link_count >= PRODUCTS_LINK_THRESHOLD and page_type != "row_catalog":
        logger.info(
            f"[Router] {product_link_count} product link(s) detected, but page "
            f"classification confidence is only {page_type.confidence:.0%} "
            f"({page_type.type}) — preserving layered Strategy 1 first"
        )

    # ── Strategy 1: Listing Page Heuristics ───────────────────────────────────
    s1 = {"id": listing.ID, "name": listing.NAME, "run": listing.run}
    logger.info(f"[Router] Trying Strategy {s1['id']}: {s1['name']}")

    products_s1 = s1["run"](html, url, use_playwright=use_playwright)

    if len(products_s1) >= STRATEGY_1_MIN_RESULTS:
        reason = f"Strategy 1 found {len(products_s1)} products directly on the listing page"
        logger.info(f"[Router] Strategy 1 succeeded — {reason}")

        # Enrich with detail pages if products have URLs.
        # This captures BARCODE→upc, ITEM-NO→sku, MASTER CASE→case_pack,
        # unit_size, unit_price, and pricing_unit that only live on detail pages.
        urls_present = sum(1 for p in products_s1 if p.get("product_url"))
        if urls_present:
            logger.info(
                f"[Router] Enriching {urls_present} product(s) from detail pages "
                f"(listing-page products have URLs)"
            )
            try:
                products_s1 = enrich_from_detail_pages(products_s1)
                reason += f"; detail pages enriched {urls_present} product(s)"
            except Exception as e:
                logger.warning(f"[Router] Detail enrichment failed (non-fatal): {e}")
        else:
            logger.info("[Router] No product URLs found — skipping detail enrichment")

        return _result(s1, products_s1, reason)

    logger.info(
        f"[Router] Strategy 1 returned only {len(products_s1)} product(s) "
        f"(threshold: {STRATEGY_1_MIN_RESULTS}). Escalating to Strategy 2."
    )

    # ── Strategy 2: Detail Page Crawl (fallback) ──────────────────────────────
    s2 = {"id": detail.ID, "name": detail.NAME, "run": detail.run}
    logger.info(f"[Router] Trying Strategy {s2['id']}: {s2['name']}")

    products_s2 = s2["run"](html, url)

    if products_s2:
        reason = (
            f"Strategy 1 found only {len(products_s1)} product(s) on the listing page; "
            f"Strategy 2 scraped {len(products_s2)} from individual detail pages"
        )
        logger.info(f"[Router] Strategy 2 succeeded — {reason}")
        return _result(s2, products_s2, reason)

    # ── Both failed — try Playwright as last resort ──────────────────
    if not products_s1:
        logger.info("[Router] Both strategies returned empty — trying Playwright as last resort")
        try:
            from scraper import fetch_html_playwright
            pw_html = fetch_html_playwright(url)
            if pw_html and len(pw_html) > 500:
                # Re-run Strategy 1 with JS-rendered HTML (and Playwright for pagination)
                products_pw = s1["run"](pw_html, url, use_playwright=True)
                if products_pw:
                    reason = (
                        f"Both strategies failed on static HTML, but Playwright "
                        f"JS-rendering found {len(products_pw)} product(s)"
                    )
                    logger.info(f"[Router] Playwright last resort succeeded — {reason}")
                    return _result(s1, products_pw, reason)

                # Try Strategy 2 with JS-rendered HTML
                products_pw2 = s2["run"](pw_html, url)
                if products_pw2:
                    reason = (
                        f"Both strategies failed on static HTML, but Playwright "
                        f"+ Strategy 2 found {len(products_pw2)} product(s)"
                    )
                    logger.info(f"[Router] Playwright + Strategy 2 succeeded — {reason}")
                    return _result(s2, products_pw2, reason)
        except Exception as e:
            logger.warning(f"[Router] Playwright last resort failed: {e}")

    reason = (
        f"Both strategies returned few results. "
        f"The site likely requires JavaScript rendering or blocks automated requests. "
        f"Showing {len(products_s1)} result(s) from the listing page."
    )
    logger.warning(f"[Router] Both strategies failed — {reason}")
    return _result(s1, products_s1, reason)


def _result(strategy: dict, products: list, reason: str) -> dict:
    """
    Build the standard result dict returned to app.py.
    Logs a structured scrape-summary line for every completed run.
    """
    # ── Per-scrape summary logging ─────────────────────────────────────────
    has_name     = sum(1 for p in products if p.get("product_name"))
    has_price    = sum(1 for p in products if p.get("price"))
    has_url      = sum(1 for p in products if p.get("product_url"))
    has_image    = sum(1 for p in products if p.get("image_url"))
    has_sku      = sum(1 for p in products if p.get("sku"))
    has_upc      = sum(1 for p in products if p.get("upc"))
    logger.info(
        f"[Router] Scrape summary — "
        f"strategy={strategy['name']!r} | "
        f"total={len(products)} | "
        f"name={has_name} price={has_price} url={has_url} "
        f"image={has_image} sku={has_sku} upc={has_upc}"
    )
    return {
        "strategy_id":   strategy["id"],
        "strategy_name": strategy["name"],
        "reason":        reason,
        "products":      products,
    }
