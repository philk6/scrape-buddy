"""
scraper.py — Core scraping logic for Scrape Buddy v1

How it works:
1. fetch_html() downloads the raw HTML of the given URL using requests,
   with a realistic browser User-Agent to avoid basic bot blocks.

2. extract_products() parses that HTML with BeautifulSoup and uses
   CSS class heuristics to find product cards on the page:
   - It looks for elements whose class names contain keywords like
     "product", "item", "card" — common patterns across e-commerce sites.
   - From each candidate element it tries to pull: name, brand, price,
     product URL, image URL, and SKU using further keyword matching.
   - Missing fields default to empty string "".
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
_COLLECTION_URL_PATTERNS = [
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

# Name patterns that indicate a category or navigation tile, not a product.
# Checked via _looks_like_category_name().
_CATEGORY_NAME_SEPARATORS = [" : ", " > ", " / ", " | "]
_CATEGORY_NAME_PREFIXES = [
    "shop all", "view all", "see all", "browse all",
    "shop ", "browse ", "all ",
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
    """
    Fetch the HTML content of the given URL.
    Raises requests.RequestException on network/HTTP errors.
    """
    response = requests.get(url, headers=HEADERS, timeout=15)
    response.raise_for_status()
    return response.text


def fetch_html_playwright(url: str, wait_ms: int = 3000, strict: bool = False) -> str:
    """
    Fetch HTML using Playwright with full JavaScript rendering.

    Args:
        url:      Page to render.
        wait_ms:  Base milliseconds to wait after domcontentloaded for JS to run.
                  For KnockoutJS / API-driven sites, use 6000-8000.
        strict:   If True, raise PlaywrightUnavailableError instead of falling
                  back to requests when Playwright is not installed or fails.
                  The router uses this to distinguish "Playwright not available"
                  from "Playwright rendered but page was empty".

    Falls back to regular fetch_html() if Playwright is not available AND
    strict=False (legacy behaviour for callers that don't care).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if strict:
            raise PlaywrightUnavailableError("Playwright is not installed")
        import logging
        logging.getLogger(__name__).warning(
            "[scraper] Playwright not available — falling back to requests"
        )
        return fetch_html(url)

    try:
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
            # Try to wait for common product selectors — covers grid layouts,
            # KnockoutJS data-bind containers, and price text appearing.
            _PW_PRODUCT_SELECTORS = [
                "[class*='product']", "[class*='item']", "[class*='card']",
                "[data-testid*='product']", ".product-list", ".products-grid",
                "[data-bind]",           # KnockoutJS
                "[data-reactroot]",      # React
                "[class*='price']",      # Price element rendered by JS
                "td:has-text('$')",      # Table cell with price (B2B catalogs)
            ]
            for selector in _PW_PRODUCT_SELECTORS:
                try:
                    page.wait_for_selector(selector, timeout=3000)
                    break
                except Exception:
                    continue

            # Second wait: if the page had KnockoutJS or Angular bindings,
            # the initial selectors may fire on empty templates before data
            # arrives. Wait for actual product content to appear in the DOM.
            # Check for prices OR product names/images (some sites hide
            # prices behind login but still render product data).
            try:
                page.wait_for_function(
                    """() => {
                        // Price text rendered
                        if (document.body.innerText.match(/\\$\\d+\\.\\d{2}/)) return true;
                        // 3+ product images rendered
                        if (document.querySelectorAll('img[src*="product"], img[src*="item"], [class*="product"] img').length >= 3) return true;
                        // 3+ product name elements rendered
                        if (document.querySelectorAll('[class*="product-name"], [class*="product-title"], [class*="item-name"], [data-bind*="text"]').length >= 3) return true;
                        // 3+ data-bind elements with visible text (KnockoutJS finished)
                        var bound = document.querySelectorAll('[data-bind]');
                        var withText = 0;
                        for (var i = 0; i < bound.length && i < 50; i++) {
                            if (bound[i].textContent.trim().length > 5) withText++;
                            if (withText >= 3) return true;
                        }
                        return false;
                    }""",
                    timeout=wait_ms,
                )
            except Exception:
                pass  # Not all pages match — don't block on this

            html = page.content()
            browser.close()
            return html
    except Exception as e:
        if strict:
            raise
        import logging
        logging.getLogger(__name__).warning(
            f"[scraper] Playwright fetch failed ({e}) — falling back to requests"
        )
        return fetch_html(url)


class PlaywrightUnavailableError(Exception):
    """Raised when strict=True and Playwright is not installed or fails to launch."""
    pass


def make_auth_fetch_fn(cookies: list):
    """
    Build a fetch_fn that injects browser session cookies into every request.

    cookies: list of dicts from Playwright context.cookies(), each containing
             at least {"name": ..., "value": ..., "domain": ...}.

    Returns a callable with the same signature as fetch_html(url) -> str,
    suitable for passing to strategies.detail.run(fetch_fn=...).
    The requests.Session is created once and reused for all calls.
    """
    session = requests.Session()
    for c in cookies:
        session.cookies.set(
            c["name"],
            c["value"],
            domain=c.get("domain", ""),
        )

    def _fetch(url: str) -> str:
        response = session.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        return response.text

    return _fetch


def _class_contains(element, keywords: list) -> bool:
    """Return True if the element's class attribute contains any of the keywords."""
    classes = " ".join(element.get("class", [])).lower()
    return any(kw in classes for kw in keywords)


def _is_chrome_context(element) -> bool:
    """
    Return True if the element lives inside page chrome (nav, header, footer,
    sidebar, breadcrumb, filter bar, etc.) rather than the product content area.

    Walks up to 10 ancestor levels.  Checks both the tag name and the combined
    class+id string of every ancestor.  A match at any level means the element
    should be treated as chrome, not a product card.
    """
    node = element
    for _ in range(10):
        node = getattr(node, "parent", None)
        if node is None:
            break
        name = getattr(node, "name", None)
        if name in (None, "[document]", "body", "html"):
            break

        # Tag-name check
        if name in _CHROME_TAGS:
            return True

        # Class + id check
        try:
            combined = (
                " ".join(node.get("class") or []).lower()
                + " "
                + (node.get("id") or "").lower()
            )
            if any(sig in combined for sig in _CHROME_CLASS_SIGNALS):
                return True
        except Exception:
            pass

    return False


def _get_text(element) -> str:
    """Return stripped text content of an element, or empty string."""
    return element.get_text(strip=True) if element else ""


def _looks_like_category_name(name: str) -> bool:
    """
    Return True if the candidate product name looks like a category/collection
    label rather than an individual product name.

    Signals:
      - Contains a hierarchy separator like " : ", " > ", " / ", " | "
        (e.g. "Family Care : Dishwashing", "Home > Kitchen")
      - Starts with a navigation prefix like "Shop All", "View All", "Browse"
      - Is suspiciously short AND starts with a capital browse word

    Examples that return True:
      "Family Care : Dishwashing", "Home > Cleaning", "Shop All", "Browse Items"
    Examples that return False:
      "Scotch-Brite Non-Scratch 1pk", "Elmer's 4oz School Glue", "SKYN 3pk"
    """
    if not name:
        return False
    lower = name.lower().strip()

    # Category hierarchy separators
    if any(sep in name for sep in _CATEGORY_NAME_SEPARATORS):
        return True

    # Navigation prefixes
    if any(lower.startswith(prefix) for prefix in _CATEGORY_NAME_PREFIXES):
        return True

    # Category tile patterns like "Paper Towels114 PRODUCTS" or "Cleaning Tools (147)"
    if re.search(r'\d+\s*products?\b', lower):
        return True
    if re.search(r'\(\d+\)\s*$', name.strip()):
        return True
    if re.search(r'\d+\s*items?\b', lower):
        return True

    return False


def _resolve_url(href: str, base_url: str) -> str:
    """Convert a relative URL to an absolute URL using the page's base URL."""
    if not href:
        return ""
    return urljoin(base_url, href)


def _clean_product_title(text: str) -> str:
    text = re.sub(r'^last\s+purchased\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^(?:\d+[\d,]*\s*(?:case|pack|ct|count)\s*)+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^(?:\d+\s+sizes?\s*)+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^(?:\d+\s+colors?\s*)+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip(' -|')
    return text


def _extract_name(card) -> str:
    """
    Try to find a product name inside a card element.
    Checks explicit description/title structures first, then product-like links.
    """
    description_el = card.find(attrs={"data-testid": "itemDescription"})
    if description_el:
        text = _clean_product_title(_get_text(description_el))
        if text:
            return text

    for tag in ["h1", "h2", "h3", "h4", "h5"]:
        heading = card.find(tag)
        if heading:
            text = _clean_product_title(_get_text(heading))
            if text:
                return text

    name_el = card.find(
        lambda el: el.name not in ["script", "style"]
        and any(kw in " ".join(el.get("class", [])).lower() for kw in ["name", "title", "description", "itemdescription", "product-title", "product-details"])
    )
    if name_el:
        text = _clean_product_title(_get_text(name_el))
        if text:
            return text

    product_link_texts = []
    for a in card.find_all("a", href=True):
        href = (a.get("href") or "").strip().lower()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        if any(bad in href for bad in ["/collections/", "/category/", "/categories/", "/search", "/cart", "/checkout", "/account", "/login", "/register", "/wishlist", "/compare", "/pages/", "/blogs/", "/tag/", "/tags/", "/contact", "/about", "/faq", "/help", "/plus/"]):
            continue

        text = _get_text(a)
        if not text:
            continue
        text = _clean_product_title(text)
        if len(text) < 8:
            continue
        if _looks_like_category_name(text):
            continue
        product_link_texts.append((len(text), text))

    if product_link_texts:
        product_link_texts.sort(reverse=True)
        text = product_link_texts[0][1]
        if text:
            return text

    # ── Fallback: data attributes ──────────────────────────────────────
    for attr in ["data-name", "data-product-name", "data-title", "data-description",
                 "data-product-title", "aria-label"]:
        val = card.get(attr, "")
        if val and len(val) >= 5:
            return _clean_product_title(val)
        for child in card.find_all(attrs={attr: True}, limit=3):
            val = child.get(attr, "")
            if val and len(val) >= 5:
                return _clean_product_title(val)

    # ── Fallback: image alt text (often contains product name) ─────────
    for img in card.find_all("img", limit=5):
        alt = (img.get("alt") or "").strip()
        if alt and len(alt) >= 8 and not alt.lower().startswith(("icon", "logo", "arrow", "btn", "button")):
            return _clean_product_title(alt)

    # ── Fallback: any link text ≥ 5 chars (relaxed from 8) ────────────
    for a in card.find_all("a", href=True):
        text = _get_text(a)
        if text and len(text) >= 5 and not _looks_like_category_name(text):
            return _clean_product_title(text)

    # ── Fallback: longest text node in the card ────────────────────────
    texts = []
    for el in card.find_all(True):
        if el.name in ["script", "style", "select", "option"]:
            continue
        t = el.get_text(strip=True)
        if t and len(t) >= 8 and '$' not in t[:3]:  # skip price-like text
            texts.append(t)
    if texts:
        texts.sort(key=len, reverse=True)
        # Return the longest text that looks like a product name
        for t in texts[:3]:
            if not _looks_like_category_name(t) and len(t) <= 200:
                return _clean_product_title(t)

    return card.get("title", "")


def _extract_brand(card) -> str:
    """
    Try to find a brand name inside a card element.
    Looks for elements with 'brand', 'vendor', or 'manufacturer' in class.
    """
    brand_el = card.find(
        lambda el: el.name not in ["script", "style"]
        and any(kw in " ".join(el.get("class", [])).lower() for kw in ["brand", "vendor", "manufacturer", "make"])
    )
    return _get_text(brand_el)


def _extract_price(card) -> str:
    """
    Try to find a price inside a card element.

    Prefers explicit price/cost classes, then falls back to compact price-like
    elements inside the card. Avoids broad text-node scanning so page-level
    banners and nested utility text do not become fake prices.

    Guards against the "same price for every product" bug by:
      - Only considering elements that are DIRECT descendants of the card
        (max 6 levels deep).
      - Preferring the most deeply nested (most specific) price element.
      - Rejecting price elements whose text appears in too many siblings.
    """
    def _normalize_price_text(text: str) -> str:
        text = re.sub(r'\s+', ' ', text).strip()
        text = re.sub(r'^last\s+purchased\s*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'^plus\s+member\s+price\s*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'^member\s+price\s*', '', text, flags=re.IGNORECASE)

        all_prices = re.findall(r'\$\s*\d[\d,]*(?:\.\d{2})?', text)
        if all_prices:
            lower = text.lower()
            preferred = all_prices[0]

            # If text has "sale price" or "your price" or "now", prefer that price
            sale_match = re.search(
                r'(?:sale\s+price|your\s+price|now|final|member\s+price)\s*\$\s*(\d[\d,]*(?:\.\d{2})?)',
                text, re.IGNORECASE
            )
            if sale_match and len(all_prices) >= 2:
                preferred = f"${sale_match.group(1)}"
            elif 'regularly' in lower and len(all_prices) >= 2:
                # "regularly $X.XX" means the first price is the sale price
                preferred = all_prices[0]
            elif len(all_prices) >= 2:
                # Multiple prices with no context: prefer the lowest non-negative
                valid_prices = []
                for p in all_prices:
                    try:
                        val = float(p.replace('$', '').replace(',', '').strip())
                        if val > 0:
                            valid_prices.append((val, p))
                    except ValueError:
                        continue
                if valid_prices:
                    valid_prices.sort(key=lambda x: x[0])
                    preferred = valid_prices[0][1]

            # Match unit suffix: "/case", "/ea", or standalone "BX", "CT", "EA" etc.
            suffix_match = re.search(r'(?:/\s*(?:case|each|ea|cs|box|bx|pack|pk|set|dozen|dz|lb|oz|gal|qt|pt|ct))', text, re.IGNORECASE)
            if suffix_match:
                suffix_clean = re.sub(r'\s+', '', suffix_match.group(0))
                preferred = f"{preferred}{suffix_clean}"
            else:
                # Standalone unit abbreviation after price (e.g. "$19.46 BX")
                standalone_unit = re.search(
                    r'\$\s*\d[\d,]*(?:\.\d{2})?\s+(BX|CT|CS|EA|PK|DZ|CA|LB|OZ|GL|QT|PT|BD|RL|SH|TB|BG|BT|JR|DR)\b',
                    text, re.IGNORECASE
                )
                if standalone_unit:
                    preferred = f"{preferred}/{standalone_unit.group(1).upper()}"
            return preferred.replace(' / ', '/').strip()

        return text.replace(' / ', '/').strip()

    def _nesting_depth(el, card_el, max_depth=8) -> int:
        """Return nesting depth of el inside card_el. 1 = direct child, 0 = not found."""
        node = el.parent
        depth = 1
        while node is not None and depth <= max_depth:
            if node is card_el:
                return depth
            node = getattr(node, 'parent', None)
            depth += 1
        return 0

    def _is_leaf_price(el) -> bool:
        """Return True if this element has no child elements containing $ — i.e. it's
        the most specific price container, not a wrapper that inherits price text."""
        for child in el.find_all(True, recursive=False):
            child_text = child.get_text(strip=True) if child else ""
            if child_text and '$' in child_text:
                return False
        return True

    price_candidates = []
    for el in card.find_all(True):
        if el.name in ["script", "style", "form", "select", "option"]:
            continue

        text = _get_text(el)
        if not text or '$' not in text:
            continue
        if len(text) > 160:
            continue

        classes = ' '.join(el.get('class', [])).lower()
        lower_text = text.lower()
        score = 0

        # ── Class-based signals (strongest) ──────────────────────────────
        if any(kw in classes for kw in ['price', 'cost', 'pricing', 'amount']):
            score += 5
        if any(kw in classes for kw in ['sale', 'current', 'final', 'now']):
            score += 2
        if any(kw in classes for kw in ['original', 'was', 'old', 'compare', 'regular']):
            score -= 3

        # ── Content signals ──────────────────────────────────────────────
        if re.search(r'\$\s*\d', text):
            score += 2
        if any(unit in lower_text for unit in ['/ each', '/ case', '/ea', '/cs', '/pack', '/box']):
            score += 1
        if 'plus member price' in lower_text or 'member price' in lower_text:
            score += 2
        if 'regularly' in lower_text or 'was $' in lower_text:
            score -= 2
        if text.count('$') > 3:
            score -= 2
        if len(text) <= 40:
            score += 2
        elif len(text) <= 60:
            score += 1

        # ── Leaf node preference (most specific element) ─────────────────
        # Strongly prefer the innermost element that contains the price,
        # not a parent wrapper that also has the price in its text.
        if _is_leaf_price(el):
            score += 4
        else:
            score -= 3

        # ── Nesting depth: prefer elements that are deeper in the card ───
        depth = _nesting_depth(el, card)
        if depth >= 3:
            score += 2
        elif depth >= 2:
            score += 1

        price_candidates.append((score, len(text), text, el))

    if price_candidates:
        # Sort by score desc, then shorter text (more specific)
        price_candidates.sort(key=lambda x: (x[0], -x[1]), reverse=True)
        best_text = price_candidates[0][2]
        best_el = price_candidates[0][3]
        normalized = _normalize_price_text(best_text)
        if normalized and '$' in normalized:
            # Check for sibling unit element (e.g. <span class="unit">BX</span>)
            # that isn't included in the price element's own text.
            if '/' not in normalized:
                unit_sibling = None
                for sib in (best_el.find_next_siblings() or []):
                    sib_classes = ' '.join(sib.get('class', [])).lower()
                    if 'unit' in sib_classes or 'uom' in sib_classes:
                        unit_sibling = sib
                        break
                if unit_sibling is None and best_el.parent:
                    for sib in (best_el.parent.find_all(True, recursive=False) or []):
                        if sib is best_el:
                            continue
                        sib_classes = ' '.join(sib.get('class', [])).lower()
                        if 'unit' in sib_classes or 'uom' in sib_classes:
                            unit_sibling = sib
                            break
                if unit_sibling:
                    unit_text = unit_sibling.get_text(strip=True).upper()
                    if unit_text and len(unit_text) <= 5:
                        normalized = f"{normalized}/{unit_text}"
            return normalized

    return ""


def _extract_product_url(card, base_url: str) -> str:
    """
    Find the main product link inside a card.
    Scores links instead of blindly taking the first one so public grid/listing
    pages with utility links, filters, or plus/promo links stay usable.
    """
    try:
        from strategies.link_scorer import best_link
        best = best_link(card.find_all("a", href=True), base_url)
        if best:
            return best
    except Exception:
        pass

    # Compatibility fallback: preserve prior behaviour if scoring fails.
    for a in card.find_all("a", href=True):
        href = a["href"]
        if any(kw in href.lower() for kw in ["product", "item", "detail", "p/"]):
            return _resolve_url(href, base_url)

    first_link = card.find("a", href=True)
    if first_link:
        return _resolve_url(first_link["href"], base_url)

    return ""


def _extract_image_url(card, base_url: str) -> str:
    """
    Find a product image inside a card.
    Checks src, data-src, and data-lazy-src attributes (lazy-loading patterns).
    Falls back to parent/sibling elements when the card is an inner text container
    (e.g. product-item-details) and the image lives in a sibling (product-item-photo).
    """
    def _first_img_url(container):
        """Return the first valid image URL from an element."""
        for img in container.find_all("img", limit=5):
            for attr in ["src", "data-src", "data-lazy-src", "data-original",
                         "data-img", "data-image", "data-full-src"]:
                val = (img.get(attr) or "").strip()
                if val and not val.startswith("data:"):
                    return _resolve_url(val, base_url)
        return ""

    # 1. Look inside the card itself
    url = _first_img_url(card)
    if url:
        return url

    # 2. Fallback: check the card's parent (common when the card is an inner
    #    text container like <div class="product-item-details"> and the image
    #    is in a sibling <a class="product-item-photo">)
    parent = card.parent
    if parent and parent.name not in (None, "[document]", "body", "html"):
        url = _first_img_url(parent)
        if url:
            return url

    return ""


def _extract_sku(card) -> str:
    """
    Try to find a SKU/product ID on the card element itself or a child.
    Checks common data attributes first, then explicit item/SKU text patterns.
    """
    sku_attrs = ["data-sku", "data-product-id", "data-id", "data-item-id", "data-pid"]
    for attr in sku_attrs:
        val = card.get(attr, "")
        if val:
            return str(val).strip().upper()
        child = card.find(attrs={attr: True})
        if child:
            return str(child[attr]).strip().upper()

    full_text = card.get_text(" ", strip=True)
    for pattern in [
        r"item\s*number\s*#\s*([A-Z0-9\-]{2,})",
        r"item\s*#\s*([A-Z0-9\-]{2,})",
        r"sku\s*#?\s*([A-Z0-9\-]{2,})",
        r"model\s*#?\s*([A-Z0-9\-]{2,})",
    ]:
        m = re.search(pattern, full_text, re.I)
        if m:
            return m.group(1).upper()

    sku_el = card.find(
        lambda el: el.name not in ["script", "style"]
        and any(kw in " ".join(el.get("class", [])).lower() for kw in ["sku", "product-sku", "mpn", "part-number", "model", "item-number"])
    )
    text = _get_text(sku_el)
    if text:
        m = re.search(r"([A-Z0-9\-]{2,})", text, re.I)
        if m:
            return m.group(1).upper()

    return ""


def _extract_page_intent_terms(soup, base_url: str) -> set[str]:
    """Infer page intent from URL, title, and headings while avoiding noisy page chrome text."""
    terms: set[str] = set()

    sources = [base_url]
    title = soup.find('title')
    if title:
        sources.append(title.get_text(' ', strip=True))
    for h in soup.find_all(['h1', 'h2'], limit=4):
        txt = h.get_text(' ', strip=True)
        if txt:
            sources.append(txt)

    stop_terms = {
        'https', 'http', 'www', 'html', 'page', 'products', 'product',
        'store', 'wholesale', 'search', 'category', 'categories', 'restaurant',
        'equipment', 'your', 'with', 'within', 'make', 'sure', 'sort', 'filters',
        'reach', 'webstaurantstore', 'stands', 'tables'
    }

    for src in sources:
        for token in re.findall(r'[a-z0-9]{4,}', src.lower().replace('-', ' ')):
            if token not in stop_terms and not token.isdigit():
                terms.add(token)
    return terms


def _intent_overlap_score(text: str, intent_terms: set[str]) -> int:
    if not intent_terms:
        return 0
    text_terms = set(re.findall(r'[a-z0-9]{4,}', text.lower().replace('-', ' ')))
    return len(intent_terms & text_terms)


def _product_semantic_text(el) -> str:
    parts = []

    for node in el.find_all(attrs={"data-testid": "itemDescription"}, limit=6):
        txt = _get_text(node)
        if txt:
            parts.append(txt)

    for a in el.find_all("a", href=True, limit=12):
        href = (a.get("href") or "").strip()
        text = _get_text(a)
        if text:
            parts.append(text)
        if href:
            parts.append(href)

    return " ".join(parts)


def _group_name_semantic_score(elements, intent_terms: set[str]) -> int:
    if not intent_terms:
        return 0

    names = []
    for el in elements[:8]:
        txt = _extract_name(el)
        if txt:
            names.append(txt)

    if not names:
        return -8

    combined = ' '.join(names)
    overlap = _intent_overlap_score(combined, intent_terms)
    if overlap == 0:
        return -10
    if overlap == 1:
        return -4
    return min(overlap * 4, 12)


def _intent_penalty(text: str, intent_terms: set[str]) -> int:
    if not intent_terms:
        return 0
    overlap = _intent_overlap_score(text, intent_terms)
    if overlap == 0:
        return -10
    if overlap == 1:
        return -4
    return min(overlap * 3, 9)


def _remove_ancestor_cards(candidates: list) -> list:
    """
    Remove elements from the candidate list that are ancestors of other candidates.
    This prevents a wrapper div (e.g. products-grid) from being treated as a
    product card alongside the actual product cards inside it.
    """
    if len(candidates) <= 1:
        return candidates

    # Build a set of candidate ids for fast lookup
    candidate_ids = {id(el) for el in candidates}

    filtered = []
    for el in candidates:
        # Check if this element is an ancestor of any OTHER candidate
        is_ancestor = False
        for other in candidates:
            if other is el:
                continue
            # Walk up from 'other' to see if we hit 'el'
            node = other.parent
            depth = 0
            while node is not None and depth < 15:
                if node is el:
                    is_ancestor = True
                    break
                node = getattr(node, 'parent', None)
                depth += 1
            if is_ancestor:
                break

        if not is_ancestor:
            filtered.append(el)

    # Only use filtered if it still has enough results
    if len(filtered) >= 2:
        return filtered
    return candidates


def _find_product_cards(soup, base_url: str = "") -> list:
    """
    Find all product card elements on the page using class heuristics.

    Strategy:
    1. Find class-based candidates.
    2. Filter out page chrome.
    3. Group by repeated tag+class pattern.
    4. Score pattern quality based on container semantics.
    5. Validate individual containers before returning them.
    6. Prefer groups whose content matches page intent.
    7. Fall back conservatively if validation becomes too strict.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    intent_terms = _extract_page_intent_terms(soup, base_url)

    raw_candidates = [el for el in soup.find_all(True) if _class_contains(el, PRODUCT_KEYWORDS)]

    candidates = [el for el in raw_candidates if not _is_chrome_context(el)]

    chrome_removed = len(raw_candidates) - len(candidates)
    if chrome_removed:
        _log.debug(
            f"[scraper] _find_product_cards: {len(raw_candidates)} raw candidates, "
            f"{chrome_removed} removed as chrome/nav context, "
            f"{len(candidates)} remaining"
        )

    if not candidates:
        return []

    def _main_product_link_count(el) -> int:
        strong = 0
        for a in el.find_all('a', href=True):
            href = (a.get('href') or '').lower()
            if not href or href.startswith(('javascript:', 'mailto:', 'tel:', '#')):
                continue
            if any(sig in href for sig in ['/search', '/category', '/categories/', '/collections/', '/cart', '/account', '/login', '/compare', '/wishlist', '/plus/']):
                continue
            if any(sig in href for sig in ['product', 'item', 'detail', '/p/', '/g/', '/products/', '/product/']):
                strong += 1
                continue
            slug = href.rstrip('/').split('/')[-1]
            if slug and len(slug) > 5 and not slug.isdigit():
                strong += 1
        return strong

    def _container_metrics(el) -> dict:
        text = el.get_text(' ', strip=True)
        classes = ' '.join(el.get('class', [])).lower()
        links = el.find_all('a', href=True)
        images = el.find_all('img')
        child_tags = [c for c in el.children if getattr(c, 'name', None) is not None]
        strong_links = _main_product_link_count(el)
        utility_links = 0
        for a in links:
            href = (a.get('href') or '').lower()
            if any(sig in href for sig in ['/plus/', '/search', '/category', '/categories/', '/collections/', '/cart', '/account', '/login', '/compare', '/wishlist']):
                utility_links += 1
        metrics = {
            'text_len': len(text),
            'link_count': len(links),
            'image_count': len(images),
            'child_count': len(child_tags),
            'strong_links': strong_links,
            'utility_links': utility_links,
            'price_like': bool(re.search(r"\$\s*\d", text)),
            'sku_like': bool(re.search(r"\b(?:sku|item\s*(?:no|number|#)|part\s*(?:no|number|#))\b", text, re.IGNORECASE)),
            'has_container_class': 'container' in classes,
            'classes': classes,
            'text': text,
        }
        return metrics

    def _pattern_quality(sample_el) -> tuple:
        m = _container_metrics(sample_el)
        score = 0
        if m['strong_links'] == 1:
            score += 5
        elif m['strong_links'] >= 2:
            score += 2
        if m['image_count'] == 1:
            score += 3
        elif m['image_count'] >= 1:
            score += 1
        if m['price_like']:
            score += 2
        if m['sku_like']:
            score += 1
        if 40 <= m['text_len'] <= 700:
            score += 2
        elif m['text_len'] > 2200:
            score -= 4
        if m['child_count'] <= 8:
            score += 2
        elif m['child_count'] >= 18:
            score -= 4
        if m['utility_links'] >= 3:
            score -= 3
        elif m['utility_links'] >= 1:
            score -= 1
        if m['link_count'] >= 12:
            score -= 3
        if m['has_container_class'] and not any(sig in m['classes'] for sig in ['product-box-container', 'product-container']):
            score -= 1
        semantic_text = _product_semantic_text(sample_el)
        intent_score = _intent_overlap_score(semantic_text, intent_terms)
        score += _intent_penalty(semantic_text, intent_terms)
        return (
            score,
            intent_score,
            m['strong_links'],
            m['image_count'],
            m['price_like'],
            -m['utility_links'],
            -m['child_count'],
        )

    def _validate_container(el) -> bool:
        m = _container_metrics(el)
        if m['text_len'] < 20:
            return False
        if m['text_len'] > 3000:
            return False
        if m['strong_links'] == 0:
            return False
        if m['link_count'] > 18:
            return False
        if m['child_count'] > 28:
            return False
        if m['utility_links'] >= 4 and m['strong_links'] <= 1:
            return False
        if not (m['image_count'] or m['price_like'] or m['sku_like']):
            return False
        # Semantic gate: skip elements that don't match page intent.
        # But override if the element has strong structural signals
        # (price + image or price + SKU) — product names often don't
        # contain category keywords (e.g. "Boardwalk Bags" on /Food/).
        if intent_terms and _intent_overlap_score(_product_semantic_text(el), intent_terms) == 0:
            has_strong_signals = (
                m['price_like'] and (m['image_count'] >= 1 or m['sku_like'])
            )
            if not has_strong_signals:
                return False
        return True

    pattern_counts = Counter()
    el_by_pattern = {}

    for el in candidates:
        classes = frozenset(el.get('class', []))
        pattern = (el.name, classes)
        pattern_counts[pattern] += 1
        el_by_pattern.setdefault(pattern, []).append(el)

    ranked_patterns = []
    for pattern, count in pattern_counts.items():
        if count < MIN_PRODUCTS:
            continue
        sample_el = el_by_pattern[pattern][0]
        quality = _pattern_quality(sample_el)
        name_semantic = _group_name_semantic_score(el_by_pattern[pattern], intent_terms)
        semantic_text = _product_semantic_text(sample_el)
        semantic_overlap = _intent_overlap_score(semantic_text, intent_terms)
        ranked_patterns.append(((quality[0] + name_semantic, name_semantic, semantic_overlap, *quality[1:]), count, pattern))

    if not ranked_patterns:
        return _remove_ancestor_cards(candidates)

    ranked_patterns.sort(reverse=True)

    # Pass 1: patterns that pass both validation AND semantic gate
    best_validated_fallback = None
    for best_quality, best_count, best_pattern in ranked_patterns:
        chosen = el_by_pattern[best_pattern]
        validated = [el for el in chosen if _validate_container(el)]
        semantic_gate = _group_name_semantic_score(validated or chosen, intent_terms) if intent_terms else 0
        _log.info(
            f"[scraper] pattern tag={best_pattern[0]!r} count={best_count} "
            f"quality={best_quality} validated={len(validated)} semantic_gate={semantic_gate}"
        )
        if len(validated) >= MIN_PRODUCTS:
            if not intent_terms or semantic_gate >= 0:
                return validated
            # Good structural match but semantic gate failed — save as fallback
            # (product names may not match URL keywords, e.g. "Tootsie Roll" on a /candy/ page)
            # Prefer the pattern with the most validated items (larger catalog is
            # more likely to be the real product grid, not a small carousel/feature).
            if best_validated_fallback is None or len(validated) > len(best_validated_fallback):
                best_validated_fallback = validated

    # Pass 2: if no pattern passed the semantic gate, use the pattern with the
    # MOST validated items (larger catalog is more likely real products, not
    # a carousel or featured-category widget with only a few items).
    if best_validated_fallback is not None and len(best_validated_fallback) >= MIN_PRODUCTS:
        _log.info(
            f"[scraper] Semantic gate rejected all patterns — using best "
            f"structurally-validated fallback ({len(best_validated_fallback)} elements)"
        )
        return best_validated_fallback

    # Final fallback: return all candidates but remove wrapper elements that
    # contain other candidate elements (prevents parent containers from
    # being treated as product cards alongside their children).
    return _remove_ancestor_cards(candidates)


def debug_scrape(url: str) -> dict:
    """
    Diagnostic function — returns a dict of debug info without producing final products.
    Call this to understand why a page returns zero results.
    """
    info = {}

    # --- Fetch ---
    try:
        response = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        info["status_code"] = response.status_code
        info["final_url"] = response.url
        info["redirected"] = response.url != url
        html = response.text
    except Exception as e:
        info["fetch_error"] = str(e)
        return info

    # --- Bot/block detection ---
    lower_html = html.lower()
    info["possible_bot_block"] = any(phrase in lower_html for phrase in [
        "access denied", "403 forbidden", "captcha", "are you a robot",
        "cloudflare", "ddos-guard", "please enable javascript", "checking your browser",
        "you have been blocked",
    ])

    # --- JS-rendered detection ---
    soup_full = BeautifulSoup(html, "html.parser")
    body_text = soup_full.body.get_text(strip=True) if soup_full.body else ""
    info["js_rendered_likely"] = (
        len(body_text) < 500
        or "window.__" in html
        or "react" in lower_html
        or "vue" in lower_html
        or "__NEXT_DATA__" in html
        or "ng-app" in html
    )

    # --- Page title ---
    title_tag = soup_full.find("title")
    info["page_title"] = title_tag.get_text(strip=True) if title_tag else "(no title)"

    # --- Strip scripts for product search (mirrors extract_products) ---
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # --- Candidate product containers ---
    candidates = [el for el in soup.find_all(True) if _class_contains(el, PRODUCT_KEYWORDS)]
    info["candidate_count"] = len(candidates)

    # --- Pattern breakdown ---
    pattern_counts = Counter()
    for el in candidates:
        classes = frozenset(el.get("class", []))
        pattern_counts[(el.name, classes)] += 1
    info["top_patterns"] = [
        {"tag": tag, "classes": sorted(classes), "count": count}
        for (tag, classes), count in pattern_counts.most_common(5)
    ]

    # --- Product links anywhere on page ---
    all_links = soup.find_all("a", href=True)
    product_links = [
        a["href"] for a in all_links
        if any(kw in a["href"].lower() for kw in ["product", "item", "detail", "p/", "/pd/", "/dp/"])
    ]
    info["product_links_found"] = len(product_links)
    info["product_link_samples"] = product_links[:5]

    # --- HTML sample (first 2000 chars of body) ---
    body = soup_full.find("body")
    info["html_sample"] = str(body)[:2000] if body else html[:2000]

    return info


def extract_products(html: str, base_url: str) -> list:
    """
    Parse the HTML and extract product data from listing/category pages.

    Returns a list of dicts, each with keys:
        product_name, brand, price, product_url, image_url, sku
    Missing fields are empty strings.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove script/style tags to avoid false matches in text searches
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    cards = _find_product_cards(soup, base_url)

    products = []
    seen_urls = set()  # deduplicate by product URL

    rejected = 0
    for card in cards:
        name = _extract_name(card)
        brand = _extract_brand(card)
        price = _extract_price(card)
        product_url = _extract_product_url(card, base_url)
        image_url = _extract_image_url(card, base_url)
        sku = _extract_sku(card)

        reject_reason = ""

        # ── Positive-signal validation ─────────────────────────────────────
        # Require at least TWO of: name, product_url, image_url.
        # A card that only has a name (nav link text) or only a URL
        # (breadcrumb anchor) is not a product.
        positive_signals = sum([bool(name), bool(product_url), bool(image_url)])
        if positive_signals < 2:
            reject_reason = f"only {positive_signals} positive signal(s) (name/url/image)"

        # ── Data-richness check ───────────────────────────────────────────
        # A product must have at least a name OR (price + sku).
        # Category tiles that were name-rejected land here with
        # empty name/price/sku but valid URL+image — reject them.
        if not reject_reason and not name and not price and not sku:
            reject_reason = "no name, no price, and no SKU — likely a category tile or nav element"

        # ── Category-name rejection ────────────────────────────────────────
        # Reject candidates whose name looks like a category hierarchy label
        # ("Family Care : Dishwashing") or a navigation tile ("Shop All").
        # This catches collection tiles even when the URL pattern doesn't match.
        if not reject_reason and _looks_like_category_name(name):
            reject_reason = f"name looks like category/collection: {name!r}"

        # ── Collection/category URL rejection ─────────────────────────────
        # Reject any candidate whose resolved URL points to a collection,
        # category, or other non-product-detail page.
        if not reject_reason and product_url:
            from urllib.parse import urlparse as _urlparse
            path = _urlparse(product_url).path.lower()
            if any(pat in path for pat in _COLLECTION_URL_PATTERNS):
                reject_reason = f"URL is a collection/category path: {product_url!r}"

        if reject_reason:
            rejected += 1
            import logging as _logging
            _logging.getLogger(__name__).debug(
                f"[scraper] candidate rejected — {reject_reason}"
            )
            continue

        # ── Deduplicate by product URL ────────────────────────────────────
        if product_url and product_url in seen_urls:
            continue
        if product_url:
            seen_urls.add(product_url)

        # ── SKU-from-name extraction ───────────────────────────────────────
        # Some wholesale sites embed the item number in the product name,
        # e.g. "34768 - AFTERSHOCKS GUMMY FRUITY...". Extract it as SKU
        # and clean the name.
        if name and not sku:
            sku_name_match = re.match(r'^(\d{4,8})\s*[-–]\s*(.+)', name)
            if sku_name_match:
                sku = sku_name_match.group(1)
                name = sku_name_match.group(2).strip()

        # ── Title-based pack/case parsing ──────────────────────────────────
        # Extracts unit_size and case_pack from title abbreviations like
        # "16oz/3pk", "20oz/24pk", "75ct/6pk" when not available elsewhere.
        unit_size = ""
        case_pack = ""
        if name:
            try:
                from strategies.pack_parser import parse_pack_from_title
                parsed_pack = parse_pack_from_title(name)
                if parsed_pack and parsed_pack.get("pack_confidence", 0) >= 0.60:
                    unit_size = parsed_pack.get("unit_size", "")
                    case_pack = parsed_pack.get("case_pack", "")
            except Exception:
                pass

        products.append({
            "product_name": name,
            "brand":        brand,
            "price":        price,
            "product_url":  product_url,
            "image_url":    image_url,
            "sku":          sku,
            "unit_size":    unit_size,
            "case_pack":    case_pack,
        })

    if rejected:
        import logging as _logging
        _logging.getLogger(__name__).info(
            f"[scraper] extract_products: {rejected} candidate(s) rejected as "
            f"non-product (category URL, nav name, or insufficient signals); "
            f"{len(products)} product(s) accepted"
        )

    # ── Post-extraction price validation ───────────────────────────────────
    # Flag suspicious price patterns (same price on >80% of products)
    if len(products) > 5:
        import logging as _logging
        _log = _logging.getLogger(__name__)

        price_counter = Counter(p.get("price", "") for p in products if p.get("price"))
        if price_counter:
            most_common_price, count = price_counter.most_common(1)[0]
            pct_same = count / len(products) if products else 0

            if pct_same > 0.80 and most_common_price:
                _log.warning(
                    f"[scraper] Price anomaly: {count}/{len(products)} products "
                    f"({pct_same*100:.1f}%) have identical price '{most_common_price}'. "
                    f"This suggests a price extraction issue (shared banner, nested cards, etc.). "
                    f"Attempting re-extraction with stricter scoping..."
                )

                # Re-extract prices with stricter approach (explicit classes only, no fallback)
                re_extracted_prices = []
                for i, (card, product) in enumerate(zip(cards, products)):
                    if i >= len(products):
                        break
                    try:
                        strict_price = _extract_price_strict(card)
                        if strict_price and strict_price != most_common_price:
                            re_extracted_prices.append((i, strict_price))
                    except Exception:
                        pass

                # If re-extraction yielded diverse prices, use them
                if re_extracted_prices and len(re_extracted_prices) > len(products) * 0.3:
                    _log.info(
                        f"[scraper] Re-extraction yielded {len(re_extracted_prices)} "
                        f"diverse prices. Updating products..."
                    )
                    for idx, new_price in re_extracted_prices:
                        if idx < len(products):
                            products[idx]["price"] = new_price
                else:
                    _log.info(
                        f"[scraper] Re-extraction inconclusive. Keeping original prices."
                    )

    return products


def _extract_price_strict(card) -> str:
    """
    Extract price using ONLY explicit price/cost CSS classes.
    No fallback to broad text search. Used for re-validation when
    suspicious price patterns are detected.
    """
    def _normalize_price_text(text: str) -> str:
        text = re.sub(r'\s+', ' ', text).strip()
        text = re.sub(r'^last\s+purchased\s*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'^plus\s+member\s+price\s*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'^member\s+price\s*', '', text, flags=re.IGNORECASE)

        all_prices = re.findall(r'\$\s*\d[\d,]*(?:\.\d{2})?', text)
        if all_prices:
            if 'regularly' in text.lower() and len(all_prices) >= 2:
                preferred = all_prices[0]
            elif len(all_prices) > 1:
                try:
                    prices_float = []
                    for p in all_prices:
                        p_clean = p.replace('$', '').replace(',', '').strip()
                        prices_float.append((float(p_clean), p))
                    prices_float.sort()
                    preferred = prices_float[0][1]
                except (ValueError, IndexError):
                    preferred = all_prices[0]
            else:
                preferred = all_prices[0]

            suffix_match = re.search(r'(?:/\s*(?:case|each|ea|cs|box|pack|set|dozen|dz|lb|oz|gal|qt|pt))', text, re.IGNORECASE)
            if suffix_match:
                suffix_part = re.sub(r'\s+', '', suffix_match.group(0))
                preferred = f"{preferred}{suffix_part}"
            return preferred.replace(' / ', '/').strip()

        return text.replace(' / ', '/').strip()

    # Only look for elements with explicit price classes, immediate children/descendants only
    for el in card.find_all(True, limit=50):  # limit to avoid huge trees
        if el.name in ["script", "style", "form", "select", "option"]:
            continue

        classes = ' '.join(el.get('class', [])).lower()
        if not any(kw in classes for kw in ['price', 'cost', 'pricing']):
            continue

        text = _get_text(el)
        if not text or '$' not in text or len(text) > 160:
            continue

        normalized = _normalize_price_text(text)
        if normalized and '$' in normalized:
            return normalized

    return ""
