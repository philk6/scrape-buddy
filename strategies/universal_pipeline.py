"""
strategies/universal_pipeline.py — 3-Tier Universal Extraction Pipeline

Orchestrates product extraction through three tiers:

  Tier 1: Structured data (JSON-LD, microdata, Open Graph)
    → Fastest, most reliable. Checked first on every page.

  Tier 1.5: CSS heuristics + row extraction (existing strategies)
    → Fast-path for sites without structured data but with predictable layouts.
    → Kept as a "free" intermediate step before the LLM tier.

  Tier 2: LLM-assisted extraction
    → Send cleaned HTML to gpt-4o-mini. The universal safety net.
    → Only called when Tiers 1 and 1.5 fail.

  Tier 3: Playwright JS rendering
    → For JS-heavy SPAs where static HTML has no product content.
    → Renders the page, then re-runs Tiers 1 → 2 on the rendered output.

Public API:
  run_pipeline(html, url, use_playwright=False) -> dict
    Returns the standard result dict:
      {
        "strategy_id": int,
        "strategy_name": str,
        "reason": str,
        "products": list,
        "tier": str,        # "tier1", "tier1.5", "tier2", "tier3"
      }
"""

import logging
import re

from . import structured_extractor
from . import llm_extractor
from .row_extractor import extract_products_from_page as extract_structured_rows
from .pagination import crawl_listing_pages, dedup_products

logger = logging.getLogger(__name__)

# Minimum products to consider a tier successful
MIN_PRODUCTS = 2


