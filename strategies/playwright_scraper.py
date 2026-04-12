"""
strategies/playwright_scraper.py — Linear Playwright pipeline for JS-rendered sites.

Designed for sites like Betty Mills (KnockoutJS) where:
  - Static HTML has no product data
  - A real browser is required to render the catalog
  - Pagination must stay in the same browser session

Pipeline:
  1. Open one persistent Playwright browser
  2. Render the listing page, wait for products in DOM
  3. Collect all product detail links from the rendered page
  4. Follow pagination with the SAME browser, collecting more links
  5. Visit every detail page and extract fields
  6. Close browser, return results

Entry point:
  run(url, max_pages=3) -> dict with strategy_id, strategy_name, reason, products
"""

import logging
import re
from collections import Counter
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from urllib.parse import parse_qs, urlencode, urlunparse

from scraper import HEADERS
from strategies.detail import _extract_from_detail_page

logger = logging.getLogger(__name__)

ID = 4
NAME = "Playwright JS Pipeline"

# Selectors that signal product content has rendered
_PRODUCT_WAIT_SELECTORS = [
    "[class*='product']", "[class*='item']", "[class*='card']",
    "[data-bind]", "[data-reactroot]", "[class*='price']",
    ".product-list", ".products-grid",
]


def _wait_for_products(page, wait_ms: int = 5000):
    """Wait for product content to appear in the rendered DOM."""
    page.wait_for_timeout(wait_ms)

    # Try to find a product-like selector
    for selector in _PRODUCT_WAIT_SELECTORS:
        try:
            page.wait_for_selector(selector, timeout=3000)
            break
        except Exception:
            continue

    # Wait for price text as a secondary signal
    try:
        page.wait_for_function(
            "() => document.body.innerText.match(/\\$\\d+\\.\\d{2}/)",
            timeout=wait_ms,
        )
    except Exception:
        pass


# ── Universal pagination ────────────────────────────────────────────────────
# Finds the next page using only universal signals that work on every site.

# Text values (lowercased) that indicate a "next page" link or button.
_NEXT_TEXT = {"next", "next page", "next »", "next ›", "next>", ">", "›", "»", ">>", "→"}


def _find_next_page_universal(soup: BeautifulSoup, current_url: str, visited: set) -> str | None:
    """
    Find the next listing page URL using universal signals only.

    Signal A — Link/button text containing "next", ">", "›", "»", "→"
    Signal B — aria-label containing "next" or "next page"
    Signal C — URL incrementing ?page=N or /page/N

    Returns absolute URL or None.
    """
    base_netloc = urlparse(current_url).netloc

    def _is_valid_next(href: str) -> bool:
        if not href or href.startswith("#") or href.startswith("javascript:"):
            return False
        abs_url = urljoin(current_url, href)
        if urlparse(abs_url).netloc != base_netloc:
            return False
        if abs_url in visited:
            return False
        return True

    # ── Signal A: text-based "next" links ────────────────────────────────────
    for el in soup.find_all(["a", "button"]):
        text = el.get_text(strip=True).lower()
        if text in _NEXT_TEXT:
            href = el.get("href")
            if href and _is_valid_next(href):
                return urljoin(current_url, href)

    # ── Signal B: aria-label "next" / "next page" ───────────────────────────
    for el in soup.find_all(["a", "button"], attrs={"aria-label": True}):
        aria = el["aria-label"].lower()
        if "next" in aria:
            href = el.get("href")
            if href and _is_valid_next(href):
                return urljoin(current_url, href)

    # ── Signal C: rel="next" ────────────────────────────────────────────────
    rel_next = soup.find("a", rel="next")
    if rel_next:
        href = rel_next.get("href")
        if href and _is_valid_next(href):
            return urljoin(current_url, href)

    return None


