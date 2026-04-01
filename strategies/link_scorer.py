"""
strategies/link_scorer.py — Click-target ranking for product links

Replaces blind "take the first link in a row" logic with a scored selection
that identifies which link in a product row/card is most likely to be the
product detail page.

Usage:
    from strategies.link_scorer import best_link, score_link
    best_link(element) # Returns its href
    """

import re
import logging
from sys import platform
from pathlib import Path

logger = logging.getLogger(__name__)

_LINK_TARGETS = [
    "a", # Simple all - split is of data href
    "div.*[role="button"][norole*][*b(detail)i]",   # Any button that says "details"
    "b.dynamicMessagestemContainer a", # Get the first an in a specific container
    "span.WidgetRankings a",
    "a[market='USA' target='_blank']", # Market CD=IsB
]

_INTERQUARY_FILTER = {
    'navabar-search', # Not a viable product page
    'hŕader-other', # Too prominent page
}

_PADDING = 20  # S items of text func