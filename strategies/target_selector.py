"""
strategies/target_selector.py — Product-target selection engine

For every detected product row/card, discovers all clickable candidates,
scores them by likelihood of leading to rich product-detail data, then
provides a fetch + validate + fallback loop so the scraper always ends up
on the most information-dense page available.

Public API
──────────
  discover_candidates(container, base_url)
      → list[Candidate]  (sorted by score, descending)

  validate_rich_detail(html)
      → RichDetailResult  (score + signal list + is_rich flag)

  select_best_target(candidates, fetch_fn, row_index)
      → (url, html, RichDetailResult) | None
        Fetches candidates in score order, validates each, returns the first
        "rich" one.  Falls back to the best non-rich result if none pass.

Integration points in detail.py
────────────────────────────────
  Pass 0  (_collect_product_links_with_alternatives):
    discover_candidates() per container — scoring only, no extra fetches.
    primary = candidates[0].url
    alternatives = [c.url for c in candidates[1:]]

  run() detail-scraping phase:
    After fetching the primary URL, call validate_rich_detail().
    If not rich, call select_best_target() on the stored alternatives.

  enrich_from_detail_pages():
    After fetching each product's detail page, call validate_rich_detail()
    and log the richness score for diagnostics.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# ── Candidate scoring signals ────────────────────────────────────────────────

# Context keywords found IN THE CONTAINER TEXT that indicate rich product data nearby
_HIGH_VALUE_CONTEXT_KEYWORDS = [
    "order", "details", "detail", "item", "product", "sku",
    "barcode", "gtin", "upc", "ean", "specifications", "specs",
    "case", "pack", "unit", "price", "qty", "quantity",
    "part number", "catalog", "model",
]

# Anchor / button text that strongly suggests a product detail link
_DETAIL_LINK_TEXTS = [
    "details", "detail", "view details", "more details",
    "view product", "product details", "item details",
    "view item", "quick view", "info", "information",
    "order", "view", "see more",
]

# Anchor / button text that suggests a cart / checkout action (not a detail link)
_ACTION_TEXTS = [
    "add to cart", "add to bag", "buy now", "checkout",
    "purchase", "add to order", "buy",
]

# Navigation / utility text — these are NOT product detail links
_NAV_TEXTS = [
    "view all", "see all", "shop all", "browse", "next", "previous",
    "prev", "back", "home", "filter", "sort", "contact",
    "about", "faq", "help", "login", "sign in", "register",
]

# URL path fragments that strongly indicate a product detail page
_PRODUCT_URL_HIGH = [
    "/products/", "/product/", "/item/", "/items/",
    "/detail/", "/pd/", "/catalog/product/", "/shop/product/",
    "/sku/", "/prod/",
]

# URL path fragments that give a moderate confidence boost
_PRODUCT_URL_MED = [
    "/p/", "/view/", "/buy/",
]

# URL path fragments that disqualify a link from being a product detail link
_REJECT_URL_FRAGMENTS = [
    "/collections/", "/category/", "/categories/",
    "/search", "/cart", "/checkout", "/account",
    "/login", "/register", "/wishlist", "/compare",
    "/pages/", "/blogs/", "/tag/", "/tags/",
    "/contact", "/about", "/faq", "/help",
    "javascript:", "mailto:", "tel:",
]


# ── Rich-detail validation signals ───────────────────────────────────────────

# Label strings whose presence in page text indicates rich product data
_RICH_LABEL_SIGNALS = [
    # Identifiers
    ("upc",             2),
    ("barcode",         2),
    ("gtin",            2),
    ("ean",             1),
    ("sku",             1),
    ("item no",         2),
    ("item number",     2),
    ("item #",          2),
    ("item code",       2),
    ("part number",     1),
    # Pack / sizing
    ("case pack",       2),
    ("master case",     2),
    ("units per case",  2),
    ("pack size",       1),
    ("unit size",       1),
    # Pricing structure
    ("unit price",      1),
    ("price per unit",  1),
    ("price table",     2),
    ("quantity break",  2),
    ("price tier",      2),
    # Structured content
    ("specifications",  1),
    ("attributes",      1),
]

# Minimum accumulated score to consider a page "rich"
RICH_DETAIL_THRESHOLD = 4

# Minimum score for a candidate to be tried at all (< this → skip)
CANDIDATE_MIN_SCORE = 1

# Max candidates to try per row before giving up
MAX_CANDIDATES_PER_ROW = 5


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    """A single clickable target extracted from a product card/row."""
    url:         str
    element_tag: str          # "a", "button", "div", etc.
    text:        str          # visible text of the element
    source:      str          # "href", "onclick_url", "data_href", "data_url"
    wraps_image: bool = False # True if the element contains an <img>
    score:       int  = 0
    reasons:     list = field(default_factory=list)


@dataclass
class RichDetailResult:
    """Result of validating whether a fetched page is a rich product detail page."""
    score:         int
    signals_found: list[str]
    is_rich:       bool


# ── Candidate discovery ───────────────────────────────────────────────────────

def discover_candidates(container, base_url: str) -> list[Candidate]:
    """
    Extract ALL clickable candidates from a product card/row element.

    Sources:
      1. <a href="...">  — standard anchor links
      2. onclick="location.href='...'"  — JavaScript navigation
      3. data-href / data-url / data-link / data-product-url — data-attribute URLs

    Each candidate is scored immediately. Returns the list sorted by score
    descending so candidates[0] is always the best choice.

    Never raises — returns [] on error.
    """
    try:
        base_netloc = urlparse(base_url).netloc
        container_text = container.get_text(separator=" ", strip=True).lower()
        candidates: list[Candidate] = []
        seen_urls: set[str] = set()

        # ── Source 1: <a href> ──────────────────────────────────────────────────────
        for a in container.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue
            url = urljoin(base_url, href)
            if urlparse(url).netloc != base_netloc:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)

            text = a.get_text(separator=" ", strip=True)
            wraps_img = bool(a.find("img"))
            c = Candidate(
                url=url, element_tag="a", text=text,
                source="href", wraps_image=wraps_img,
            )
            _score_candidate(c, container_text)
            candidates.append(c)

        # ── Source 2: onclick navigation ──────────────────────────────────────
       _ONCLICK_RE = re.compile(
            r"(?:location\.href|window\.location(?:\.href)?)\s*=\s*\['\"]([^'\"]+)['\"]",
            re.IGNORECASE,
        )
        for el in container.find_all(attrs={"onclick": True}):
            onclick = el.get("onclick") or ""
            m = _ONCLICK_RE.search(onclick)
            if not m:
                # Fallback: any quoted path-like string in onclick
                m = re.search(r"['\"]([/]^[]'\"?#]+)['\"]", onclick)
            if not m:
                continue
            href = m.group(1).strip()
            url = urljoin(base_url, href)
            if urlparse(url).netloc != base_netloc:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)

            text = el.get_text(separator=" ", strip=True)
            c = Candidate(
                url=url, element_tag=el.name or "div", text=text,
                source="onclick_url",
            )
            _score_candidate(c, container_text)
            candidates.append(c)

        # ── Source 3: data-href / data-url / data-link / data-product-url ─────
        for attr in ["data-href", "data-url", "data-link", "data-product-url",
                     "data-pdp-url", "data-item-url"]:
            for el in container.find_all(attrs={attr: True}):
                href = (el.get(attr) or "").strip()
                if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
                    continue
                url = urljoin(base_url, href)
                if urlparse(url).netloc != base_netloc:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                text = el.get_text(separator=" ", strip=True)
                wraps_img = bool(el.find("img"))
                c = Candidate(
                    url=url, element_tag=el.name or "div", text=text,
                    source=attr, wraps_image=wraps_img,
                )
                _score_candidate(c, container_text)
                candidates.append(c)

        # Sort best-first
        candidates.sort(key=lambda x: x.score, reverse=True)

        logger.debug(
            f"[TargetSelector] {len(candidates)} candidate(s) found in container"
        )
        for i, c in enumerate(candidates[:8]):
            logger.debug(
                f"  [{i+1}] score={c.score:+d} source={c.source} "
                f"text={c.text[:40]!r} url={c.url!r} reasons={c.reasons}"
            )

        return candidates

    except Exception as e:
        logger.warning(f"[TargetSelector] discover_candidates error: {e}")
        return []


def _score_candidate(c: Candidate, container_text: str) -> None:
    """
    Score a Candidate in-place using URL, text, and container context signals.

    Higher score = more likely to lead to a rich product detail page.
    Negative score = likely a navigation/action link, skip.
    """
    score = 0
    reasons: list[str] = []
    url_lower = c.url.lower()
    text_lower = c.text.lower().strip()

    # ── URL signals ───────────────────────────────────────────────────────────
    rejected_by_url = False
    for frag in _REJECT_URL_FRAGMENTS:
        if frag in url_lower:
            score -= 5
            reasons.append(f"url_reject:{frag}")
            rejected_by_url = True
            break

    if not rejected_by_url:
        for pat in _PRODUCT_URL_HIGH:
            if pat in url_lower:
                score += 4
                reasons.append(f"url_high:{pat}")
                break
        else:
            for pat in _PRODUCT_URL_MED:
                if pat in url_lower:
                    score += 2
                    reasons.append(f"url_med:{pat}")
                    break

        # URL slug quality: meaningful non-numeric slug is a good signal
        try:
            slug = url_lower.rstrip("/").split("/")[-1].split("?")[0]
            if slug and len(slug) > 4 and not slug.isdigit():
                score += 1
                reasons.append("good_slug")
        except Exception:
            pass

    # ── Candidate text signals ────────────────────────────────────────────────
    if not text_lower:
        # Empty text = image-only link — usually a product tile image
        score += 1
        reasons.append("img_link")
    elif any(kw in text_lower for kw in _NAV_TEXTS):
        score -= 3
        reasons.append("nav_text")
    elif any(kw in text_lower for kw in _ACTION_TEXTS):
        score -= 2
        reasons.append("action_text")
    elif any(kw in text_lower for kw in _DETAIL_LINK_TEXTS):
        score += 3
        reasons.append("detail_text")
    elif len(text_lower) > 100:
        score -= 1
        reasons.append("long_text")

    # ── Image-wrap bonus ──────────────────────────────────────────────────────
    if c.wraps_image:
        score += 2
        reasons.append("wraps_img")

    # ── Source bonus (data attributes are usually intentional product links) ──
    if c.source in ("data-href", "data-url", "data-product-url",
                    "data-pdp-url", "data-item-url"):
        score += 2
        reasons.append("data_attr")

    # ── Container context bonus ───────────────────────────────────────────────
    # How many high-value data keywords appear anywhere in the container?
    context_hits = sum(1 for kw in _HIGH_VALUE_CONTEXT_KEYWORDS if kw in container_text)
    if context_hits >= 5:
        score += 2
        reasons.append(f"rich_ctx({context_hits})")
    elif context_hits >= 2:
        score += 1
        reasons.append(f"ctx({context_hits})")

    c.score = score
    c.reasons = reasons


# ── Rich-detail validation ────────────────────────────────────────────────────

def validate_rich_detail(html: str) -> RichDetailResult:
    """
    Analyse a fetched HTML page and judge whether it is a rich product-detail
    page (contains barcode/UPC/SKU/case-pack/pricing-tier data).

    Scoring:
      JSON-LD Product schema    +3  (strongest structural signal)
      UPC/barcode digit string  +2  (8–14 digit number near a label)
      Structured spec table     +1  (dl/dt/dd or multi-row table)
      Each matching label kw    +N  (see _RICH_LABEL_SIGNALS)

    Returns RichDetailResult.is_rich = True when score >= RICH_DETAIL_THRESHOLD.
    Never raises.
    """
    signals: list[str] = []
    score = 0

    try:
        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text(separator=" ", strip=True).lower()

        # JSON-LD Product schema
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            raw = script.string or ""
            if '"Product"' in raw or "'Product'" in raw:
                signals.append("json_ld_product")
                score += 3
                break

        # Label keyword presence
        for kw, pts in _RICH_LABEL_SIGNALS:
            if kw in page_text:
                signals.append(kw)
                score += pts

        # UPC/barcode-like digit string (8–14 consecutive digits)
        if re.search(r"\b\d{8,14}\b", page_text):
            signals.append("upc_digits")
            score += 2

        # Structured spec block: definition list or multi-row table
        dt_count = len(soup.find_all("dt"))
        if dt_count >= 2:
            signals.append("spec_dl")
            score += 1

        for tbl in soup.find_all("table"):
            if len(tbl.find_all("tr")) >= 3:
                signals.append("spec_table")
                score += 1
                break

    except Exception as e:
        logger.debug(f"[TargetSelector] validate_rich_detail error: {e}")

    is_rich = score >= RICH_DETAIL_THRESHOLD
    return RichDetailResult(score=score, signals_found=signals, is_rich=is_rich)


# ── Target selection with fetch + fallback ────────────────────────────────────

def select_best_target(
    candidates: list[Candidate],
    fetch_fn,
    row_index: int = 0,
) -> tuple[str, str, RichDetailResult] | None:
    """
    Given a pre-scored list of Candidates (from discover_candidates), fetch
    them in score order and return the first one that passes rich-detail
    validation.

    Falls back to the best non-rich result if no candidate passes.
    Returns None if all candidates fail to fetch.

    Args:
        candidates: List of Candidate objects, sorted by score descending.
        fetch_fn:   Callable(url) -> str.
        row_index:  Row number for logging.

    Returns:
        (url, html, RichDetailResult) tuple, or None.
    """
    viable = [c for c in candidates if c.score >= CANDIDATE_MIN_SCORE]

    if not viable:
        logger.info(f"[TargetSelector] Row {row_index}: no viable candidates (all scored < {CANDIDATE_MIN_SCORE})")
        return None

    logger.info(
        f"[TargetSelector] Row {row_index}: "
        f"{len(viable)} viable candidate(s) to try "
        f"(scores: {[c.score for c in viable[:MAX_CANDIDATES_PER_ROW]]})"
    )

    best_fallback: tuple[str, str, RichDetailResult] | None = None

    for rank, candidate in enumerate(viable[:MAX_CANDIDATES_PER_ROW], 1):
        logger.info(
            f"[TargetSelector] Row {row_index} [{rank}/{min(len(viable), MAX_CANDIDATES_PER_ROW)}]: "
            f"trying score={candidate.score:+d} source={candidate.source!r} "
            f"url={candidate.url!r} text={candidate.text[:50]!r} "
            f"reasons={candidate.reasons}"
        )
        try:
            html = fetch_fn(candidate.url)
            rich = validate_rich_detail(html)

            logger.info(
                f"[TargetSelector] Row {row_index} [{rank}]: "
                f"validation score={rich.score} is_rich={rich.is_rich} "
                f"signals={rich.signals_found}"
            )

            if rich.is_rich:
                logger.info(
                    f"[TargetSelector] Row {row_index}: "
                    f"✓ accepted candidate [{rank}] ▒ rich detail confirmed"
                )
                return candidate.url, html, rich

            # Not rich enough — keep as fallback if it's the best so far
            if best_fallback is None or rich.score > best_fallback[2].score:
                best_fallback = (candidate.url, html, rich)

            logger.info(
                f"[TargetSelector] Row {row_index} [{rank}]: "
                f"✗ not rich (score={rich.score} < threshold={RICH_DETAIL_THRESHOLD}) — "
                f"{'trying next candidate' if rank < min(len(viable), MAX_CANDIDATES_PER_ROW) else 'no more candidates'}"
            )

        except Exception as e:
            logger.warning(
                f"[TargetSelector] Row {row_index} [{rank}]: "
                f"fetch failed for {candidate.url!r}: {e}"
            )
            continue

    # No candidate passed rich validation
    if best_fallback:
        url, html, rich = best_fallback
        logger.info(
            f"[TargetSelector] Row {row_index}: "
            f"no rich target found — using best fallback "
            f"(validation score={rich.score}, url={url!r})"
        )
        return best_fallback

    logger.info(f"[TargetSelector] Row {row_index}: all candidates failed to fetch")
    return None