def _try_url_page_increment(current_url: str, next_page_num: int, visited: set) -> str | None:
    """
    Fallback: try incrementing ?page=N or /page/N in the URL.
    Returns candidate URL if it hasn't been visited, else None.
    """
    parsed = urlparse(current_url)
    qs = parse_qs(parsed.query)

    # Try ?page=N
    if "page" in qs:
        qs["page"] = [str(next_page_num)]
        new_query = urlencode(qs, doseq=True)
        candidate = urlunparse(parsed._replace(query=new_query))
        if candidate not in visited:
            return candidate

    # Try adding ?page=N if not present
    if "page" not in qs:
        qs["page"] = [str(next_page_num)]
        new_query = urlencode(qs, doseq=True)
        candidate = urlunparse(parsed._replace(query=new_query))
        if candidate not in visited:
            return candidate

    # Try /page/N path pattern
    path = parsed.path.rstrip("/")
    page_match = re.search(r"/page/(\d+)$", path)
    if page_match:
        new_path = path[:page_match.start()] + f"/page/{next_page_num}"
        candidate = urlunparse(parsed._replace(path=new_path))
        if candidate not in visited:
            return candidate

    return None


# ── Universal junk link exclusions ───────────────────────────────────────────
# Links matching these path fragments are NEVER product detail pages on any site.
# Covers: shopping flow, account, legal, informational, navigation chrome.
_REJECT_PATHS = [
    # Shopping flow
    "/cart", "/basket", "/checkout", "/wishlist", "/compare",
    # Account / auth
    "/account", "/login", "/register", "/signup", "/signin",
    "/users/", "/invoices/", "/my-account", "/profile",
    # Search and filters
    "/search",
    # Legal / informational
    "/terms", "/privacy", "/cookie", "/policy",
    "/contact", "/about", "/faq", "/help", "/support",
    # Content pages
    "/blog", "/pages/", "/tags/", "/brands/",
    # Category/collection navigation (not detail pages)
    "/collections", "/category", "/categories", "/dept/", "/department",
    # Rewards / promo programs
    "/rewards", "/referr", "/redempti",
]


def _is_same_site(href: str, base_netloc: str) -> bool:
    """Check if a URL belongs to the same site."""
    return urlparse(href).netloc == base_netloc


def _is_nav_or_chrome(href: str, base_url: str) -> bool:
    """Reject links that are clearly navigation, account, or utility pages."""
    parsed = urlparse(href)
    path = parsed.path.lower().rstrip("/")
    # Reject site root
    if path == "" or path == "/":
        return True
    # Reject if path matches a known non-product pattern
    if any(path.startswith(p) or p in path for p in _REJECT_PATHS):
        return True
    # Reject if it's the same as the listing page we're on
    if href.rstrip("/") == base_url.rstrip("/"):
        return True
    # Reject fragment-only links
    if parsed.path == urlparse(base_url).path:
        return True
    return False


def _has_url_product_signal(href: str) -> bool:
    """Signal 1: URL path contains a known product-detail indicator."""
    path = urlparse(href).path.lower()
    return any(seg in path for seg in [
        "/product/", "/products/", "/item/", "/items/",
        "/p/", "/dp/", "/pd/", "/sku/",
    ])


def _link_has_image_and_text(anchor: Tag) -> bool:
    """
    Signal 2: The link (or its closest containing block) has both an <img>
    and meaningful text — the universal shape of a product card link.
    """
    # Check the anchor itself first
    scope = anchor
    has_img = scope.find("img") is not None
    text = scope.get_text(strip=True)
    has_text = len(text) > 3

    if has_img and has_text:
        return True

    # Walk up to the nearest block-level parent (the "card")
    # Max 3 levels — beyond that we're outside the card
    parent = anchor.parent
    for _ in range(3):
        if parent is None or parent.name in ("body", "html", "[document]"):
            break
        if parent.name in ("div", "li", "article", "section", "td"):
            has_img = parent.find("img") is not None
            text = parent.get_text(strip=True)
            has_text = len(text) > 3
            if has_img and has_text:
                return True
        parent = parent.parent

    return False


