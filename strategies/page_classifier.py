"""
strategies/page_classifier.py — Lightweight page-type detection

classify(html, url) returns a ClassificationResult with:
  .type        one of: "listing_grid", "row_catalog", "detail_page",
                        "js_app", "login_required"
  .confidence  float 0-1: how certain the classifier is
  .reason      human-readable explanation of why this type was chosen

ClassificationResult compares equal to its .type string so all existing
router code like  `if page_type == "row_catalog"`  continues to work
without modification.

Used by router.py to tune strategy selection without adding extra HTTP requests.
All classification is done on the already-fetched HTML — zero extra requests.
"""

import dataclasses
import logging
import re
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class ClassificationResult:
    """
    Result of classify().  Compares equal to its .type string so that all
    existing `if page_type == "..."` comparisons in router.py still work.

    Attributes:
        type:       One of listing_grid | row_catalog | detail_page |
                    js_app | login_required
        confidence: Float 0–1.  Rough estimate; not a calibrated probability.
        reason:     One-line human-readable explanation of the decision.
    """
    type:       str
    confidence: float
    reason:     str

    # ── String-comparison transparency ──────────────────────────────────────
    def __eq__(self, other):
        if isinstance(other, str):
            return self.type == other
        if isinstance(other, ClassificationResult):
            return self.type == other.type
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        return hash(self.type)

    def __str__(self):
        return self.type

    def __repr__(self):
        return (
            f"ClassificationResult(type={self.type!r}, "
            f"confidence={self.confidence:.2f}, reason={self.reason!r})"
        )

# Minimum text content length below which we suspect a JS-rendered shell page
_JS_APP_MIN_CONTENT = 500

# Minimum number of product-card-like elements to count as a listing_grid
_LISTING_GRID_MIN_CARDS = 3

# Minimum number of table rows with product data to count as a row_catalog
_ROW_CATALOG_MIN_ROWS = 3

# Class/id signals for product card elements (listing grid)
_CARD_CLASS_SIGNALS = [
    "product", "item", "card", "tile", "grid-item",
    "listing", "result", "catalogue", "prod-",
]

# Class/id signals for B2B row catalog tables
_ROW_CATALOG_SIGNALS = [
    "product-list", "item-list", "catalog", "catalogue",
    "price-list", "order-form", "product-row", "item-row",
]

# Login form signals
_LOGIN_SIGNALS = [
    "login", "sign-in", "signin", "log-in", "password", "username",
]

# JS framework signals in page source
_JS_FRAMEWORK_PATTERNS = [
    r"__NEXT_DATA__",
    r"window\.__nuxt__",
    r"window\.React",
    r'id=["\']app["\']',
    r'id=["\']root["\']',
    r"ng-app",
    r"ng-controller",
    r"data-reactroot",
    r"vue-app",
    r"data-bind=",            # KnockoutJS
    r"ko\.applyBindings",     # KnockoutJS
    r"ember-application",     # Ember.js
    r"data-ember",            # Ember.js
]

# JSON-LD / itemprop signals for detail page
_DETAIL_PAGE_SIGNALS = [
    '"@type": "Product"',
    '"@type":"Product"',
    'itemtype="http://schema.org/Product"',
    'itemtype="https://schema.org/Product"',
]


def _count_jsonld_products(soup: BeautifulSoup) -> int:
    """
    Count the number of JSON-LD Product objects on the page.

    A listing page with per-product JSON-LD will have multiple Product blocks;
    a single product detail page will have exactly one.  This distinction is
    critical for accurate page classification.

    Handles: direct Product objects, @graph arrays, ItemList wrappers,
    and multiple <script type="application/ld+json"> blocks.
    """
    import json
    count = 0
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                dtype = item.get("@type", "")
                if isinstance(dtype, list):
                    dtype_set = set(dtype)
                else:
                    dtype_set = {dtype}

                if "Product" in dtype_set:
                    count += 1
                # Count Products inside @graph arrays
                for graph_item in item.get("@graph", []):
                    if isinstance(graph_item, dict):
                        gt = graph_item.get("@type", "")
                        if (isinstance(gt, list) and "Product" in gt) or gt == "Product":
                            count += 1
                # Count Products inside ItemList / CollectionPage wrappers
                if dtype_set & {"ItemList", "CollectionPage", "SearchResultsPage",
                                "OfferCatalog", "ProductCollection"}:
                    for list_item in item.get("itemListElement", []):
                        if isinstance(list_item, dict):
                            inner = list_item.get("item", list_item)
                            if isinstance(inner, dict):
                                it = inner.get("@type", "")
                                if (isinstance(it, list) and "Product" in it) or it == "Product":
                                    count += 1
        except (json.JSONDecodeError, Exception):
            continue
    return count


