"""
strategies/pagination.py — Reusable pagination traversal for HTTP-fetched listing pages.

Provides crawl_listing_pages() which:
  1. Calls extract_fn(html, url) on the first (already-fetched) page.
  2. Detects total item count and total page count from result-count text
     ("Items 1-100 of 1240", "Showing 1-24 of 53 results") and/or numbered
     page buttons in a pagination container.
  3. Follows "Next" links (rel=next, aria-label, text) across all pages,
     calling extract_fn on each.
  4. Deduplicates the merged product list by product_url, sku, and
     normalised name+brand+price.
  5. Returns the deduplicated list with structured logging throughout.

Loop protection:
  - Tracks visited page URLs — never re-fetches a URL.
  - Hard cap of MAX_LISTING_PAGES (imported from detail.py).
  - If a page fetch fails, preserves everything scraped so far and stops.

Used by:
  strategies/listing.py  — Strategy 1 (public listing pages, HTTP-fetched)

NOT used by:
  strategies/playwright_catalog.py — Strategy 3 has its own Playwright-based
  pagination engine because it needs click-based SPA navigation and DOM access
  that is not available from raw HTML.
"""

import logging
import math
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from scraper import fetch_html
from strategies.detail import (
    _find_next_page,
    _try_next_page_by_url,
    _stabilise_shopify_sort,
    MAX_LISTING_PAGES,
)


def _fetch_with_playwright_fallback(url: str) -> str:
    """
    Try regular HTTP fetch first; on failure (403, timeout, etc.) fall back
    to Playwright JS rendering.  This ensures pagination works on sites like
    Betty Mills that block raw HTTP requests but serve content to real browsers.
    """
    try:
        html = fetch_html(url)
        # Quick sanity check — if the page looks blocked, try Playwright
        lower = html.lower()
        if any(phrase in lower for phrase in [
            "access denied", "403 forbidden", "captcha",
            "checking your browser", "please enable javascript",
        ]):
            raise RuntimeError("Page appears blocked")
        return html
    except Exception as e:
        logger.info(
            f"[Pagination] Regular fetch failed ({e}) — trying Playwright"
        )
        try:
            from scraper import fetch_html_playwright
            return fetch_html_playwright(url, wait_ms=4000)
        except Exception as pw_err:
            logger.warning(
                f"[Pagination] Playwright fallback also failed: {pw_err}"
            )
            raise

logger = logging.getLogger(__name__)

# ── Pagination-detection regexes ──────────────────────────────────────────────

# Matches: "Items 1-100 of 1240", "Showing 1-24 of 53 results", "1 to 24 of 53"
_RESULT_COUNT_RE = re.compile(
    r"(?:items?\s+|showing\s+)?"
    r"([\d,]+)"               # range start  e.g. "1"
    r"\s*[–\-\u2013to]+\s*"  # separator: –, -, to
    r"([\d,]+)"               # range end    e.g. "100"
    r"\s+of\s+"
    r"([\d,]+)",              # total        e.g. "1240"
    re.IGNORECASE,
)

# Matches: "Page 1 of 3"
_PAGE_OF_TOTAL_RE = re.compile(r"page\s+\d+\s+of\s+(\d+)", re.IGNORECASE)

# Class/id fragments that indicate a pagination container
_PAGINATION_CLASS_SIGNALS = ["pagination", "pager", "pages", "page-nav", "paginate"]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _detect_max_page_num_html(soup: BeautifulSoup) -> int | None:
    """
    Scan the parsed HTML for numbered pagination buttons and return the highest
    page number found.

    Requires at least 2 numeric buttons inside a recognised pagination container
    before trusting the result — a single stray number elsewhere on the page
    should not trigger this.

    Returns the max page number (int) or None if not detected.
    """
    container = None
    for el in soup.find_all(["nav", "div", "ul", "ol"]):
        classes = " ".join(el.get("class") or []).lower()
        if any(sig in classes for sig in _PAGINATION_CLASS_SIGNALS):
            container = el
            break
        aria = (el.get("aria-label") or "").lower()
        if "pag" in aria:
            container = el
            break

    if container is None:
        return None

    nums = []
    for el in container.find_all(["a", "button", "li", "span"]):
        text = el.get_text(strip=True)
        if re.fullmatch(r"\d+", text):
            n = int(text)
            if 1 <= n <= 999:
                nums.append(n)

    return max(nums) if len(nums) >= 2 else None