def _is_filter_link_group(hrefs: list[str]) -> bool:
    """
    Detect filter/facet link groups: links that all share the same base path
    and only differ by query parameter (e.g. ?brand=X, ?color=Y).
    Product grids link to distinct paths; filter sidebars don't.
    """
    paths = set()
    for href in hrefs[:30]:  # sample first 30
        parsed = urlparse(href)
        paths.add(parsed.path.rstrip("/"))
    # If 80%+ share the same path, it's a filter group
    if not paths:
        return False
    from collections import Counter as C
    most_common_path, count = C(
        urlparse(h).path.rstrip("/") for h in hrefs[:30]
    ).most_common(1)[0]
    return count / min(len(hrefs), 30) >= 0.8


def _find_repeated_link_groups(anchors: list[Tag], base_url: str) -> list[str]:
    """
    Signal 3: Links that repeat in a grid/list pattern.

    Product grids are structurally repetitive: many sibling elements with the
    same tag+class signature, each containing one link.  We find the parent
    element that contains the most same-structured children with links, and
    extract those links.

    Skips groups that look like filter/facet sidebars (all same base path,
    differing only by query params).

    Returns a list of hrefs from the best repeating group, or [] if none found.
    """
    # Map each anchor to its "card" — the nearest block-level ancestor
    card_signatures: Counter = Counter()  # (parent_id, tag, frozenset(classes)) -> count
    card_links: dict = {}  # signature -> [href, ...]

    for a in anchors:
        href = a.get("href", "")
        if not href:
            continue
        href = urljoin(base_url, href)

        # Find the nearest block-level ancestor as the "card"
        card = a.parent
        for _ in range(4):
            if card is None or card.name in ("body", "html", "[document]"):
                break
            if card.name in ("div", "li", "article", "section", "td"):
                break
            card = card.parent

        if card is None or card.name in ("body", "html", "[document]"):
            continue

        # The card's parent is the "grid container"
        grid = card.parent
        if grid is None:
            continue

        # Signature: grid's identity + card's tag + card's class list
        grid_id = id(grid)
        card_classes = frozenset(card.get("class") or [])
        sig = (grid_id, card.name, card_classes)

        card_signatures[sig] += 1
        if sig not in card_links:
            card_links[sig] = []
        card_links[sig].append(href)

    # Walk groups from largest to smallest, skip filter groups
    if not card_signatures:
        return []

    for sig, count in card_signatures.most_common():
        if count < 3:
            break
        hrefs = card_links[sig]
        if _is_filter_link_group(hrefs):
            logger.info(
                f"[PW Links] Skipping filter group: {count} links, "
                f"tag={sig[1]}, sample={hrefs[0][:60]}"
            )
            continue

        # Deduplicate while preserving order
        seen = set()
        result = []
        for href in hrefs:
            if href not in seen:
                seen.add(href)
                result.append(href)
        return result

    return []


