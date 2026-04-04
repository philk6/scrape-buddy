"""
strategies/detail.py — Strategy 2: Detail Page Crawl

How it works:
  1. Crawls ALL listing/category pages by following pagination (rel="next",
     "Next" button text, aria-label) until no further pages are found.
  2. On each listing page, collects <a> links whose href contains /products/.
     - Tries a context-aware pass first (links inside product card elements).
     - Falls back to URL-pattern-only if the context pass returns nothing.
  3. Deduplicates all collected product URLs across all pages.
  4. Visits each product detail page and extracts rich fields using JSON-LD,
     Open Graph meta tags, itemprop attributes, and label→value text patterns.

Crash safety:
  - Every .get() on a BeautifulSoup Tag uses `tag is not None` not `if tag`
    (empty self-closing tags like <meta> evaluate as falsy in BS4).
  - ld_offers is always coerced to a dict before .get() is called.
  - All per-product extraction is wrapped in try/except so one bad page
    cannot crash the whole run.
"""

import json
import logging
import math
import os
import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

from scraper import fetch_html
from strategies.target_selector import (
    Candidate,
    discover_candidates,
    validate_rich_detail,
    select_best_target,
    CANDIDATE_MIN_SCORE,
)
from strategies.row_extractor import extract_row_links as _row_extractor_links

logger = logging.getLogger(__name__)

# Strategy metadata
ID = 2
NAME = "Detail Page Crawl"


