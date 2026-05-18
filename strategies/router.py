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
import os
import re
from . import listing, detail, firecrawl_fallback
from .detail import count_product_links, enrich_from_detail_pages
from .page_classifier import classify as classify_page
from .product_quality import build_quality_report, normalize_products
# universal_pipeline import is lazy — see _run() — to keep startup fast

logger = logging.getLogger(__name__)


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
        return value if value > 0 else default
    except ValueError:
        return default


def _playwright_wait_plan() -> list[int]:
    if os.environ.get("SCRAPEBUDDY_BENCHMARK_MODE"):
        wait_ms = _positive_int_env("SCRAPEBUDDY_PLAYWRIGHT_WAIT_MS", 2000)
        return [wait_ms]
    wait_ms = os.environ.get("SCRAPEBUDDY_PLAYWRIGHT_WAIT_MS")
    if wait_ms:
        first = _positive_int_env("SCRAPEBUDDY_PLAYWRIGHT_WAIT_MS", 5000)
        return [first, min(first * 2, 12_000)]
    return [5000, 8000]


def _finalize_result_dict(result: dict) -> dict:
    """Normalize products and attach a reusable quality report."""
    products = normalize_products(result.get("products", []))
    result["products"] = products
    result["diagnostics"] = build_quality_report(
        products,
        strategy_name=result.get("strategy_name", ""),
    )
    return result


def _try_firecrawl_fallback(url: str, reason_prefix: str = "") -> dict | None:
    if not firecrawl_fallback.enabled():
        logger.info("[Router] Firecrawl fallback disabled or FIRECRAWL_API_KEY not set")
        return None

    products = firecrawl_fallback.run(url)
    if not products:
        return None

    reason = (
        f"{reason_prefix}; Firecrawl hosted rendered extraction returned "
        f"{len(products)} product(s)"
    ).strip("; ")
    return _result(
        {"id": firecrawl_fallback.ID, "name": firecrawl_fallback.NAME},
        products,
        reason,
    )


def _firecrawl_mode() -> str:
    return os.environ.get("SCRAPEBUDDY_FIRECRAWL_MODE", "fallback").strip().lower()


def _should_try_firecrawl_first() -> bool:
    return firecrawl_fallback.enabled() and _firecrawl_mode() in {
        "first",
        "primary",
        "always",
        "firecrawl_first",
    }


