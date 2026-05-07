"""
Product normalization and scrape quality diagnostics.

This module is intentionally site-agnostic. It standardizes product rows from
every extraction strategy and emits warnings when a scrape looks incomplete or
low-confidence.
"""

from __future__ import annotations

from collections import Counter
import re
from typing import Iterable


BARCODE_FIELDS = (
    "upc",
    "ean",
    "gtin",
    "gtin_case",
    "barcode",
    "barcode_raw",
)

NON_PRODUCT_NAME_PATTERNS = (
    r"\byour shopping cart\b",
    r"\bshopping cart\b",
    r"\bsign\s*in\b",
    r"\blog\s*in\b",
    r"\bjavascript is disabled\b",
    r"\bsearch instead\b",
    r"^\s*showing results for",
    r"^\s*\d[\d,]*\s+(?:results?|items?|products?)\s+for\b",
)

NON_PRODUCT_URL_PATTERNS = (
    r"/basket",
    r"/cart",
    r"/checkout",
    r"/account",
    r"/myaccount",
    r"/users/",
    r"/login",
    r"/register",
    r"/signin",
    r"/signup",
    r"/wishlist",
)


def classify_scrape_error(error: object) -> str:
    """Return a stable category for a scrape failure."""
    text = str(error or "")
    lower = text.lower()
    if any(token in lower for token in ("timed out", "timeout", "read timed out")):
        return "timeout"
    if any(
        token in lower
        for token in (
            "403",
            "401",
            "captcha",
            "access denied",
            "forbidden",
            "blocked",
            "checking your browser",
            "are you a robot",
            "robot",
        )
    ):
        return "blocked"
    if any(
        token in lower
        for token in (
            "name resolution",
            "getaddrinfo",
            "connection",
            "dns",
            "ssl",
            "proxy",
        )
    ):
        return "network"
    if re.search(r"\b(4\d\d|5\d\d)\b", lower):
        return "http_error"
    return "runtime_error"


def build_error_report(error: object, *, stage: str = "") -> dict:
    """Build diagnostics for a failed scrape run."""
    category = classify_scrape_error(error)
    text = str(error or "Unknown error.").strip()
    warnings_by_category = {
        "timeout": "The site did not finish within the configured timeout.",
        "blocked": "The site appears to block automated access or requires a browser/login session.",
        "network": "The site could not be reached reliably from this machine.",
        "http_error": "The site returned an HTTP error before extraction could run.",
        "runtime_error": "The scraper hit an unexpected runtime error.",
    }
    report = {
        "score": 0.0,
        "counts": {
            "products": 0,
            "name": 0,
            "price": 0,
            "sku": 0,
            "barcode": 0,
            "product_url": 0,
            "image_url": 0,
        },
        "error_category": category,
        "error_message": text[:500],
        "warnings": [warnings_by_category.get(category, warnings_by_category["runtime_error"])],
    }
    if stage:
        report["stage"] = stage
        report["warnings"].append(f"Failure stage: {stage}.")
    return report


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def normalize_barcode(value: object) -> str:
    """Return a digits-only UPC/EAN/GTIN candidate, or empty string."""
    digits = _digits(value)
    if 8 <= len(digits) <= 14:
        return digits
    return ""


def classify_barcode(digits: str) -> str:
    """Classify a normalized barcode by length."""
    if not digits:
        return ""
    if len(digits) == 8:
        return "ean8"
    if len(digits) == 12:
        return "upc"
    if len(digits) == 13:
        return "ean13"
    if len(digits) == 14:
        return "gtin14"
    return "barcode"


def normalize_product(product: dict) -> dict:
    """
    Normalize one product row without dropping unknown fields.

    Legacy code uses "upc" as the canonical barcode column, so we keep filling
    it while also adding explicit ean/gtin/barcode_raw/identifier_type fields.
    """
    normalized = dict(product or {})

    for key, value in list(normalized.items()):
        if isinstance(value, str):
            normalized[key] = re.sub(r"\s+", " ", value).strip()

    barcode = ""
    raw = ""
    for field in BARCODE_FIELDS:
        candidate = normalized.get(field)
        barcode = normalize_barcode(candidate)
        if barcode:
            raw = str(candidate or "").strip()
            break

    if barcode:
        id_type = classify_barcode(barcode)
        normalized["barcode_raw"] = raw or barcode
        normalized["identifier_type"] = id_type
        if not normalized.get("upc"):
            normalized["upc"] = barcode
        if id_type.startswith("ean") and not normalized.get("ean"):
            normalized["ean"] = barcode
        if id_type.startswith("gtin") and not normalized.get("gtin"):
            normalized["gtin"] = barcode

    return normalized