def _count_card_elements(soup: BeautifulSoup) -> int:
    """Count repeated product-card-like elements while avoiding broad wrapper hits."""
    from collections import Counter

    signature_counts = Counter()

    for el in soup.find_all(True):
        try:
            classes = el.get("class") or []
            classes_str = " ".join(classes).lower()
            el_id = (el.get("id") or "").lower()
            combined = f"{classes_str} {el_id}".strip()
            if not combined:
                continue
            if not any(sig in combined for sig in _CARD_CLASS_SIGNALS):
                continue

            # Ignore likely mega-wrappers that contain many nested cards.
            child_tags = [c for c in el.children if getattr(c, "name", None) is not None]
            if len(child_tags) >= 12:
                continue

            signature = (el.name, tuple(sorted(c.lower() for c in classes)))
            signature_counts[signature] += 1
        except Exception:
            continue

    if not signature_counts:
        return 0

    return max(signature_counts.values())


def _count_catalog_rows(soup: BeautifulSoup) -> int:
    """Count repeated table/list rows that plausibly represent catalog products."""
    from collections import Counter

    # Look for repeated li/tr children inside catalog-like containers first.
    signature_counts = Counter()
    for t in soup.find_all(["table", "ul", "ol", "div"]):
        try:
            classes = " ".join(t.get("class") or []).lower()
            if not any(sig in classes for sig in _ROW_CATALOG_SIGNALS):
                continue
            for child in t.find_all(["tr", "li"], recursive=False):
                child_classes = tuple(sorted(c.lower() for c in (child.get("class") or [])))
                signature = (child.name, child_classes)
                signature_counts[signature] += 1
        except Exception:
            continue

    if signature_counts:
        best = max(signature_counts.values())
        if best >= _ROW_CATALOG_MIN_ROWS:
            return best

    # Fallback: count <tr> rows containing product-like data.
    rows = soup.find_all("tr")
    product_rows = 0
    for row in rows:
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) >= 2:
            text = row.get_text(separator=" ", strip=True).lower()
            if any(kw in text for kw in ["$", "price", "sku", "item", "upc", "pack"]):
                product_rows += 1
    return product_rows


def _has_login_form(soup: BeautifulSoup) -> bool:
    """Return True if the page appears to require login."""
    # Check for password input field (strongest signal)
    if soup.find("input", {"type": "password"}):
        return True

    # Check form action or surrounding text
    for form in soup.find_all("form"):
        action = (form.get("action") or "").lower()
        if any(sig in action for sig in ["login", "sign-in", "signin", "auth"]):
            return True

    # Check page-level class/id signals
    for el in soup.find_all(True):
        combined = " ".join(el.get("class") or []).lower() + " " + (el.get("id") or "").lower()
        if any(sig in combined for sig in ["login-form", "signin-form", "auth-form", "login-page"]):
            return True

    return False


def _is_detail_page(html: str, soup: BeautifulSoup) -> bool:
    """Return True if the page looks like a single product detail page."""
    # Check for JSON-LD Product schema
    if any(signal in html for signal in _DETAIL_PAGE_SIGNALS):
        return True

    # Single H1 + price element can indicate a detail page, but many listing
    # pages also contain one page title H1 plus repeated price elements. To
    # avoid false positives, require the page to NOT already look like a
    # substantial listing/grid/row catalog.
    card_count = _count_card_elements(soup)
    row_count = _count_catalog_rows(soup)

    h1_tags = soup.find_all("h1")
    if len(h1_tags) == 1 and card_count < _LISTING_GRID_MIN_CARDS and row_count < _ROW_CATALOG_MIN_ROWS:
        price_el = soup.find(
            lambda el: el.name not in ["script", "style"]
            and any(kw in " ".join(el.get("class") or []).lower()
                    for kw in ["price", "cost"])
        )
        if price_el is not None:
            return True

    return False


def _is_js_app(html: str, soup: BeautifulSoup) -> bool:
    """Return True if the page is a JS-rendered SPA shell with little real content."""
    body = soup.find("body")
    if body is None:
        return True

    has_js_framework = any(re.search(pat, html) for pat in _JS_FRAMEWORK_PATTERNS)

    body_text = body.get_text(strip=True)
    if len(body_text) < _JS_APP_MIN_CONTENT:
        # Short body — check for JS framework fingerprints
        if has_js_framework:
            return True

    # JS binding frameworks (Knockout, Angular, etc.) often have product card
    # HTML structure in the page but data is injected by JS.  Detect this by
    # checking if product-class elements exist but NO price text ($X.XX) appears.
    if has_js_framework:
        has_product_els = bool(soup.find(
            lambda el: el.name and el.get("class") and any(
                kw in " ".join(el.get("class")).lower()
                for kw in ["product", "item", "card"]
            )
        ))
        has_price_text = bool(re.search(r"\$\d+\.\d{2}", body_text))
        if has_product_els and not has_price_text:
            return True

    return False