def _runtime_limits() -> tuple[int, int]:
    """Return detail/listing limits, with a lighter benchmark mode for validation runs."""
    benchmark_mode = os.getenv("SCRAPEBUDDY_BENCHMARK_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
    if benchmark_mode:
        return BENCHMARK_MAX_DETAIL_PAGES, BENCHMARK_MAX_LISTING_PAGES
    return MAX_DETAIL_PAGES, MAX_LISTING_PAGES

# Hard safety cap: max product detail pages to visit in one run.
# Production/full runs can go high, but benchmark/test mode should stay lean.
MAX_DETAIL_PAGES = 2000
BENCHMARK_MAX_DETAIL_PAGES = 20

# Hard safety cap: max listing/category pages to paginate through.
MAX_LISTING_PAGES = 100
BENCHMARK_MAX_LISTING_PAGES = 8

# ── Link collection rules ─────────────────────────────────────────────────────

# Product URL path signals — if href contains any of these, it's a STRONG
# candidate (no context check needed). Covers Shopify, WooCommerce, Magento,
# BigCommerce, custom B2B platforms, and more.
PRODUCT_PATH_SIGNALS = [
    "/products/", "/product/", "/item/", "/p/", "/pd/",
    "/dp/",           # Amazon-style
    "/catalog/",      # B2B wholesale
    "/shop/",         # WooCommerce
    "/buy/",          # various
    "product_detail", "productdetail", "product-detail",
    "itemdetail", "item-detail", "item_detail",
]

# href containing any of these is immediately rejected (before context check)
REJECT_PATH_FRAGMENTS = [
    "/collections/", "/collection/", "/categories/", "/category/",
    "/pages/", "/blogs/", "/blog/", "/search", "/tag/", "/tags/",
    "/cart", "/checkout", "/account", "/login", "/register",
    "/wishlist", "/compare", "/review", "/contact",
    "/about", "/faq", "/help", "/policy", "/terms", "/privacy",
    "/sitemap", "/feed", "/rss", "/api/",
    "javascript:", "mailto:", "tel:", "#",
]

# Semantic HTML tag names that are always navigation chrome — safe to strip
NAV_SEMANTIC_TAGS = ["nav", "header", "footer", "aside"]

# Element id values that reliably identify non-product regions
NAV_IDS = {
    "nav", "navigation", "main-nav", "site-nav", "top-nav",
    "header", "site-header", "page-header",
    "footer", "site-footer",
    "sidebar", "side-bar",
    "breadcrumb", "breadcrumbs",
    "filters", "facets",
    "announcement-bar", "cookie-bar",
}

# Ancestor class signals that indicate a product card context
CARD_CLASS_SIGNALS = [
    "product", "item", "card", "tile", "grid-item", "listing",
    "result", "catalogue",
]

# ── Structured field label sets (all lowercase, hyphens normalised to spaces) ──
# Used by _find_label_value().  The normaliser strips hyphens before comparison,
# so "ITEM-NO" on a page will match the keyword "item no" here.

_CASE_PACK_LABELS = [
    "master case", "master pack", "case pack", "case qty", "case quantity",
    "units per case", "qty per case", "quantity per case", "pcs per case",
    "pieces per case", "inner pack", "cases per pallet",
]

_UNIT_SIZE_LABELS = [
    "unit size", "each size", "item size", "size per unit",
    "net weight", "net content", "net contents", "net volume",
    "unit of measure", "uom",
]

_ITEM_NO_LABELS = [
    "item no", "item number", "item #", "item#", "item code",
    "sku", "part number", "part #", "part no", "model number", "model #",
    "product code", "product number", "catalog number", "catalog #",
    "vendor part #", "vendor item #",
]

_UNIT_PRICE_LABELS = [
    "unit price", "price per unit", "price each", "price ea", "each price",
    "list price", "retail price", "your price", "sale price",
]

_MIN_ORDER_LABELS = [
    "minimum order", "min order", "minimum order qty", "min order qty",
    "minimum quantity", "min quantity", "min qty", "minimum qty",
    "order minimum", "order min",
]

_BULK_PRICE_LABELS = [
    "bulk price", "case price", "wholesale price", "price per case",
    "case rate", "volume price",
]

# Price tier pattern: "1 EA = $37.50 EA"  or  "1 EA @ $37.50"
_PRICE_TIER_RE = re.compile(
    r"\b1\s*(?:ea|each|unit|pc|pcs)?\b\s*[@=]\s*\$?\s*([\d,]+\.\d{2})"
    r"(?:\s*(ea|each|unit|pc|pcs|lb|oz|kg|g))?",
    re.IGNORECASE,
)

# Standalone price: "$37.50" optionally followed by a unit
_PRICE_RE = re.compile(
    r"\$\s*([\d,]+\.\d{2})"
    r"(?:\s*(?:per\s+)?(ea|each|unit|pc|pcs|lb|oz|kg|g|cs|case))?",
    re.IGNORECASE,
)


# ── Product container detection ───────────────────────────────────────────────

# Class/id signals that identify repeated product card/row containers
_CONTAINER_CLASS_SIGNALS = [
    "product", "item", "card", "tile", "grid-item", "listing-item",
    "result", "catalogue", "prod-card", "product-card", "product-item",
]

# Minimum number of repeated container elements to trust as product cards
_CONTAINER_MIN_COUNT = 2


def _find_product_containers(soup: BeautifulSoup) -> list:
    """
    Detect repeated product card/row container elements on the page.

    Uses a Counter-style approach: finds elements whose class contains a
    product-card signal, groups by tag+class signature, and returns the
    group with the most members (if at least _CONTAINER_MIN_COUNT found).

    Returns a list of BeautifulSoup Tag objects (the containers), or [].
    """
    from collections import Counter
    signature_map: dict = {}

    for el in soup.find_all(True):
        try:
            classes = el.get("class") or []
            classes_str = " ".join(classes).lower()
            if not any(sig in classes_str for sig in _CONTAINER_CLASS_SIGNALS):
                continue
            # Build a signature from tag name + sorted class list
            sig = el.name + "|" + "|".join(sorted(classes))
            if sig not in signature_map:
                signature_map[sig] = []
            signature_map[sig].append(el)
        except Exception:
            continue

    if not signature_map:
        return []

    # Find the signature with the most instances
    best_sig = max(signature_map, key=lambda s: len(signature_map[s]))
    candidates = signature_map[best_sig]

    if len(candidates) < _CONTAINER_MIN_COUNT:
        return []

    logger.info(
        f"[Strategy 2] Container detection: "
        f"found {len(candidates)} x '{best_sig.split('|')[0]}."
        f"{best_sig.split('|')[1]}' elements"
    )
    return candidates


# ── Product deduplication ─────────────────────────────────────────────────────

def _normalise_name(name: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation for name comparison."""
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


def _deduplicate_products(products: list) -> list:
    """
    Remove duplicate products using a three-key strategy:
      1. product_url (exact match)
      2. sku (exact match, non-empty)
      3. normalised name + brand combination

    Preserves the first occurrence of each unique product.
    Logs how many duplicates were removed.
    """
    seen_urls: set = set()
    seen_skus: set = set()
    seen_name_brand: set = set()
    result = []

    for p in products:
        url = (p.get("product_url") or "").strip()
        sku = (p.get("sku") or "").strip()
        name = _normalise_name(p.get("product_name") or "")
        brand = _normalise_name(p.get("brand") or "")
        name_brand = f"{name}::{brand}"

        if url and url in seen_urls:
            continue
        if sku and sku in seen_skus:
            continue
        if name and name_brand in seen_name_brand:
            continue

        if url:
            seen_urls.add(url)
        if sku:
            seen_skus.add(sku)
        if name:
            seen_name_brand.add(name_brand)

        result.append(p)

    removed = len(products) - len(result)
    if removed:
        logger.info(f"[Strategy 2] Deduplication removed {removed} duplicate product(s)")
    return result


# ── Noise-region stripping ────────────────────────────────────────────────────

def _strip_non_grid_regions(soup: BeautifulSoup) -> None:
    """
    Remove semantic nav/header/footer/aside tags and known non-product regions
    from soup IN PLACE. Only strips by exact id match — never by partial class
    name, to avoid accidentally removing product card headers or nav-style
    class names inside the product grid.
    """
    removed = 0

    # 1. Semantic tags that are always nav chrome
    for tag_name in NAV_SEMANTIC_TAGS:
        for el in soup.find_all(tag_name):
            el.decompose()
            removed += 1

    # 2. Exact id match only (no partial class matching — too many false positives)
    for el in soup.find_all(id=True):
        if el.get("id", "").lower().strip() in NAV_IDS:
            try:
                el.decompose()
                removed += 1
            except Exception:
                pass  # element may already be decomposed

    logger.info(f"[Strategy 2] Stripped {removed} non-grid regions from page")


# ── Link collection ───────────────────────────────────────────────────────────

def _is_in_product_card_context(a_tag) -> bool:
    """
    Return True if the <a> tag is plausibly inside a product card.

    Checks (in order):
      1. The <a> itself wraps an <img> (common image-link pattern).
      2. Any ancestor within 10 levels has a class suggesting a product card.
      3. Any ancestor within 10 levels contains an <img> as a descendant.
    """
    # Pattern 1: the link itself wraps an image
    try:
        if a_tag.find("img"):
            return True
    except Exception:
        pass

    # Patterns 2 & 3: walk up ancestor chain
    node = a_tag.parent
    for _ in range(10):
        if node is None or getattr(node, "name", None) in (None, "body", "html", "[document]"):
            break
        try:
            classes = " ".join(node.get("class") or []).lower()
            if any(sig in classes for sig in CARD_CLASS_SIGNALS):
                return True
            if node.find("img"):
                return True
        except Exception:
            pass
        node = node.parent

    return False


def _collect_links_from_soup(soup: BeautifulSoup, base_url: str, base_netloc: str,
                              require_context: bool,
                              require_path_signal: bool = True) -> tuple[list, dict]:
    """
    Scan soup for product detail links, applying URL rules and optionally a
    product-card context check.

    Args:
        require_path_signal: If True, links must contain a known product path
            signal (e.g. /products/, /item/, /p/). If False, accepts any
            same-domain link that passes rejection filters and context checks.
            This enables discovery on sites with non-standard URL patterns.

    Returns (accepted_urls, stats_dict).
    """
    seen = set()
    accepted = []
    stats = {
        "collections_rejected": 0,
        "other_rejected":       0,
        "context_rejected":     0,
        "no_signal_rejected":   0,
    }

    for a in soup.find_all("a", href=True):
        try:
            href = (a.get("href") or "").strip()
            if not href:
                continue

            absolute = urljoin(base_url, href)
            abs_lower = absolute.lower()

            # Same-domain only
            if urlparse(absolute).netloc != base_netloc:
                continue

            # Deduplicate
            if absolute in seen:
                continue

            # Reject known non-product fragments
            if any(frag in abs_lower for frag in REJECT_PATH_FRAGMENTS):
                stats["other_rejected"] += 1
                continue

            # If requiring path signal, check for known product URL patterns
            if require_path_signal:
                if not any(sig in abs_lower for sig in PRODUCT_PATH_SIGNALS):
                    stats["no_signal_rejected"] += 1
                    continue

            # Optional context check (is this link inside a product card?)
            if require_context and not _is_in_product_card_context(a):
                stats["context_rejected"] += 1
                continue

            seen.add(absolute)
            accepted.append(absolute)

        except Exception as e:
            logger.debug(f"[Strategy 2] Link scan error: {e}")
            continue

    return accepted, stats


def count_product_links(html: str, base_url: str) -> int:
    """
    Quick count of product detail links on the page (no context check, no stripping).
    Uses the full set of product path signals (not just /products/).
    Used by the router to decide whether to prefer Strategy 2.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        base_netloc = urlparse(base_url).netloc
        links, _ = _collect_links_from_soup(soup, base_url, base_netloc,
                                             require_context=False,
                                             require_path_signal=True)
        return len(links)
    except Exception as e:
        logger.warning(f"[Strategy 2] count_product_links error: {e}")
        return 0


def _collect_product_links(html: str, base_url: str) -> list:
    """
    Collect real product detail links from the listing page.

    Pass 0 — container-aware + link scorer.
      Uses _find_product_containers() to locate repeated card elements, then
      applies the link scorer to pick the best link from each card. This is
      the strongest signal when card markup is present.

    Pass 1 — strip nav regions, apply URL rules + product-card context check.
      Falls back to this when Pass 0 finds fewer than 2 links.

    Pass 2 — full page, URL rules only (no context check).
      Used when Pass 1 also finds nothing (unusual grid markup).

    Always logs a clear breakdown of accepted vs rejected links.
    """
    base_netloc = urlparse(base_url).netloc

    # ── Pass -1: Structured row/table layout (highest priority) ──────────────
    # Uses explicit labeled columns (IMAGE, DESCRIPTION, ITEM, PRICE) to
    # select the correct product-detail link from each row. Bypasses the
    # /products/ URL requirement — works for non-Shopify B2B wholesale sites.
    try:
        soup_early = BeautifulSoup(html, "html.parser")
        row_links = _row_extractor_links(soup_early, base_url)
        if row_links:
            logger.info(
                f"[Strategy 2] Pass -1 (structured row): "
                f"{len(row_links)} (primary, alts) tuple(s) — "
                f"using explicit column structure"
            )
            return row_links
        logger.info("[Strategy 2] Pass -1: no structured layout detected — continuing to Pass 0")
    except Exception as e:
        logger.warning(f"[Strategy 2] Pass -1 error (non-fatal): {e}")

    # ── Pass 0: container-aware target selection (scoring only, no extra fetches) ──
    soup0 = BeautifulSoup(html, "html.parser")
    _strip_non_grid_regions(soup0)
    containers = _find_product_containers(soup0)

    links0 = []
    if containers:
        seen0: set = set()
        for container in containers:
            candidates = discover_candidates(container, base_url)
            # Pick the best-scored candidate that passes URL rules
            for c in candidates:
                if c.score < CANDIDATE_MIN_SCORE:
                    break  # sorted descending — no point continuing
                url = c.url
                if url in seen0:
                    continue
                if (not any(frag in url.lower() for frag in REJECT_PATH_FRAGMENTS)
                        and urlparse(url).netloc == base_netloc):
                    seen0.add(url)
                    links0.append(url)
                    break  # one primary per container

        logger.info(
            f"[Strategy 2] Pass 0 (container+target-scorer): "
            f"{len(containers)} containers → {len(links0)} product link(s)"
        )

        if len(links0) >= 2:
            logger.info(f"[Strategy 2] Pass 0 accepted {len(links0)} product link(s)")
            return links0

        logger.info("[Strategy 2] Pass 0 found < 2 links — trying Pass 1")

    # ── Pass 1: stripped soup + product path signals + context check ─────────
    soup1 = BeautifulSoup(html, "html.parser")
    _strip_non_grid_regions(soup1)
    links1, stats1 = _collect_links_from_soup(soup1, base_url, base_netloc,
                                               require_context=True,
                                               require_path_signal=True)

    logger.info(
        f"[Strategy 2] Pass 1 (signal+context): {len(links1)} accepted | "
        f"{stats1['other_rejected']} rejected | "
        f"{stats1['context_rejected']} context-rejected | "
        f"{stats1['no_signal_rejected']} no-signal-rejected"
    )

    if links1:
        logger.info(f"[Strategy 2] Pass 1 accepted {len(links1)} product link(s) from this page")
        return links1

    # ── Pass 2: full page, product path signals only (no context check) ─────
    soup2 = BeautifulSoup(html, "html.parser")
    links2, stats2 = _collect_links_from_soup(soup2, base_url, base_netloc,
                                               require_context=False,
                                               require_path_signal=True)

    logger.info(
        f"[Strategy 2] Pass 2 (signal-only): {len(links2)} accepted | "
        f"{stats2['other_rejected']} rejected"
    )

    if links2:
        return links2

    # ── Pass 3: context-only (no path signal required) ──────────────────────
    # For sites with completely custom URL schemes (e.g. /sku/12345, /view?id=99)
    # Accept any same-domain link that's inside a product card context.
    logger.info(
        "[Strategy 2] Pass 2 returned 0 links — "
        "trying Pass 3: context-only (no path signal required)"
    )
    soup3 = BeautifulSoup(html, "html.parser")
    _strip_non_grid_regions(soup3)
    links3, stats3 = _collect_links_from_soup(soup3, base_url, base_netloc,
                                               require_context=True,
                                               require_path_signal=False)

    logger.info(
        f"[Strategy 2] Pass 3 (context-only): {len(links3)} accepted | "
        f"{stats3['other_rejected']} rejected | "
        f"{stats3['context_rejected']} context-rejected"
    )
    return links3


def _collect_product_links_with_alternatives(html: str, base_url: str) -> list[tuple[str, list[str]]]:
    """
    Like _collect_product_links(), but returns (primary_url, [alternative_urls])
    tuples so the detail-scraping phase can fall back to alternatives when the
    primary URL doesn't yield rich product data.

    For containers detected in Pass 0, alternatives are the lower-ranked
    candidates from the same container.  Passes 1 and 2 return empty
    alternative lists (no container context available).

    Return format: [(primary_url, [alt1, alt2, ...]), ...]
    """
    base_netloc = urlparse(base_url).netloc

    # ── Pass -1: Structured row/table layout (highest priority) ──────────────
    # Uses explicit labeled columns (IMAGE, DESCRIPTION, ITEM, PRICE) to select
    # the correct product-detail link from each row.  Bypasses the /products/
    # URL requirement so it works for non-Shopify B2B wholesale catalogs.
    try:
        soup_early = BeautifulSoup(html, "html.parser")
        row_links = _row_extractor_links(soup_early, base_url)
        if row_links:
            logger.info(
                f"[Strategy 2] Pass -1 (structured row, with-alts): "
                f"{len(row_links)} (primary, alts) tuple(s) — "
                f"using explicit column structure"
            )
            return row_links
        logger.info(
            "[Strategy 2] Pass -1 (with-alts): no structured layout detected "
            "— continuing to Pass 0"
        )
    except Exception as e:
        logger.warning(f"[Strategy 2] Pass -1 (with-alts) error (non-fatal): {e}")

    # ── Pass 0: container-aware target selection ──────────────────────────────
    soup0 = BeautifulSoup(html, "html.parser")
    _strip_non_grid_regions(soup0)
    containers = _find_product_containers(soup0)

    results: list[tuple[str, list[str]]] = []

    if containers:
        seen_primary: set = set()
        for container in containers:
            candidates = discover_candidates(container, base_url)
            # Separate viable candidates that pass URL rules
            valid_urls = []
            for c in candidates:
                if c.score < CANDIDATE_MIN_SCORE:
                    break  # sorted descending
                url = c.url
                if url in seen_primary:
                    continue
                if (not any(frag in url.lower() for frag in REJECT_PATH_FRAGMENTS)
                        and urlparse(url).netloc == base_netloc):
                    valid_urls.append(url)

            if not valid_urls:
                continue

            primary = valid_urls[0]
            alts = valid_urls[1:]
            seen_primary.add(primary)
            results.append((primary, alts))

        logger.info(
            f"[Strategy 2] Pass 0 (with-alts): "
            f"{len(containers)} containers → {len(results)} (primary, alts) tuples"
        )
        if len(results) >= 2:
            return results

        logger.info("[Strategy 2] Pass 0 found < 2 tuples — trying Pass 1")

    # ── Pass 1: stripped soup + signal + context check ───────────────────────
    soup1 = BeautifulSoup(html, "html.parser")
    _strip_non_grid_regions(soup1)
    links1, stats1 = _collect_links_from_soup(soup1, base_url, base_netloc,
                                               require_context=True,
                                               require_path_signal=True)

    logger.info(
        f"[Strategy 2] Pass 1 (signal+context): {len(links1)} accepted | "
        f"{stats1['other_rejected']} rejected | "
        f"{stats1['context_rejected']} context-rejected"
    )

    if links1:
        return [(url, []) for url in links1]

    # ── Pass 2: full page, signal only ───────────────────────────────────────
    soup2 = BeautifulSoup(html, "html.parser")
    links2, stats2 = _collect_links_from_soup(soup2, base_url, base_netloc,
                                               require_context=False,
                                               require_path_signal=True)

    logger.info(
        f"[Strategy 2] Pass 2 (signal-only): {len(links2)} accepted | "
        f"{stats2['other_rejected']} rejected"
    )

    if links2:
        return [(url, []) for url in links2]

    # ── Pass 3: context-only (no path signal) ────────────────────────────────
    soup3 = BeautifulSoup(html, "html.parser")
    _strip_non_grid_regions(soup3)
    links3, stats3 = _collect_links_from_soup(soup3, base_url, base_netloc,
                                               require_context=True,
                                               require_path_signal=False)

    logger.info(
        f"[Strategy 2] Pass 3 (context-only): {len(links3)} accepted | "
        f"{stats3['other_rejected']} rejected | "
        f"{stats3['context_rejected']} context-rejected"
    )
    return [(url, []) for url in links3]


# ── Detail page extraction ────────────────────────────────────────────────────

def _extract_json_ld(soup: BeautifulSoup) -> dict:
    """
    Parse the first JSON-LD Product schema block on the page.
    Always returns a dict (empty if not found or unparseable).
    """
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            raw = json.loads(script.string or "")
            candidates = raw if isinstance(raw, list) else [raw]
            # Also unwrap @graph arrays
            expanded = []
            for item in candidates:
                if isinstance(item, dict):
                    if item.get("@type") == "Product":
                        return item
                    expanded.extend(item.get("@graph", []))
            for item in expanded:
                if isinstance(item, dict) and item.get("@type") == "Product":
                    return item
        except Exception:
            continue
    return {}


def _safe_offers(ld: dict) -> dict:
    """
    Extract the offers object from a JSON-LD Product dict and ensure it is
    always returned as a plain dict (never None, never a list, never a string).

    This is the primary crash source: JSON-LD can have "offers": null, or
    "offers": [...] where the first item is None or a non-dict.
    Using dict.get(key, default) only falls back for MISSING keys — if the
    key exists with value null, .get() returns None, not the default.
    """
    raw = ld.get("offers")          # may be None, dict, or list
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        # Take the first dict item in the list
        for item in raw:
            if isinstance(item, dict):
                return item
        return {}
    return {}  # string, number, etc. — discard


def _meta_content(soup: BeautifulSoup, attrs: dict) -> str:
    """
    Safely get the content attribute of a <meta> tag.

    IMPORTANT: BeautifulSoup self-closing tags (like <meta>) have no child
    elements, so len(tag) == 0, and `if tag` evaluates to False even when
    the tag exists. Always use `tag is not None` for existence checks.
    """
    try:
        tag = soup.find("meta", attrs)
        if tag is not None:                     # ← correct check for BS4 Tags
            content = tag.get("content")
            if content:
                return str(content).strip()
    except Exception:
        pass
    return ""


# ── UPC extraction ────────────────────────────────────────────────────────────

# Multi-word variants listed first so they match before their substrings do.
_UPC_LABEL_KEYWORDS = [
    "item upc", "product upc", "upc code", "upc/ean", "upc/ean code",
    "universal product code", "upc", "ean", "barcode", "gtin",
]

# Inline regex: matches "Item UPC: 00037000082170" in plain text.
# Allows up to 30 chars of whitespace/punctuation between label and digits.
_UPC_TEXT_RE = re.compile(
    r"(?:item\s+upc|product\s+upc|upc\s*code|upc\s*/\s*ean|"
    r"universal\s+product\s+code|upc|ean|barcode|gtin)"
    r"[:\s\-]{0,30}"
    r"(\d[\d\s]{6,15}\d)",   # 8–17 chars to allow internal spaces/hyphens
    re.IGNORECASE,
)


def _normalise_label(text: str) -> str:
    """Lowercase, collapse whitespace and strip punctuation for label comparison."""
    return re.sub(r"[:\.\s]+", " ", text.lower()).strip()


def _clean_detail_product_name(name: str) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"\s*\|\s*Nassau Candy\s*$", "", name, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -|")
    return cleaned


def _looks_like_valid_detail_sku(value: str) -> bool:
    if not value:
        return False
    value = value.strip()
    if not (2 <= len(value) <= 32):
        return False
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value, re.IGNORECASE):
        return False
    if not re.search(r"\d", value):
        return False
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-\./ ]*", value):
        return False
    return True