def _collect_links_from_html(html: str, base_url: str) -> list[str]:
    """
    Extract product detail links from rendered HTML using universal signals.

    No site-specific CSS classes.  No hardcoded selectors.  Three signals:

      Signal 1 — URL path: links containing /product/, /item/, /p/, etc.
      Signal 2 — Card shape: links inside elements with both an image and text.
      Signal 3 — Grid pattern: links that repeat as siblings in a list/grid.

    Links that match multiple signals score higher.  Final list is deduplicated
    and filtered to remove navigation/chrome links.
    """
    soup = BeautifulSoup(html, "html.parser")
    base_netloc = urlparse(base_url).netloc

    # Strip nav, header, footer, sidebar regions
    for tag_name in ("nav", "header", "footer", "aside"):
        for el in soup.find_all(tag_name):
            el.decompose()

    # Collect all internal <a> links
    all_anchors = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if not _is_same_site(href, base_netloc):
            continue
        if _is_nav_or_chrome(href, base_url):
            continue
        all_anchors.append(a)

    logger.info(f"[PW Links] {len(all_anchors)} internal links after filtering chrome")

    # ── Score each link ──────────────────────────────────────────────────────
    # Each signal adds 1 point.  We keep links with score >= 1, preferring
    # higher scores.  This lets URL-pattern sites work (signal 1 alone) AND
    # lets sites like Betty Mills work (signals 2+3, no /product/ in URL).

    link_scores: dict[str, int] = {}  # href -> score
    link_anchors: dict[str, Tag] = {}  # href -> first anchor (for signal 2)

    for a in all_anchors:
        href = urljoin(base_url, a["href"])
        if href not in link_scores:
            link_scores[href] = 0
            link_anchors[href] = a

        # Signal 1: URL path
        if _has_url_product_signal(href):
            link_scores[href] = max(link_scores[href], 1)

    # Signal 2: image + text (check each unique link once)
    for href, a in link_anchors.items():
        if _link_has_image_and_text(a):
            link_scores[href] += 1

    # Signal 3: grid/list repetition
    grid_links = _find_repeated_link_groups(all_anchors, base_url)
    grid_set = set(grid_links)
    for href in grid_set:
        if href in link_scores:
            link_scores[href] += 1
        else:
            link_scores[href] = 1

    # ── Assemble results ─────────────────────────────────────────────────────
    # Collect links that passed image+text check (signal 2) — these are the
    # strongest product signal because every product card has an image + name.
    s2_links = [href for href, a in link_anchors.items() if _link_has_image_and_text(a)]

    seen_result = set()
    results = []

    def _add_result(href):
        if href not in seen_result:
            seen_result.add(href)
            results.append(href)

    # If we have enough image+text links, use them as the primary source.
    # Grid-only links (no image+text) are likely subcategory navigation.
    if len(s2_links) >= 3:
        for href in s2_links:
            _add_result(href)
        # Also add any grid links that ALSO passed signal 2
        for href in grid_links:
            if href in link_anchors and _link_has_image_and_text(link_anchors[href]):
                _add_result(href)
    else:
        # Few or no image+text links — use all signals equally
        for href in grid_links:
            if link_scores.get(href, 0) >= 1:
                _add_result(href)
        for href, score in sorted(link_scores.items(), key=lambda x: -x[1]):
            if score >= 1:
                _add_result(href)

    # Log signal breakdown
    s1 = sum(1 for h in results if _has_url_product_signal(h))
    s2 = sum(1 for h in results if h in link_anchors and _link_has_image_and_text(link_anchors[h]))
    s3 = sum(1 for h in results if h in grid_set)
    logger.info(
        f"[PW Links] {len(results)} product links collected | "
        f"url_signal={s1} image+text={s2} grid_pattern={s3}"
    )

    return results


