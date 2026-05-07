"""
strategies/playwright_catalog.py — Strategy 3: Playwright Rendered Catalog

Designed for authenticated catalog pages that require a logged-in browser
session and/or render products via JavaScript.

Entry point:
  run(state_file, listing_url)
    - state_file:   path to a Playwright storage-state JSON file produced by
                    browser_login.confirm_session().  Contains the authenticated
                    cookies / localStorage — NO live Playwright objects.
    - listing_url:  URL of the authenticated catalog/category page.

  Creates a brand-new sync_playwright() session in the CALLER'S thread using
  the saved state.  This avoids cross-thread greenlet errors entirely.

Card detection (the hard part):
  Tries CARD_SELECTORS in order.  For each selector that returns >= 2 elements
  it runs a sibling check via page.evaluate() to verify the elements share a
  common parent (strong signal they are individual cards, not a wrapper
  containing all cards).  Prefers sibling-grouped selectors; falls back to
  count-only if no selector passes the sibling check.

  Logs EVERY selector and its count so the Flask console always shows the
  full picture, not just the winning selector.

Card extraction (_extract_card_fields):
  Purpose-built for card HTML fragments — does NOT reuse _extract_from_detail_page
  which targets full pages with JSON-LD / <h1> / meta tags.  Pulls product fields
  directly from class-name heuristics, data attributes, and text patterns that
  are typical of catalog grid cards.

Phase labels logged:
  "reopen authenticated context"  — launching fresh browser from state file
  "begin scrape"                  — navigating to listing URL and crawling

Crash safety:
  - All per-page and per-product operations wrapped in try/except.
  - Playwright timeouts caught and logged; extraction continues.
  - Browser is always closed in a finally block.
"""

import logging
import json
import os
import re
from bs4 import BeautifulSoup
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from scraper import HEADERS, _DYNAMIC_EXPANSION_SELECTORS, _expand_dynamic_catalog, extract_products
from strategies.detail import (
    _collect_product_links,
    _extract_from_detail_page,
    _find_next_page,
    _find_label_value,
)

logger = logging.getLogger(__name__)
LAST_CRAWL_DIAGNOSTICS: dict = {}

# Strategy metadata
ID   = 3
NAME = "Playwright Rendered Catalog"

def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "").strip())
        return value if value > 0 else default
    except Exception:
        return default


# Hard caps. Defaults favor full catalog coverage; override with env vars for
# benchmarks or when a supplier has an unusually large catalog. The generic
# SCRAPEBUDDY_BROWSER_* names apply to public and authenticated browser crawls;
# legacy SCRAPEBUDDY_AUTH_* names remain as fallbacks.
MAX_PAGES = _positive_int_env(
    "SCRAPEBUDDY_BROWSER_LISTING_LIMIT",
    _positive_int_env("SCRAPEBUDDY_AUTH_LISTING_LIMIT", 100),
)
RENDER_WAIT_MS = _positive_int_env(
    "SCRAPEBUDDY_BROWSER_RENDER_WAIT_MS",
    _positive_int_env("SCRAPEBUDDY_AUTH_RENDER_WAIT_MS", 4_000),
)
DETAIL_PAGE_LIMIT = _positive_int_env(
    "SCRAPEBUDDY_BROWSER_DETAIL_LIMIT",
    _positive_int_env("SCRAPEBUDDY_AUTH_DETAIL_LIMIT", 2_000),
)
AUTH_DETAIL_ENRICH_LIMIT = _positive_int_env(
    "SCRAPEBUDDY_BROWSER_DETAIL_ENRICH_LIMIT",
    _positive_int_env("SCRAPEBUDDY_AUTH_DETAIL_ENRICH_LIMIT", DETAIL_PAGE_LIMIT),
)

GENERIC_ERROR_TITLES = {"oops.", "404", "page not found", "not found", "access denied"}

# Minimum visible cards before we start extracting.
# If fewer than this many are found after the full wait, we log a warning and
# proceed anyway (it may be a small catalog or a slow renderer).
MIN_CARDS_THRESHOLD = 2

# CSS selectors tried in order to find product card elements.
# _detect_cards tries all of them, logs counts for each, then picks the best.
# Ordering: most-specific patterns first to minimise false parent-wrapper hits.
CARD_SELECTORS = [
    # Explicit product card patterns
    "[class*='product-card']",
    "[class*='ProductCard']",
    "[class*='product-grid-item']",
    "[class*='product-grid__item']",
    "[class*='product-list-item']",
    "[class*='product-item']",
    "[class*='ProductItem']",
    "[class*='product-tile']",
    "[class*='product-row']",
    # Catalog / order-guide patterns (Sysco, Shamrock, etc.)
    "[class*='catalog-item']",
    "[class*='catalog-product']",
    "[class*='catalog-grid-item']",
    "[class*='orderguide-item']",
    "[class*='order-guide-item']",
    "[class*='order-item']",
    # Generic item/card patterns
    "[class*='item-card']",
    "[class*='search-result-item']",
    # Data attribute signals
    "[data-testid*='product']",
    "[data-product-id]",
    "[data-item-id]",
    # Structural fallbacks (used only if nothing above matches)
    "li[class*='product']",
    "li[class*='item']",
    "article",
]

# CSS selectors for pagination container elements (tried in order, first match used).
# Used by both page-count detection and numbered-page clicking.
_PAGINATION_CONTAINER_SELECTORS = [
    "[class*='pagination']",
    "[class*='Pagination']",
    "[class*='pager']",
    "[class*='page-nav']",
    "[class*='pages']",
    "[class*='paginate']",
    "nav[aria-label*='page' i]",
    "nav[aria-label*='pagination' i]",
    "[role='navigation'][aria-label*='page' i]",
]

# Matches "Showing 1–24 of 53 results", "1-24 of 53", "1 to 24 of 53"
_RESULT_COUNT_RE = re.compile(
    r"(?:showing\s+)?"
    r"([\d,]+)"                   # range start (e.g. "1")
    r"\s*[–\-\u2013to]+\s*"       # separator: –, -, to
    r"([\d,]+)"                   # range end   (e.g. "24")
    r"\s+of\s+"
    r"([\d,]+)",                  # total       (e.g. "53")
    re.IGNORECASE,
)
from strategies.product_quality import normalize_product
_SIMPLE_RESULT_TOTAL_RE = re.compile(
    r"\b([\d,]+)\s+(?:results?|items?|products?)\b",
    re.IGNORECASE,
)

# Matches "Page 1 of 3" style indicators
_PAGE_OF_TOTAL_RE = re.compile(
    r"page\s+\d+\s+of\s+(\d+)",
    re.IGNORECASE,
)

# Playwright selectors for "Next page" buttons (tried in order)
NEXT_BTN_SELECTORS = [
    "a[rel='next']",
    "link[rel='next']",
    "[aria-label='Next page']",
    "[aria-label='next page']",
    "[aria-label='Next']",
    "a:text-is('Next')",
    "a:text-is('next')",
    "button:text-is('Next')",
    "a:has-text('›')",
    "a:has-text('»')",
    "button:has-text('Next')",
    "button:has-text('Load More')",
    "button:has-text('Show More')",
]

# ── Price / SKU extraction constants ──────────────────────────────────────────

# Matches a dollar amount: "$23.36", "$23", "$1,234.56"
# Requires the $ sign so we don't match arbitrary numbers (e.g. image ids)
_PRICE_AMOUNT_RE = re.compile(r"\$\s*(\d[\d,]*(?:\.\d{1,2})?)")

# Unit abbreviations used in food-service / B2B catalogs (CS=case, EA=each, …)
_PRICE_UNIT_RE = re.compile(
    r"\b(CS|EA|LB|OZ|CT|PK|BX|BG|DZ|GAL|QT|PT|CASE|EACH|POUND)\b",
    re.IGNORECASE,
)

# Priority class fragments that indicate the *current* price sub-element
# (as opposed to strikethrough / original / compare prices)
_PRICE_PRIORITY_CLASSES = (
    "current-price", "sale-price", "unit-price",
    "price-value", "price-amount", "product-price",
)

# SKU: patterns whose presence in the candidate string means it is a
# DOM/CSS/autogenerated identifier, not a real supplier item number.
# Checked AFTER the underscore and alphanumeric guards so this is a backstop.
_BAD_SKU_RE = re.compile(
    r"product[\-]card|image|icon|btn[\-]|container|wrapper|tooltip|modal",
    re.IGNORECASE,
)

_API_URL_HINTS = (
    "api", "graphql", "search", "catalog", "category", "product", "products",
    "items", "browse", "listing", "facets",
)

_PRODUCT_OBJECT_KEYS = {
    "name", "title", "productname", "product_name", "description",
    "sku", "itemnumber", "item_number", "productid", "product_id",
    "id", "price", "brand", "image", "imageurl", "image_url",
    "url", "href", "producturl", "product_url", "upc", "ean", "gtin",
    "barcode",
}


# ── Page helpers ──────────────────────────────────────────────────────────────