def _has_product_content(html: str) -> bool:
    """
    Quick check: does the static HTML contain rendered product data?
    (Prices or SKUs in visible text, not inside <script> tags.)
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    visible = soup.get_text(separator=" ")
    if re.search(r"\$\d+\.\d{2}", visible):
        return True
    if re.search(
        r"\b(?:SKU|Item\s*#|Part\s*#|UPC)\s*[:\s]?\s*[A-Z0-9]{3,}",
        visible, re.IGNORECASE,
    ):
        return True
    return False


def _try_playwright_render(url: str, wait_ms: int = 5000) -> str | None:
    """Attempt Playwright JS render. Returns HTML or None."""
    try:
        from scraper import fetch_html_playwright, PlaywrightUnavailableError
    except ImportError:
        return None
    try:
        return fetch_html_playwright(url, wait_ms=wait_ms, strict=True)
    except Exception:
        return None


def _run_css_heuristics(html: str, url: str) -> list[dict]:
    """
    Run the existing CSS heuristic extractors (row_extractor + generic scraper).
    This is the fast-path "Tier 1.5" — no network calls, no LLM cost.
    """
    # Try structured row/table extraction first
    try:
        rows = extract_structured_rows(html, url)
        if rows is not None and len(rows) >= MIN_PRODUCTS:
            for p in rows:
                p["_source"] = "css_rows"
            return rows
    except Exception as e:
        logger.warning(f"[Pipeline] Row extractor error: {e}")

    # Try generic CSS class heuristics
    try:
        from scraper import extract_products
        products = extract_products(html, url)
        if products and len(products) >= MIN_PRODUCTS:
            for p in products:
                p["_source"] = "css_heuristic"
            return products
    except Exception as e:
        logger.warning(f"[Pipeline] CSS heuristic error: {e}")

    return []


def _result(
    products: list,
    tier: str,
    strategy_name: str,
    reason: str,
) -> dict:
    """Build the standard result dict."""
    # Clean _source from products before returning (internal metadata)
    clean_products = []
    for p in products:
        cp = {k: v for k, v in p.items() if not k.startswith("_")}
        clean_products.append(cp)

    # Log summary
    has_name = sum(1 for p in clean_products if p.get("product_name"))
    has_price = sum(1 for p in clean_products if p.get("price"))
    has_sku = sum(1 for p in clean_products if p.get("sku"))
    has_upc = sum(1 for p in clean_products if p.get("upc"))
    logger.info(
        f"[Pipeline] Result — tier={tier} strategy={strategy_name!r} "
        f"total={len(clean_products)} name={has_name} price={has_price} "
        f"sku={has_sku} upc={has_upc}"
    )

    return {
        "strategy_id": {"tier1": 10, "tier1.5": 11, "tier2": 20, "tier3": 30}.get(tier, 0),
        "strategy_name": strategy_name,
        "reason": reason,
        "products": clean_products,
        "tier": tier,
    }


def _extract_single_page(html: str, url: str, playwright_rendered: bool = False) -> tuple[list[dict], str, str]:
    """
    Run tiered extraction on a SINGLE page of HTML.

    Returns (products, tier_name, strategy_name).
    This is the core extraction logic used both for the first page
    and as the callback for pagination on subsequent pages.
    """
    # ── Tier 1: Structured data extraction ───────────────────────────────────
    tier1_products = structured_extractor.extract(html, url)
    if len(tier1_products) >= MIN_PRODUCTS:
        tier = "tier3" if playwright_rendered else "tier1"
        return tier1_products, tier, "Structured Data (JSON-LD/Microdata/OG)"

    tier1_single = tier1_products  # might be 0 or 1

    # ── Tier 1.5: CSS heuristics (fast, free) ────────────────────────────────
    css_products = _run_css_heuristics(html, url)
    if len(css_products) >= MIN_PRODUCTS:
        tier = "tier3" if playwright_rendered else "tier1.5"
        return css_products, tier, "CSS Heuristics"

    # ── Tier 2: LLM-assisted extraction ──────────────────────────────────────
    llm_products = llm_extractor.extract(html, url)
    if len(llm_products) >= 1:
        tier = "tier3" if playwright_rendered else "tier2"
        return llm_products, tier, "LLM-Assisted Extraction"

    # ── Nothing worked on this page ──────────────────────────────────────────
    best = tier1_single or css_products or llm_products or []
    return best, "none", "No extraction succeeded"


def run_pipeline(html: str, url: str, use_playwright: bool = False) -> dict:
    """
    Run the 3-tier universal extraction pipeline WITH pagination.

    Extracts products from page 1 using the tiered approach, then
    automatically paginates through all subsequent pages using the
    same extraction logic.

    Args:
        html: Raw HTML (already fetched).
        url: The page URL.
        use_playwright: If True, skip Tier 3 auto-detection (already rendered).

    Returns:
        Standard result dict with products and metadata.
    """
    logger.info(f"[Pipeline] Starting universal extraction for: {url}")

    # ── Check if static HTML has any product content at all ──────────────────
    static_has_products = _has_product_content(html)
    logger.info(f"[Pipeline] Static HTML has product content: {static_has_products}")

    # ── Tier 3 pre-check: if no product content, render with Playwright first ──
    rendered_html = html
    playwright_rendered = False

    if not static_has_products and not use_playwright:
        logger.info(
            "[Pipeline] No product data in static HTML — "
            "trying Playwright rendering before extraction"
        )
        for wait_ms in [5000, 8000]:
            pw_html = _try_playwright_render(url, wait_ms=wait_ms)
            if pw_html is None:
                logger.info("[Pipeline] Playwright unavailable")
                break
            if _has_product_content(pw_html):
                logger.info(
                    f"[Pipeline] Playwright ({wait_ms}ms) rendered product content "
                    f"({len(pw_html)} chars)"
                )
                rendered_html = pw_html
                playwright_rendered = True
                use_playwright = True  # subsequent pages should also use Playwright
                break
            logger.info(f"[Pipeline] Playwright ({wait_ms}ms) — no products yet, retrying")

    # ── Extract page 1 using tiered approach ─────────────────────────────────
    logger.info("[Pipeline] Extracting page 1...")
    page1_products, tier, strategy_name = _extract_single_page(
        rendered_html, url, playwright_rendered
    )

    if not page1_products:
        # ── Tier 3 retry: if we haven't tried Playwright yet, try now ────────
        if not playwright_rendered and not use_playwright:
            logger.info(
                "[Pipeline] All tiers failed on static HTML — "
                "trying Playwright as last resort"
            )
            for wait_ms in [5000, 8000]:
                pw_html = _try_playwright_render(url, wait_ms=wait_ms)
                if pw_html is None:
                    break
                if not _has_product_content(pw_html):
                    continue

                logger.info(f"[Pipeline] Playwright ({wait_ms}ms) rendered content — re-running extraction")
                page1_products, tier, strategy_name = _extract_single_page(pw_html, url, True)
                if page1_products:
                    rendered_html = pw_html
                    playwright_rendered = True
                    use_playwright = True
                    break

        if not page1_products:
            return _result(
                [],
                tier="none",
                strategy_name="No extraction succeeded",
                reason=(
                    "All extraction tiers failed. "
                    "The site may require authentication, have anti-bot protection, "
                    "or use an unusual rendering approach."
                ),
            )

    logger.info(
        f"[Pipeline] Page 1: {len(page1_products)} product(s) via {tier} ({strategy_name})"
    )

    # ── Paginate: crawl all subsequent pages ─────────────────────────────────
    # Build an extract_fn callback that uses the tiered extraction.
    # We capture playwright_rendered in the closure so each page gets
    # the right tier label, but functionally it runs all tiers.
    pw_flag = playwright_rendered  # capture for closure

    def _paginated_extract_fn(page_html: str, page_url: str) -> list[dict]:
        """Extraction callback for crawl_listing_pages — runs all tiers."""
        products, _tier, _strat = _extract_single_page(page_html, page_url, pw_flag)
        return products

    logger.info("[Pipeline] Starting pagination crawl...")
    all_products = crawl_listing_pages(
        start_html=rendered_html,
        start_url=url,
        extract_fn=_paginated_extract_fn,
        use_playwright=use_playwright,
    )

    if not all_products:
        # Pagination returned nothing (shouldn't happen since page 1 had products,
        # but crawl_listing_pages re-extracts page 1 via extract_fn)
        all_products = page1_products

    # ── Build final result ───────────────────────────────────────────────────
    reason = (
        f"{'Playwright + ' if playwright_rendered else ''}"
        f"{strategy_name} extracted products across pagination"
        f" ({len(all_products)} total)"
    )

    return _result(
        all_products,
        tier=tier,
        strategy_name=strategy_name,
        reason=reason,
    )
