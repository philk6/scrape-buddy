"""
strategies/link_scorer.py — Click-target ranking for product links

Replaces blind "take the first link in a row" logic with a scored selection
that identifies which link in a product row/card is most likely to be the
product detail page.

Usage:
    from strategies.link_scorer import best_link, score_link

    # Score a single <a> tag (BeautifulSoup Tag)
    score = score_link(a_tag, base_url)

    # Pick the best link from a list of <a> tags
    url = best_link(a_tags, base_url)

Scoring rules (additive — higher is better):
  URL path signals  (+3 to +5 for product paths, -3 for reject paths)
  Wraps an <img>    (+2 — image-link is a strong product card pattern)
  Card context      (+2 — ancestor has product/card class)
  Anchor text       (-1 if text looks like navigation chrome)
  Fragment/js       (force 0 — always reject)

A score <= 0 means "reject this link".
"""

import logging
import re
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

# ── URL path scoring ───────────────────────────────────────────────────────────

# Paths that strongly indicate a product detail page
_HIGH_SCORE_PATHS = [
    "/products/", "/product/", "/item/", "/items/",
    "/detail/", "/p/", "/catalog/product/", "/shop/product/",
    "/sku/", "/prod/",
]

# Paths that give a moderate confidence boost
_MED_SCORE_PATHS = [
    "/pd/", "/view/", "/buy/",
]

# Paths that disqualify a link from being a product detail link
_REJECT_PATHS = [
    "/collections/", "/category/", "/categories/",
    "/search", "/cart", "/checkout", "/account",
    "/login", "/register", "/wishlist", "/compare",
    "/pages/", "/blogs/", "/tag/", "/tags/",
    "/contact", "/about", "/faq",
]

# Anchor text strings (lowercased) that indicate navigation, not a product
_NAV_TEXT_SIGNALS = [
    "view all", "see all", "shop all", "browse", "more",
    "next", "previous", "prev", "back", "home",
    "add to cart", "add to bag", "checkout", "login", "sign in",
    "contact", "about", "faq", "help",
]

# Ancestor class/id signals that suggest a product card context
_CARD_CLASS_SIGNALS = [
    "product", "item", "card", "tile", "grid-item",
    "listing", "result", "catalogue", "prod-",
]

# Minimum score to be considered a valid candidate
MIN_SCORE = 1


def _url_score(href: str) -> int:
    """Score a URL path based on product-likelihood signals."""
    path = urlparse(href).path.lower()

    # Hard reject
    if any(frag in path for frag in _REJECT_PATHS):
        return -5

    score = 0
    for p in _HIGH_SCORE_PATHS:
        if p in path:
            score += 4
            break

    for p in _MED_SCORE_PATHS:
        if p in path:
            score += 2
            break

    # URL has a meaningful slug (not just a number or bare domain)
    slug = path.rstrip("/").split("/")[-1]
    if slug and len(slug) > 3 and not slug.isdigit():
        score += 1

    return score


def _anchor_text_penalty(a_tag) -> int:
    """Return a negative penalty if anchor text looks like nav chrome."""
    try:
        text = a_tag.get_text(strip=True).lower()
        if not text:
            return 0   # empty text is fine (image link)
        if any(nav in text for nav in _NAV_TEXT_SIGNALS):
            return -3
        if len(text) > 60:
            return -1  # very long anchor text is unlikely to be a product link
    except Exception:
        pass
    return 0


def _card_context_score(a_tag) -> int:
    """Return a bonus if the link is inside a product card ancestor."""
    node = a_tag.parent
    for _ in range(8):
        if node is None or getattr(node, "name", None) in (None, "body", "html", "[document]"):
            break
        try:
            classes = " ".join(node.get("class") or []).lower()
            el_id   = (node.get("id") or "").lower()
            combined = f"{classes} {el_id}"
            if any(sig in combined for sig in _CARD_CLASS_SIGNALS):
                return 2
        except Exception:
            pass
        node = node.parent
    return 0


def _wraps_image_score(a_tag) -> int:
    """Return a bonus if the link directly wraps an <img>."""
    try:
        if a_tag.find("img"):
            return 2
    except Exception:
        pass
    return 0


def score_link(a_tag, base_url: str) -> int:
    """
    Score a BeautifulSoup <a> tag for product-link likelihood.

    Returns an integer. Values <= 0 should be rejected.
    Higher values indicate stronger confidence the link goes to a product page.
    """
    try:
        href = (a_tag.get("href") or "").strip()
        if not href:
            return 0
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            return 0

        absolute = urljoin(base_url, href)
        # Same-domain check
        if urlparse(absolute).netloc != urlparse(base_url).netloc:
            return 0

        score = (
            _url_score(absolute)
            + _anchor_text_penalty(a_tag)
            + _card_context_score(a_tag)
            + _wraps_image_score(a_tag)
        )

        logger.debug(
            f"[LinkScorer] score={score} href={href!r} "
            f"(url={_url_score(absolute)}, "
            f"anchor={_anchor_text_penalty(a_tag)}, "
            f"card={_card_context_score(a_tag)}, "
            f"img={_wraps_image_score(a_tag)})"
        )
        return score

    except Exception as e:
        logger.debug(f"[LinkScorer] Error scoring link: {e}")
        return 0


def best_link(a_tags, base_url: str) -> str:
    """
    Given a list of <a> BeautifulSoup Tags, return the absolute URL of the
    best-scoring candidate (score > MIN_SCORE), or "" if none qualify.

    This replaces the old "pick first link" heuristic.
    """
    best_score = MIN_SCORE - 1  # threshold: anything <= this is rejected
    best_url = ""

    for a in a_tags:
        s = score_link(a, base_url)
        if s > best_score:
            best_score = s
            href = (a.get("href") or "").strip()
            best_url = urljoin(base_url, href) if href else ""

    return best_url