def _wait_for_render(page) -> None:
    """
    Wait for the page to finish its initial JS render.
    Tries network-idle first (most accurate), falls back to a fixed pause.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
        return
    except Exception:
        pass
    try:
        page.wait_for_timeout(RENDER_WAIT_MS)
    except Exception:
        pass


def _wait_for_cards(page, min_count: int = MIN_CARDS_THRESHOLD) -> bool:
    """
    Wait until at least one card selector becomes visible.
    Returns True if any selector matched >= min_count elements, False otherwise.

    Tries the first 10 selectors with a short per-selector timeout so we
    don't spend too long waiting if the page has already rendered.
    """
    for selector in CARD_SELECTORS[:10]:
        try:
            page.wait_for_selector(selector, state="visible", timeout=4_000)
            # Confirm we actually have enough elements, not just 1
            elements = page.query_selector_all(selector)
            if len(elements) >= min_count:
                logger.debug(
                    f"[Strategy 3] Pre-extract wait: '{selector}' visible "
                    f"with {len(elements)} element(s)"
                )
                return True
        except Exception:
            continue
    return False


# ── Card detection ─────────────────────────────────────────────────────────────

def _check_siblings_js(page, selector: str) -> bool:
    """
    Return True if all elements matching selector share the same immediate
    parent element (i.e., they are siblings).

    Sibling-grouped elements are very likely individual product cards in a
    grid, not a parent wrapper that contains all the cards.

    Runs a small JS snippet via page.evaluate() — no Playwright element
    handles are shared across calls.
    """
    try:
        return page.evaluate(
            """(selector) => {
                const els = Array.from(document.querySelectorAll(selector));
                if (els.length < 2) return false;
                const parent = els[0].parentElement;
                if (!parent) return false;
                return els.every(el => el.parentElement === parent);
            }""",
            selector,
        )
    except Exception:
        return False


def _detect_cards(page, base_url: str) -> tuple:
    """
    Scan the rendered DOM for product card elements.

    Algorithm:
      1. Try every CARD_SELECTOR and record its match count.
      2. Log ALL counts at INFO level for diagnostics.
      3. Among selectors with count >= 2, prefer those where all elements
         share the same parent (sibling-grouped = almost certainly cards).
      4. If no sibling-grouped selector found, fall back to the selector with
         the highest count >= 2.
      5. Return (elements, winning_selector, count) or ([], None, 0).

    Logs a warning if the winning selector has count == 1 (possible wrapper).
    """
    # Step 1 & 2: collect counts for every selector
    counts = {}
    for selector in CARD_SELECTORS:
        try:
            elements = page.query_selector_all(selector)
            n = len(elements)
            counts[selector] = n
            if n > 0:
                logger.info(f"[Strategy 3] Selector '{selector}' → {n} element(s)")
        except Exception as e:
            counts[selector] = 0
            logger.debug(f"[Strategy 3] Selector '{selector}' error: {e}")

    # Selectors with at least 2 elements (prerequisite)
    candidates = {sel: n for sel, n in counts.items() if n >= 2}

    if not candidates:
        # Check if anything matched at all (even with count == 1)
        single_matches = [sel for sel, n in counts.items() if n == 1]
        if single_matches:
            logger.warning(
                f"[Strategy 3] Only single-element matches found — "
                f"likely matched a parent wrapper, not individual cards. "
                f"Selectors with 1 match: {single_matches[:5]}"
            )
        else:
            logger.warning(
                "[Strategy 3] No CARD_SELECTOR matched any elements. "
                "Page may not have rendered or uses unrecognised class names."
            )
        return [], None, 0

    # Step 3: prefer sibling-grouped selectors (elements share a common parent)
    sibling_candidates = {}
    for selector, n in candidates.items():
        if _check_siblings_js(page, selector):
            sibling_candidates[selector] = n
            logger.debug(
                f"[Strategy 3] '{selector}' → {n} sibling elements (same parent)"
            )

    pool = sibling_candidates if sibling_candidates else candidates

    # Step 4: pick the selector with the highest count from the pool
    winning_selector = max(pool, key=lambda s: pool[s])
    winning_count = pool[winning_selector]

    if not sibling_candidates:
        logger.warning(
            f"[Strategy 3] No sibling-grouped selector found. "
            f"Using '{winning_selector}' ({winning_count} elements) — "
            f"elements may not share a common parent; could be a partial wrapper match."
        )
    else:
        logger.info(
            f"[Strategy 3] Winning selector: '{winning_selector}' "
            f"({winning_count} sibling elements)"
        )

    # Fetch the actual ElementHandles for the winning selector
    try:
        elements = page.query_selector_all(winning_selector)
    except Exception as e:
        logger.error(f"[Strategy 3] Could not retrieve elements for '{winning_selector}': {e}")
        return [], None, 0

    return elements, winning_selector, len(elements)


# ── Card-level field extraction ────────────────────────────────────────────────

def _first_text_by_class(soup, *class_fragments) -> tuple:
    """
    Find the first element whose class contains any of the given fragments
    (case-insensitive substring).  Returns (text, matched_fragment) or ("", "").
    """
    for fragment in class_fragments:
        try:
            el = soup.find(
                lambda tag, _f=fragment: tag.name not in ("script", "style", "head")
                and any(_f in cls.lower() for cls in (tag.get("class") or []))
            )
            if el is not None:
                text = el.get_text(separator=" ", strip=True)
                if text:
                    return text, fragment
        except Exception:
            continue
    return "", ""


def _is_valid_sku(raw: str) -> bool:
    """
    Return True if raw looks like a genuine supplier SKU / item number.

    Rejects:
      - strings containing underscores (DOM/CSS naming convention)
      - strings longer than 25 chars (autogenerated ids tend to be long)
      - strings with no digits (pure letter strings are labels, not SKUs)
      - strings that don't match [AlphaNum][AlphaNum-./]* (reject spaces, etc.)
      - strings matching known autogenerated patterns (image ids, btn ids, etc.)
    """
    if not raw:
        return False
    raw = raw.strip()
    if len(raw) < 3 or len(raw) > 25:
        return False
    if "_" in raw:                                     # CSS/DOM naming
        return False
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-\.\/]*", raw):
        return False
    if not re.search(r"\d", raw):                      # must contain a digit
        return False
    if _BAD_SKU_RE.search(raw):                        # known bad patterns
        return False
    return True


def _extract_price_clean(root) -> str:
    """
    Extract the primary price value from a card fragment.

    Root cause of "$23.36CS$24.59" bug:
      get_text(strip=True) concatenates sibling spans with no separator.
      A price container like:
        <div class="price"><span>$23.36</span><span>CS</span><span>$24.59</span></div>
      produces "$23.36CS$24.59" with strip=True but "$23.36 CS $24.59" with separator=" ".
      This function always uses separator=" " then extracts the FIRST dollar amount.

    Strategy:
      1. Find price sub-element by priority class → walk UP to its parent to
         capture sibling unit spans (e.g. "CS" lives next to the dollar value).
      2. Find a generic price container (class contains "price", "cost", "amount").
      3. Full-card text scan as last resort.

    Returns "$23.36", "$23.36 CS", or "" — never a concatenated multi-price string.
    """
    def _parse(text: str) -> str:
        """Extract first $amount and optional trailing unit from text."""
        m = _PRICE_AMOUNT_RE.search(text)
        if not m:
            return ""
        price = m.group(0).strip()
        # Check up to 12 chars after the amount for a unit abbreviation
        after = text[m.end(): m.end() + 12].strip()
        um = _PRICE_UNIT_RE.match(after)
        if um:
            price = f"{price} {um.group(1).upper()}"
        return price

    # 1. Priority class: find the specific price element, then use its PARENT's
    #    text so sibling unit spans (e.g. <span class="price-unit">CS</span>)
    #    are included in the parsed text.
    for cls in _PRICE_PRIORITY_CLASSES:
        try:
            el = root.find(
                lambda tag, _c=cls: tag.name not in ("script", "style", "head")
                and any(_c in c.lower() for c in (tag.get("class") or []))
            )
            if el is not None:
                # Use parent container text to capture sibling unit/currency spans
                container = el.parent if (el.parent and el.parent != root) else el
                p = _parse(container.get_text(separator=" ", strip=True))
                if p:
                    return p
        except Exception:
            continue

    # 2. Generic price container
    for cls in ("price", "cost", "amount"):
        try:
            el = root.find(
                lambda tag, _c=cls: tag.name not in ("script", "style", "head")
                and any(_c in c.lower() for c in (tag.get("class") or []))
            )
            if el is not None:
                p = _parse(el.get_text(separator=" ", strip=True))
                if p:
                    return p
        except Exception:
            continue

    # 3. Full card text (last resort)
    try:
        return _parse(root.get_text(separator=" ", strip=True))
    except Exception:
        return ""


def _extract_card_fields(card_html: str, base_url: str) -> dict:
    """
    Extract product fields from a single card element's inner HTML.

    PURPOSE-BUILT for catalog card fragments.  Does NOT reuse
    _extract_from_detail_page (full pages with JSON-LD / <h1> / meta tags).

    Returns a dict with all product fields plus a "_sources" key (dict of
    field → method used) for debug logging.  _sources is stripped before
    the dict is returned to the caller; it's only used inside this function.
    """
    soup = BeautifulSoup(f"<div>{card_html}</div>", "html.parser")
    root = soup.find("div")
    if root is None:
        return {}

    sources = {}   # tracks which method found each field (for debug logging)

    # ── product_name ──────────────────────────────────────────────────────────
    product_name = ""
    # Heading tags: most catalog grids use h2/h3 for product name
    for tag in ("h1", "h2", "h3", "h4", "h5"):
        el = root.find(tag)
        if el is not None:
            product_name = el.get_text(strip=True)
            if product_name:
                sources["product_name"] = tag
                break
    # Class-name heuristics
    if not product_name:
        product_name, frag = _first_text_by_class(
            root,
            "product-name", "productname", "item-name", "itemname",
            "product-title", "producttitle", "card-title",
        )
        if product_name:
            sources["product_name"] = f"class:{frag}"
    # aria-label on the card's main link
    if not product_name:
        try:
            a = root.find("a", attrs={"aria-label": True})
            if a is not None:
                product_name = (a.get("aria-label") or "").strip()
                if product_name:
                    sources["product_name"] = "aria-label"
        except Exception:
            pass

    # ── brand ─────────────────────────────────────────────────────────────────
    brand, frag = _first_text_by_class(root, "brand", "vendor", "manufacturer")
    if brand:
        sources["brand"] = f"class:{frag}"

    # ── price ─────────────────────────────────────────────────────────────────
    # Uses _extract_price_clean which adds separator=" " to avoid concatenation.
    price = _extract_price_clean(root)
    if price:
        sources["price"] = "price_clean"

    # ── sku / item number ─────────────────────────────────────────────────────
    # All candidates go through _is_valid_sku before being accepted.
    sku = ""

    # 1. Validated data attributes (data-sku, data-product-id, etc.)
    #    Note: data-id is intentionally NOT in this list — Sysco and other React
    #    apps use data-id for DOM elements like images (e.g., data-id="product_card_image_6902340").
    for attr in ("data-sku", "data-product-id", "data-supc",
                 "data-item-number", "data-product-code"):
        try:
            el = root.find(attrs={attr: True})
            if el is not None:
                candidate = str(el.get(attr) or "").strip()
                if _is_valid_sku(candidate):
                    sku = candidate
                    sources["sku"] = f"attr:{attr}"
                    break
        except Exception:
            continue

    # 2. Class-name heuristics (validated)
    if not sku:
        for cls in ("supc", "item-number", "itemnumber", "item-id",
                    "product-id", "sku", "product-code", "catalog-number"):
            try:
                el = root.find(
                    lambda tag, _c=cls: tag.name not in ("script", "style", "head")
                    and any(_c in c.lower() for c in (tag.get("class") or []))
                )
                if el is not None:
                    candidate = el.get_text(strip=True)
                    if _is_valid_sku(candidate):
                        sku = candidate
                        sources["sku"] = f"class:{cls}"
                        break
            except Exception:
                continue

    # 3. Label→value text scan ("Item #: 12345", "SUPC: 4897613", etc.)
    if not sku:
        candidate = _find_label_value(root, [
            "sku", "item #", "item no", "item number", "item id",
            "part number", "part #", "product #",
            "supc", "product code", "catalog #",
        ])
        if candidate and _is_valid_sku(candidate):
            sku = candidate
            sources["sku"] = "label_value"

    # ── pack_size ─────────────────────────────────────────────────────────────
    pack_size, frag = _first_text_by_class(
        root, "pack-size", "packsize", "pack-info", "pack-qty", "unit-size",
    )
    if not pack_size:
        pack_size = _find_label_value(root, [
            "pack size", "pack qty", "pack quantity",
            "units per pack", "qty per pack",
            "pack of", "per pack", "pack/size",
        ])

    # ── case_pack ─────────────────────────────────────────────────────────────
    case_pack = _find_label_value(root, [
        "case pack", "case qty", "case quantity",
        "units per case", "qty per case",
        "inner pack", "master pack",
    ])

    # ── image_url ─────────────────────────────────────────────────────────────
    image_url = ""
    try:
        for img in root.find_all("img"):
            for attr in ("src", "data-src", "data-lazy-src", "data-original"):
                val = (img.get(attr) or "").strip()
                if val and not val.startswith("data:"):
                    image_url = urljoin(base_url, val)
                    break
            if image_url:
                break
    except Exception:
        pass

    # ── product_url ───────────────────────────────────────────────────────────
    product_url = ""
    try:
        for a in root.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if href and not href.startswith("#") and not href.startswith("javascript"):
                product_url = urljoin(base_url, href)
                sources["product_url"] = "a[href]"
                break
    except Exception:
        pass

    return {
        "product_name": product_name,
        "brand":        brand,
        "price":        price,
        "sku":          sku,
        "pack_size":    pack_size,
        "case_pack":    case_pack,
        "image_url":    image_url,
        "product_url":  product_url,
        "upc":          "",
        "_sources":     sources,    # stripped by _extract_card_product before returning
    }


def _extract_card_product(element, base_url: str, index: int = 0) -> dict:
    """
    Extract product fields from a single card ElementHandle.

    Calls _extract_card_fields, logs per-field sources at DEBUG level,
    then strips the internal _sources key before returning.
    """
    try:
        html    = element.inner_html()
        product = _extract_card_fields(html, base_url)

        # Log which extraction method found each field (debug only — too verbose for INFO)
        sources = product.pop("_sources", {})
        logger.debug(
            f"[Strategy 3] Card #{index}: "
            f"name={product.get('product_name', '')[:40]!r} | "
            f"price={product.get('price', '')!r} | "
            f"sku={product.get('sku', '')!r} | "
            f"sources={sources}"
        )
        return product

    except Exception as e:
        logger.debug(f"[Strategy 3] Card #{index} extraction error: {e}")
        return {}


def _lookup_any(obj: dict, names: tuple[str, ...]) -> str:
    lowered = {str(k).lower().replace("-", "").replace("_", ""): v for k, v in obj.items()}
    for name in names:
        key = name.lower().replace("-", "").replace("_", "")
        value = lowered.get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            value = value.get("name") or value.get("url") or value.get("src")
        if isinstance(value, list):
            value = next((v for v in value if isinstance(v, (str, int, float))), "")
        text = str(value or "").strip()
        if text and text.lower() not in {"none", "null", "false"}:
            return text
    return ""


def _api_object_to_product(obj: dict, base_url: str) -> dict:
    if not isinstance(obj, dict):
        return {}
    product_url = _lookup_any(
        obj,
        ("product_url", "productUrl", "url", "href", "canonicalUrl", "pdpUrl", "detailUrl"),
    )
    if product_url:
        product_url = urljoin(base_url, product_url)
    image_url = _lookup_any(
        obj,
        ("image_url", "imageUrl", "image", "thumbnail", "thumbnailUrl", "src"),
    )
    if image_url:
        image_url = urljoin(base_url, image_url)
    price = _lookup_any(obj, ("price", "salePrice", "unitPrice", "currentPrice"))
    if price and not price.startswith("$") and re.fullmatch(r"\d+(?:\.\d{1,2})?", price):
        price = f"${price}"
    product = {
        "product_name": _lookup_any(obj, ("product_name", "productName", "name", "title", "description")),
        "brand": _lookup_any(obj, ("brand", "brandName", "manufacturer", "vendor")),
        "sku": _lookup_any(obj, ("sku", "itemNumber", "itemNo", "itemId", "productId", "id")),
        "upc": _lookup_any(obj, ("upc", "gtin", "gtin12", "gtin13", "gtin14", "ean", "barcode")),
        "price": price,
        "pack_size": _lookup_any(obj, ("packSize", "pack_size", "size")),
        "case_pack": _lookup_any(obj, ("casePack", "case_pack", "caseQty", "unitsPerCase")),
        "image_url": image_url,
        "product_url": product_url,
    }
    if not (product["product_name"] or product["product_url"] or product["sku"]):
        return {}
    return normalize_product(product)


def _walk_api_payload(payload, base_url: str, products: list[dict], urls: list[str], *, limit: int = 3000) -> None:
    if len(products) >= limit:
        return
    if isinstance(payload, dict):
        keys = {str(k).lower().replace("-", "").replace("_", "") for k in payload.keys()}
        if len(keys & _PRODUCT_OBJECT_KEYS) >= 2:
            product = _api_object_to_product(payload, base_url)
            if product:
                products.append(product)
                url = product.get("product_url")
                if url and _looks_like_product_detail_url(url):
                    urls.append(url)
        for value in payload.values():
            if isinstance(value, (dict, list)):
                _walk_api_payload(value, base_url, products, urls, limit=limit)
            elif isinstance(value, str):
                found_url = urljoin(base_url, value)
                if _looks_like_product_detail_url(found_url):
                    urls.append(found_url)
    elif isinstance(payload, list):
        for item in payload:
            if len(products) >= limit:
                break
            _walk_api_payload(item, base_url, products, urls, limit=limit)


def _attach_network_product_capture(page, base_url: str) -> dict:
    capture = {"products": [], "urls": [], "seen_urls": set(), "seen_products": set()}

    def on_response(response):
        try:
            url = response.url or ""
            if not any(hint in url.lower() for hint in _API_URL_HINTS):
                return
            headers = response.headers or {}
            content_type = (headers.get("content-type") or headers.get("Content-Type") or "").lower()
            if "json" not in content_type and "graphql" not in url.lower():
                return
            payload = response.json()
            products: list[dict] = []
            urls: list[str] = []
            _walk_api_payload(payload, base_url, products, urls)

            new_products = 0
            for product in products:
                key = (
                    product.get("product_url")
                    or product.get("sku")
                    or f"{product.get('product_name')}|{product.get('price')}"
                )
                if not key or key in capture["seen_products"]:
                    continue
                capture["seen_products"].add(key)
                capture["products"].append(product)
                new_products += 1

            new_urls = 0
            for found_url in urls:
                if found_url in capture["seen_urls"]:
                    continue
                capture["seen_urls"].add(found_url)
                capture["urls"].append(found_url)
                new_urls += 1

            if new_products or new_urls:
                logger.info(
                    f"[Strategy 3] API capture: +{new_products} product object(s), "
                    f"+{new_urls} product URL(s) from {url[:120]}"
                )
        except Exception:
            return

    try:
        page.on("response", on_response)
    except Exception as e:
        logger.debug(f"[Strategy 3] API capture unavailable: {e}")
    return capture


def _extract_embedded_json_products(html: str, base_url: str) -> tuple[list[dict], list[str]]:
    """
    Extract products from JSON blobs embedded in the rendered page.

    This catches Next.js/Nuxt/Hydrogen/etc. pages where the product data is
    present in script tags even if the card DOM is sparse or virtualized.
    """
    products: list[dict] = []
    urls: list[str] = []
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        for script in soup.find_all("script"):
            script_type = (script.get("type") or "").lower()
            script_id = (script.get("id") or "").lower()
            if (
                "json" not in script_type
                and script_id not in {"__next_data__", "__nuxt_data__"}
            ):
                continue
            text = (script.string or script.get_text() or "").strip()
            if not text or len(text) > 5_000_000:
                continue
            if not (text.startswith("{") or text.startswith("[")):
                continue
            try:
                payload = json.loads(text)
            except Exception:
                continue
            before_products = len(products)
            before_urls = len(urls)
            _walk_api_payload(payload, base_url, products, urls)
            if len(products) > before_products or len(urls) > before_urls:
                logger.info(
                    f"[Strategy 3] Embedded JSON capture: +{len(products) - before_products} "
                    f"product object(s), +{len(urls) - before_urls} product URL(s)"
                )
    except Exception as e:
        logger.debug(f"[Strategy 3] Embedded JSON capture failed: {e}")
    return products, urls


# ── Pagination helpers ────────────────────────────────────────────────────────

def _follow_next_page(page, visited: set, base_netloc: str) -> bool:
    """
    Detect and navigate to the next pagination page.

    Strategy:
      1. Use existing _find_next_page() on rendered HTML (URL-based).
      2. If not found, try clicking a visible "Next" button (SPA-style).

    Returns True if navigation succeeded, False if no next page.
    """
    current_url = page.url

    # ── URL-based next ────────────────────────────────────────────────────────
    try:
        html     = page.content()
        soup     = BeautifulSoup(html, "html.parser")
        next_url = _find_next_page(soup, current_url, visited, base_netloc)
        if next_url:
            logger.info(f"[Strategy 3] URL-based pagination → {next_url}")
            visited.add(next_url)
            page.goto(next_url, wait_until="domcontentloaded", timeout=30_000)
            _wait_for_render(page)
            return True
    except Exception as e:
        logger.debug(f"[Strategy 3] URL-based pagination check error: {e}")

    # ── Click-based next (SPA pagination) ─────────────────────────────────────
    for selector in NEXT_BTN_SELECTORS:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible() and btn.is_enabled():
                href = btn.get_attribute("href") or ""
                if href:
                    abs_href = urljoin(current_url, href)
                    if abs_href in visited:
                        continue
                    visited.add(abs_href)

                logger.info(f"[Strategy 3] Click-based pagination via '{selector}'")
                btn.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    page.wait_for_timeout(RENDER_WAIT_MS)

                new_url = page.url
                if new_url == current_url and not href:
                    logger.info(
                        "[Strategy 3] URL unchanged after click — "
                        "may be end of pagination or dynamic content loaded"
                    )
                return True
        except Exception as e:
            logger.debug(
                f"[Strategy 3] Click-based pagination '{selector}' error: {e}"
            )
            continue

    return False


# ── Pagination helpers (numbered-page / SPA style) ────────────────────────────

def _get_content_fingerprint(page, selector: str | None) -> str:
    """
    Return a short string that represents the current product grid content.

    Built from the text of the first 3 elements matching `selector`.
    Used to confirm that a pagination click actually changed the displayed items
    (prevents silent infinite loops when clicks land on the same page).

    Returns "" if selector is None or the page evaluation fails.
    """
    if not selector:
        return ""
    try:
        return page.evaluate(
            """(sel) => {
                const els = Array.from(document.querySelectorAll(sel));
                return els.slice(0, 3)
                          .map(el => (el.textContent || '').trim().slice(0, 100))
                          .join('||');
            }""",
            selector,
        ) or ""
    except Exception:
        return ""


def _candidate_page_urls(current_url: str, target_page: int) -> list[str]:
    """
    Build conservative page-N URL candidates for catalogs that expose a result
    count but do not render clickable pagination controls until interaction.
    """
    parsed = urlparse(current_url)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query_keys = {key.lower() for key, _ in query_pairs}
    candidates: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        if url and url != current_url and url not in seen:
            seen.add(url)
            candidates.append(url)

    page_params = (
        "page", "p", "pg", "pageNumber", "page_number",
        "currentPage", "current_page", "pageIndex", "page_index",
    )
    for param in page_params:
        updated = [(key, value) for key, value in query_pairs if key != param]
        updated.append((param, str(target_page)))
        add(urlunparse(parsed._replace(query=urlencode(updated, doseq=True))))

    for key, value in query_pairs:
        if key.lower() in {"perpage", "per_page", "pagesize", "page_size", "limit", "count"}:
            try:
                per_page = int(value)
            except Exception:
                continue
            if per_page <= 0:
                continue
            offset = (target_page - 1) * per_page
            for offset_key in ("offset", "start", "skip"):
                updated = [(k, v) for k, v in query_pairs if k != offset_key]
                updated.append((offset_key, str(offset)))
                add(urlunparse(parsed._replace(query=urlencode(updated, doseq=True))))

    if not query_keys:
        add(urlunparse(parsed._replace(query=urlencode({"page": target_page}))))

    path = parsed.path.rstrip("/")
    if re.search(r"/page/\d+$", path, re.IGNORECASE):
        add(urlunparse(parsed._replace(path=re.sub(r"/page/\d+$", f"/page/{target_page}", path, flags=re.IGNORECASE))))
    else:
        add(urlunparse(parsed._replace(path=f"{path}/page/{target_page}")))

    return candidates[:16]


def _try_candidate_page_urls(
    page,
    target_page: int,
    visited: set,
    base_netloc: str,
    previous_fingerprint: str,
    winning_selector: str | None,
) -> bool:
    current_url = page.url
    for candidate in _candidate_page_urls(current_url, target_page):
        parsed = urlparse(candidate)
        if parsed.netloc and parsed.netloc != base_netloc:
            continue
        if candidate in visited:
            continue
        try:
            logger.info(f"[Strategy 3] URL-pattern pagination probe -> {candidate}")
            page.goto(candidate, wait_until="domcontentloaded", timeout=30_000)
            _wait_for_render(page)
            new_fingerprint = _get_content_fingerprint(page, winning_selector)
            html = page.content()
            links = _collect_product_links(html, candidate)
            if (
                links
                and (
                    not previous_fingerprint
                    or not new_fingerprint
                    or new_fingerprint != previous_fingerprint
                )
            ):
                visited.add(candidate)
                logger.info(
                    f"[Strategy 3] URL-pattern pagination accepted page {target_page}: "
                    f"{len(links)} product link(s)"
                )
                return True
        except Exception as e:
            logger.debug(f"[Strategy 3] URL-pattern pagination probe failed: {e}")

    try:
        if page.url != current_url:
            page.goto(current_url, wait_until="domcontentloaded", timeout=30_000)
            _wait_for_render(page)
    except Exception:
        pass
    return False


def _harvest_product_links_while_scrolling(
    page,
    current_url: str,
    *,
    max_rounds: int = 30,
) -> list[str]:
    """
    Collect product links from dynamic/virtualized lists while moving through
    the page. Some SPAs only keep the current viewport in the DOM, so waiting
    until the end and reading page.content() can miss earlier/later products.
    """
    found: list[str] = []
    seen: set[str] = set()
    stable_rounds = 0
    previous_signature: tuple[int, int, int] | None = None

    for round_no in range(max_rounds + 1):
        try:
            html = page.content()
            for link in _collect_product_links(html, current_url):
                if link not in seen:
                    seen.add(link)
                    found.append(link)
        except Exception as e:
            logger.debug(f"[Strategy 3] Scroll harvest content error: {e}")

        try:
            signature = page.evaluate(
                """() => [
                    window.scrollY || document.documentElement.scrollTop || 0,
                    document.body.scrollHeight || document.documentElement.scrollHeight || 0,
                    document.querySelectorAll('a[href*="/product"], a[href*="/products"], a[href*="/item"], a[href*="/p/"]').length
                ]"""
            )
            signature = tuple(int(x or 0) for x in signature)
        except Exception:
            signature = (0, 0, len(found))

        if previous_signature == signature:
            stable_rounds += 1
        else:
            stable_rounds = 0
            previous_signature = signature

        if round_no >= max_rounds or stable_rounds >= 4:
            break

        clicked = False
        for selector in _DYNAMIC_EXPANSION_SELECTORS:
            try:
                button = page.query_selector(selector)
                if button and button.is_visible() and button.is_enabled():
                    button.scroll_into_view_if_needed()
                    button.click()
                    clicked = True
                    logger.info(f"[Strategy 3] Scroll harvest clicked '{selector}'")
                    break
            except Exception:
                continue

        try:
            page.evaluate(
                """() => {
                    const step = Math.max(450, Math.floor(window.innerHeight * 0.85));
                    window.scrollBy(0, step);
                }"""
            )
            page.wait_for_timeout(900 if clicked else 550)
        except Exception:
            pass

    if found:
        logger.info(
            f"[Strategy 3] Scroll harvest collected {len(found)} unique product link(s)"
        )
    return found


def _detect_max_page_button(page) -> int | None:
    """
    Scan the rendered page for numbered pagination buttons and return the
    highest page number found.

    Searches only inside recognised pagination containers, and requires at
    least TWO numeric buttons (e.g. "2" and "3") before trusting the result
    — a single stray number elsewhere on the page should not trigger this.

    Returns the max page number (int) or None if not detected.
    """
    try:
        return page.evaluate(
            """() => {
                const pagSels = [
                    '[class*="pagination"]', '[class*="Pagination"]',
                    '[class*="pager"]', '[class*="page-nav"]',
                    '[class*="pages"]', '[class*="paginate"]',
                    'nav[aria-label*="pag" i]',
                    '[role="navigation"][aria-label*="pag" i]',
                ];
                let container = null;
                for (const sel of pagSels) {
                    container = document.querySelector(sel);
                    if (container) break;
                }
                if (!container) return null;

                // Collect numeric texts from interactive elements only
                const interactive = Array.from(
                    container.querySelectorAll('a, button')
                );
                const nums = [];
                for (const el of interactive) {
                    const text = (el.textContent || '').trim();
                    if (/^\\d+$/.test(text)) {
                        const n = parseInt(text, 10);
                        if (n >= 1 && n <= 999) nums.push(n);
                    }
                }
                // Require at least 2 numeric buttons to be confident it is pagination
                return nums.length >= 2 ? Math.max(...nums) : null;
            }"""
        )
    except Exception as e:
        logger.debug(f"[Strategy 3] _detect_max_page_button error: {e}")
        return None


def _detect_pagination_info(page) -> dict:
    """
    Detect the total number of pages and total product count from the rendered
    page, combining two independent signals:

    Signal A — result-count text (e.g. "Showing 1–24 of 53 results"):
        Calculates total_pages = ceil(total_products / per_page).

    Signal B — numbered page buttons in a pagination control:
        Reads the highest numeric button text inside a pagination container.

    Returns:
        {
            "total_products": int | None,
            "per_page":       int | None,
            "total_pages":    int | None,
        }
    If both signals disagree, the larger value is used (safer — we'd rather
    scrape one extra empty page than miss pages).
    """
    import math

    info = {"total_products": None, "per_page": None, "total_pages": None}

    # ── Signal A: result-count text ───────────────────────────────────────────
    try:
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator=" ")

        m = _RESULT_COUNT_RE.search(text)
        if m:
            start   = int(m.group(1).replace(",", ""))
            end     = int(m.group(2).replace(",", ""))
            total   = int(m.group(3).replace(",", ""))
            per_pg  = end - start + 1
            if per_pg > 0 and total > 0:
                info["total_products"] = total
                info["per_page"]       = per_pg
                info["total_pages"]    = math.ceil(total / per_pg)
                logger.info(
                    f"[Strategy 3] Result-count text: "
                    f"{start}–{end} of {total} → "
                    f"{info['total_pages']} page(s)"
                )

        # Fallback: "Page 1 of 3" style
        if info["total_pages"] is None:
            m2 = _PAGE_OF_TOTAL_RE.search(text)
            if m2:
                info["total_pages"] = int(m2.group(1))
                logger.info(
                    f"[Strategy 3] 'Page X of N' indicator: "
                    f"{info['total_pages']} page(s)"
                )

        if info["total_products"] is None:
            m3 = _SIMPLE_RESULT_TOTAL_RE.search(text)
            if m3:
                info["total_products"] = int(m3.group(1).replace(",", ""))
                logger.info(
                    f"[Strategy 3] Simple result count: "
                    f"{info['total_products']} product(s)"
                )
    except Exception as e:
        logger.debug(f"[Strategy 3] Result-count text detection error: {e}")

    # ── Signal B: numbered page buttons ───────────────────────────────────────
    try:
        max_btn = _detect_max_page_button(page)
        if max_btn is not None:
            logger.info(
                f"[Strategy 3] Numbered page buttons detected: "
                f"max page = {max_btn}"
            )
            # Use the larger of the two signals
            if info["total_pages"] is None or max_btn > info["total_pages"]:
                info["total_pages"] = max_btn
    except Exception as e:
        logger.debug(f"[Strategy 3] Page-button detection error: {e}")

    return info


def _click_numbered_page(page, target_page: int, winning_selector: str | None) -> bool:
    """
    Find and click the numbered pagination button for `target_page`.

    Approach 1 — Playwright element handles (preferred):
        Iterates over recognised pagination containers, finds the first
        visible interactive element (a, button, li, span) whose exact text
        is the target page number, and calls .click() on it.

    Approach 2 — JS evaluate fallback:
        If no Playwright element was found, runs a document.querySelectorAll
        over the whole page and clicks the first visible matching element
        via JavaScript.

    After clicking, waits for network-idle / render to settle.
    Returns True if a click was performed, False if no button was found.
    """
    logger.info(
        f"[Strategy 3] Numbered-page navigation: targeting page {target_page}"
    )
    target_str = str(target_page)

    # ── Approach 1: Playwright element handles ────────────────────────────────
    for container_sel in _PAGINATION_CONTAINER_SELECTORS:
        try:
            containers = page.query_selector_all(container_sel)
            if not containers:
                continue
            for container in containers:
                for tag in ("a", "button", "li", "span"):
                    items = container.query_selector_all(tag)
                    for item in items:
                        try:
                            text = (item.text_content() or "").strip()
                            attrs = " ".join(
                                item.get_attribute(name) or ""
                                for name in (
                                    "aria-label", "title", "data-page",
                                    "data-page-number", "data-testid", "value",
                                )
                            )
                            haystack = f"{text} {attrs}".strip()
                            exact_text = text == target_str
                            attr_match = re.search(
                                rf"(?:^|\b)(?:page\s*)?{re.escape(target_str)}(?:\b|$)",
                                haystack,
                                re.IGNORECASE,
                            )
                            if (exact_text or attr_match) and item.is_visible():
                                item.scroll_into_view_if_needed()
                                item.click()
                                try:
                                    page.wait_for_load_state(
                                        "networkidle", timeout=12_000
                                    )
                                except Exception:
                                    page.wait_for_timeout(RENDER_WAIT_MS)
                                logger.info(
                                    f"[Strategy 3] Clicked page {target_page} "
                                    f"button inside '{container_sel}'"
                                )
                                return True
                        except Exception:
                            continue
        except Exception as e:
            logger.debug(
                f"[Strategy 3] Paginator '{container_sel}' error: {e}"
            )

    # ── Approach 2: JS evaluate fallback ─────────────────────────────────────
    try:
        clicked = page.evaluate(
            """(target) => {
                const targetStr = String(target);
                const all = Array.from(
                    document.querySelectorAll('a, button, li, span')
                );
                const matches = (el) => {
                    const text = (el.textContent || '').trim();
                    if (text === targetStr) return true;
                    const attrs = [
                        'aria-label', 'title', 'data-page',
                        'data-page-number', 'data-testid', 'value'
                    ].map(name => el.getAttribute(name) || '').join(' ');
                    const haystack = `${text} ${attrs}`.trim().toLowerCase();
                    return haystack.split(/\\D+/).filter(Boolean).includes(targetStr);
                };
                for (const el of all) {
                    // offsetParent !== null means the element is visible
                    if (matches(el) && el.offsetParent !== null) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }""",
            target_page,
        )
        if clicked:
            try:
                page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                page.wait_for_timeout(RENDER_WAIT_MS)
            logger.info(
                f"[Strategy 3] JS-click fallback: page {target_page} clicked"
            )
            return True
    except Exception as e:
        logger.debug(f"[Strategy 3] JS click fallback error: {e}")

    logger.warning(
        f"[Strategy 3] Could not find page {target_page} button — "
        f"checked all pagination containers"
    )
    return False


# ── Main entry point ──────────────────────────────────────────────────────────

def _run_with_browser_context(
    listing_url: str,
    *,
    storage_state: str | None = None,
    context_label: str = "browser context",
) -> list:
    LAST_CRAWL_DIAGNOSTICS.clear()
    """
    Strategy 3 entry point.

    Creates a fresh Playwright browser/context from a saved session-state
    file, runs the catalog crawl entirely within this thread, then closes
    the browser.  No live Playwright objects are passed in.

    Args:
        listing_url:    URL of the catalog/category page.
        storage_state:  Optional Playwright storage-state JSON file.
        context_label:  Human-readable label for logs.

    Returns:
        List of product dicts (may be empty if nothing found).
        Never raises — all errors are caught and logged.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error(
            "[Strategy 3] Playwright is not installed. "
            "Run: pip install playwright && playwright install chromium"
        )
        return []

    logger.info(f"[Strategy 3] Phase: open {context_label}")

    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as e:
                logger.error(
                    f"[Strategy 3] reopen authenticated context — "
                    f"Chromium launch failed: {e}"
                )
                return []

            try:
                context_kwargs = {
                    "user_agent": HEADERS.get("User-Agent"),
                    "viewport": {"width": 1920, "height": 1080},
                }
                if storage_state:
                    context_kwargs["storage_state"] = storage_state
                context = browser.new_context(**context_kwargs)
            except Exception as e:
                logger.error(
                    f"[Strategy 3] open {context_label} — "
                    f"failed to create browser context: {e}"
                )
                try:
                    browser.close()
                except Exception:
                    pass
                return []

            page = context.new_page()
            try:
                logger.info(f"[Strategy 3] Phase: begin scrape at {listing_url}")
                return _run_inner(page, listing_url)
            except Exception as e:
                logger.error(f"[Strategy 3] begin scrape — unexpected error: {e}")
                return []
            finally:
                try:
                    page.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

    except Exception as e:
        logger.error(f"[Strategy 3] Playwright initialisation failed: {e}")
        return []


def run(state_file: str, listing_url: str) -> list:
    """
    Authenticated Strategy 3 entry point.

    Args:
        state_file:   Path to a Playwright storage-state JSON file from
                      browser_login.confirm_session().
        listing_url:  URL of the authenticated catalog/category page.
    """
    return _run_with_browser_context(
        listing_url,
        storage_state=state_file,
        context_label=f"authenticated context from {state_file}",
    )


def run_public(listing_url: str) -> list:
    """
    Public Strategy 3 entry point for JavaScript-heavy catalogs.

    Uses the same universal browser crawler as authenticated scrapes, but
    without a saved storage state. This keeps public JS catalogs on the path
    that can scroll, click pagination, capture API JSON, and emit crawl
    diagnostics.
    """
    return _run_with_browser_context(
        listing_url,
        storage_state=None,
        context_label="public browser context",
    )


def _looks_like_product_detail_url(url: str) -> bool:
    url = (url or "").strip().lower()
    if not url:
        return False
    if any(token in url for token in ["/customer/", "/search", "?product_list", "javascript:"]):
        return False
    if any(token in url for token in ["/g/", "/collections/", "/category", "/categories/", "filter="]):
        return False
    if any(token in url for token in (
        "/products/", "/product/", "/item/", "/p/", "/pd/", "/dp/",
        "product_detail", "productdetail", "product-detail",
        "itemdetail", "item-detail", "item_detail",
    )):
        return True
    if not url.endswith('.html'):
        return False

    # Category-like .html URLs can still appear in listing cards. Real product
    # detail pages are usually shallow slugs, while category-like pages contain
    # deeper taxonomy path segments.
    path = urlparse(url).path.strip('/')
    if not path:
        return False
    if '/' in path:
        return False
    return True


def _is_valid_detail_product(detail: dict) -> tuple[bool, str]:
    """Return (is_valid, reason) for authenticated detail enrichment results."""
    if not detail:
        return False, "empty detail result"

    name = (detail.get("product_name") or "").strip()
    sku = (detail.get("sku") or "").strip()
    price = (detail.get("price") or "").strip()
    upc = (detail.get("upc") or "").strip()
    image_url = (detail.get("image_url") or "").strip().lower()

    if name.lower() in GENERIC_ERROR_TITLES:
        return False, f"error title: {name}"
    if "logo" in image_url and not any([sku, price, upc]):
        return False, "site-level title without product fields"
    if not any([name, sku, price, upc]):
        return False, "no trusted product fields"
    return True, "ok"


def _looks_like_aggregate_product(product: dict) -> bool:
    """Reject search/category summary rows that are not actual products."""
    name = (product.get("product_name") or "").strip().lower()
    sku = (product.get("sku") or "").strip()
    upc = (product.get("upc") or "").strip()
    url = (product.get("product_url") or "").strip().lower()
    if not name:
        return False
    summary_patterns = [
        r"^\d[\d,]*\s+results?\s+for\b",
        r"^\d[\d,]*\s+items?\s+for\b",
        r"^\d[\d,]*\s+products?\s+for\b",
        r"^showing\s+\d[\d,]*\s*(?:-|to|–)\s*\d[\d,]*\s+of\s+\d[\d,]*",
    ]
    if any(re.search(pattern, name, re.IGNORECASE) for pattern in summary_patterns):
        return not (sku or upc)
    if " results for " in name and not (sku or upc):
        return True
    if url and not _looks_like_product_detail_url(url) and re.search(r"\b(results?|items?|products?)\b", name):
        return True
    return False


def _merge_listing_and_detail(listing: dict, detail: dict) -> dict:
    """
    Merge authenticated listing-card data with richer detail-page data.

    Preference order:
      - keep richer detail fields when present
      - preserve listing image/url/name if detail is unexpectedly sparse
      - always return a complete product dict shape for downstream UI/export
    """
    merged = {
        "product_name": detail.get("product_name") or listing.get("product_name") or "",
        "brand": detail.get("brand") or listing.get("brand") or "",
        "sku": detail.get("sku") or listing.get("sku") or "",
        "upc": detail.get("upc") or listing.get("upc") or "",
        "price": detail.get("price") or listing.get("price") or "",
        "pack_size": detail.get("pack_size") or listing.get("pack_size") or "",
        "case_pack": detail.get("case_pack") or listing.get("case_pack") or "",
        "image_url": detail.get("image_url") or listing.get("image_url") or "",
        "product_url": detail.get("product_url") or listing.get("product_url") or "",
        "unit_size": detail.get("unit_size") or listing.get("unit_size") or "",
        "unit_price": detail.get("unit_price") or listing.get("unit_price") or "",
        "pricing_unit": detail.get("pricing_unit") or listing.get("pricing_unit") or "",
        "bulk_price": detail.get("bulk_price") or listing.get("bulk_price") or "",
        "minimum_order_qty": detail.get("minimum_order_qty") or listing.get("minimum_order_qty") or "",
        "raw_price_text": detail.get("raw_price_text") or listing.get("raw_price_text") or "",
        "gtin_case": detail.get("gtin_case") or listing.get("gtin_case") or "",
        "ean": detail.get("ean") or listing.get("ean") or "",
        "gtin": detail.get("gtin") or listing.get("gtin") or "",
        "barcode_raw": detail.get("barcode_raw") or listing.get("barcode_raw") or "",
        "identifier_type": detail.get("identifier_type") or listing.get("identifier_type") or "",
    }
    return merged


def _select_detail_enrichment_links(
    all_product_links: list[str],
    valid_listing_urls: set[str],
) -> list[str]:
    """
    Choose detail URLs to visit after listing crawl.

    Product links collected from the rendered page are more authoritative than
    card-level extraction. Card extraction can collapse a grid into one wrapper
    row, but real hrefs are still useful detail targets.
    """
    detail_link_pool = [
        url for url in all_product_links
        if _looks_like_product_detail_url(url)
    ]
    return detail_link_pool or [url for url in all_product_links if url in valid_listing_urls] or list(valid_listing_urls)


def _set_last_crawl_diagnostics(**values) -> None:
    LAST_CRAWL_DIAGNOSTICS.clear()
    LAST_CRAWL_DIAGNOSTICS.update({k: v for k, v in values.items() if v not in (None, "", [])})


def _dedup_products(products: list) -> tuple:
    """
    Remove duplicate product rows using a 3-tier priority key.

    Priority 1 — product_url (exact match)
      Most reliable: same URL = definitely the same product.
    Priority 2 — normalised product_name + brand + price_digits
      Catches duplicates without URLs that appear on multiple pages.
    Priority 3 — image_url + product_name
      Last-resort for products where URL and name differ slightly but the
      image is the same (e.g. different page URLs for the same item).

    Returns (deduped_list, removed_count).
    Logs the count of removed duplicates at INFO level.
    """
    seen_urls      = set()
    seen_identity  = set()
    seen_img_name  = set()
    deduped        = []
    removed        = 0

    for p in products:
        url       = (p.get("product_url") or "").strip()
        name      = (p.get("product_name") or "").lower().strip()
        brand     = (p.get("brand") or "").lower().strip()
        price_d   = re.sub(r"[^\d.]", "", p.get("price") or "")
        img       = (p.get("image_url") or "").strip()

        is_dup = False

        # Priority 1: product_url
        if url:
            if url in seen_urls:
                is_dup = True
            else:
                seen_urls.add(url)

        # Priority 2: name + brand + price (even if we already handled via URL)
        if not is_dup and name:
            identity = f"{name}|{brand}|{price_d}"
            if identity in seen_identity:
                is_dup = True
            else:
                seen_identity.add(identity)

        # Priority 3: image + name
        if not is_dup and img and name:
            key = f"{img}|{name}"
            if key in seen_img_name:
                is_dup = True
            else:
                seen_img_name.add(key)

        if is_dup:
            removed += 1
        else:
            deduped.append(p)

    if removed:
        logger.info(
            f"[Strategy 3] Deduplication: {removed} duplicate row(s) removed, "
            f"{len(deduped)} unique product(s) remaining"
        )
    return deduped, removed


def _run_inner(page, listing_url: str) -> list:
    """
    Inner implementation — called by run() inside try/except.

    Pagination strategy (layered, in priority order per page transition):
      Layer 1 — URL-based next link (<a rel="next">, "Next" anchor text)
      Layer 2 — Click-based "Next" button (SPA pagination without href)
      Layer 3 — Numbered page button click (Sysco-style JS-rendered 1 2 3 buttons)

    After page 1 we attempt to read the total page count from:
      - Result-count text ("Showing 1–24 of 53 results")
      - "Page X of N" indicators
      - Highest numbered page button in a pagination control

    Knowing the total lets us loop precisely and log clearly ("Page 2 of 3").
    Without it, we continue until all three navigation layers return False.
    """
    base_netloc       = urlparse(listing_url).netloc
    visited_pages     = {listing_url}
    api_capture       = _attach_network_product_capture(page, listing_url)

    all_product_links: list    = []
    seen_links:        set     = set()
    all_card_products: list    = []

    # winning_selector from page 1 is used for content fingerprinting on later
    # pages — tracks which card selector worked best so far.
    winning_selector:  str | None = None

    logger.info(f"[Strategy 3] Navigating to: {listing_url}")
    try:
        page.goto(listing_url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        logger.error(f"[Strategy 3] Navigation failed: {e}")
        _set_last_crawl_diagnostics(
            expected_products=None,
            expected_pages=None,
            pages_visited=0,
            product_links_collected=0,
            api_products_collected=0,
            stop_reason=f"initial navigation failed: {e}",
        )
        return []

    _wait_for_render(page)

    page_num:    int           = 0
    total_pages: int | None    = None   # set after page 1
    total_products: int | None = None
    stop_reason = ""

    while page_num < MAX_PAGES:
        page_num += 1
        current_url = page.url

        page_label = (
            f"Page {page_num} of {total_pages}"
            if total_pages else
            f"Page {page_num}"
        )
        logger.info(f"[Strategy 3] === {page_label}: {current_url} ===")

        try:
            _expand_dynamic_catalog(page, max_rounds=6)
        except Exception as e:
            logger.debug(f"[Strategy 3] Dynamic expansion skipped: {e}")

        harvested_links = _harvest_product_links_while_scrolling(
            page,
            current_url,
            max_rounds=30,
        )
        if harvested_links:
            new_harvested = [lk for lk in harvested_links if lk not in seen_links]
            seen_links.update(harvested_links)
            all_product_links.extend(new_harvested)
            logger.info(
                f"[Strategy 3] {page_label}: scroll harvest "
                f"{len(harvested_links)} link(s), {len(new_harvested)} new"
            )
            try:
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(500)
            except Exception:
                pass

        if api_capture["urls"]:
            new_api_links = [lk for lk in api_capture["urls"] if lk not in seen_links]
            seen_links.update(api_capture["urls"])
            all_product_links.extend(new_api_links)
            if new_api_links:
                logger.info(
                    f"[Strategy 3] {page_label}: API capture contributed "
                    f"{len(new_api_links)} new product link(s)"
                )

        if api_capture["products"]:
            existing_keys = {
                p.get("product_url") or p.get("sku") or f"{p.get('product_name')}|{p.get('price')}"
                for p in all_card_products
            }
            new_api_products = []
            for product in api_capture["products"]:
                key = product.get("product_url") or product.get("sku") or f"{product.get('product_name')}|{product.get('price')}"
                if key and key not in existing_keys:
                    existing_keys.add(key)
                    new_api_products.append(product)
            if new_api_products:
                logger.info(
                    f"[Strategy 3] {page_label}: API capture contributed "
                    f"{len(new_api_products)} product row(s)"
                )
                all_card_products.extend(new_api_products)

        # ── Get fully rendered HTML ───────────────────────────────────────────
        try:
            html = page.content()
        except Exception as e:
            logger.warning(
                f"[Strategy 3] Failed to get page content on page {page_num}: {e}"
            )
            break

        # ── Approach A: collect /products/ links ──────────────────────────────
        embedded_products, embedded_urls = _extract_embedded_json_products(
            html,
            current_url,
        )
        if embedded_urls:
            new_embedded_links = [lk for lk in embedded_urls if lk not in seen_links]
            seen_links.update(embedded_urls)
            all_product_links.extend(new_embedded_links)
            if new_embedded_links:
                logger.info(
                    f"[Strategy 3] {page_label}: embedded JSON contributed "
                    f"{len(new_embedded_links)} new product link(s)"
                )
        if embedded_products:
            existing_keys = {
                p.get("product_url") or p.get("sku") or f"{p.get('product_name')}|{p.get('price')}"
                for p in all_card_products
            }
            new_embedded_products = []
            for product in embedded_products:
                key = product.get("product_url") or product.get("sku") or f"{product.get('product_name')}|{product.get('price')}"
                if key and key not in existing_keys:
                    existing_keys.add(key)
                    new_embedded_products.append(product)
            if new_embedded_products:
                logger.info(
                    f"[Strategy 3] {page_label}: embedded JSON contributed "
                    f"{len(new_embedded_products)} product row(s)"
                )
                all_card_products.extend(new_embedded_products)

        page_links: list[str] = []
        try:
            page_links = _collect_product_links(html, current_url)
            new_links  = [lk for lk in page_links if lk not in seen_links]
            seen_links.update(page_links)
            all_product_links.extend(new_links)
            logger.info(
                f"[Strategy 3] {page_label}: "
                f"{len(page_links)} product link(s) ({len(new_links)} new)"
            )
        except Exception as e:
            logger.debug(f"[Strategy 3] Link collection error: {e}")

        # ── Approach B: card-level extraction from rendered DOM ───────────────
        logger.info(f"[Strategy 3] {page_label}: card-level extraction")

        found_during_wait = _wait_for_cards(page, MIN_CARDS_THRESHOLD)
        if not found_during_wait:
            logger.warning(
                f"[Strategy 3] {page_label}: "
                f"no card selector reached {MIN_CARDS_THRESHOLD}+ visible elements "
                f"during wait — proceeding with current DOM"
            )

        card_elements, detected_selector, card_count = _detect_cards(
            page, current_url
        )

        # Track best card selector for fingerprinting on page transitions.
        # Only trust selectors that look card-granular enough to avoid wrapper explosions
        # on later pages (e.g. broad wrapper matches vs real product cards).
        if (
            detected_selector
            and card_count >= MIN_CARDS_THRESHOLD
            and card_count <= 200
        ):
            winning_selector = detected_selector

        if card_elements:
            if card_count > 200 and detected_selector == "[class*='product-item']":
                tighter_elements = page.query_selector_all("li[class*='product']")
                tighter_count = len(tighter_elements)
                if tighter_count >= MIN_CARDS_THRESHOLD and tighter_count < card_count:
                    logger.info(
                        f"[Strategy 3] {page_label}: replacing broad selector "
                        f"'{detected_selector}' ({card_count}) with tighter "
                        f"'li[class*=''product'']' ({tighter_count})"
                    )
                    card_elements = tighter_elements
                    detected_selector = "li[class*='product']"
                    card_count = tighter_count

            logger.info(
                f"[Strategy 3] {page_label}: "
                f"{card_count} card element(s) via '{detected_selector}'"
            )
            page_card_products = []
            incomplete_count   = 0

            for i, elem in enumerate(card_elements, 1):
                product = _extract_card_product(elem, current_url, index=i)
                if _looks_like_aggregate_product(product):
                    incomplete_count += 1
                elif product.get("product_name") or product.get("product_url"):
                    page_card_products.append(product)
                else:
                    incomplete_count += 1

            logger.info(
                f"[Strategy 3] {page_label}: "
                f"{len(page_card_products)} product(s) extracted | "
                f"{incomplete_count} incomplete card(s) skipped | "
                f"{card_count} total card element(s)"
            )
            all_card_products.extend(page_card_products)

        else:
            # Last resort: full-page heuristic extraction (Strategy 1 logic)
            logger.info(
                f"[Strategy 3] {page_label}: "
                f"no card elements — falling back to full-page heuristics"
            )
            try:
                fallback = extract_products(html, current_url)
                logger.info(
                    f"[Strategy 3] {page_label}: "
                    f"full-page heuristics found {len(fallback)} product(s)"
                )
                filtered = [p for p in fallback if not _looks_like_aggregate_product(p)]
                if len(filtered) != len(fallback):
                    logger.info(
                        f"[Strategy 3] {page_label}: rejected "
                        f"{len(fallback) - len(filtered)} aggregate fallback row(s)"
                    )
                all_card_products.extend(filtered)
            except Exception as e:
                logger.warning(f"[Strategy 3] Full-page heuristics failed: {e}")

        # ── After page 1: detect total pagination ─────────────────────────────
        if page_num == 1:
            pagination = _detect_pagination_info(page)
            total_pages = pagination["total_pages"]
            total_products = pagination.get("total_products")

            if total_products:
                page_link_count = max(
                    len(page_links),
                    len(harvested_links),
                    len(api_capture["urls"]),
                )
                if not pagination.get("per_page") and page_link_count:
                    pagination["per_page"] = page_link_count
                if pagination.get("per_page") and not total_pages:
                    import math
                    total_pages = max(
                        1,
                        math.ceil(total_products / pagination["per_page"]),
                    )
                    pagination["total_pages"] = total_pages
                logger.info(
                    f"[Strategy 3] Result count: "
                    f"{total_products} total product(s) | "
                    f"{pagination.get('per_page', '?')} per page"
                )
            if total_pages:
                logger.info(
                    f"[Strategy 3] Pagination: {total_pages} page(s) total — "
                    f"will scrape all of them"
                )
            else:
                logger.info(
                    "[Strategy 3] Pagination: total pages not determinable — "
                    "will follow navigation until exhausted"
                )

        # ── Have we visited all known pages? ──────────────────────────────────
        if total_pages is not None and page_num >= total_pages:
            stop_reason = f"all {total_pages} page(s) visited"
            logger.info(
                f"[Strategy 3] All {total_pages} page(s) scraped — "
                f"pagination complete"
            )
            break

        # ── Navigate to next page (three-layer fallback) ──────────────────────
        fingerprint_before = _get_content_fingerprint(page, winning_selector)

        # Layer 1 & 2: URL-based next link / "Next" button click
        navigated = _follow_next_page(page, visited_pages, base_netloc)

        # Layer 3: Numbered page button (for SPAs like Sysco)
        if not navigated:
            target_page = page_num + 1
            if total_pages is None or target_page <= total_pages:
                navigated = _click_numbered_page(
                    page, target_page, winning_selector
                )
            if not navigated and total_pages is not None and target_page <= total_pages:
                navigated = _try_candidate_page_urls(
                    page,
                    target_page,
                    visited_pages,
                    base_netloc,
                    fingerprint_before,
                    winning_selector,
                )

        if not navigated:
            if total_pages and page_num < total_pages:
                stop_reason = f"no navigation path found after page {page_num}"
            else:
                stop_reason = f"navigation exhausted after page {page_num}"
            logger.info(
                f"[Strategy 3] No navigation path found after page {page_num} — "
                f"pagination complete"
            )
            break

        # ── Verify content actually changed ───────────────────────────────────
        # Prevents silent infinite loops when SPA clicks land on the same page
        # (e.g. clicking an already-active page button with no effect).
        _wait_for_render(page)
        fingerprint_after = _get_content_fingerprint(page, winning_selector)

        if (fingerprint_before
                and fingerprint_after
                and fingerprint_after == fingerprint_before):
            stop_reason = f"content unchanged after navigating from page {page_num}"
            logger.warning(
                f"[Strategy 3] Content unchanged after navigating from "
                f"page {page_num} — stopping to prevent loop. "
                f"This may mean the last real page was page {page_num}."
            )
            break

    # ── Listing crawl summary ─────────────────────────────────────────────────
    if not stop_reason:
        if page_num >= MAX_PAGES:
            stop_reason = f"max page cap reached ({MAX_PAGES})"
        else:
            stop_reason = f"crawl stopped after {page_num} page(s)"

    _set_last_crawl_diagnostics(
        expected_products=total_products,
        expected_pages=total_pages,
        pages_visited=page_num,
        product_links_collected=len(all_product_links),
        api_products_collected=len(api_capture["products"]),
        stop_reason=stop_reason,
    )

    logger.info(
        f"[Strategy 3] Listing crawl complete: "
        f"{page_num} page(s) visited | "
        f"{len(all_product_links)} /products/ link(s) collected | "
        f"{len(all_card_products)} card-level product(s) before dedup"
    )
    # Phase 2a: deduplicate listing rows and optionally enrich from detail pages
    if all_card_products:
        raw_count = len(all_card_products)
        deduped, removed = _dedup_products(all_card_products)
        logger.info(
            f"[Strategy 3] Listing result: "
            f"{raw_count} raw row(s) -> "
            f"{removed} duplicate(s) removed -> "
            f"{len(deduped)} unique listing row(s)"
        )

        listing_by_url = {
            (p.get("product_url") or "").strip(): p
            for p in deduped
            if (p.get("product_url") or "").strip()
        }

        valid_listing_urls = {
            url for url, row in listing_by_url.items()
            if _looks_like_product_detail_url(url)
        }
        if valid_listing_urls:
            logger.info(
                f"[Strategy 3] Detail candidates: {len(valid_listing_urls)} valid listing detail URL(s)"
            )

        detail_link_pool = _select_detail_enrichment_links(all_product_links, valid_listing_urls)
        if len(detail_link_pool) > len(valid_listing_urls):
            logger.info(
                f"[Strategy 3] Using collected product links as authoritative "
                f"detail targets: {len(detail_link_pool)} link(s) vs "
                f"{len(valid_listing_urls)} listing-row URL(s)"
            )

        enrich_links = detail_link_pool

        if len(enrich_links) > AUTH_DETAIL_ENRICH_LIMIT:
            logger.info(
                f"[Strategy 3] Auth detail enrichment capped at {AUTH_DETAIL_ENRICH_LIMIT} "
                f"of {len(enrich_links)} listing row(s)"
            )
            enrich_links = enrich_links[:AUTH_DETAIL_ENRICH_LIMIT]

        if enrich_links:
            enriched = []
            for i, link in enumerate(enrich_links, 1):
                listing_row = listing_by_url.get(link, {})
                try:
                    logger.info(f"[Strategy 3] Detail enrich [{i}/{len(enrich_links)}] {link}")
                    page.goto(link, wait_until="domcontentloaded", timeout=25_000)
                    _wait_for_render(page)
                    detail_html = page.content()
                    detail_product = _extract_from_detail_page(detail_html, link)
                    is_valid, reason = _is_valid_detail_product(detail_product)
                    if not is_valid:
                        logger.warning(
                            f"[Strategy 3] Detail enrich rejected ({link}): {reason}. "
                            f"Preserving listing row fallback."
                        )
                        if listing_row:
                            enriched.append(listing_row)
                        continue

                    merged = _merge_listing_and_detail(listing_row, detail_product)
                    if merged.get("product_name") or merged.get("product_url"):
                        enriched.append(merged)
                    elif listing_row:
                        enriched.append(listing_row)
                except Exception as e:
                    logger.warning(f"[Strategy 3] Detail enrich failed ({link}): {e}")
                    if listing_row:
                        enriched.append(listing_row)

            enriched_urls = {(p.get("product_url") or "").strip() for p in enriched}
            for row in deduped:
                row_url = (row.get("product_url") or "").strip()
                if row_url and row_url not in enriched_urls:
                    enriched.append(row)

            final_products, _ = _dedup_products(enriched)
            logger.info(
                f"[Strategy 3] Final authenticated result: "
                f"{len(final_products)} product(s) returned "
                f"after {len(enrich_links)} detail enrichment attempt(s)"
            )
            return final_products

        logger.info(f"[Strategy 3] Final result: {len(deduped)} listing product(s) returned")
        return deduped

    # Phase 2b: no listing rows, try detail-only fallback
    if all_product_links:
        if len(all_product_links) > DETAIL_PAGE_LIMIT:
            logger.warning(
                f"[Strategy 3] Capping detail pages at {DETAIL_PAGE_LIMIT} "
                f"(collected {len(all_product_links)})"
            )
            all_product_links = all_product_links[:DETAIL_PAGE_LIMIT]

        logger.info(
            f"[Strategy 3] Scraping {len(all_product_links)} product detail page(s)..."
        )
        products = []
        for i, link in enumerate(all_product_links, 1):
            try:
                logger.info(f"[Strategy 3] [{i}/{len(all_product_links)}] {link}")
                page.goto(link, wait_until="domcontentloaded", timeout=25_000)
                _wait_for_render(page)
                detail_html = page.content()
                product = _extract_from_detail_page(detail_html, link)
                is_valid, reason = _is_valid_detail_product(product)
                if not is_valid:
                    logger.warning(f"[Strategy 3] Detail-only reject ({link}): {reason}")
                    continue
                if product.get("product_name") or product.get("product_url"):
                    products.append(product)
            except Exception as e:
                logger.warning(f"[Strategy 3] Detail page failed ({link}): {e}")
                continue

        logger.info(
            f"[Strategy 3] Detail scraping complete -> "
            f"{len(products)} product(s) extracted"
        )
        return products

    # ── Nothing found ─────────────────────────────────────────────────────────
    logger.warning(
        "[Strategy 3] No products found. Possible causes: "
        "page requires further interaction, grid uses unrecognised markup, "
        "or session cookies did not persist correctly. "
        "Check the selector scan above for clues."
    )
    return []