def _extract_nassau_case_gtin(soup: BeautifulSoup) -> str:
    raw = _find_label_value(soup, ["gtin case", "gtin (case)", "case gtin", "gtin case code"])
    digits = re.sub(r"\D", "", raw or "")
    return digits if re.fullmatch(r"\d{8,18}", digits) else ""


def _extract_nassau_sales_per_case(soup: BeautifulSoup) -> str:
    raw = _find_label_value(soup, ["sales per case", "units per case", "case qty", "case quantity"])
    if raw:
        m = re.search(r"\d+", raw)
        if m:
            return m.group(0)
    page_text = soup.get_text(" ", strip=True)
    m = re.search(r"sales\s+per\s+case\s*(\d+)", page_text, re.IGNORECASE)
    return m.group(1) if m else ""


def _extract_nassau_price(page_text: str) -> tuple[str, str, str]:
    if not page_text:
        return "", "", ""
    m = re.search(r"(\$\d[\d,]*\.\d{2})\s*/\s*(Each|EA|Case|CS)", page_text, re.IGNORECASE)
    if m:
        unit = m.group(2).upper()
        return m.group(1), m.group(1), unit
    m = re.search(r"(\$\d[\d,]*\.\d{2})", page_text)
    if m:
        return m.group(1), "", ""
    return "", "", ""