def classify(html: str, url: str) -> ClassificationResult:
    """
    Classify the page type from its HTML.

    Returns a ClassificationResult with .type, .confidence, and .reason.
    ClassificationResult compares equal to its .type string, so all existing
    `if page_type == "..."` code continues to work unchanged.

    Never raises — returns listing_grid as the safe default.
    """
    def _result(page_type: str, confidence: float, reason: str) -> ClassificationResult:
        r = ClassificationResult(type=page_type, confidence=confidence, reason=reason)
        logger.info(
            f"[PageClassifier] {url} -> {page_type} "
            f"(confidence={confidence:.0%}, reason: {reason})"
        )
        return r

    try:
        soup = BeautifulSoup(html, "html.parser")

        # Pre-compute card and row counts — needed to disambiguate login widgets
        # from true login gates (many e-commerce pages embed a small login form
        # in the header while the main content is a public product listing).
        card_count = _count_card_elements(soup)
        row_count  = _count_catalog_rows(soup)

        # 1. Login gate — check first since it overrides everything.
        #    BUT: if the page also has substantial product content (cards or rows),
        #    the login form is likely just a header widget, not a gate.
        has_login = _has_login_form(soup)
        if has_login and card_count < _LISTING_GRID_MIN_CARDS and row_count < _ROW_CATALOG_MIN_ROWS:
            return _result("login_required", 0.99, "password input or login form detected")
        elif has_login:
            logger.info(
                f"[PageClassifier] Login form detected but page has "
                f"{card_count} cards / {row_count} rows — treating as public listing"
            )

        # 2. JS SPA shell — body text too short + JS framework fingerprint.
        if _is_js_app(html, soup):
            return _result("js_app", 0.90, "body text short + JS framework fingerprint")

        # 3. Single product detail page.
        #    JSON-LD Product schema is very reliable; H1+price heuristic less so.
        #    BUT: many listing pages embed multiple JSON-LD Product blocks (one per
        #    product in the grid).  Only classify as detail_page when the page has
        #    exactly ONE Product schema block.  Multiple blocks → listing page.
        jsonld_product_count = sum(
            1 for signal in _DETAIL_PAGE_SIGNALS if signal in html
        )
        # More precise: count actual Product objects in JSON-LD blocks
        if jsonld_product_count > 0:
            actual_product_count = _count_jsonld_products(soup)
            if actual_product_count == 1 and card_count < _LISTING_GRID_MIN_CARDS:
                return _result("detail_page", 0.95, "single JSON-LD @type:Product schema found")
            elif actual_product_count > 1:
                logger.info(
                    f"[PageClassifier] {actual_product_count} JSON-LD Product blocks found "
                    f"— treating as listing page, not detail page"
                )

        h1_tags = soup.find_all("h1")
        if len(h1_tags) == 1 and card_count < _LISTING_GRID_MIN_CARDS and row_count < _ROW_CATALOG_MIN_ROWS:
            price_el = soup.find(
                lambda el: el.name not in ["script", "style"]
                and any(kw in " ".join(el.get("class") or []).lower()
                        for kw in ["price", "cost"])
            )
            if price_el is not None:
                return _result(
                    "detail_page", 0.65,
                    "single H1 + price-class element without listing/catalog signals"
                )

        # 4. B2B row / table catalog.
        if row_count >= _ROW_CATALOG_MIN_ROWS and row_count > card_count:
            confidence = min(0.95, 0.60 + row_count * 0.005)
            return _result(
                "row_catalog", round(confidence, 2),
                f"{row_count} product-like table rows (vs {card_count} card elements)"
            )

        # 5. Standard grid/card listing page.
        if card_count >= _LISTING_GRID_MIN_CARDS:
            confidence = min(0.90, 0.55 + card_count * 0.01)
            return _result(
                "listing_grid", round(confidence, 2),
                f"{card_count} product-card elements detected"
            )

        # Default: listing_grid with low confidence — safest fallback.
        return _result(
            "listing_grid", 0.35,
            f"default fallback (rows={row_count}, cards={card_count})"
        )

    except Exception as e:
        logger.warning(f"[PageClassifier] Error classifying {url}: {e} — defaulting to listing_grid")
        return ClassificationResult(
            type="listing_grid", confidence=0.20,
            reason=f"classification error: {e}"
        )
