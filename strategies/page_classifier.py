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