def _is_upc_label(text: str) -> bool:
    """Return True if the normalised text is or contains a UPC-related label."""
    norm = _normalise_label(text)
    return any(kw in norm for kw in _UPC_LABEL_KEYWORDS)


def _coerce_upc(raw: str) -> str:
    """
    Given a raw candidate string, strip whitespace/hyphens and validate as a
    UPC/EAN (8–14 digits). Returns digits-only string or "" if invalid.
    Leading zeros are preserved.
    """
    if not raw:
        return ""
    digits = re.sub(r"[\s\-]", "", raw.strip())
    if re.fullmatch(r"\d{8,14}", digits):
        return digits
    return ""


def _extract_upc(soup: BeautifulSoup) -> str:
    """
    Multi-strategy UPC extraction with debug logging.

    Strategy A — regex scan on full page text.
      Catches all inline "Label: Value" patterns in a single pass, including
      "Item UPC: 00037000082170" regardless of HTML structure.

    Strategy B — element-level label/value traversal.
      Scans every text node for a UPC label, then tries (in order):
        B1. colon-split within the same text node (label and value in one string)
        B2. colon-split within the parent element's full text
        B3. next sibling element  (validated — must look like a UPC)
        B4. grandparent's next sibling element (validated)
        B5. next text node in the document (validated)

    Returns a digits-only UPC string, or "" if nothing found.
    """
    # ── Strategy A: full-page text regex ─────────────────────────────────────
    try:
        # get_text with a space separator keeps inline label:value pairs on one line
        page_text = soup.get_text(separator=" ")
        m = _UPC_TEXT_RE.search(page_text)
        if m:
            raw = m.group(1)
            upc = _coerce_upc(raw)
            if upc:
                logger.info(
                    f"[Strategy 2] UPC via A (regex scan) | "
                    f"raw={raw!r} | upc={upc}"
                )
                return upc
    except Exception as e:
        logger.debug(f"[Strategy 2] UPC Strategy A error: {e}")

    # ── Strategy B: element-level traversal ───────────────────────────────────
    try:
        for el in soup.find_all(string=True):
            text = (el or "").strip()
            if not text or len(text) > 200:
                continue
            if not _is_upc_label(text):
                continue

            logger.debug(f"[Strategy 2] UPC: found label node {text!r}")
            parent = el.parent
            if parent is None:
                continue

            # B1: colon-split within the same text node
            if ":" in text:
                after = text.split(":", 1)[1].strip()
                upc = _coerce_upc(after.split()[0]) if after.split() else ""
                if not upc:
                    upc = _coerce_upc(after)
                if upc:
                    logger.info(
                        f"[Strategy 2] UPC via B1 (colon in text node) | "
                        f"label={text!r} | raw={after!r} | upc={upc}"
                    )
                    return upc

            # B2: colon-split within the parent element's full text
            parent_text = parent.get_text(separator=" ", strip=True)
            if ":" in parent_text:
                after = parent_text.split(":", 1)[1].strip()
                # Try first whitespace-delimited token first (most precise)
                first_token = after.split()[0] if after.split() else ""
                upc = _coerce_upc(first_token) or _coerce_upc(after)
                if upc:
                    logger.info(
                        f"[Strategy 2] UPC via B2 (colon in parent) | "
                        f"label={text!r} | raw={after!r} | upc={upc}"
                    )
                    return upc

            # B3: next sibling element — validated before accepting
            sibling = parent.find_next_sibling()
            if sibling is not None:
                val = sibling.get_text(strip=True)
                upc = _coerce_upc(val)
                if upc:
                    logger.info(
                        f"[Strategy 2] UPC via B3 (next sibling) | "
                        f"label={text!r} | raw={val!r} | upc={upc}"
                    )
                    return upc

            # B4: grandparent's next sibling — validated
            if parent.parent is not None:
                gp_sib = parent.parent.find_next_sibling()
                if gp_sib is not None:
                    val = gp_sib.get_text(strip=True)
                    upc = _coerce_upc(val)
                    if upc:
                        logger.info(
                            f"[Strategy 2] UPC via B4 (grandparent sibling) | "
                            f"label={text!r} | raw={val!r} | upc={upc}"
                        )
                        return upc

            # B5: next text node in the document — validated
            next_str = el.find_next(string=True)
            if next_str:
                val = next_str.strip()
                upc = _coerce_upc(val)
                if upc:
                    logger.info(
                        f"[Strategy 2] UPC via B5 (next text node) | "
                        f"label={text!r} | raw={val!r} | upc={upc}"
                    )
                    return upc

    except Exception as e:
        logger.debug(f"[Strategy 2] UPC Strategy B error: {e}")

    logger.info("[Strategy 2] UPC: no value found by any strategy")
    return ""


def _find_label_value(soup: BeautifulSoup, label_keywords: list) -> str:
    """
    Scan for a label→value pattern in product spec tables/definition lists.

    Handles:
      <td>UPC:</td><td>012345678901</td>
      <span class="label">Pack Size</span><span class="value">6</span>
      <dt>Case Pack</dt><dd>12</dd>
    """
    try:
        for el in soup.find_all(string=True):
            try:
                raw = (el or "").strip()
                if not raw or len(raw) > 200:
                    continue
                # Normalise: lowercase, collapse hyphens/underscores to space,
                # strip trailing colon/space so "ITEM-NO:" matches "item no"
                text = re.sub(r"[\-_]", " ", raw.lower()).rstrip(": ").strip()
                if not any(kw == text or kw in text for kw in label_keywords):
                    continue

                parent = el.parent
                if parent is None:
                    continue

                # Try adjacent sibling element
                sibling = parent.find_next_sibling()
                if sibling is not None:
                    val = sibling.get_text(strip=True)
                    if val and len(val) < 100:  # sanity cap
                        return val

                # Try colon-split within the same parent
                full = parent.get_text(separator=" ", strip=True)
                if ":" in full:
                    parts = full.split(":", 1)
                    if len(parts) == 2 and parts[1].strip():
                        return parts[1].strip()

            except Exception:
                continue
    except Exception:
        pass
    return ""


def _normalize_money_value(raw: str) -> str:
    if not raw:
        return ""
    m = re.search(r"\$\s*(\d[\d,]*(?:\.\d{2})?)", raw)
    if not m:
        return ""
    return f"${m.group(1).replace(',', '')}"


def _looks_like_broad_page_price(raw: str, unit_price: str = "", pricing_unit: str = "") -> bool:
    value = _normalize_money_value(raw)
    if not value:
        return False
    if value == "$1500.00" and unit_price:
        return True
    if pricing_unit and pricing_unit.upper() in {"EACH", "EA", "UNIT"} and value != unit_price:
        try:
            broad = float(value.replace("$", ""))
            unit = float((unit_price or "").replace("$", "")) if unit_price else 0.0
            if unit > 0 and broad >= unit * 5:
                return True
        except Exception:
            return False
    return False


def _extract_unit_price(soup: BeautifulSoup) -> tuple[str, str]:
    """
    Extract a clean unit price and pricing unit from the page.

    Strategy 1: price tier table pattern — "1 EA = $37.50 EA"
    Strategy 2: label→value for known unit-price labels — "Unit Price: $37.50"

    Returns (unit_price_str, pricing_unit_str) — both "" if not found.
    """
    try:
        page_text = soup.get_text(separator=" ")

        # Strategy 1: price tier pattern
        m = _PRICE_TIER_RE.search(page_text)
        if m:
            price_val   = m.group(1).replace(",", "")
            pricing_unit = (m.group(2) or "EA").upper()
            logger.info(f"[Detail] unit_price via tier pattern: ${price_val} / {pricing_unit}")
            return f"${price_val}", pricing_unit

        # Strategy 2: label→value
        raw = _find_label_value(soup, _UNIT_PRICE_LABELS)
        if raw:
            pm = _PRICE_RE.search(raw)
            if pm:
                price_val    = pm.group(1).replace(",", "")
                pricing_unit = (pm.group(2) or "").upper()
                logger.info(f"[Detail] unit_price via label: ${price_val} / {pricing_unit!r}")
                return f"${price_val}", pricing_unit
            return raw.strip(), ""

    except Exception as e:
        logger.debug(f"[Detail] _extract_unit_price error: {e}")

    return "", ""