def run(url: str, max_pages: int = 3) -> dict:
    """
    Full Playwright pipeline: render, collect links, paginate, extract.

    Args:
        url:        Listing/category page URL.
        max_pages:  Maximum number of listing pages to traverse.

    Returns:
        Standard result dict with strategy_id, strategy_name, reason, products.
    """
    from playwright.sync_api import sync_playwright

    logger.info(f"[PW Pipeline] Starting for: {url}")
    logger.info(f"[PW Pipeline] Max pages: {max_pages}")

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=HEADERS["User-Agent"],
        viewport={"width": 1920, "height": 1080},
    )
    page = context.new_page()

    try:
        all_product_links = []
        pages_visited = 0
        visited_urls = set()
        current_url = url

        # ── STEP 1-3: Render listing pages and collect ALL links first ───────
        while pages_visited < max_pages:
            pages_visited += 1

            if current_url in visited_urls:
                logger.info(f"[PW Pipeline] Already visited {current_url} — stopping")
                break
            visited_urls.add(current_url)

            logger.info(f"[PW Pipeline] Rendering page {pages_visited}/{max_pages}: {current_url}")
            page.goto(current_url, wait_until="domcontentloaded", timeout=30000)
            _wait_for_products(page, wait_ms=5000)

            html = page.content()
            logger.info(f"[PW Pipeline] Page {pages_visited} rendered: {len(html)} chars")

            # Verify we got product content
            has_prices = bool(re.search(r"\$\d+\.\d{2}", html))
            logger.info(f"[PW Pipeline] Page {pages_visited} has prices: {has_prices}")

            # Collect product links from this page
            page_links = _collect_links_from_html(html, current_url)
            new_links = [l for l in page_links if l not in set(all_product_links)]
            all_product_links.extend(new_links)
            logger.info(
                f"[PW Pipeline] Page {pages_visited}: "
                f"{len(page_links)} links found, {len(new_links)} new "
                f"(total: {len(all_product_links)})"
            )

            if not has_prices and pages_visited == 1:
                logger.warning(
                    "[PW Pipeline] No prices found on page 1 — "
                    "site may not be rendering properly"
                )

            # Find next page using universal signals
            if pages_visited >= max_pages:
                logger.info(f"[PW Pipeline] Reached max pages ({max_pages}) — stopping pagination")
                break

            soup = BeautifulSoup(html, "html.parser")
            next_url = _find_next_page_universal(soup, current_url, visited_urls)

            if not next_url:
                next_url = _try_url_page_increment(current_url, pages_visited + 1, visited_urls)
                if next_url:
                    logger.info(f"[PW Pipeline] No 'Next' link — trying URL increment")

            if not next_url:
                logger.info(f"[PW Pipeline] No more pages after page {pages_visited}")
                break

            logger.info(f"[PW Pipeline] Next page: {next_url}")
            current_url = next_url

        logger.info(
            f"[PW Pipeline] Link collection complete: "
            f"{len(all_product_links)} product links from {pages_visited} page(s)"
        )

        if not all_product_links:
            return {
                "strategy_id": ID,
                "strategy_name": NAME,
                "reason": (
                    f"Playwright rendered {pages_visited} page(s) but found "
                    f"no product detail links. The site may use an unusual layout."
                ),
                "products": [],
            }

        # ── STEP 4: Visit every detail page and extract ──────────────────────
        products = []
        for i, product_url in enumerate(all_product_links, 1):
            logger.info(
                f"[PW Pipeline] Detail {i}/{len(all_product_links)}: {product_url}"
            )
            try:
                page.goto(product_url, wait_until="domcontentloaded", timeout=30000)
                _wait_for_products(page, wait_ms=3000)
                detail_html = page.content()

                product = _extract_from_detail_page(detail_html, product_url)
                if product and product.get("product_name"):
                    products.append(product)
                    logger.info(
                        f"[PW Pipeline] Detail {i}: "
                        f"{product.get('product_name', '?')[:50]} | "
                        f"${product.get('price', '?')} | "
                        f"UPC={product.get('upc', '') or 'missing'}"
                    )
                else:
                    logger.warning(f"[PW Pipeline] Detail {i}: no product data extracted")

            except Exception as e:
                logger.warning(f"[PW Pipeline] Detail {i} failed ({e}) — skipping")

        # ── STEP 5: Summary ──────────────────────────────────────────────────
        has_name = sum(1 for p in products if p.get("product_name"))
        has_price = sum(1 for p in products if p.get("price"))
        has_upc = sum(1 for p in products if p.get("upc"))
        has_gtin = sum(1 for p in products if p.get("gtin"))
        has_ean = sum(1 for p in products if p.get("ean"))
        has_sku = sum(1 for p in products if p.get("sku"))
        has_case = sum(1 for p in products if p.get("case_pack"))

        logger.info(
            f"[PW Pipeline] COMPLETE: {len(products)} products from "
            f"{len(all_product_links)} detail pages across {pages_visited} listing page(s) | "
            f"names={has_name} prices={has_price} UPCs={has_upc} "
            f"GTINs={has_gtin} EANs={has_ean} "
            f"SKUs={has_sku} case_packs={has_case}"
        )

        missing_upc = [p.get("product_name", "?")[:40] for p in products if not p.get("upc")]
        if missing_upc:
            logger.info(
                f"[PW Pipeline] Missing UPC on {len(missing_upc)} product(s): "
                f"{missing_upc[:5]}{'...' if len(missing_upc) > 5 else ''}"
            )

        reason = (
            f"Playwright rendered {pages_visited} listing page(s), "
            f"collected {len(all_product_links)} product links, "
            f"extracted {len(products)} products"
        )

        return {
            "strategy_id": ID,
            "strategy_name": NAME,
            "reason": reason,
            "products": products,
        }

    finally:
        logger.info("[PW Pipeline] Closing browser")
        browser.close()
        pw.stop()