def _static_html_has_product_content(html: str) -> bool:
    """
    Quick heuristic: does the static HTML contain actual product data?

    Returns True if the page has prices, SKUs, or repeated product elements
    (names/images) outside of <script> tags. Some sites hide prices behind
    login but still render product names and images for logged-out users.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    visible_text = soup.get_text(separator=" ")

    # Check for price patterns — strongest signal
    if re.search(r"\$\d+\.\d{2}", visible_text):
        return True

    # Check for SKU patterns
    if re.search(
        r"\b(?:SKU|Item\s*#|Part\s*#|UPC)\s*[:\s]?\s*[A-Z0-9]{3,}",
        visible_text, re.IGNORECASE
    ):
        return True

    # Check for repeated product elements (names or images) — catches sites
    # that hide prices behind login but still render product cards
    product_name_els = soup.find_all(
        lambda el: el.name not in ("script", "style") and el.get("class")
        and any(sig in " ".join(el.get("class")).lower()
                for sig in ("product-name", "product-title", "item-name",
                            "item-title", "product-description"))
    )
    if len(product_name_els) >= 3:
        return True

    product_imgs = soup.find_all("img", src=True)
    product_img_count = sum(
        1 for img in product_imgs
        if any(sig in (img.get("src") or "").lower()
               for sig in ("product", "item", "catalog"))
    )
    if product_img_count >= 3:
        return True

    return False

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


def _try_playwright_render(url: str, wait_ms: int = 5000) -> str | None:
    """
    Attempt to fetch the page with Playwright JS rendering.
    Returns the rendered HTML string, or None if Playwright is unavailable
    or the fetch fails.

    Uses strict=True so we can distinguish "not installed" from "installed
    but page is empty" — the former means we should stop retrying.
    """
    try:
        from scraper import fetch_html_playwright, PlaywrightUnavailableError
    except ImportError:
        return None

    try:
        return fetch_html_playwright(url, wait_ms=wait_ms, strict=True)
    except PlaywrightUnavailableError:
        logger.warning("[Router] Playwright is not installed — JS rendering unavailable")
        return None
    except Exception as e:
        logger.warning(f"[Router] Playwright fetch failed: {e}")
        return None


def _run(html: str, url: str, use_playwright: bool = False) -> dict:
    """Internal implementation — called by run_best_strategy inside a try/except."""
    logger.info(f"[Router] Starting strategy selection for: {url}")

    # ── Page classification ───────────────────────────────────────────────────
    page_type = classify_page(html, url)
    logger.info(
        f"[Router] Page type: {page_type.type} "
        f"(confidence={page_type.confidence:.0%}, reason: {page_type.reason})"
    )

    if page_type.type == "login_required":
        logger.warning("[Router] Page requires login — cannot scrape without authentication")
        return {
            "strategy_id":   0,
            "strategy_name": "None",
            "reason":        "Page requires login. Use the Login & Scrape feature for this supplier.",
            "products":      [],
        }

    # ── Detect JS-rendered pages and escalate to Playwright ──────────────────
    # Two triggers:
    #   A) Page classifier explicitly says "js_app" (short body + framework)
    #   B) Static HTML has no product content (prices/SKUs) — the page might
    #      LOOK normal (long body text, nav chrome) but products are JS-rendered.
    #      This catches KnockoutJS, Angular, and API-driven sites that the
    #      simple js_app heuristic misses.
    if _should_try_firecrawl_first():
        firecrawl_result = _try_firecrawl_fallback(
            url,
            reason_prefix="Firecrawl primary extraction is enabled",
        )
        if firecrawl_result:
            logger.info("[Router] Firecrawl primary extraction succeeded")
            return firecrawl_result
        logger.info(
            "[Router] Firecrawl primary extraction returned no products; "
            "continuing with local extraction"
        )

    playwright_attempted = False
    static_has_products = _static_html_has_product_content(html)

    if page_type.type == "js_app" or not static_has_products:
        trigger = "js_app classification" if page_type.type == "js_app" else "no product data in static HTML"
        logger.info(
            f"[Router] Escalating to Playwright — trigger: {trigger} "
            f"(static_has_products={static_has_products})"
        )
        use_playwright = True
        playwright_attempted = True

        # Try with progressively longer waits. KnockoutJS/Angular sites
        # need time to: (1) download the framework, (2) fetch API data,
        # (3) render the DOM. 3 seconds is rarely enough.
        for wait_ms in _playwright_wait_plan():
            js_html = _try_playwright_render(url, wait_ms=wait_ms)
            if js_html is None:
                logger.info("[Router] Playwright unavailable — skipping JS rendering")
                break

            # Check if the rendered content actually has product data
            pw_has_products = _static_html_has_product_content(js_html)
            logger.info(
                f"[Router] Playwright render ({wait_ms}ms): "
                f"{len(js_html)} chars, has_products={pw_has_products}"
            )

            if pw_has_products:
                html = js_html
                page_type = classify_page(html, url)
                logger.info(
                    f"[Router] Re-classified after JS render: {page_type.type} "
                    f"(confidence={page_type.confidence:.0%})"
                )
                break

            # If first attempt didn't produce products, try longer wait
            logger.info(
                f"[Router] Playwright ({wait_ms}ms) rendered but no products — "
                f"trying longer wait"
            )
        else:
            logger.warning(
                "[Router] Playwright rendered HTML but no product data appeared "
                "— continuing with best available HTML"
            )
            # Use the Playwright HTML even if no products detected — it might
            # still have more content than the static version
            if js_html and len(js_html) > len(html) + 200:
                html = js_html
                page_type = classify_page(html, url)

    # Set classification_confident AFTER any Playwright re-classification
    classification_confident = page_type.confidence >= MIN_CLASSIFICATION_CONFIDENCE

    if page_type.type == "detail_page":
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
    # Universal pipeline first: it combines structured data, B2B row/table
    # extraction, generic cards, LLM fallback, Playwright rendering, and
    # pagination. Legacy strategies remain below as fallback paths.
    universal_result = None
    pipeline_result = None
    try:
        from .universal_pipeline import run_pipeline as run_universal_pipeline
        universal_result = run_universal_pipeline(
            html, url, use_playwright=playwright_attempted or use_playwright
        )
        universal_products = universal_result.get("products", [])
        universal_min = 1 if page_type.type == "detail_page" else STRATEGY_1_MIN_RESULTS
        if len(universal_products) >= universal_min:
            urls_present = sum(1 for p in universal_products if p.get("product_url"))
            if urls_present:
                logger.info(
                    f"[Router] Universal pipeline found {len(universal_products)} "
                    f"product(s); enriching {urls_present} detail URL(s)"
                )
                try:
                    universal_products = enrich_from_detail_pages(universal_products)
                    universal_result["reason"] = (
                        universal_result.get("reason", "")
                        + f"; detail pages enriched {urls_present} product(s)"
                    ).strip("; ")
                except Exception as e:
                    logger.warning(f"[Router] Universal detail enrichment failed: {e}")
            universal_result["products"] = universal_products
            return _finalize_result_dict(universal_result)
        logger.info(
            f"[Router] Universal pipeline returned {len(universal_products)} "
            f"product(s); keeping legacy fallbacks available"
        )
    except Exception as e:
        logger.warning(f"[Router] Universal pipeline failed; falling back: {e}")

    product_link_count = count_product_links(html, url)
    logger.info(f"[Router] Found {product_link_count} product detail link(s) on the listing page")

    should_fast_track_to_detail = (
        product_link_count >= PRODUCTS_LINK_THRESHOLD
        and page_type.type != "row_catalog"
        and (
            page_type.type == "detail_page"
            or (page_type.type == "listing_grid" and classification_confident)
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
    elif product_link_count >= PRODUCTS_LINK_THRESHOLD and page_type.type != "row_catalog":
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

    # ── Universal Pipeline fallback (Tiers 1→1.5→2→3) ─────────────────
    # Both legacy strategies failed or returned sparse results.
    # Hand off to the universal pipeline which adds LLM extraction (Tier 2)
    # and smarter Playwright integration (Tier 3).
    logger.info(
        f"[Router] Legacy strategies returned {len(products_s1)} + {len(products_s2)} products — "
        f"escalating to Universal Pipeline"
    )
    if universal_result is not None:
        pipeline_result = universal_result
    else:
        from .universal_pipeline import run_pipeline as run_universal_pipeline
        pipeline_result = run_universal_pipeline(html, url, use_playwright=playwright_attempted)

    if pipeline_result.get("products"):
        logger.info(
            f"[Router] Universal Pipeline succeeded — "
            f"{len(pipeline_result['products'])} product(s) via {pipeline_result.get('tier', '?')}"
        )
        return _finalize_result_dict(pipeline_result)

    # ── Nothing worked anywhere — return best partial result ──────────────────
    firecrawl_result = _try_firecrawl_fallback(
        url,
        reason_prefix="Local extraction and Playwright pipeline returned no products",
    )
    if firecrawl_result:
        return firecrawl_result

    best_products = products_s1 or products_s2 or pipeline_result.get("products", [])
    reason = (
        f"All extraction methods returned few results. "
        f"The site may require authentication, have anti-bot protection, "
        f"or use an unusual rendering approach. "
        f"Showing {len(best_products)} result(s)."
    )
    logger.warning(f"[Router] All methods failed — {reason}")
    return _result(s1, best_products, reason)


def _result(strategy: dict, products: list, reason: str) -> dict:
    """
    Build the standard result dict returned to app.py.
    Logs a structured scrape-summary line for every completed run.
    """
    # ── Per-scrape summary logging ─────────────────────────────────────────
    products = normalize_products(products)
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
    diagnostics = build_quality_report(products, strategy_name=strategy["name"])
    for warning in diagnostics.get("warnings", []):
        logger.warning(f"[Router] Quality warning: {warning}")
    return {
        "strategy_id":   strategy["id"],
        "strategy_name": strategy["name"],
        "reason":        reason,
        "products":      products,
        "diagnostics":   diagnostics,
    }