def _extract_from_detail_page(html: str, url: str) -> dict:
    """
    Extract product fields from a single product detail page.

    Priority order per field:
      1. JSON-LD Product schema (richest, most structured)
      2. <meta> itemprop / Open Graph tags  (note: use `is not None` not `if tag`)
      3. CSS class heuristics on visible elements
      4. Label→value text scan (for supplier-specific fields like UPC, Pack Size)

    Always returns a complete dict with all fields (empty string for missing).
    Never raises — all extraction is wrapped in try/except.
    """
    soup = BeautifulSoup(html, "html.parser")
    ld = _extract_json_ld(soup)
    ld_offers = _safe_offers(ld)    # always a dict — crash-safe
    extracted = {}                  # tracks which fields were found (for logging)

    # ── product_name ──────────────────────────────────────────────────────────
    try:
        product_name = (
            (ld.get("name") or "")
            or _meta_content(soup, {"property": "og:title"})
            or _meta_content(soup, {"name": "twitter:title"})
        ).strip()
        if not product_name:
            h1 = soup.find("h1")
            if h1 is not None:
                product_name = h1.get_text(strip=True)
        product_name = _clean_detail_product_name(product_name)
        if product_name:
            extracted["product_name"] = True
    except Exception:
        product_name = ""

    # ── brand ─────────────────────────────────────────────────────────────────
    try:
        brand = ""
        ld_brand = ld.get("brand") or {}
        if isinstance(ld_brand, dict):
            brand = (ld_brand.get("name") or "").strip()
        elif isinstance(ld_brand, str):
            brand = ld_brand.strip()

        if not brand:
            brand = (
                _meta_content(soup, {"itemprop": "brand"})
                or _meta_content(soup, {"property": "product:brand"})
                or _meta_content(soup, {"name": "brand"})
            )

        if not brand:
            brand_el = soup.find(
                lambda el: el.name not in ["script", "style"]
                and any(kw in " ".join(el.get("class") or []).lower()
                        for kw in ["brand", "vendor", "manufacturer"])
            )
            if brand_el is not None:
                brand = brand_el.get_text(strip=True)

        if not brand:
            brand = _find_label_value(soup, ["brand", "manufacturer"])

        brand = brand.strip()
        if brand:
            extracted["brand"] = True
    except Exception:
        brand = ""

    # ── sku ───────────────────────────────────────────────────────────────────
    try:
        sku = (ld.get("sku") or "").strip()

        if not sku:
            sku = (
                _meta_content(soup, {"itemprop": "sku"})
                or _meta_content(soup, {"itemprop": "productID"})
                or _meta_content(soup, {"name": "sku"})
            )

        if not sku:
            for attr in ["data-sku", "data-product-id", "data-variant-id", "data-item-id"]:
                el = soup.find(attrs={attr: True})
                if el is not None:
                    sku = str(el.get(attr) or "").strip()
                    if sku:
                        break

        if not sku:
            sku = _find_label_value(soup, _ITEM_NO_LABELS)

        sku = sku.strip()
        if not _looks_like_valid_detail_sku(sku):
            sku = _find_label_value(soup, ["sku"])
        if not _looks_like_valid_detail_sku(sku):
            page_text = soup.get_text(" ", strip=True)
            m = re.search(r"\bSKU\s*(\d{3,})\b", page_text, re.IGNORECASE)
            sku = m.group(1) if m else ""
        if _looks_like_valid_detail_sku(sku):
            extracted["sku"] = True
            logger.info(f"[Detail] sku={sku!r}")
        else:
            sku = ""
    except Exception:
        sku = ""

    # ── upc ───────────────────────────────────────────────────────────────────
    try:
        upc = ""

        # 1. JSON-LD gtin fields (most reliable when present)
        for gtin_key in ["gtin13", "gtin12", "gtin", "gtin8"]:
            raw = (ld.get(gtin_key) or "").strip()
            upc = _coerce_upc(raw)
            if upc:
                logger.info(f"[Strategy 2] UPC via JSON-LD ({gtin_key}) | upc={upc}")
                break

        # 2. <meta> tags (itemprop / name attributes)
        if not upc:
            for meta_attrs in [
                {"itemprop": "gtin13"},
                {"itemprop": "gtin12"},
                {"itemprop": "gtin"},
                {"name": "upc"},
            ]:
                raw = _meta_content(soup, meta_attrs)
                upc = _coerce_upc(raw)
                if upc:
                    logger.info(f"[Strategy 2] UPC via meta {meta_attrs} | upc={upc}")
                    break

        # 3. Page-level extraction (regex + element traversal with UPC validation)
        if not upc:
            upc = _extract_upc(soup)

        if upc:
            extracted["upc"] = True
    except Exception:
        upc = ""

    # ── price ─────────────────────────────────────────────────────────────────
    try:
        page_text_for_price = soup.get_text(" ", strip=True)
        nassau_price, nassau_unit_price, nassau_pricing_unit = _extract_nassau_price(page_text_for_price)
        price = nassau_price or str(ld_offers.get("price") or "").strip()

        if not price:
            currency = str(ld_offers.get("priceCurrency") or "").strip()
            raw_price = (
                _meta_content(soup, {"itemprop": "price"})
                or _meta_content(soup, {"property": "product:price:amount"})
            )
            if raw_price:
                raw_price = raw_price.strip()
                if re.fullmatch(r"\d[\d,]*(?:\.\d{2})?", raw_price):
                    price = f"${raw_price.replace(',', '')}" if not raw_price.startswith('$') else raw_price
                else:
                    price = f"{currency}{raw_price}".strip() if currency else raw_price

        if not price:
            price_el = soup.find(
                lambda el: el.name not in ["script", "style"]
                and any(kw in " ".join(el.get("class") or []).lower()
                        for kw in ["price", "cost", "amount"])
            )
            if price_el is not None:
                candidate = price_el.get_text(" ", strip=True)
                m = re.search(r"\$\d[\d,]*\.\d{2}", candidate)
                price = m.group(0) if m else ""

        price = _normalize_money_value(price) or price.strip()
        compare_unit_price = locals().get('nassau_unit_price', '') or locals().get('unit_price', '') if 'unit_price' in locals() else locals().get('nassau_unit_price', '')
        compare_pricing_unit = locals().get('nassau_pricing_unit', '') or locals().get('pricing_unit', '') if 'pricing_unit' in locals() else locals().get('nassau_pricing_unit', '')
        if _looks_like_broad_page_price(price, compare_unit_price, compare_pricing_unit):
            price = compare_unit_price or ''
        if price:
            extracted["price"] = True
    except Exception:
        price = ""
        nassau_unit_price = ""
        nassau_pricing_unit = ""

    # ── unit_size (individual unit size — e.g. "1 GAL", "32 OZ") ─────────────
    try:
        unit_size = _find_label_value(soup, _UNIT_SIZE_LABELS)
        if unit_size:
            extracted["unit_size"] = True
            logger.info(f"[Detail] unit_size={unit_size!r}")
    except Exception:
        unit_size = ""

    # ── pack_size (legacy / ambiguous — kept for backwards compat) ────────────
    try:
        pack_size = _find_label_value(soup, [
            "pack size", "pack qty", "pack quantity",
            "units per pack", "qty per pack", "pack of", "per pack",
        ])
        if pack_size:
            extracted["pack_size"] = True
    except Exception:
        pack_size = ""

    # ── case_pack ─────────────────────────────────────────────────────────────
    try:
        case_pack = _find_label_value(soup, _CASE_PACK_LABELS)
        if not case_pack:
            case_pack = _extract_nassau_sales_per_case(soup)
        if case_pack:
            extracted["case_pack"] = True
            logger.info(f"[Detail] case_pack={case_pack!r}")
    except Exception:
        case_pack = ""

    # ── Title-abbreviation fallback: unit_size / case_pack from product name ──
    # Runs only when label-based extraction above left one or both fields empty.
    # Handles patterns like "20oz/24pk", "75ct/6pk", "4pk" in the product title.
    if not unit_size or not case_pack:
        try:
            from strategies.pack_parser import parse_pack_from_title
            parsed_pack = parse_pack_from_title(product_name)
            if parsed_pack and parsed_pack.get("pack_confidence", 0) >= 0.60:
                if not unit_size and parsed_pack.get("unit_size"):
                    unit_size = parsed_pack["unit_size"]
                    extracted["unit_size"] = True
                    logger.info(
                        f"[Detail] unit_size={unit_size!r} "
                        f"(title-parsed from {parsed_pack.get('raw_pack_text')!r})"
                    )
                if not case_pack and parsed_pack.get("case_pack"):
                    case_pack = parsed_pack["case_pack"]
                    extracted["case_pack"] = True
                    logger.info(
                        f"[Detail] case_pack={case_pack!r} "
                        f"(title-parsed from {parsed_pack.get('raw_pack_text')!r})"
                    )
        except Exception as e:
            logger.debug(f"[Detail] title pack parse error: {e}")

    # ── unit_price / pricing_unit ─────────────────────────────────────────────
    try:
        if 'nassau_unit_price' in locals() and nassau_unit_price:
            unit_price = nassau_unit_price
            pricing_unit = nassau_pricing_unit
        else:
            unit_price, pricing_unit = _extract_unit_price(soup)
        unit_price = _normalize_money_value(unit_price) or unit_price
        if unit_price:
            extracted["unit_price"] = True
    except Exception:
        unit_price = ""
        pricing_unit = ""

    # ── bulk_price (case/wholesale price) ─────────────────────────────────────
    try:
        bulk_raw = _find_label_value(soup, _BULK_PRICE_LABELS)
        if bulk_raw:
            pm = _PRICE_RE.search(bulk_raw)
            bulk_price = f"${pm.group(1).replace(',', '')}" if pm else bulk_raw.strip()
            if bulk_price:
                extracted["bulk_price"] = True
                logger.info(f"[Detail] bulk_price={bulk_price!r}")
        else:
            bulk_price = ""
    except Exception:
        bulk_price = ""

    # ── minimum_order_qty ─────────────────────────────────────────────────────
    try:
        minimum_order_qty = _find_label_value(soup, _MIN_ORDER_LABELS)
        # Also check data-min-qty / data-minimum-quantity attributes
        if not minimum_order_qty:
            for attr in ["data-min-qty", "data-minimum-quantity", "data-min-order",
                         "data-moq", "min"]:
                el = soup.find(attrs={attr: True})
                if el is not None:
                    val = str(el.get(attr) or "").strip()
                    if val and val.isdigit():
                        minimum_order_qty = val
                        break
        minimum_order_qty = minimum_order_qty.strip() if minimum_order_qty else ""
        if minimum_order_qty:
            extracted["minimum_order_qty"] = True
            logger.info(f"[Detail] minimum_order_qty={minimum_order_qty!r}")
    except Exception:
        minimum_order_qty = ""

    # ── raw_price_text (all price-like text from the page) ────────────────────
    try:
        price_elements = soup.find_all(
            lambda el: el.name not in ["script", "style"]
            and any(kw in " ".join(el.get("class") or []).lower()
                    for kw in ["price", "cost", "amount", "rate"])
        )
        raw_price_texts = []
        for el in price_elements[:5]:   # cap at 5 elements to avoid bloat
            t = el.get_text(separator=" ", strip=True)
            if t and len(t) < 200:
                raw_price_texts.append(t)
        raw_price_text = " | ".join(raw_price_texts) if raw_price_texts else ""
        if raw_price_text:
            extracted["raw_price_text"] = True
    except Exception:
        raw_price_text = ""

    # ── image_url ─────────────────────────────────────────────────────────────
    try:
        image_url = ""

        ld_image = ld.get("image") or ""
        if isinstance(ld_image, list):
            ld_image = next((i for i in ld_image if i), "")
        if isinstance(ld_image, dict):
            ld_image = ld_image.get("url") or ""
        image_url = str(ld_image).strip()

        if not image_url:
            image_url = (
                _meta_content(soup, {"property": "og:image"})
                or _meta_content(soup, {"name": "twitter:image"})
            )

        if not image_url:
            for img in soup.find_all("img"):
                for attr in ["src", "data-src", "data-lazy-src"]:
                    val = img.get(attr) or ""
                    if val and not val.startswith("data:"):
                        image_url = urljoin(url, val)
                        break
                if image_url:
                    break

        if image_url:
            extracted["image_url"] = True
    except Exception:
        image_url = ""

    try:
        gtin_case = _extract_nassau_case_gtin(soup)
        if gtin_case:
            extracted["gtin_case"] = True
    except Exception:
        gtin_case = ""

    # ── Log what was found ────────────────────────────────────────────────────
    all_fields = [
        "product_name", "brand", "sku", "upc",
        "price", "unit_price", "pricing_unit",
        "unit_size", "pack_size", "case_pack",
        "bulk_price", "minimum_order_qty", "image_url",
    ]
    found   = [f for f in all_fields if f in extracted]
    missing = [f for f in all_fields if f not in extracted]
    page_slug = url.rstrip("/").split("/")[-1] or "page"
    logger.info(f"[Detail] {page_slug} | Found: {found} | Missing: {missing}")

    return {
        "product_name":      product_name,
        "brand":             brand,
        "sku":               sku,
        "upc":               upc,
        "price":             price,
        "unit_price":        unit_price,
        "pricing_unit":      pricing_unit,
        "unit_size":         unit_size,
        "pack_size":         pack_size,
        "case_pack":         case_pack,
        "bulk_price":        bulk_price,
        "minimum_order_qty": minimum_order_qty,
        "raw_price_text":    raw_price_text,
        "image_url":         image_url,
        "product_url":       url,
        "gtin_case":         gtin_case,
    }


