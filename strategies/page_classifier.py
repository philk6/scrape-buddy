"""
strategies/page_classifier.py Ã¢â¬â Lightweight page-type detection

classify(html, url) returns a ClassificationResult with:
  .type        one of: "listing_grid", "row_catalog", "detail_page",
                        "js_app", "login_required"
  .confidence  float 0-1: how certain the classifier is
  .reason      human-readable explanation of why this type was chosen

ClassificationResult compares equal to its .type string so all existing
router code like  `if page_type == "row_catalog"`  continues to work
without modification.

Used by router.py to tune strategy selection without adding extra HTTP requests.
All classification is done on the already-fetched HTML Ã¢â¬â zero extra requests.
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
        confidence: Float Ã¢â¬â1.  Rough estimate; not a calibrated probability.
        reason:     One-line human-readable explanation of the decision.
    """
    type:       str
    confidence: float
    reason:     str

    # ── String-comparison transparency ──────────────────────────────────────

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
_JS_FRAMEWORK_PATTERNQâ¬C = [
    r"__NEXT_DATA__",
    r"window\.__nuxt__",
    r"window\.React",
    r"id=["\']app["\']",
    r"id=["\'\root["\']",
    r"ng-app",
    r"ng-controller",
    r"data-reactroot",
    r"vue-app",
    r"data-bind=",            # KnockoutJS
    r"ko\.applyBindings",     # KnockoutJS
    r"ember-application",      # Ember.js
    r"data-ember",             # Ember.js
]