def _detect_pagination_info(soup: BeautifulSoup) -> dict:
    """
    Detect total product count, per-page count, and total pages from a parsed
    listing page, combining two independent signals:

    Signal A — result-count text ("Items 1-100 of 1240"):
        Calculates total_pages = ceil(total / per_page).

    Signal B — numbered page buttons inside a pagination container:
        Reads the highest numeric button.

    If both signals disagree, the larger value is used (safer — one extra
    empty page is better than missing pages).

    Returns dict with keys: total_products, per_page, total_pages (int|None).
    """
    info: dict = {"total_products": None, "per_page": None, "total_pages": None}

    # Signal A: result-count text
    page_text = soup.get_text(separator=" ")

    m = _RESULT_COUNT_RE.search(page_text)
    if m:
        start  = int(m.group(1).replace(",", ""))
        end    = int(m.group(2).replace(",", ""))
        total  = int(m.group(3).replace(",", ""))
        per_pg = end - start + 1
        if per_pg > 0 and total > 0:
            info["total_products"] = total
            info["per_page"]       = per_pg
            info["total_pages"]    = math.ceil(total / per_pg)
            logger.info(
                f"[Pagination] Result-count text: "
                f"{start}-{end} of {total} -> {info['total_pages']} page(s)"
            )

    if info["total_pages"] is None:
        m2 = _PAGE_OF_TOTAL_RE.search(page_text)
        if m2:
            info["total_pages"] = int(m2.group(1))
            logger.info(
                f"[Pagination] 'Page X of N' indicator: "
                f"{info['total_pages']} page(s)"
            )

    # Signal A2: Simple "N items/products/results" without range
    # (e.g. "788 items" on Betty Mills).  We store total_products so it can
    # be combined with the actual per-page count after page 1 extraction.
    if info["total_products"] is None:
        simple_count = re.search(
            r"([\d,]+)\s+(?:items?|products?|results?)\b",
            page_text, re.IGNORECASE,
        )
        if simple_count:
            total = int(simple_count.group(1).replace(",", ""))
            if total >= 2:
                info["total_products"] = total
                logger.info(
                    f"[Pagination] Simple item-count text: {total} item(s)"
                )

    # Signal B: numbered page buttons
    if info["total_pages"] is None:
        max_btn = _detect_max_page_num_html(soup)
        if max_btn is not None:
            logger.info(
                f"[Pagination] Numbered page buttons: max page = {max_btn}"
            )
            info["total_pages"] = max_btn
    else:
        # Both signals present — use the larger
        max_btn = _detect_max_page_num_html(soup)
        if max_btn is not None and max_btn > info["total_pages"]:
            logger.info(
                f"[Pagination] Numbered buttons ({max_btn}) > result-count "
                f"estimate ({info['total_pages']}) — using {max_btn}"
            )
            info["total_pages"] = max_btn

    return info


def dedup_products(products: list) -> tuple:
    """
    Remove duplicate product rows using a 3-tier priority key.

    Priority 1 — product_url (exact match):
        Most reliable — same URL = same product.
    Priority 2 — sku (exact match, non-empty):
        Catches same product appearing on multiple pages with different URLs.
    Priority 3 — normalised product_name + brand + price_digits:
        Catches duplicates without URLs or SKUs.

    Returns (deduped_list, removed_count).
    """
    seen_urls     : set = set()
    seen_skus     : set = set()
    seen_identity : set = set()
    deduped       : list = []
    removed       : int = 0

    for p in products:
        url     = (p.get("product_url") or "").strip()
        sku     = (p.get("sku") or "").strip()
        name    = (p.get("product_name") or "").lower().strip()
        brand   = (p.get("brand") or "").lower().strip()
        price_d = re.sub(r"[^\d.]", "", p.get("price") or "")

        is_dup = False

        if url:
            if url in seen_urls:
                is_dup = True
            else:
                seen_urls.add(url)

        if not is_dup and sku:
            if sku in seen_skus:
                is_dup = True
            else:
                seen_skus.add(sku)

        if not is_dup and name:
            identity = f"{name}|{brand}|{price_d}"
            if identity in seen_identity:
                is_dup = True
            else:
                seen_identity.add(identity)

        if is_dup:
            removed += 1
        else:
            deduped.append(p)

    if removed:
        logger.info(
            f"[Pagination] Deduplication: {removed} duplicate(s) removed, "
            f"{len(deduped)} unique product(s) remaining"
        )
    return deduped, removed