# ── Pagination detection ──────────────────────────────────────────────────────

# Link text values (lowercased) that indicate a "next page" link.
_NEXT_PAGE_TEXTS = {"next", "next page", "›", "»", ">>", "next »", "next>", ">"}

# Class/id fragments that suggest a pagination container.
_PAGINATION_CLASS_SIGNALS = ["pagination", "pager", "pages", "page-nav", "paginate"]

# Matches: "Showing 1-48 of 2771 products", "Items 1-100 of 1240", "1 to 24 of 53"
_RESULT_COUNT_RE = re.compile(
    r"(?:items?\s+|showing\s+|products?\s+)?"
    r"([\d,]+)"
    r"\s*[–\-\u2013to]+\s*"
    r"([\d,]+)"
    r"\s+of\s+"
    r"([\d,]+)",
    re.IGNORECASE,
)

# Matches: "Page 1 of 3"
_PAGE_OF_TOTAL_RE = re.compile(r"page\s+\d+\s+of\s+(\d+)", re.IGNORECASE)


def _stabilise_shopify_sort(html: str, url: str) -> str:
    """
    For Shopify collection pages, return the URL with sort_by=title-ascending
    added so that pagination is stable and covers the full catalog.

    Shopify's default 'best-selling' sort is unstable across requests:
    the same products appear on multiple pages while others are never shown,
    causing crawls to miss 20-30% of a catalog even when all pages are visited.
    title-ascending is a server-stable sort that guarantees each product
    appears on exactly one page, and Shopify propagates it through rel=next.

    Only modifies the URL when:
      - /collections/ is in the URL path (Shopify collection pattern)
      - Shopify fingerprints are found in the HTML
      - No sort_by parameter is already set

    Returns the original URL unchanged for non-Shopify pages or when
    sort_by is already specified.
    """
    if "/collections/" not in urlparse(url).path:
        return url

    params = parse_qs(urlparse(url).query)
    if "sort_by" in params:
        return url

    shopify_markers = [
        "cdn.shopify.com",
        "Shopify.shop",
        "window.Shopify",
        "shopify-section",
        "/cdn/shop/",
    ]
    if not any(marker in html for marker in shopify_markers):
        return url

    parsed = urlparse(url)
    params["sort_by"] = ["title-ascending"]
    new_query = urlencode(params, doseq=True)
    return parsed._replace(query=new_query).geturl()


