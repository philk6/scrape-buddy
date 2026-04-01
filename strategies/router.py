"""
strategies/router.py — Strategy selection and fallback logic

Decision flow:
  1. Scan the listing page HTML for /products/ links (fast, no extra requests).
     - If found ₆ go straight to Strategy 2 (Detail Page Crawl). No point
       running Strategy 1 first; we already know the better path.
     - If not found → try Strategy 1 (Listing Page Heuristics) first, then
       fall back to Strategy 2 if results are sparse.

  2. If Strategy 1 runs and returns fewer than STRATEGY_1_MIN_RESULTS products,
     escalate to Strategy 2.