# ── Public API ────────────────────────────────────────────────────────────────

def crawl_listing_pages(
    start_html: str,
    start_url: str,
    extract_fn,
    fetch_fn=None,
    use_playwright: bool = False,
) -> list:
    """
    Traverse all pagination pages for a listing/catalog URL and return a
    deduplicated list of all products found.

    Args:
        start_html:  Already-fetched HTML of the first page.  The caller is
                     responsible for the initial fetch so that network errors
                     can be surfaced before the scrape job is created.
        start_url:   URL of the first page.
        extract_fn:  Callable(html: str, url: str) -> list[dict].
                     Called once per listing page.  Should never raise —
                     exceptions are caught and logged with the page preserved.
        fetch_fn:    Optional Callable(url: str) -> str.
                     Defaults to _fetch_with_playwright_fallback (tries HTTP
                     first, then Playwright on failure).  Pass a session-aware
                     fetch function for login-required supplier portals.
        use_playwright: If True, use Playwright directly for all page fetches
                     instead of trying HTTP first.  Set when the initial page
                     was fetched with Playwright (indicating the site blocks
                     regular HTTP requests).

    Returns:
        Deduplicated list of product dicts from all pages combined.
        Never raises — all errors are caught and logged.
    """
    if fetch_fn is None:
        if use_playwright:
            try:
                from scraper import fetch_html_playwright
                fetch_fn = lambda url: fetch_html_playwright(url, wait_ms=4000)
                logger.info("[Pagination] Using Playwright for all page fetches")
            except ImportError:
                fetch_fn = _fetch_with_playwright_fallback
        else:
            fetch_fn = _fetch_with_playwright_fallback

    # ── Shopify sort stabilisation ────────────────────────────────────────────
    stable_url = _stabilise_shopify_sort(start_html, start_url)
    if stable_url != start_url:
        logger.info(
            "[Pagination] Shopify store detected — switching to "
            "sort_by=title-ascending for complete catalog coverage."
        )
        try:
            start_html = fetch_fn(stable_url)
            start_url  = stable_url
            logger.info(f"[Pagination] Re-fetched page 1 with stable sort: {stable_url}")
        except Exception as e:
            logger.warning(
                f"[Pagination] Could not re-fetch with stable sort ({e}) — "
                f"continuing with original URL (catalog may be incomplete)"
            )

    base_netloc   = urlparse(start_url).netloc
    visited_pages = {start_url}
    all_products  = []
    current_html  = start_html
    current_url   = start_url
    page_num      = 0
    total_pages   = None   # filled in after page 1

    while page_num < MAX_LISTING_PAGES:
        page_num += 1

        page_label = (
            f"Page {page_num} of {total_pages}"
            if total_pages else f"Page {page_num}"
        )
        logger.info(f"[Pagination] === {page_label}: {current_url} ===")

        # ── Parse HTML once per page — used for extraction, pagination, next-URL ─
        try:
            soup = BeautifulSoup(current_html, "html.parser")
        except Exception as e:
            logger.warning(f"[Pagination] {page_label}: HTML parse failed ({e}) — stopping")
            break

        # ── Extract products from this page ───────────────────────────────────
        try:
            page_products = extract_fn(current_html, current_url)
        except Exception as e:
            logger.warning(
                f"[Pagination] {page_label}: extraction error ({e}) — "
                f"preserving {len(all_products)} product(s) from prior pages"
            )
            break

        logger.info(
            f"[Pagination] {page_label}: "
            f"{len(page_products)} product(s) extracted"
        )
        all_products.extend(page_products)

        # ── Detect pagination info on page 1 only ─────────────────────────────
        if page_num == 1:
            try:
                pag_info    = _detect_pagination_info(soup)
                total_pages = pag_info["total_pages"]

                # If we have total_products but not total_pages (e.g. "788 items"
                # without a range), estimate from page 1 product count.
                if total_pages is None and pag_info.get("total_products") and len(page_products) > 0:
                    per_page_est = len(page_products)
                    total_pages = math.ceil(pag_info["total_products"] / per_page_est)
                    pag_info["per_page"] = per_page_est
                    pag_info["total_pages"] = total_pages
                    logger.info(
                        f"[Pagination] Estimated {total_pages} page(s) from "
                        f"{pag_info['total_products']} total items / "
                        f"{per_page_est} per page"
                    )

                if pag_info.get("total_products"):
                    logger.info(
                        f"[Pagination] Total visible item count: "
                        f"{pag_info['total_products']} "
                        f"({pag_info.get('per_page', '?')} per page)"
                    )
                if total_pages:
                    logger.info(
                        f"[Pagination] {total_pages} page(s) detected — "
                        f"will traverse all of them"
                    )
                else:
                    logger.info(
                        "[Pagination] Total page count not determinable — "
                        "will follow 'Next' links until none remain"
                    )
            except Exception as e:
                logger.debug(f"[Pagination] Pagination-info detection error: {e}")

        # ── Find next page URL ─────────────────────────────────────────────────
        # Note: total_pages is informational — we do NOT stop here just because
        # page_num >= total_pages.  We let next-link detection drive stopping so
        # a slightly off page-count estimate never cuts the traversal short.
        try:
            next_url = _find_next_page(soup, current_url, visited_pages, base_netloc)
        except Exception as e:
            logger.warning(f"[Pagination] Next-page detection error: {e}")
            next_url = None

        # ── Fallback: URL-based page increment when next link disappears ───────
        if not next_url and total_pages and page_num < total_pages:
            candidate = _try_next_page_by_url(current_url, page_num + 1)
            if candidate and candidate not in visited_pages:
                logger.info(
                    f"[Pagination] No 'Next' link on page {page_num} but "
                    f"only {page_num}/{total_pages} pages visited — "
                    f"trying URL increment: {candidate}"
                )
                next_url = candidate

        # ── Stop conditions ────────────────────────────────────────────────────
        if not next_url:
            if total_pages and page_num < total_pages:
                logger.warning(
                    f"[Pagination] Stopped at page {page_num}/{total_pages} — "
                    f"no 'Next' link and URL increment unavailable. "
                    f"May have missed {total_pages - page_num} page(s)."
                )
            else:
                reason = (
                    f"all {total_pages} expected page(s) visited"
                    if total_pages
                    else "no further pages detected"
                )
                logger.info(f"[Pagination] No further pages after page {page_num} — {reason}")
            break

        if page_num >= MAX_LISTING_PAGES:
            logger.warning(
                f"[Pagination] Safety cap of {MAX_LISTING_PAGES} pages reached — stopping"
            )
            break

        logger.info(f"[Pagination] Following pagination -> {next_url}")
        visited_pages.add(next_url)

        # ── Fetch next page ────────────────────────────────────────────────────
        try:
            current_html = fetch_fn(next_url)
            current_url  = next_url
        except Exception as e:
            logger.warning(
                f"[Pagination] Failed to fetch page {page_num + 1} "
                f"({next_url}): {e} — "
                f"stopping, preserving {len(all_products)} product(s) from "
                f"{page_num} page(s) already scraped"
            )
            break

    # ── Summary and dedup ──────────────────────────────────────────────────────
    raw_count        = len(all_products)
    deduped, removed = dedup_products(all_products)

    # Completeness assessment
    if total_pages:
        complete = page_num >= total_pages
        status = "COMPLETE" if complete else f"POSSIBLY INCOMPLETE ({page_num}/{total_pages} pages)"
    else:
        status = "done (page count unknown)"

    pag_info_ref = locals().get("pag_info") or {}
    expected_total = pag_info_ref.get("total_products")
    if expected_total:
        pct = len(deduped) / expected_total * 100
        logger.info(
            f"[Pagination] Complete: "
            f"{page_num} page(s) visited | "
            f"{raw_count} raw | {removed} dupes removed | "
            f"{len(deduped)} unique | "
            f"expected ~{expected_total} ({pct:.0f}% of catalog) | "
            f"{status}"
        )
    else:
        logger.info(
            f"[Pagination] Complete: "
            f"{page_num} page(s) visited | "
            f"{raw_count} raw product(s) | "
            f"{removed} duplicate(s) removed | "
            f"{len(deduped)} unique product(s) returned | "
            f"{status}"
        )
    return deduped
