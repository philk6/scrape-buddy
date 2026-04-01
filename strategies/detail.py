"""
strategies/detail.py â€” Strategy 2: Detail Page Crawl

How it works:
  1. Crawls ALL listing/category pages by following pagination (rel="next",
     "Next" button text, aria-label) until no further pages are found.
  2. On each listing page, collects <a> links whose href contains /products/.
     - Tries a context-aware pass first (links inside product card elements).
     - Falls back to URL-pattern-only if the context pass returns nothing.
  3. Deduplicates all collected product URLs across all pages.
  4. Visits each product detail page and extracts rich fields using JSON-LD,
     Open Graph meta tags, itemprop attributes, and labelâ†’value text patterns.

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

# â”€â”€ Link collection rules â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# href must contain one of these to be a candidate product detail link
PRODUCT_PATH_REQUIRED = ["/products/"]

# href containing any of these is immediately rejected (before context check)
REJECT_PATH_FRAGMENTS = [
    "/collections/", "/pages/", "/blogs/", "/search",
    "/cart", "/checkout", "/account", "/login", "/register",
    "/wishlist", "/compare", "/tag/", "javascript:", "mailto:", "tel:",
]

# Semantic HTML tag names that are always navigation chrome â€” safe to strip
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

# â”€â”€ Structured field label sets (all lowercase, hyphens normalised to spaces) â”€â”€
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
    r"\b1\s*(?:ea|each|unit|pc|pcs)?\b\s*\s*[@=]\s*\$?\s*([\d,]+\.\d{2})"
    r"(?:\s*(ea|each|unit|pc|pcs|lb|oz|kg|g))?",
    re.IGNORECASE,
)

# Standalone price: "$37.50" optionally followed by a unit
_PRICE_RE = re.compile(
    r"\$\s*([\d,]+\.\d{2})"
    r"(?:\s*(?:per\s+)?(ea|each|unit|pc|pcs|lb|oz|kg|g|cs|case))?",
    re.IGNORECASE,
)


# â”€â”€ Product container detection â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


# â”€â”€ Product deduplication â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


# â”€â”€ Noise-region stripping â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _strip_non_grid_regions(soup: BeautifulSoup) -> None:
    """
    Remove semantic nav/header/footer/aside tags and known non-product regions
    from soup IN PLACE. Only strips by exact id match â€” never by partial class
    name, to avoid accidentally removing product card headers or nav-style
    class names inside the product grid.
    """
    removed = 0

    # 1. Semantic tags that are always nav chrome
    for tag_name in NAV_SEMANTIC_TAGS:
        for el in soup.find_all(tag_name):
            el.decompose()
            removed += 1

    # 2. Exact id match only (no partial class matching â€” too many false positives)
    for el in soup.find_all(id=True):
        if el.get("id", "").lower().strip() in NAV_IDS:
            try:
                el.decompose()
                removed += 1
            except Exception:
                pass  # element may already be decomposed

    logger.info(f"[Strategy 2] Stripped {removed} non-grid regions from page")


# â”€â”€ Link collection â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
                              require_context: bool) -> tuple[list, dict]:
    """
    Scan soup for /products/ links, applying URL rules and optionally a
    product-card context check.

    Returns (accepted_urls, stats_dict).
    """
    seen = set()
    accepted = []
    stats = {
        "collections_rejected": 0,
        "other_rejected":       0,
        "context_rejected":     0,
    }

    for a in soup.find_all("a", href=True):
        try:
            href = (a.get("href") or "").strip()
            if not href:
                continue

            absolute = urljoin(base_url, href)

            # Same-domain only
            if urlparse(absolute).netloc != base_netloc:
                continue

            # Deduplicate
            if absolute in seen:
                continue

            # Must contain /products/
            if not any(req in absolute for req in PRODUCT_PATH_REQUIRED):
                continue

            # Reject /collections/ links explicitly
            if "/collections/" in absolute:
                stats["collections_rejected"] += 1
                continue

            # Reject other known non-product fragments
            if any(frag in absolute.lower() for frag in REJECT_PATH_FRAGMENTS):
                stats["other_rejected"] += 1
                continue

            # Optional context check
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
    Quick count of /products/ links on the page (no context check, no stripping).
    Used by the router to decide whether to prefer Strategy 2.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        base_netloc = urlparse(base_url).netloc
        links, _ = _collect_links_from_soup(soup, base_url, base_netloc, require_context=False)
        return len(links)
    except Exception as e:
        logger.warning(f"[Strategy 2] count_product_links error: {e}")
        return 0


def _collect_product_links(html: str, base_url: str) -> list:
    """
    Collect real product detail links from the listing page.

    Pass 0 â€” container-aware + link scorer.
      Uses _find_product_containers() to locate repeated card elements, then
      applies the link scorer to pick the best link from each card. This is
      the strongest signal when card markup is present.

    Pass 1 â€” strip nav regions, apply URL rules + product-card context check.
      Falls back to this when Pass 0 finds fewer than 2 links.

    Pass 2 â€” full page, URL rules only (no context check).
      Used when Pass 1 also finds nothing (unusual grid markup).

    Always logs a clear breakdown of accepted vs rejected links.
    """
    base_netloc = urlparse(base_url).netloc

    # â”€â”€ Pass -1: Structured row/table layout (highest priority) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Uses explicit labeled columns (IMAGE, DESCRIPTION, ITEM, PRICE) to
    # select the correct product-detail link from each row. Bypasses the
    # /products/ URL requirement â€” works for non-Shopify B2B wholesale sites.
    try:
        soup_early = BeautifulSoup(html, "html.parser")
        row_links = _row_extractor_links(soup_early, base_url)
        if row_links:
            logger.info(
                f"[Strategy 2] Pass -1 (structured row): "
                f"{len(row_links)} (primary, alts) tuple(s) â€” "
                f"using explicit column structure"
            )
            return row_links
        logger.info("[Strategy 2] Pass -1: no structured layout detected â€” continuing to Pass 0")
    except Exception as e:
        logger.warning(f"[Strategy 2] Pass -1 error (non-fatal): {e}")

    # â”€â”€ Pass 0: container-aware target selection (scoring only, no extra fetches) â”€â”€
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
                    break  # sorted descending â€” no point continuing
                url = c.url
                if url in seen0:
                    continue
                if (any(req in url for req in PRODUCT_PATH_REQUIRED)
                        and not any(frag in url.lower() for frag in REJECT_PATH_FRAGMENTS)
                        and urlparse(url).netloc == base_netloc):
                    seen0.add(url)
                    links0.append(url)
                    break  # one primary per container

        logger.info(
            f"[Strategy 2] Pass 0 (container+target-scorer): "
            f"{len(containers)} containers â†’ {len(links0)} product link(s)"
        )

        if len(links0) >= 2:
            logger.info(f"[Strategy 2] Pass 0 accepted {len(links0)} product link(s)")
            return links0

        logger.info("[Strategy 2] Pass 0 found < 2 links â€” trying Pass 1")

    # â”€â”€ Pass 1: stripped soup + context check â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    soup1 = BeautifulSoup(html, "html.parser")
    _strip_non_grid_regions(soup1)
    links1, stats1 = _collect_links_from_soup(soup1, base_url, base_netloc, require_context=True)

    logger.info(
        f"[Strategy 2] Pass 1 (context): {len(links1)} accepted | "
        f"{stats1['collections_rejected']} /collections/ rejected | "
        f"{stats1['context_rejected']} context-rejected | "
        f"{stats1['other_rejected']} other rejected"
    )

    if links1:
        logger.info(f"[Strategy 2] Pass 1 accepted {len(links1)} product link(s) from this page")
        return links1

    # â”€â”€ Pass 2: full page, URL rules only (no context check) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    logger.warning(
        "[Strategy 2] Pass 1 returned 0 links â€” "
        "falling back to URL-only filter on full page (no context check)"
    )
    soup2 = BeautifulSoup(html, "html.parser")
    links2, stats2 = _collect_links_from_soup(soup2, base_url, base_netloc, require_context=False)

    logger.info(
        f"[Strategy 2] Pass 2 (url-only): {len(links2)} accepted | "
        f"{stats2['collections_rejected']} /collections/ rejected | "
        f"{stats2['other_rejected']} other rejected"
    )
    return links2


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

    # â”€â”€ Pass -1: Structured row/table layout (highest priority) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Uses explicit labeled columns (IMAGE, DESCRIPTION, ITEM, PRICE) to select
    # the correct product-detail link from each row.  Bypasses the /products/
    # URL requirement so it works for non-Shopify B2B wholesale catalogs.
    try:
        soup_early = BeautifulSoup(html, "html.parser")
        row_links = _row_extractor_links(soup_early, base_url)
        if row_links:
            logger.info(
                f"[Strategy 2] Pass -1 (structured row, with-alts): "
                f"{len(row_links)} (primary, alts) tuple(s) â€” "
                f"using explicit column structure"
            )
            return row_links
        logger.info(
            "[Strategy 2] Pass -1 (with-alts): no structured layout detected "
            "â€” continuing to Pass 0"
        )
    except Exception as e:
        logger.warning(f"[Strategy 2] Pass -1 (with-alts) error (non-fatal): {e}")

    # â”€â”€ Pass 0: container-aware target selection â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
                if (any(req in url for req in PRODUCT_PATH_REQUIRED)
                        and not any(frag in url.lower() for frag in REJECT_PATH_FRAGMENTS)
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
            f"{len(containers)} containers â†’ {len(results)} (primary, alts) tuples"
        )
        if len(results) >= 2:
            return results

        logger.info("[Strategy 2] Pass 0 found < 2 tuples â€” trying Pass 1")

    # â”€â”€ Pass 1: stripped soup + context check (no alternatives) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    soup1 = BeautifulSoup(html, "html.parser")
    _strip_non_grid_regions(soup1)
    links1, stats1 = _collect_links_from_soup(soup1, base_url, base_netloc, require_context=True)

    logger.info(
        f"[Strategy 2] Pass 1 (context): {len(links1)} accepted | "
        f"{stats1['collections_rejected']} /collections/ rejected | "
        f"{stats1['context_rejected']} context-rejected | "
        f"{stats1['other_rejected']} other rejected"
    )

    if links1:
        return [(url, []) for url in links1]

    # â”€â”€ Pass 2: full page, URL rules only â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€(€€€±½•È¹Ý…É¹¥¹œ (€€€€€€€€‰mMÑÉ…Ñ•ä€ÉtA…ÍÌ€ÄÉ•ÑÕÉ¹•€À±¥¹­ÌƒŠP€ˆ(€€€€€€€€‰™…±±¥¹œ‰…¬Ñ¼UI0µ½¹±ä™¥±Ñ•È½¸™Õ±°Á…”€¡¹¼½¹Ñ•áÐ¡•¬¤ˆ(€€€€¤(€€€Í½ÕÀÈ€ô	•…ÕÑ¥™Õ±M½ÕÀ¡¡Ñµ°°€‰¡Ñµ°¹Á…ÉÍ•Èˆ¤(€€€±¥¹­ÌÈ°ÍÑ…ÑÌÈ€ô}½±±•Ñ}±¥¹­Í}™É½µ}Í½ÕÀ¡Í½ÕÀÈ°‰…Í•}ÕÉ°°‰…Í•}¹•Ñ±½Œ°É•ÅÕ¥É•}½¹Ñ•áÐõ…±Í”¤((€€€±½•È¹¥¹™¼ (€€€€€€€˜‰mMÑÉ…Ñ•ä€ÉtA…ÍÌ€È€¡ÕÉ°µ½¹±ä¤èí±•¸¡±¥¹­ÌÈ¥ô…•ÁÑ•ð€ˆ(€€€€€€€˜‰íÍÑ…ÑÌÉl½±±•Ñ¥½¹Í}É•©•Ñ•uô€½½±±•Ñ¥½¹Ì¼É•©•Ñ•ð€ˆ(€€€€€€€˜‰íÍÑ…ÑÌÉl½Ñ¡•É}É•©•Ñ•uô½Ñ¡•ÈÉ•©•Ñ•ˆ(€€€€¤(€€€É•ÑÕÉ¸l¡ÕÉ°°mt¤™½ÈÕÉ°¥¸±¥¹­ÌÉt(((ŒƒŠRŠR •Ñ…¥°Á…”•áÑÉ…Ñ¥½¸ƒŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠR ()‘•˜}•áÑÉ…Ñ}©Í½¹}±¡Í½ÕÀè	•…ÕÑ¥™Õ±M½ÕÀ¤€´ø‘¥Ðè(€€€€ˆˆˆ(€€€A…ÉÍ”Ñ¡”™¥ÉÍÐ)M=8µ1AÉ½‘ÕÐÍ¡•µ„‰±½¬½¸Ñ¡”Á…”¸(€€€±Ý…åÌÉ•ÑÕÉ¹Ì„‘¥Ð€¡•µÁÑä¥˜¹½Ð™½Õ¹½ÈÕ¹Á…ÉÍ•…‰±”¤¸(€€€€ˆˆˆ(€€€™½ÈÍÉ¥ÁÐ¥¸Í½ÕÀ¹™¥¹‘}…±° ‰ÍÉ¥ÁÐˆ°ì‰ÑåÁ”ˆè€‰…ÁÁ±¥…Ñ¥½¸½±­©Í½¸‰ô¤è(€€€€€€€ÑÉäè(€€€€€€€€€€€É…Ü€ô©Í½¸¹±½…‘Ì¡ÍÉ¥ÁÐ¹ÍÑÉ¥¹œ½Èp‰pˆ¤(€€€€€€€€€€€…¹‘¥‘…Ñ•Ì€ôÉ…Ü¥˜¥Í¥¹ÍÑ…¹”¡É…Ü°±¥ÍÐ¤•±Í”mÉ…Ýt(€€€€€€€€€€€€Œ±Í¼Õ¹ÝÉ…ÀÉ…Á …ÉÉ…åÌ(€€€€€€€€€€€•áÁ…¹‘•€ômt(€€€€€€€€€€€™½È¥Ñ•´¥¸…¹‘¥‘…Ñ•Ìè(€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡¥Ñ•´°‘¥Ð¤è(€€€€€€€€€€€€€€€€€€€¥˜¥Ñ•´¹•Ð ‰ÑåÁ”ˆ¤€ôô€‰AÉ½‘ÕÐˆè(€€€€€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸¥Ñ•´(€€€€€€€€€€€€€€€€€€€•áÁ…¹‘•¹•áÑ•¹¡¥Ñ•´¹•Ð ‰É…Á ˆ°mt¤¤(€€€€€€€€€€€™½È¥Ñ•´¥¸•áÁ…¹‘•è(€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡¥Ñ•´°‘¥Ð¤…¹¥Ñ•´¹•Ð ‰ÑåÁ”ˆ¤€ôô€‰AÉ½‘ÕÐˆè(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸¥Ñ•´(€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€É•ÑÕÉ¸íô(()‘•˜}Í…™•}½™™•ÉÌ¡±è‘¥Ð¤€´ø‘¥Ðè(€€€€ˆˆˆ(€€€áÑÉ…ÐÑ¡”½™™•ÉÌ½‰©•Ð™É½´„)M=8µ1AÉ½‘ÕÐ‘¥Ð…¹•¹ÍÕÉ”¥Ð¥Ì(€€€…±Ý…åÌÉ•ÑÕÉ¹•…Ì„Á±…¥¸‘¥Ð€¡¹•Ù•È9½¹”°¹•Ù•È„±¥ÍÐ°¹•Ù•È„ÍÑÉ¥¹œ¤¸((€€€Q¡¥Ì¥ÌÑ¡”ÁÉ¥µ…ÉäÉ…Í Í½ÕÉ”è)M=8µ1…¸¡…Ù”€‰½™™•ÉÌˆè¹Õ±°°½È(€€€€‰½™™•ÉÌˆèl¸¸¹tÝ¡•É”Ñ¡”™¥ÉÍÐ¥Ñ•´¥Ì9½¹”½È„¹½¸µ‘¥Ð¸(€€€UÍ¥¹œ‘¥Ð¹•Ð¡­•ä°‘•™…Õ±Ð¤½¹±ä™…±±Ì‰…¬™½È5%MM%9“…ÌŠP¥˜Ñ¡”(€€€­•ä•á¥ÍÑÌÝ¥Ñ Ù…±Õ”¹Õ±°°€¹•Ð ¤É•ÑÕÉ¹Ì9½¹”°¹½ÐÑ¡”‘•™…Õ±Ð¸(€€€€ˆˆˆ(€€€É…Ü€ô±¹•Ð ‰½™™•ÉÌˆ¤€€€€€€€€€Œµ…ä‰”9½¹”°‘¥Ð°½È±¥ÍÐ(€€€¥˜É…Ü¥Ì9½¹”è(€€€€€€€É•ÑÕÉ¸íô(€€€¥˜¥Í¥¹ÍÑ…¹”¡É…Ü°‘¥Ð¤è(€€€€€€€É•ÑÕÉ¸É…Ü(€€€¥˜¥Í¥¹ÍÑ…¹”¡É…Ü°±¥ÍÐ¤è(€€€€€€€€ŒQ…­”Ñ¡”™¥ÉÍÐ‘¥Ð¥Ñ•´¥¸Ñ¡”±¥ÍÐ(€€€€€€€™½È¥Ñ•´¥¸É…Üè(€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡¥Ñ•´°‘¥Ð¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸¥Ñ•´(€€€€€€€É•ÑÕÉ¸íô(€€€É•ÑÕÉ¸íô€€ŒÍÑÉ¥¹œ°¹Õµ‰•È°•ÑŒ¸ƒŠP‘¥Í…É(()‘•˜}µ•Ñ…}½¹Ñ•¹Ð¡Í½ÕÀè	•…ÕÑ¥™Õ±M½ÕÀ°…ÑÑÉÌè‘¥Ð¤€´øÍÑÈè(€€€€ˆˆˆ(€€€M…™•±ä•ÐÑ¡”½¹Ñ•¹Ð…ÑÑÉ¥‰ÕÑ”½˜„€ñµ•Ñ„øÑ…œ¸((€€€%5A=IQ9Pè	•…ÕÑ¥™Õ±M½ÕÀÍ•±˜µ±½Í¥¹œÑ…Ì€¡±¥­”€ñµ•Ñ„ø¤¡…Ù”¹¼¡¥±(€€€•±•µ•¹ÑÌ°Í¼±•¸¡Ñ…œ¤€ôô€À°…¹¥˜Ñ…€•Ù…±Õ…Ñ•ÌÑ¼…±Í”•Ù•¸Ý¡•¸(€€€Ñ¡”Ñ…œ•á¥ÍÑÌ¸±Ý…åÌÕÍ”Ñ…œ¥Ì¹½Ð9½¹•€™½È•á¥ÍÑ•¹”¡•­Ì¸(€€€€ˆˆˆ(€€€ÑÉäè(€€€€€€€Ñ…œ€ôÍ½ÕÀ¹™¥¹ ‰µ•Ñ„ˆ°…ÑÑÉÌ¤(€€€€€€€¥˜Ñ…œ¥Ì¹½Ð9½¹”è€€€€€€€€€€€€€€€€€€€€ŒƒŠ@½ÉÉ•Ð¡•¬™½È	LÐQ…Ì(€€€€€€€€€€€½¹Ñ•¹Ð€ôÑ…œ¹•Ð ‰½¹Ñ•¹Ðˆ¤(€€€€€€€€€€€¥˜½¹Ñ•¹Ðè(€€€€€€€€€€€€€€€É•ÑÕÉ¸ÍÑÈ¡½¹Ñ•¹Ð¤¹ÍÑÉ¥À ¤(€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€Á…ÍÌ(€€€É•ÑÕÉ¸€ˆˆ(((ŒƒŠRŠR UA•áÑÉ…Ñ¥½¸ƒŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠR ((Œ5Õ±Ñ¤µÝ½ÉÙ…É¥…¹ÑÌ±¥ÍÑ•™¥ÉÍÐÍ¼Ñ¡•äµ…Ñ ‰•™½É”Ñ¡•¥ÈÍÕ‰ÍÑÉ¥¹Ì‘¼¸)}UA}1	1}-e]=IL€ôl(€€€€‰¥Ñ•´ÕÁŒˆ°€‰ÁÉ½‘ÕÐÕÁŒˆ°€‰ÕÁŒ½‘”ˆ°€‰ÕÁŒ½•…¸ˆ°€‰ÕÁŒ½•…¸½‘”ˆ°(€€€€‰Õ¹¥Ù•ÉÍ…°ÁÉ½‘ÕÐ½‘”ˆ°€‰ÕÁŒˆ°€‰•…¸ˆ°€‰‰…É½‘”ˆ°€‰Ñ¥¸ˆ°)t((Œ%¹±¥¹”É••àèµ…Ñ¡•Ì€‰%Ñ•´UAè€ÀÀÀÌÜÀÀÀÀàÈÄÜÀˆ¥¸Á±…¥¸Ñ•áÐ¸(Œ±±½ÝÌÕÀÑ¼€ÌÀ¡…ÉÌ½˜Ý¡¥Ñ•ÍÁ…”½ÁÕ¹ÑÕ…Ñ¥½¸‰•ÑÝ••¸±…‰•°…¹‘¥¥ÑÌ¸)}UA}QaQ}I€ôÉ”¹½µÁ¥±” (€€€Èˆ üé¥Ñ•µqÌ­ÕÁñÁÉ½‘ÕÑqÌ­ÕÁñÕÁqÌ©½‘•ñÕÁqÌ¨½qÌ©•…¹ðˆ(€€€È‰Õ¹¥Ù•ÉÍ…±qÌ­ÁÉ½‘ÕÑqÌ­½‘•ñÕÁñ•…¹ñ‰…É½‘•ñÑ¥¸¤ˆ(€€€È‰léqÍpµuìÀ°ÌÁôˆ(€€€Èˆ¡q‘mq‘qÍuìØ°ÄÕõq¤ˆ°€€€Œ€ãŠLÄÜ¡…ÉÌÑ¼…±±½Ü¥¹Ñ•É¹…°ÍÁ…•Ì½¡åÁ¡•¹Ì(€€€É”¹%9=IM°(¤(()‘•˜}¹½Éµ…±¥Í•}±…‰•°¡Ñ•áÐèÍÑÈ¤€´øÍÑÈè(€€€€ˆˆ‰1½Ý•É…Í”°½±±…ÁÍ”Ý¡¥Ñ•ÍÁ…”…¹ÍÑÉ¥ÀÁÕ¹ÑÕ…Ñ¥½¸™½È±…‰•°½µÁ…É¥Í½¸¸ˆˆˆ(€€€É•ÑÕÉ¸É”¹ÍÕˆ¡È‰lép¹qÍt¬ˆ°€ˆ€ˆ°Ñ•áÐ¹±½Ý•È ¤¤¹ÍÑÉ¥À ¤(()‘•˜}±•…¹}‘•Ñ…¥±}ÁÉ½‘ÕÑ}¹…µ”¡¹…µ”èÍÑÈ¤€´øÍÑÈè(€€€¥˜¹½Ð¹…µ”è(€€€€€€€É•ÑÕÉ¸€ˆˆ(€€€±•…¹•€ôÉ”¹ÍÕˆ¡È‰qÌ©qñqÌ©9…ÍÍ…Ô…¹‘åqÌ¨ˆ°€ˆˆ°¹…µ”°™±…ÌõÉ”¹%9=IM¤(€€€±•…¹•€ôÉ”¹ÍÕˆ¡È‰qÌ¬ˆ°€ˆ€ˆ°±•…¹•¤¹ÍÑÉ¥À ˆ€µðˆ¤(€€€É•ÑÕÉ¸±•…¹•(()‘•˜}±½½­Í}±¥­•}Ù…±¥‘}‘•Ñ…¥±}Í­Ô¡Ù…±Õ”èÍÑÈ¤€´ø‰½½°è(€€€¥˜¹½ÐÙ…±Õ”è(€€€€€€€É•ÑÕÉ¸…±Í”(€€€Ù…±Õ”€ôÙ…±Õ”¹ÍÑÉ¥À ¤(€€€¥˜¹½Ð€ È€ðô±•¸¡Ù…±Õ”¤€ðô€ÌÈ¤è(€€€€€€€É•ÑÕÉ¸…±Í”(€€€¥˜É”¹™Õ±±µ…Ñ ¡È‰lÀ´å„µ™uìáôµlÀ´å„µ™uìÑôµlÀ´å„µ™uìÑôµlÀ´å„µ™uìÑôµlÀ´å„µ™uìÄÉôˆ°Ù…±Õ”°É”¹%9=IM¤è(€€€€€€€É•ÑÕÉ¸…±Í”(€€€¥˜¹½ÐÉ”¹Í•…É ¡È‰qˆ°Ù…±Õ”¤è(€€€€€€€É•ÑÕÉ¸…±Í”(€€€¥˜¹½ÐÉ”¹™Õ±±µ…Ñ ¡È‰mµi„µèÀ´åumµi„µèÀ´åpµp¸¼t¨ˆ°Ù…±Õ”¤è(€€€€€€€É•ÑÕÉ¸…±Í”(€€€É•ÑÕÉ¸QÉÕ”(()‘•˜}•áÑÉ…Ñ}¹…ÍÍ…Õ}…Í•}Ñ¥¸¡Í½ÕÀè	•…ÕÑ¥™Õ±M½ÕÀ¤€´øÍÑÈè(€€€É…Ü€ô}™¥¹‘}±…‰•±}Ù…±Õ”¡Í½ÕÀ°l‰Ñ¥¸…Í”ˆ°€‰Ñ¥¸€¡…Í”¤ˆ°€‰…Í”Ñ¥¸ˆ°€‰Ñ¥¸…Í”½‘”‰t¤(€€€‘¥¥ÑÌ€ôÉ”¹ÍÕˆ¡È‰qˆ°€ˆˆ°É…Ü½È€ˆˆ¤(€€€É•ÑÕÉ¸‘¥¥ÑÌ¥˜É”¹™Õ±±µ…Ñ ¡È‰q‘ìà°Äáôˆ°‘¥¥ÑÌ¤•±Í”€ˆˆ(()‘•˜}•áÑÉ…Ñ}¹…ÍÍ…Õ}Í…±•Í}Á•É}…Í”¡Í½ÕÀè	•…ÕÑ¥™Õ±M½ÕÀ¤€´øÍÑÈè(€€€É…Ü€ô}™¥¹‘}±…‰•±}Ù…±Õ”¡Í½ÕÀ°l‰Í…±•ÌÁ•È…Í”ˆ°€‰Õ¹¥ÑÌÁ•È…Í”ˆ°€‰…Í”ÅÑäˆ°€‰…Í”ÅÕ…¹Ñ¥Ñä‰t¤(€€€¥˜É…Üè(€€€€€€€´€ôÉ”¹Í•…É ¡È‰q¬ˆ°É…Ü¤(€€€€€€€¥˜´è(€€€€€€€€€€€É•ÑÕÉ¸´¹É½ÕÀ À¤(€€€Á…•}Ñ•áÐ€ôÍ½ÕÀ¹•Ñ}Ñ•áÐ ˆ€ˆ°ÍÑÉ¥ÀõQÉÕ”¤(€€€´€ôÉ”¹Í•…É ¡È‰Í…±•ÍqÌ­Á•ÉqÌ­…Í•qÌ¨¡q¬¤ˆ°Á…•}Ñ•áÐ°É”¹%9=IM¤(€€€É•ÑÕÉ¸´¹É½ÕÀ Ä¤¥˜´•±Í”€ˆˆ(()‘•˜}•áÑÉ…Ñ}¹…ÍÍ…Õ}ÁÉ¥”¡Á…•}Ñ•áÐèÍÑÈ¤€´øÑÕÁ±•mÍÑÈ°ÍÑÈ°ÍÑÉtè(€€€¥˜¹½ÐÁ…•}Ñ•áÐè(€€€€€€€É•ÑÕÉ¸€ˆˆ°€ˆˆ°€ˆˆ(€€€´€ôÉ”¹Í•…É ¡Èˆ¡p‘q‘mq±t©p¹q‘ìÉô¥qÌ¨½qÌ¨¡…¡ññ…Í•ñL¤ˆ°Á…•}Ñ•áÐ°É”¹%9=IM¤(€€€¥˜´è(€€€€€€€Õ¹¥Ð€ô´¹É½ÕÀ È¤¹ÕÁÁ•È ¤(€€€€€€€É•ÑÕÉ¸´¹É½ÕÀ Ä¤°´¹É½ÕÀ Ä¤°Õ¹¥Ð(€€€´€ôÉ”¹Í•…É ¡Èˆ¡p‘q‘mq±t©p¹q‘ìÉô¤ˆ°Á…•}Ñ•áÐ¤(€€€¥˜´è(€€€€€€€É•ÑÕÉ¸´¹É½ÕÀ Ä¤°€ˆˆ°€ˆˆ(€€€É•ÑÕÉ¸€ˆˆ°€ˆˆ°€ˆˆ(()‘•˜}¥Í}ÕÁ}±…‰•°¡Ñ•áÐèÍÑÈ¤€´ø‰½½°è(€€€€ˆˆ‰I•ÑÕÉ¸QÉÕ”¥˜Ñ¡”¹½Éµ…±¥Í•Ñ•áÐ¥Ì½È½¹Ñ…¥¹Ì„UAµÉ•±…Ñ•±…‰•°¸ˆˆˆ(€€€¹½É´€ô}¹½Éµ…±¥Í•}±…‰•°¡Ñ•áÐ¤(€€€É•ÑÕÉ¸…¹ä¡­Ü¥¸¹½É´™½È­Ü¥¸}UA}1	1}-e]=IL¤(()‘•˜}½•É•}ÕÁŒ¡É…ÜèÍÑÈ¤€´øÍÑÈè(€€€€ˆˆˆ(€€€¥Ù•¸„É…Ü…¹‘¥‘…Ñ”ÍÑÉ¥¹œ°ÍÑÉ¥ÀÝ¡¥Ñ•ÍÁ…”½¡åÁ¡•¹Ì…¹Ù…±¥‘…Ñ”…Ì„(€€€UA½8€ ãŠLÄÐ‘¥¥ÑÌ¤¸I•ÑÕÉ¹Ì‘¥¥ÑÌµ½¹±äÍÑÉ¥¹œ½È€ˆˆ¥˜¥¹Ù…±¥¸(€€€1•…‘¥¹œé•É½Ì…É”ÁÉ•Í•ÉÙ•¸(€€€€ˆˆˆ(€€€¥˜¹½ÐÉ…Üè(€€€€€€€É•ÑÕÉ¸€ˆˆ(€€€‘¥¥ÑÌ€ôÉ”¹ÍÕˆ¡È‰mqÍpµtˆ°€ˆˆ°É…Ü¹ÍÑÉ¥À ¤¤(€€€¥˜É”¹™Õ±±µ…Ñ ¡È‰q‘ìà°ÄÑôˆ°‘¥¥ÑÌ¤è(€€€€€€€É•ÑÕÉ¸‘¥¥ÑÌ(€€€É•ÑÕÉ¸€ˆˆ(()‘•˜}•áÑÉ…Ñ}ÕÁŒ¡Í½ÕÀè	•…ÕÑ¥™Õ±M½ÕÀ¤€´øÍÑÈè(€€€€ˆˆˆ(€€€5Õ±Ñ¤µÍÑÉ…Ñ•äUA•áÑÉ…Ñ¥½¸Ý¥Ñ ‘•‰Õœ±½¥¹œ¸((€€€MÑÉ…Ñ•äƒŠPÉ••àÍ…¸½¸™Õ±°Á…”Ñ•áÐ¸(€€€€€…Ñ¡•Ì…±°¥¹±¥¹”€‰1…‰•°èY…±Õ”ˆÁ…ÑÑ•É¹Ì¥¸„Í¥¹±”Á…ÍÌ°¥¹±Õ‘¥¹œ(€€€€€€‰%Ñ•´UAè€ÀÀÀÌÜÀÀÀÀàÈÄÜÀˆÉ•…É‘±•ÍÌ½˜!Q50ÍÑÉÕÑÕÉ”¸((€€€MÑÉ…Ñ•äƒŠP•±•µ•¹Ðµ±•Ù•°±…‰•°½Ù…±Õ”ÑÉ…Ù•ÉÍ…°¸(€€€€€M…¹Ì•Ù•ÉäÑ•áÐ¹½‘”™½È„UA±…‰•°°Ñ¡•¸ÑÉ¥•Ì€¡¥¸½É‘•È¤è(€€€€€€€Ä¸½±½¸µÍÁ±¥ÐÝ¥Ñ¡¥¸Ñ¡”Í…µ”Ñ•áÐ¹½‘”€¡±…‰•°…¹Ù…±Õ”¥¸½¹”ÍÑÉ¥¹œ¤(€€€€€€€È¸½±½¸µÍÁ±¥ÐÝ¥Ñ¡¥¸Ñ¡”Á…É•¹Ð•±•µ•¹ÐÌ™Õ±°Ñ•áÐ(€€€€€€€Ì¸¹•áÐÍ¥‰±¥¹œ•±•µ•¹Ð€€¡Ù…±¥‘…Ñ•ƒŠPÑ½¼µÕÍÐ±½½¬±¥­”„UA¤(€€€€€€€Ð¸É…¹‘Á…É•¹ÐÌ¹•áÐÍ¥‰±¥¹œ•±•µ•¹Ð€¡Ù…±¥‘…Ñ•¤(€€€€€€€Ô¸¹•áÐÑ•áÐ¹½‘”¥¸Ñ¡”‘½Õµ•¹Ð€¡Ù…±¥‘…Ñ•¤((€€€I•ÑÕÉ¹Ì„‘¥¥ÑÌµ½¹±äUAÍÑÉ¥¹œ°½È€ˆˆ¥˜¹½Ñ¡¥¹œ™½Õ¹¸(€€€€ˆˆˆ(€€€€ŒƒŠRŠR MÑÉ…Ñ•äè™Õ±°µÁ…”Ñ•áÐÉ••àƒŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠR (€€€ÑÉäè(€€€€€€€€Œ•Ñ}Ñ•áÐÝ¥Ñ „ÍÁ…”Í•Á…É…Ñ½È­••ÁÌ¥¹±¥¹”±…‰•°éÙ…±Õ”Á…¥ÉÌ½¸½¹”±¥¹”(€€€€€€€Á…•}Ñ•áÐ€ôÍ½ÕÀ¹•Ñ}Ñ•áÐ¡Í•Á…É…Ñ½Èôˆ€ˆ¤(€€€€€€€´€ô}UA}QaQ}I¹Í•…É ¡Á…•}Ñ•áÐ¤(€€€€€€€¥˜´è(€€€€€€€€€€€É…Ü€ô´¹É½ÕÀ Ä¤(€€€€€€€€€€€ÕÁŒ€ô}½•É•}ÕÁŒ¡É…Ü¤(€€€€€€€€€€€¥˜ÕÁŒè(€€€€€€€€€€€€€€€±½•È¹¥¹™¼ (€€€€€€€€€€€€€€€€€€€˜‰mMÑÉ…Ñ•ä€ÉtUAÙ¥„€¡É••àÍ…¸¤ð€ˆ(€€€€€€€€€€€€€€€€€€€˜‰É…ÜõíÉ…Ü…ÉôðÕÁŒõíÕÁôˆ(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸ÕÁŒ(€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€±½•È¹‘•‰Õœ¡˜‰mMÑÉ…Ñ•ä€ÉtUAMÑÉ…Ñ•ä•ÉÉ½Èèí•ôˆ¤(((€€€€ŒƒŠRŠR MÑÉ…Ñ•äè•±•µ•¹Ðµ±•Ù•°ÑÉ…Ù•ÉÍ…°ƒŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠRŠR (€€€ÑÉäè(€€€€€€€™½È•°¥¸Í½ÕÀ¹™¥¹‘}…±°¡ÍÑÉ¥¹œõQÉÕ”¤è(€€€€€€€€€€€Ñ•áÐ€ô€¡•°½Èp‰pˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€¥˜¹½ÐÑ•áÐ½È±•¸¡Ñ•áÐ¤€ø€ÈÀÀè(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€¥˜¹½Ð}¥Í}ÕÁ}±…‰•°¡Ñ•áÐ¤è(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”((€€€€€€€€€€€±½•È¹‘•‰Õœ¡˜‰mMÑÉ…Ñ•ä€ÉtUAè™½Õ¹±…‰•°¹½‘”íÑ•áÐ…Éôˆ¤(€€€€€€€€€€€Á…É•¹Ð€ô•°¹Á…É•¹Ð(€€€€€€€€€€€¥˜Á…É•¹Ð¥Ì9½¹”è(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”((€€€€€€€€€€€€ŒÄè½±½¸µÍÁ±¥ÐÝ¥Ñ¡¥¸Ñ¡”Í…µ”Ñ•áÐ¹½‘”(€€€€€€€€€€€¥˜€ˆèˆ¥¸Ñ•áÐè(€€€€€€€€€€€€€€€…™Ñ•È€ôÑ•áÐ¹ÍÁ±¥Ð ˆèˆ°€Ä¥lÅt¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€ÕÁŒ€ô}½•É•}ÕÁŒ¡…™Ñ•È¹ÍÁ±¥Ð ¥³Ò’–bgFW"ç7Æ—B‚’VÇ6R" ¢–bæ÷BW3 ¢W2Òö6öW&6U÷W2†gFW"¢–bW3 ¢ÆövvW"æ–æfò€¢b%µ7G&FVw’%ÒU2f–#†6öÆöâ–âFW‡BæöFR’Â ¢b&Æ&VÃ×·FW‡B'ÒÂ&s×¶gFW"'ÒÂW3×·W7Ò ¢¢&WGW&âW0 ¢2##¢6öÆöâ×7Æ—Bv—F†–âF†R&VçBVÆVÖVçBw2gVÆÂFW‡@¢&VçE÷FW‡BÒ&VçBævWE÷FW‡B‡6W&F÷#Ò""Â7G&—ÕG'VR¢–b#¢"–â&VçE÷FW‡C ¢gFW"Ò&VçE÷FW‡Bç7Æ—B‚#¢"Â•³Òç7G&—‚¢2G'’f—'7Bv†—FW76RÖFVÆ–Ö—FVBFö¶Vâf—'7B†Ö÷7B&V6—6R¢f—'7E÷Fö¶VâÒgFW"ç7Æ—B‚•³Ò–bgFW"ç7Æ—B‚’VÇ6R" ¢W2Òö6öW&6U÷W2†f—'7E÷Fö¶Vâ’÷"ö6öW&6U÷W2†gFW"¢–bW3 ¢ÆövvW"æ–æfò€¢b%µ7G&FVw’%ÒU2f–#"†6öÆöâ–â&VçB’Â ¢b&Æ&VÃ×·FW‡B'ÒÂ&s×¶gFW"'ÒÂW3×·W7Ò ¢¢&WGW&âW0 ¢2#3¢æW‡B6–&Æ–ærVÆVÖVçB(	BfÆ–FFVB&Vf÷&R66WF–æp¢6–&Æ–ærÒ&VçBæf–æEöæW‡E÷6–&Æ–ær‚¢–b6–&Æ–ær—2æ÷BæöæS ¢fÂÒ6–&Æ–ærævWE÷FW‡B‡7G&—ÕG'VR¢W2Òö6öW&6U÷W2‡fÂ¢–bW3 ¢ÆövvW"æ–æfò€¢b%µ7G&FVw’%ÒU2f–#2†æW‡B6–&Æ–ær’Â ¢b&Æ&VÃ×·FW‡B'ÒÂ&s×·fÂ'ÒÂW3×·W7Ò ¢¢&WGW&âW0 ¢2#C¢w&æG&VçBw2æW‡B6–&Æ–ær(	BfÆ–FFV@¢–b&VçBç&VçB—2æ÷BæöæS ¢w÷6–"Ò&VçBç&VçBæf–æEöæW‡E÷6–&Æ–ær‚¢–bw÷6–"—2æ÷BæöæS ¢fÂÒw÷6–"ævWE÷FW‡B‡7G&—ÕG'VR¢W2Òö6öW&6U÷W2‡fÂ¢–bW3 ¢ÆövvW"æ–æfò€¢b%µ7G&FVw’%ÒU2f–#B†w&æG&VçB6–&Æ–ær’Â ¢b&Æ&VÃ×·FW‡B'ÒÂ&s×·fÂ'ÒÂW3×·W7Ò ¢¢&WGW&âW0 ¢2#S¢æW‡BFW‡BæöFR–âF†RFö7VÖVçB(	BfÆ–FFV@¢æW‡E÷7G"ÒVÂæf–æEöæW‡B‡7G&–æsÕG'VR¢–bæW‡E÷7G# ¢fÂÒæW‡E÷7G"ç7G&—‚¢W2Òö6öW&6U÷W2‡fÂ¢–bW3 ¢ÆövvW"æ–æfò€¢b%µ7G&FVw’%ÒU2f–#R†æW‡BFW‡BæöFR’Â ¢b&Æ&VÃ×·FW‡B'ÒÂ&s×·fÂ'ÒÂW3×·W7Ò ¢¢&WGW&âW0 ¢W†6WBW†6WF–öâ2S ¢ÆövvW"æFV'Vr†b%µ7G&FVw’%ÒU27G&FVw’"W'&÷#¢¶WÒ" ¢ÆövvW"æ–æfò‚%µ7G&FVw’%ÒU3¢æòfÇVRf÷VæB'’ç’7G&FVw’"¢&WGW&â"   ¦FVböf–æEöÆ&VÅ÷fÇVR‡6÷W¢&VWF–gVÅ6÷WÂÆ&VÅö¶W—v÷&G3¢Æ—7B’Óâ7G# ¢"" ¢66âf÷"Æ&VÎ(i'fÇVRGFW&â–â&öGV7B7V2F&ÆW2öFVf–æ—F–öâÆ—7G2à ¢†æFÆW3 ¢ÇFCåU3£Â÷FCãÇFCã#3CScsƒ“Â÷FCà¢Ç7â6Æ73Ò&Æ&VÂ#å6²6—¦SÂ÷7ããÇ7â6Æ73Ò'fÇVR#ãcÂ÷7ãà¢ÆGCä66R6³ÂöGCãÆFCã#ÂöFCà¢"" ¢G'“ ¢f÷"VÂ–â6÷Wæf–æEöÆÂ‡7G&–æsÕG'VR“ ¢G'“ ¢&rÒ†VÂ÷"""’ç7G&—‚¢–bæ÷B&r÷"ÆVâ‡&r’â# ¢6öçF–çVP¢2æ÷&ÖÆ—6S¢Æ÷vW&66RÂ6öÆÆ6R‡—†Vç2÷VæFW'66÷&W2Fò76RÀ¢27G&—G&–Æ–ær6öÆöã§76R6ò$•DTÒÔäó¢"ÖF6†W2&—FVÒæò ¢FW‡BÒ&Rç7V"‡"%µÂÕõÒ"Â""Â&ræÆ÷vW"‚’’ç'7G&—‚#¢"’ç7G&—‚¢–bæ÷Bç’†·rÓÒFW‡B÷"·r–âFW‡Bf÷"·r–âÆ&VÅö¶W—v÷&G2“ ¢6öçF–çVP ¢&VçBÒVÂç&Vç@¢–b&VçB—2æöæS ¢6öçF–çVP ¢2G'’F¦6VçB6–&Æ–ærVÆVÖVç@¢6–&Æ–ærÒ&VçBæf–æEöæW‡E÷6–&Æ–ær‚¢–b6–&Æ–ær—2æ÷BæöæS ¢fÂÒ6–&Æ–ærævWE÷FW‡B‡7G&—ÕG'VR¢–bfÂæBÆVâ‡fÂ’Â¢26æ—G’6 ¢&WGW&âfÀ ¢2G'’6öÆöâ×7Æ—Bv—F†–âF†R6ÖR&Vç@¢gVÆÂÒ&VçBævWE÷FW‡B‡6W&F÷#Ò""Â7G&—ÕG'VR¢–b#¢"–âgVÆÃ ¢'G2ÒgVÆÂç7Æ—B‚#¢"Â¢–bÆVâ‡'G2’ÓÒ"æB'G5³Òç7G&—‚“ ¢&WGW&â'G5³Òç7G&—‚ ¢W†6WBW†6WF–öã ¢6öçF–çVP¢W†6WBW†6WF–öã ¢70¢&WGW&â"   ¦FVböæ÷&ÖÆ—¦UöÖöæW•÷fÇVR‡&s¢7G"’Óâ7G# ¢–bæ÷B&s ¢&WGW&â" ¢ÒÒ&Rç6V&6‚‡"%ÂEÇ2¢…ÆEµÆBÅÒ¢ƒó¥ÂåÆG³'Ò“ò’"Â&r¢–bæ÷BÓ ¢&WGW&â" ¢&WGW&âb"G¶Òæw&÷Wƒ’ç&WÆ6R‚rÂrÂrr—Ò   ¦FVböÆöö·5öÆ–¶Uö'&öE÷vU÷&–6R‡&s¢7G"ÂVæ—E÷&–6S¢7G"Ò""Â&–6–æu÷Væ—C¢7G"Ò""’Óâ&ööÃ ¢fÇVRÒöæ÷&ÖÆ—¦UöÖöæW•÷fÇVR‡&r¢–bæ÷BfÇVS ¢&WGW&âfÇ6P¢–bfÇVRÓÒ"CSã"æBVæ—E÷&–6S ¢&WGW&âG'VP¢–b&–6–æu÷Væ—BæB&–6–æu÷Væ—BçWW"‚’–â²$T4‚"Â$T"Â%Tä•B'ÒæBfÇVRÒVæ—E÷&–6S ¢G'“ ¢'&öBÒfÆöB‡fÇVRç&WÆ6R‚"B"Â""’¢Væ—BÒfÆöB‚‡Væ—E÷&–6R÷"""’ç&WÆ6R‚"B"Â""’’–bVæ—E÷&–6RVÇ6Rã ¢–bVæ—BâæB'&öBãÒVæ—B¢S ¢&WGW&âG'VP¢W†6WBW†6WF–öã ¢&WGW&âfÇ6P¢&WGW&âfÇ6P  ¦FVböW‡G&7E÷Væ—E÷&–6R‡6÷W¢&VWF–gVÅ6÷W’ÓâGWÆU·7G"Â7G%Ó ¢"" ¢W‡G&7B6ÆVâVæ—B&–6RæB&–6–ærVæ—Bg&öÒF†RvRà ¢7G&FVw’¢&–6RF–W"F&ÆRGFW&â(	B#TÒC3rãST ¢7G&FVw’#¢Æ&VÎ(i'fÇVRf÷"¶æ÷vâVæ—B×&–6RÆ&VÇ2(	B%Væ—B&–6S¢C3rãS  ¢&WGW&ç2‡Væ—E÷&–6U÷7G"Â&–6–æu÷Væ—E÷7G"’(	B&÷F‚""–bæ÷Bf÷VæBà¢"" ¢G'“ ¢vU÷FW‡BÒ6÷WævWE÷FW‡B‡6W&F÷#Ò"" ¢27G&FVw’¢&–6RF–W"GFW&à¢ÒÒõ$”4UõD”U%õ$Rç6V&6‚‡vU÷FW‡B¢–bÓ ¢&–6U÷fÂÒÒæw&÷Wƒ’ç&WÆ6R‚"Â"Â""¢&–6–æu÷Væ—BÒ†Òæw&÷Wƒ"’÷"$T"’çWW"‚¢ÆövvW"æ–æfò†b%´FWF–ÅÒVæ—E÷&–6Rf–F–W"GFW&ã¢G·&–6U÷fÇÒò·&–6–æu÷Væ—GÒ"¢&WGW&âb"G·&–6U÷fÇÒ"Â&–6–æu÷Væ—@ ¢27G&FVw’#¢Æ&VÎ(i'fÇVP¢&rÒöf–æEöÆ&VÅ÷fÇVR‡6÷WÂõTä•Eõ$”4UôÄ$TÅ2¢–b&s ¢ÒÒõ$”4Uõ$Rç6V&6‚‡&r¢–bÓ ¢&–6U÷fÂÒÒæw&÷Wƒ’ç&WÆ6R‚"Â"Â""¢&–6–æu÷Væ—BÒ‡Òæw&÷Wƒ"’÷"""’çWW"‚¢ÆövvW"æ–æfò†b%´FWF–ÅÒVæ—E÷&–6Rf–Æ&VÃ¢G·&–6U÷fÇÒò·&–6–æu÷Væ—B'Ò"¢&WGW&âb"G·&–6U÷fÇÒ"Â&–6–æu÷Væ—@¢&WGW&â&rç7G&—‚’Â"  ¢W†6WBW†6WF–öâ2S ¢ÆövvW"æFV'Vr†b%´FWF–ÅÒöW‡G&7E÷Væ—E÷&–6RW'&÷#¢¶WÒ" ¢&WGW&â""Â&—¦UöÖöæW•÷fÇVR‡&r¢–bæ÷BfÇVS ¢&WGW&âfÇ6P¢–bfÇVRÓÒ"CSã"æBVæ—E÷&–6S ¢&WGW&âG'VP¢–b&–6–æu÷Væ—BæB&–6–æu÷Væ—BçWW"‚’–â²$T4‚"Â$T"Â%Tä•B'ÒæBfÇVRÒVæ—E÷&–6S ¢G'“ ¢'&öBÒfÆöB‡fÇVRç&WÆ6R‚"B"Â""’¢Væ—BÒfÆöB‚‡Væ—E÷&–6R÷"""’ç&WÆ6R‚"B"Â""’’–bVæ—E÷&–6RVÇ6Rã ¢–bVæ—BâæB'&öBãÒVæ—B¢S ¢&WGW&âG'VP¢W†6WBW†6WF–öã ¢&WGW&âfÇ6P¢&WGW&âfÇ6Q¥é•}µ½¹•å}Ù…±Õ”¡É…Ü¤(€€€¥˜¹½ÐÙ…±Õ”è(€€€€€€€É•ÑÕÉ¸…±Í”(€€€¥˜Ù…±Õ”€ôô€ˆÄÔÀÀ¸ÀÀˆ…¹Õ¹¥Ñ}ÁÉ¥”è(€€€€€€€É•ÑÕÉ¸QÉÕ”(€€€¥˜ÁÉ¥¥¹}Õ¹¥Ð…¹ÁÉ¥¥¹}Õ¹¥Ð¹ÕÁÁ•È ¤¥¸ì‰ ˆ°€‰ˆ°€‰U9%P‰ô…¹Ù…±Õ”€„ôÕ¹¥Ñ}ÁÉ¥”è(€€€€€€€ÑÉäè(€€€€€€€€€€€‰É½…€ô™±½…Ð¡Ù…±Õ”¹É•Á±…” ˆˆ°€ˆˆ¤¤(€€€€€€€€€€€Õ¹¥Ð€ô™±½…Ð ¡Õ¹¥Ñ}ÁÉ¥”½È€ˆˆ¤¹É•Á±…” ˆˆ°€ˆˆ¤¤¥˜Õ¹¥Ñ}ÁÉ¥”•±Í”€À¸À(€€€€€€€€€€€¥˜Õ¹¥Ð€ø€À…¹‰É½…€øôÕ¹¥Ð€¨€Ôè(€€€€€€€€€€€€€€€É•ÑÕÉ¸QÉÕ”(€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€É•ÑÕÉ¸…±Í”(€€€É•ÑÕÉ¸…±Í”