def normalize_products(products: Iterable[dict]) -> list[dict]:
    return [normalize_product(p) for p in products or [] if isinstance(p, dict)]


def build_quality_report(
    products: Iterable[dict],
    *,
    expected_products: int | None = None,
    expected_pages: int | None = None,
    pages_visited: int | None = None,
    stop_reason: str = "",
    strategy_name: str = "",
) -> dict:
    """Build a compact confidence report for one scrape result."""
    rows = list(products or [])
    total = len(rows)

    def count(field: str) -> int:
        return sum(1 for p in rows if p.get(field))

    counts = {
        "products": total,
        "name": count("product_name"),
        "price": count("price"),
        "sku": count("sku"),
        "barcode": sum(
            1
            for p in rows
            if p.get("upc") or p.get("ean") or p.get("gtin") or p.get("gtin_case")
        ),
        "product_url": count("product_url"),
        "image_url": count("image_url"),
    }

    warnings: list[str] = []
    if total == 0:
        warnings.append("No products were extracted.")
    else:
        non_product_rows = sum(
            1
            for p in rows
            if (
                any(
                    re.search(pattern, str(p.get("product_name") or ""), re.IGNORECASE)
                    for pattern in NON_PRODUCT_NAME_PATTERNS
                )
                or any(
                    re.search(pattern, str(p.get("product_url") or ""), re.IGNORECASE)
                    for pattern in NON_PRODUCT_URL_PATTERNS
                )
            )
        )
        if non_product_rows:
            warnings.append(f"{non_product_rows} extracted row(s) look like navigation, account, cart, or search/category summaries.")
        if total < 3:
            warnings.append("Very few products were extracted; this may be a detail page, search shell, or incomplete catalog crawl.")
        if counts["name"] / total < 0.70:
            warnings.append("Low product-name coverage; card/row detection may be wrong.")
        if counts["product_url"] / total < 0.30:
            warnings.append("Few product detail URLs were found; detail enrichment may be limited.")
        if counts["price"] == 0 and counts["sku"] == 0 and counts["barcode"] == 0:
            warnings.append("No price, SKU, or UPC/EAN/GTIN fields were captured; extracted rows are likely not real products or require a browser/login session.")
        if counts["barcode"] / total < 0.20:
            warnings.append("Low UPC/EAN/GTIN coverage; identifiers may live on detail pages or behind login.")

        prices = [p.get("price") for p in rows if p.get("price")]
        if len(prices) >= 6:
            common_price, common_count = Counter(prices).most_common(1)[0]
            if common_count / len(prices) > 0.80:
                warnings.append(
                    f"Price anomaly: {common_count}/{len(prices)} rows share {common_price}."
                )

    if expected_pages and pages_visited and pages_visited < expected_pages:
        warnings.append(
            f"Pagination may be incomplete: visited {pages_visited}/{expected_pages} pages."
        )

    if expected_products and total and total < expected_products:
        coverage = total / expected_products
        if coverage < 0.90:
            warnings.append(
                f"Catalog may be incomplete: captured {total}/{expected_products} visible products."
            )

    normal_stop = (
        stop_reason.startswith("all ")
        or stop_reason.startswith("navigation exhausted after")
        or stop_reason.startswith("crawl stopped after")
    )
    if stop_reason and not normal_stop:
        warnings.append(f"Stop reason: {stop_reason}")

    score = 1.0
    if total == 0:
        score = 0.0
    else:
        for warning in warnings:
            if "navigation, account, cart" in warning:
                score -= 0.35
            elif warning.startswith("No price, SKU"):
                score -= 0.35
            elif warning.startswith("Stop reason"):
                score -= 0.10
            elif "UPC" in warning:
                score -= 0.15
            elif "Pagination" in warning:
                score -= 0.25
            else:
                score -= 0.20
        score = max(0.0, min(1.0, score))

    return {
        "strategy_name": strategy_name,
        "score": round(score, 2),
        "counts": counts,
        "warnings": warnings,
    }