def _try_next_page_by_url(current_url: str, target_page: int) -> str | None:
    """
    Construct a URL for target_page by adding/incrementing the ?page= parameter.

    Works for Shopify (?page=N), BigCommerce, and similar platforms where
    the only thing that changes between pages is the page query parameter.
    Page 1 URLs often have no ?page= at all — this function adds it.

    Returns None on any error.
    """
    try:
        parsed = urlparse(current_url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        params["page"] = [str(target_page)]
        new_query = urlencode(params, doseq=True)
        return parsed._replace(query=new_query).geturl()
    except Exception:
        return None


def _find_next_page(soup: BeautifulSoup, current_url: str,
                    visited: set, base_netloc: str) -> str | None:
    """
    Detect the URL of the next pagination page.

    Priority:
      1. <a rel="next"> or <link rel="next">  (standard, most reliable)
      2. <a aria-label="...next...">
      3. <a> with "next"-like text, searched inside pagination containers first
         then the whole page as a fallback.

    Returns an absolute URL not already in `visited`, or None.
    """

    def _accept(href: str) -> str | None:
        """Resolve, validate, and de-dupe a candidate href."""
        if not href or href.startswith("#") or href.startswith("javascript"):
            return None
        abs_url = urljoin(current_url, href.strip())
        if urlparse(abs_url).netloc != base_netloc:
            return None
        if abs_url in visited:
            return None
        return abs_url

    # 1. rel="next"
    for tag in soup.find_all(["a", "link"], rel=True):
        rel = tag.get("rel") or []
        if isinstance(rel, str):
            rel = [rel]
        if "next" in [r.lower() for r in rel]:
            url = _accept(tag.get("href") or "")
            if url:
                return url

    # 2. aria-label containing "next"
    for a in soup.find_all("a", href=True):
        aria = (a.get("aria-label") or "").lower()
        if "next" in aria:
            url = _accept(a.get("href") or "")
            if url:
                return url

    # 3. "Next" text — check pagination containers first, then full page
    containers = [
        el for el in soup.find_all(True)
        if el.name in ("nav", "div", "ul", "ol", "section")
        and any(sig in " ".join(el.get("class") or []).lower()
                for sig in _PAGINATION_CLASS_SIGNALS)
    ]
    search_scopes = containers if containers else [soup]

    for scope in search_scopes:
        for a in scope.find_all("a", href=True):
            text = a.get_text(strip=True).lower()
            if text in _NEXT_PAGE_TEXTS or any(t in text for t in ["next", "›", "»"]):
                url = _accept(a.get("href") or "")
                if url:
                    return url

    return None


def _crawl_all_pages(
    start_html: str,
    start_url: str,
    fetch_fn=None,
) -> list[tuple[str, list[str]]]:
    """
    Crawl the entire paginated listing/category starting from start_url.

    Phase 1 — pagination loop:
      Visits each listing page, collects (primary_url, [alt_urls]) tuples,
      detects the next page, and continues until no further pages or
      MAX_LISTING_PAGES is reached.

    Stopping is driven by next-link detection first, with expected-page-count
    used as a fallback signal and URL-based page-increment tried when the next
    link disappears before the expected count is reached.

    Phase 2 — deduplicate by primary URL:
      Returns a single deduplicated list of (primary, alts) tuples preserving
      order of first encounter.  Alternatives may be empty [] when the listing
      page had no detected product-card containers.

    fetch_fn: optional callable(url) -> str.  Pass an authenticated fetch
              function for login-required crawls.
    """
    if fetch_fn is None:
        fetch_fn = fetch_html

    max_detail_pages, max_listing_pages = _runtime_limits()

    base_netloc    = urlparse(start_url).netloc
    visited_pages  : set[str]               = {start_url}
    seen_primary   : set[str]               = set()
    ordered        : list[tuple[str, list]] = []

    # ── Shopify sort stabilisation ────────────────────────────────────────────
    # Shopify's default sort is unstable across requests: products duplicate
    # across pages while others are never shown.  Switching to title-ascending
    # before the first link-collection ensures every product is seen exactly once.
    stable_url = _stabilise_shopify_sort(start_html, start_url)
    if stable_url != start_url:
        logger.info(
            f"[Strategy 2] Shopify store detected — switching to "
            f"sort_by=title-ascending for complete catalog coverage."
        )
        try:
            start_html = fetch_fn(stable_url)
            start_url  = stable_url
            logger.info(f"[Strategy 2] Re-fetched page 1 with stable sort: {stable_url}")
        except Exception as e:
            logger.warning(
                f"[Strategy 2] Could not re-fetch with stable sort ({e}) — "
                f"continuing with original URL (catalog may be incomplete)"
            )

    current_url  = start_url
    current_html = start_html
    page_num     = 0

    expected_pages = None   # inferred from result-count text on page 1
    expected_total = None   # total product count text (for completeness report)

    while page_num < max_listing_pages:
        page_num += 1
        page_label = (
            f"Page {page_num}/{expected_pages}"
            if expected_pages else f"Page {page_num}"
        )
        logger.info(f"[Strategy 2] === Listing {page_label}: {current_url} ===")

        # ── Collect (primary, alts) tuples from this listing page ─────────────
        try:
            page_tuples = _collect_product_links_with_alternatives(current_html, current_url)
        except Exception as e:
            logger.warning(f"[Strategy 2] Link collection error on page {page_num}: {e}")
            page_tuples = []

        new_count = 0
        for primary, alts in page_tuples:
            if primary not in seen_primary:
                seen_primary.add(primary)
                ordered.append((primary, alts))
                new_count += 1

        logger.info(
            f"[Strategy 2] {page_label}: "
            f"{len(page_tuples)} link(s) found, "
            f"{new_count} new | "
            f"{len(ordered)} total unique so far"
        )

        # ── Parse HTML (needed for pagination detection) ───────────────────────
        try:
            soup = BeautifulSoup(current_html, "html.parser")
        except Exception as e:
            logger.warning(f"[Strategy 2] HTML parse failed on {page_label}: {e} — stopping")
            break

        # ── Detect expected page count on page 1 ──────────────────────────────
        if page_num == 1:
            try:
                page_text = soup.get_text(separator=" ")
                m = _RESULT_COUNT_RE.search(page_text)
                if m:
                    start_n = int(m.group(1).replace(",", ""))
                    end_n   = int(m.group(2).replace(",", ""))
                    total_n = int(m.group(3).replace(",", ""))
                    per_pg  = end_n - start_n + 1
                    if per_pg > 0 and total_n > 0:
                        expected_pages = math.ceil(total_n / per_pg)
                        expected_total = total_n
                        logger.info(
                            f"[Strategy 2] Result count text: "
                            f"{start_n}-{end_n} of {total_n} "
                            f"-> {expected_pages} page(s) expected"
                        )
                else:
                    m2 = _PAGE_OF_TOTAL_RE.search(page_text)
                    if m2:
                        expected_pages = int(m2.group(1))
                        logger.info(
                            f"[Strategy 2] 'Page X of N' indicator: "
                            f"{expected_pages} page(s) expected"
                        )
                if expected_pages is None:
                    logger.info(
                        "[Strategy 2] Total page count not determinable — "
                        "will follow 'Next' links until none remain"
                    )
            except Exception as e:
                logger.debug(f"[Strategy 2] Page-count detection error: {e}")

        # ── Detect next page URL ───────────────────────────────────────────────
        try:
            next_url = _find_next_page(soup, current_url, visited_pages, base_netloc)
        except Exception as e:
            logger.warning(f"[Strategy 2] Next-page detection error on {page_label}: {e}")
            next_url = None

        # ── Fallback: URL-based page increment when next link disappears ───────
        # If we haven't yet reached the expected page count but the 'Next' link
        # is gone, try constructing the URL manually (e.g. Shopify ?page=N).
        if not next_url and expected_pages and page_num < expected_pages:
            candidate = _try_next_page_by_url(current_url, page_num + 1)
            if candidate and candidate not in visited_pages:
                logger.info(
                    f"[Strategy 2] No 'Next' link on {page_label} but "
                    f"only {page_num}/{expected_pages} pages visited — "
                    f"trying URL increment: {candidate}"
                )
                next_url = candidate

        # ── Stop conditions ────────────────────────────────────────────────────
        if not next_url:
            if expected_pages and page_num < expected_pages:
                logger.warning(
                    f"[Strategy 2] Stopped at {page_num}/{expected_pages} pages — "
                    f"no 'Next' link and URL increment unavailable. "
                    f"May have missed {expected_pages - page_num} page(s)."
                )
            else:
                reason = (
                    f"all {expected_pages} expected page(s) visited"
                    if expected_pages
                    else "no further pages detected"
                )
                logger.info(f"[Strategy 2] No further pages after {page_label} — {reason}")
            break

        if page_num >= max_listing_pages:
            logger.warning(
                f"[Strategy 2] Safety cap of {max_listing_pages} pages reached — stopping. "
                f"Raise runtime listing cap if this catalog has more pages."
            )
            break

        logger.info(f"[Strategy 2] Following pagination -> {next_url}")
        visited_pages.add(next_url)

        # ── Fetch next listing page ────────────────────────────────────────────
        try:
            current_html = fetch_fn(next_url)
            current_url  = next_url
        except Exception as e:
            logger.warning(
                f"[Strategy 2] Failed to fetch page {page_num + 1} ({next_url}): {e} — "
                f"stopping, preserving {len(ordered)} link(s) from {page_num} page(s)"
            )
            break

    # ── Completeness summary ───────────────────────────────────────────────────
    if expected_total and expected_pages:
        complete = page_num >= expected_pages
        pct = len(ordered) / expected_total * 100 if expected_total else 0
        status = "COMPLETE" if complete else f"POSSIBLY INCOMPLETE ({page_num}/{expected_pages} pages)"
        logger.info(
            f"[Strategy 2] Pagination complete: "
            f"{page_num} page(s) visited | "
            f"{len(ordered)} unique product URL(s) | "
            f"expected ~{expected_total} products ({pct:.0f}% of catalog) | "
            f"{status}"
        )
    else:
        logger.info(
            f"[Strategy 2] Pagination complete: "
            f"{page_num} listing page(s) visited, "
            f"{len(ordered)} unique product URL(s) collected"
        )
    return ordered


# ── Detail-page enrichment (post-Strategy-1 layer) ───────────────────────────

def _merge_detail_into_product(product: dict, detail: dict) -> None:
    """
    Merge detail-page extracted data into an existing product dict in place.

    Detail-page values take priority when non-empty.
    Listing-page values are kept as fallback for any field the detail page
    did not extract.  product_url is always preserved as-is (it is the URL
    we used to fetch the detail page).
    """
    preserve = {"product_url"}
    for key, value in detail.items():
        if key in preserve:
            continue
        if value:
            product[key] = value


def enrich_from_detail_pages(products: list, fetch_fn=None) -> list:
    """
    Visit each product's detail page and merge richer extracted data.

    Called by the router after Strategy 1 to fill in detail-page-only fields
    (BARCODE→upc, ITEM-NO→sku, MASTER CASE→case_pack, unit_size, unit_price,
    pricing_unit, etc.) that the listing page heuristics cannot see.

    Modifies products in place AND returns the same list.

    Args:
        products:  List of product dicts, each with a "product_url" key.
        fetch_fn:  Optional callable(url) -> str.  Defaults to fetch_html.
    """
    if fetch_fn is None:
        fetch_fn = fetch_html

    to_enrich = [
        (i, p["product_url"])
        for i, p in enumerate(products)
        if p.get("product_url")
    ]

    if not to_enrich:
        logger.info("[Detail Enrich] No product URLs to enrich — skipping")
        return products

    max_detail_pages, _ = _runtime_limits()
    total = len(to_enrich)
    logger.info(f"[Detail Enrich] Enriching {total} product(s) from detail pages…")

    enriched_count = 0
    rich_count = 0
    for seq, (idx, url) in enumerate(to_enrich, 1):
        if seq > max_detail_pages:
            logger.warning(
                f"[Detail Enrich] Reached detail cap ({max_detail_pages}) — "
                f"stopping. Increase runtime detail cap to process more."
            )
            break
        try:
            logger.info(f"[Detail Enrich] [{seq}/{total}] {url}")
            html = fetch_fn(url)

            # Validate richness — log diagnostic, preserve listing row regardless
            rich = validate_rich_detail(html)
            if rich.is_rich:
                rich_count += 1
                logger.info(
                    f"[Detail Enrich] [{seq}] ✓ rich (score={rich.score}) "
                    f"signals={rich.signals_found}"
                )
            else:
                logger.info(
                    f"[Detail Enrich] [{seq}] ✗ not rich (score={rich.score}) "
                    f"signals={rich.signals_found} — enriching with whatever is found"
                )

            detail = _extract_from_detail_page(html, url)
            _merge_detail_into_product(products[idx], detail)
            enriched_count += 1
        except Exception as e:
            logger.warning(f"[Detail Enrich] Failed to enrich {url}: {e}")

    logger.info(
        f"[Detail Enrich] Complete — enriched {enriched_count}/{total} product(s), "
        f"{rich_count} had rich detail pages"
    )
    return products


# ── Strategy entry point ──────────────────────────────────────────────────────

def run(html: str, url: str, fetch_fn=None) -> list:
    """
    Strategy 2 entry point.

    Crawls all paginated listing pages to collect every /products/ link,
    then visits each detail page to extract full product data.

    fetch_fn: optional callable(url) -> str. Pass an authenticated fetch
              function for login-required crawls. Defaults to fetch_html.

    Returns a list of product dicts. Always returns a list, never raises.
    """
    if fetch_fn is None:
        fetch_fn = fetch_html

    # Phase 1: collect all product links across all listing pages
    try:
        all_links = _crawl_all_pages(html, url, fetch_fn=fetch_fn)
    except Exception as e:
        logger.error(f"[Strategy 2] Catalog crawl failed: {e}")
        return []

    if not all_links:
        logger.info("[Strategy 2] No usable /products/ links found — cannot proceed")
        return []

    max_detail_pages, _ = _runtime_limits()

    # Apply safety cap and warn if hit
    if len(all_links) > max_detail_pages:
        logger.warning(
            f"[Strategy 2] Collected {len(all_links)} product URLs — "
            f"capping at {max_detail_pages}. "
            f"Increase runtime detail cap to scrape the full catalog."
        )
        all_links = all_links[:max_detail_pages]

    logger.info(f"[Strategy 2] Scraping {len(all_links)} product detail page(s)…")

    # Build a base-row lookup from the listing page's structured layout (if any).
    # These rows contain fields that the listing page exposes directly — most
    # importantly image_url, product_name, sku, and price from labeled columns
    # (IMAGE, DESCRIPTION, ITEM, PRICE).  They are used as a fallback below:
    # if the detail page does not return a field, we fill it from the listing row.
    base_rows_by_url: dict[str, dict] = {}
    try:
        from strategies.row_extractor import extract_products_from_page as _extract_listing_rows
        listing_rows = _extract_listing_rows(html, url)
        if listing_rows:
            for row in listing_rows:
                row_url = (row.get("product_url") or "").strip()
                if row_url:
                    base_rows_by_url[row_url] = row
            if base_rows_by_url:
                logger.info(
                    f"[Strategy 2] Listing-page structured layout: "
                    f"{len(base_rows_by_url)} base row(s) indexed by product URL "
                    f"(image, sku, price preserved as fallback)"
                )
        else:
            logger.info(
                "[Strategy 2] No structured listing-page layout detected — "
                "base-row fallback not available"
            )
    except Exception as e:
        logger.warning(f"[Strategy 2] Listing base-row extraction error (non-fatal): {e}")

    # _BASE_ROW_FIELDS: listing-page fields used as fallback when the detail
    # page does not return a value.  image_url is the most important — the
    # listing page IMAGE column is often the only place it appears.
    _BASE_ROW_FIELDS = ("image_url", "product_name", "sku", "price", "case_pack", "brand")

    # Phase 2: scrape each detail page with validate + fallback
    products = []
    for i, (primary_url, alt_urls) in enumerate(all_links, 1):
        try:
            logger.info(f"[Strategy 2] [{i}/{len(all_links)}] Fetching: {primary_url}")
            detail_html = fetch_fn(primary_url)
            chosen_url = primary_url

            # Validate richness of the primary result
            rich = validate_rich_detail(detail_html)
            logger.info(
                f"[Strategy 2] [{i}] rich_score={rich.score} is_rich={rich.is_rich} "
                f"signals={rich.signals_found}"
            )

            # If not rich AND we have alternatives, try them
            if not rich.is_rich and alt_urls:
                logger.info(
                    f"[Strategy 2] [{i}] Primary not rich — "
                    f"trying {len(alt_urls)} alternative(s): {alt_urls}"
                )
                # Build stub Candidate objects for select_best_target
                alt_candidates = [
                    Candidate(url=u, element_tag="a", text="", source="alt", score=1)
                    for u in alt_urls
                ]
                result = select_best_target(alt_candidates, fetch_fn, row_index=i)
                if result:
                    alt_url, alt_html, alt_rich = result
                    if alt_rich.score > rich.score:
                        logger.info(
                            f"[Strategy 2] [{i}] Switching to alternative "
                            f"(score {rich.score} → {alt_rich.score}): {alt_url!r}"
                        )
                        detail_html = alt_html
                        chosen_url = alt_url

            product = _extract_from_detail_page(detail_html, chosen_url)

            # Merge listing-page base row as fallback for any field that the
            # detail page did not populate.  Check both the chosen URL and the
            # original primary URL (they may differ when an alternative was used).
            base = base_rows_by_url.get(chosen_url) or base_rows_by_url.get(primary_url)
            if base:
                filled = []
                for field in _BASE_ROW_FIELDS:
                    if not product.get(field) and base.get(field):
                        product[field] = base[field]
                        filled.append(field)
                if filled:
                    logger.info(
                        f"[Strategy 2] [{i}] Base-row fallback filled: {filled}"
                    )

            if product.get("product_name") or product.get("product_url"):
                products.append(product)

        except Exception as e:
            logger.warning(f"[Strategy 2] Failed to scrape {primary_url}: {e}")
            continue

    products = _deduplicate_products(products)

    logger.info(
        f"[Strategy 2] Complete — "
        f"visited {len(all_links)} detail page(s), "
        f"extracted {len(products)} product(s)"
    )
    return products
