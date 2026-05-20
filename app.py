"""
app.py - Scraper Buddy product spreadsheet exporter - Flask server

Routes:
  GET  /                      → frontend
  POST /api/scrape            → scrape a URL, save to history, return products
  GET  /api/history           → list all saved scrape runs
  GET  /api/history/<id>      → get one run + its products
  PATCH /api/history/<id>     → rename a run's label
  DELETE /api/history/<id>    → delete a run (and its products)
  GET  /api/export/<id>       → download run as .xlsx
  POST /api/debug             → diagnostic info for a URL
  POST /api/chat              → AI support chat

Environment variables required:
  OPENAI_API_KEY — your OpenAI API key

Run: python app.py  →  http://localhost:5000
"""

import io
import os
import logging
import threading
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify, render_template, send_file
from flask_cors import CORS
import requests as req_lib
from openai import OpenAI
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from bs4 import BeautifulSoup

import database
import browser_login
import upc_enrichment
from scraper import fetch_html, fetch_html_playwright, debug_scrape, make_auth_fetch_fn
from strategies import run_best_strategy
from strategies.detail import run as detail_run
from strategies import playwright_catalog, firecrawl_fallback, feed_exporter, llm_extractor
from strategies.pagination import dedup_products
from strategies.product_quality import build_error_report, build_quality_report, normalize_products
from upc_providers import default_providers
from pack_parser import enrich_all as enrich_all_pack

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

# Initialise the database (creates tables if they don't exist)
database.init_db()

# ── OpenAI ────────────────────────────────────────────────────────────────────
# Lazy init: don't crash on startup if OPENAI_API_KEY isn't set yet.
# The client is only needed for /api/chat — scraping works without it.
openai_client = None
if os.environ.get("OPENAI_API_KEY"):
    openai_client = OpenAI()

CHAT_SYSTEM_PROMPT = """You are a helpful support assistant for Scraper Buddy.

This tool lets users paste a supplier store, catalog, feed, or category URL and export spreadsheet-ready product data from it.
It uses a feed-first path for supported catalog feeds, then falls back to structured data, row/table catalogs, generic product cards, detail-page enrichment, LLM fallback, JavaScript rendering, and pagination traversal.
It extracts: product_name, brand, sku, upc/ean/gtin, price, pack_size, case_pack, image_url, product_url, and quality diagnostics.

Key behaviours:
- Uses universal product-data signals first instead of supplier-specific patches.
- Follows pagination and detail links when available.
- Saves every scrape to a local SQLite database with a label and timestamp.
- Results can be exported to .xlsx from the results header or the History sidebar.

Help users with:
- How to use the tool (paste URL, optional label, click Build Spreadsheet)
- Why scraping might return no results (JavaScript-rendered sites, bot protection, unusual layouts)
- What each extracted field means (UPC, SKU, pack size, case pack)
- How to export results to Excel
- How to view, rename, and delete saved scrapes in the History sidebar
- Common issues (timeouts, empty results, missing UPC or case pack data)

Keep answers short and practical."""

# ── Helpers ───────────────────────────────────────────────────────────────────

def _auto_label(url: str) -> str:
    """
    Generate a human-readable label from a URL + timestamp.
    e.g. "example.com — 14:32 19/03/2026"
    """
    domain = urlparse(url).netloc.removeprefix("www.")
    now = datetime.now(timezone.utc).strftime("%H:%M %d/%m/%Y")
    return f"{domain} — {now}"


# All product fields, in display order.
# Used by both the export and to normalise Strategy 1 results (which lack
# the newer fields) so the spreadsheet always has consistent columns.
PRODUCT_FIELDS = [
    "product_name", "brand", "sku", "upc",
    "price", "pack_size", "case_pack",
    "image_url", "product_url",
    # UPC enrichment metadata (empty string when not applicable)
    "upc_source", "upc_match_confidence", "upc_enriched", "missing_upc",
    "upc_confidence_color", "upc_match_reason",
    # Pack enrichment metadata
    "raw_pack_text", "unit_measure", "pack_confidence",
    # Detail-page structured fields
    "unit_size", "unit_price", "pricing_unit",
    # Multi-supplier extraction fields
    "bulk_price", "minimum_order_qty", "raw_price_text",
    "gtin_case", "ean", "gtin", "barcode_raw", "identifier_type",
    # Feed-first spreadsheet fields
    "category", "description", "availability",
    "source_product_id", "source_variant_id", "source_platform", "tags",
]

PRODUCT_FIELD_LABELS = {
    "source_product_id": "Source Product ID",
    "source_variant_id": "Source Variant ID",
    "source_platform": "Source Type",
}


def _normalise_product(p: dict) -> dict:
    """Ensure every product dict has every field, defaulting to ''."""
    return {field: p.get(field, "") for field in PRODUCT_FIELDS}


def _build_xlsx(run: dict) -> io.BytesIO:
    """
    Build an .xlsx workbook for a scrape run and return it as a BytesIO buffer.
    Applies minimal styling: bold gold header row, auto column widths.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Products"

    # Header row
    headers = [PRODUCT_FIELD_LABELS.get(f, f.replace("_", " ").title()) for f in PRODUCT_FIELDS]
    ws.append(headers)

    # Style the header row: bold, gold fill, dark text
    gold_fill = PatternFill("solid", fgColor="C9A84C")
    bold_font = Font(bold=True, color="0B0B0B")
    for cell in ws[1]:
        cell.fill = gold_fill
        cell.font = bold_font
        cell.alignment = Alignment(horizontal="center")

    # Data rows
    for product in run.get("products", []):
        p = _normalise_product(product)
        ws.append([p[field] for field in PRODUCT_FIELDS])

    # Auto-size columns (rough heuristic: max of header length and first 20 values)
    for col_idx, field in enumerate(PRODUCT_FIELDS, start=1):
        col_letter = ws.cell(row=1, column=col_idx).column_letter
        max_len = len(headers[col_idx - 1])
        for row in ws.iter_rows(min_row=2, max_row=min(ws.max_row, 21), min_col=col_idx, max_col=col_idx):
            for cell in row:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max_len + 3, 50)

    ws.freeze_panes = "A2"
    try:
        ws.auto_filter.ref = ws.dimensions
    except Exception:
        pass

    summary = wb.create_sheet("Summary")
    products = run.get("products", [])
    summary_rows = [
        ["Scrape Summary", ""],
        ["Source", run.get("source_url", "")],
        ["Strategy", run.get("strategy_name", "")],
        ["Rows", len(products)],
        ["Rows with UPC/EAN/GTIN", sum(1 for p in products if p.get("upc") or p.get("ean") or p.get("gtin"))],
        ["Rows with SKU", sum(1 for p in products if p.get("sku"))],
        ["Distinct brands", len({p.get("brand") for p in products if p.get("brand")})],
        ["Distinct categories", len({c for p in products for c in str(p.get("category") or "").split("; ") if c})],
    ]
    for row in summary_rows:
        summary.append(row)
    summary["A1"].fill = gold_fill
    summary["A1"].font = Font(bold=True, color="0B0B0B", size=14)
    summary.merge_cells("A1:B1")
    for cell in summary["A"]:
        cell.font = Font(bold=True)
    summary.column_dimensions["A"].width = 26
    summary.column_dimensions["B"].width = 72

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _html_needs_browser_crawler(html: str | None) -> bool:
    """
    Return True when a public scrape should use the full Playwright catalog
    crawler instead of relying on a one-time HTML snapshot.

    This stays intentionally site-agnostic: it looks for blocked/empty pages,
    client-side framework signals, and product-like shells with missing prices.
    """
    if not html:
        return True

    lower_html = html.lower()
    soup_check = BeautifulSoup(html, "html.parser")
    body_text_len = len(soup_check.get_text(strip=True)) if soup_check.body else 0
    is_blocked = any(phrase in lower_html for phrase in [
        "access denied", "403 forbidden", "captcha", "are you a robot",
        "please enable javascript", "checking your browser",
    ])
    is_js_rendered = any(sig in html for sig in [
        "data-bind=", "ko.applyBindings", "ng-app", "ng-controller",
        "__NEXT_DATA__", "__NUXT__", "data-reactroot", "__APOLLO_STATE__",
        "graphql",
    ])

    def _has_product_class(value) -> bool:
        if not value:
            return False
        class_text = " ".join(value).lower() if isinstance(value, list) else str(value).lower()
        return any(kw in class_text for kw in ["product", "item", "card", "catalog"])

    has_product_classes = bool(soup_check.find(attrs={"class": _has_product_class}))
    has_prices = bool(soup_check.find(string=lambda s: s and "$" in s))
    products_but_no_prices = has_product_classes and not has_prices

    sparse_without_products = body_text_len < 500 and not (has_product_classes and has_prices)
    return is_blocked or is_js_rendered or products_but_no_prices or sparse_without_products


def _catalog_crawl_diagnostics() -> dict:
    return dict(getattr(playwright_catalog, "LAST_CRAWL_DIAGNOSTICS", {}) or {})


def _build_product_diagnostics(
    products: list,
    strategy_name: str,
    crawl_diagnostics: dict | None = None,
) -> dict:
    crawl_diagnostics = crawl_diagnostics or {}
    diagnostics = build_quality_report(
        products,
        strategy_name=strategy_name,
        expected_products=crawl_diagnostics.get("expected_products"),
        expected_pages=crawl_diagnostics.get("expected_pages"),
        pages_visited=crawl_diagnostics.get("pages_visited"),
        stop_reason=crawl_diagnostics.get("stop_reason", ""),
    )
    if crawl_diagnostics:
        diagnostics["crawl"] = dict(crawl_diagnostics)
    return diagnostics


def _run_public_browser_catalog(url: str) -> tuple[dict | None, dict]:
    """
    Try the full Playwright catalog crawler for public JavaScript-heavy sites.
    Returns (result_dict_or_none, crawl_diagnostics).
    """
    products = playwright_catalog.run_public(url)
    crawl_diagnostics = _catalog_crawl_diagnostics()
    if not products:
        return None, crawl_diagnostics

    return {
        "strategy_id": playwright_catalog.ID,
        "strategy_name": playwright_catalog.NAME,
        "reason": (
            "Public browser catalog crawler extracted products using rendered "
            "DOM, API capture, and interactive pagination"
        ),
        "products": products,
        "_crawl_diagnostics": crawl_diagnostics,
    }, crawl_diagnostics


def _firecrawl_mode() -> str:
    return os.environ.get("SCRAPEBUDDY_FIRECRAWL_MODE", "disabled").strip().lower()


def _auth_firecrawl_mode() -> str:
    return os.environ.get("SCRAPEBUDDY_FIRECRAWL_AUTH_MODE", "disabled").strip().lower()


def _openai_only_mode() -> bool:
    return os.environ.get("SCRAPEBUDDY_OPENAI_ONLY", "1").strip().lower() not in {
        "0", "off", "false", "disabled", "no",
    }


def _merge_products(primary: list[dict], supplemental: list[dict]) -> list[dict]:
    merged, _removed = dedup_products((primary or []) + (supplemental or []))
    return merged


def _should_try_firecrawl_supplement(products: list[dict], crawl_diagnostics: dict) -> bool:
    if not firecrawl_fallback.enabled():
        return False
    mode = _firecrawl_mode()
    if mode in {"0", "off", "disabled", "none", "never"}:
        return False
    if mode in {"supplement", "always"}:
        return True
    if not products:
        return True
    try:
        expected = int(crawl_diagnostics.get("expected_products") or 0)
    except Exception:
        expected = 0
    if len(products) < 5:
        return True
    return bool(expected and len(products) < expected * 0.5)


def _mark_external_upc_skipped(products: list[dict], reason: str) -> None:
    for product in products or []:
        if product.get("upc") or product.get("ean") or product.get("gtin"):
            product.setdefault("upc_source", "supplier_page")
            product.setdefault("upc_enriched", "0")
            product.setdefault("missing_upc", "0")
            continue
        product.setdefault("upc_enriched", "0")
        product.setdefault("missing_upc", "1")
        product.setdefault("resolution_status", "external_lookup_skipped")
        product.setdefault("resolution_reason", reason)


def _barcode_count(products: list[dict]) -> int:
    return sum(1 for product in products or [] if product.get("upc") or product.get("ean") or product.get("gtin"))


def _barcode_coverage(products: list[dict]) -> float:
    return (_barcode_count(products) / len(products)) if products else 0.0


def _should_try_browser_recovery(result: dict) -> bool:
    products = result.get("products") or []
    diagnostics = result.get("_crawl_diagnostics") or {}
    expected = int(diagnostics.get("expected_products") or 0)
    if expected and len(products) < expected * 0.9:
        return True
    return bool(products and _barcode_coverage(products) < 0.75)


def _result_strength(result: dict | None) -> tuple[int, int, float]:
    products = (result or {}).get("products") or []
    return (len(products), _barcode_count(products), _barcode_coverage(products))


def _maybe_recover_with_browser_catalog(result: dict, url: str) -> dict:
    if not _should_try_browser_recovery(result):
        return result

    try:
        browser_result, browser_diag = _run_public_browser_catalog(url)
    except Exception as e:
        logging.warning(f"[OpenAIOnly] Browser recovery failed: {e}")
        return result

    if not browser_result:
        return result

    openai_strength = _result_strength(result)
    browser_strength = _result_strength(browser_result)
    if browser_strength[0] > openai_strength[0] or browser_strength[1] > openai_strength[1]:
        merged_products = _merge_products(browser_result.get("products", []), result.get("products", []))
        browser_result["products"] = merged_products
        browser_result["strategy_name"] = "OpenAI + Browser Catalog Extraction"
        browser_result["reason"] = (
            "OpenAI extraction was supplemented by the rendered browser/API catalog "
            "crawler because product or identifier coverage was incomplete"
        )
        merged_diag = dict(browser_result.get("_crawl_diagnostics") or {})
        merged_diag.update({
            "openai_recovery_triggered": True,
            "openai_rows_before_recovery": openai_strength[0],
            "openai_barcodes_before_recovery": openai_strength[1],
            "browser_rows_before_merge": browser_strength[0],
            "browser_barcodes_before_merge": browser_strength[1],
            "browser_recovery_stop_reason": browser_diag.get("stop_reason", ""),
        })
        browser_result["_crawl_diagnostics"] = merged_diag
        browser_result["_skip_external_upc_enrichment"] = False
        return browser_result

    return result


def _render_html_for_openai(url: str, state_file: str | None = None, wait_ms: int = 8000) -> str:
    if not state_file:
        return fetch_html_playwright(url, wait_ms=wait_ms, strict=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=state_file,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(wait_ms)
        html = page.content()
        browser.close()
        return html


def _fetch_html_for_openai(url: str, state_file: str | None = None) -> tuple[str, str]:
    if state_file:
        return _render_html_for_openai(url, state_file=state_file), "authenticated_rendered_html"

    try:
        html = fetch_html(url)
    except Exception:
        html = ""

    if _html_needs_browser_crawler(html):
        try:
            rendered = _render_html_for_openai(url)
            if len(rendered or "") > len(html or ""):
                return rendered, "rendered_html"
        except Exception as e:
            logging.warning(f"[OpenAIOnly] Browser render failed for {url}: {e}")

    return html, "html"


def _merge_detail_fields_from_openai(product: dict, detail: dict) -> bool:
    changed = False
    for key in (
        "product_name", "brand", "sku", "upc", "ean", "gtin", "price",
        "pack_size", "case_pack", "image_url", "product_url",
    ):
        value = detail.get(key)
        if value and (not product.get(key) or key in {"upc", "ean", "gtin"}):
            product[key] = value
            changed = True
    return changed


def _tokenize_match_text(value: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[a-z0-9]+", value or "") if len(token) >= 3}


def _same_sku(left: str, right: str) -> bool:
    left_norm = re.sub(r"[^a-z0-9]", "", left or "", flags=re.I).lower()
    right_norm = re.sub(r"[^a-z0-9]", "", right or "", flags=re.I).lower()
    return bool(left_norm and right_norm and (left_norm == right_norm or left_norm in right_norm or right_norm in left_norm))


def _detail_product_score(listing_product: dict, detail_product: dict) -> int:
    score = 0
    if _same_sku(listing_product.get("sku", ""), detail_product.get("sku", "")):
        score += 80
    if listing_product.get("product_url") and detail_product.get("product_url"):
        if listing_product["product_url"].rstrip("/") == detail_product["product_url"].rstrip("/"):
            score += 60
    listing_tokens = _tokenize_match_text(listing_product.get("product_name", ""))
    detail_tokens = _tokenize_match_text(detail_product.get("product_name", ""))
    if listing_tokens and detail_tokens:
        score += min(30, len(listing_tokens & detail_tokens) * 5)
    if detail_product.get("upc") or detail_product.get("ean") or detail_product.get("gtin"):
        score += 10
    return score


def _choose_best_detail_product(listing_product: dict, detail_products: list[dict]) -> dict | None:
    if not detail_products:
        return None
    scored = sorted(
        detail_products,
        key=lambda item: _detail_product_score(listing_product, item),
        reverse=True,
    )
    best = scored[0]
    return best if _detail_product_score(listing_product, best) > 0 else detail_products[0]


def _is_barcode_candidate(value: str) -> bool:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) not in {8, 12, 13, 14}:
        return False
    if len(set(digits)) <= 2:
        return False
    body = [int(char) for char in digits[:-1]]
    check_digit = int(digits[-1])
    total = 0
    for index, digit in enumerate(reversed(body), start=1):
        total += digit * (3 if index % 2 else 1)
    return (10 - (total % 10)) % 10 == check_digit


def _first_identifier_from_values(*values: str) -> str:
    for value in values:
        for match in re.findall(r"\b\d[\d\s-]{6,20}\d\b", str(value or "")):
            digits = re.sub(r"\D", "", match)
            if _is_barcode_candidate(digits):
                return digits
    return ""


def _identifier_evidence_from_detail_html(html: str) -> dict:
    soup = BeautifulSoup(html or "", "html.parser")
    evidence: dict[str, str] = {}

    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        text = script.string or script.get_text(" ", strip=True)
        if not text:
            continue
        for key in ("gtin14", "gtin13", "gtin12", "gtin8", "gtin", "upc", "ean", "barcode"):
            match = re.search(rf'"{key}"\s*:\s*"([^"]+)"', text, re.I)
            if match:
                value = _first_identifier_from_values(match.group(1))
                if value:
                    evidence.setdefault("identifier", value)
                    evidence.setdefault("identifier_type", key.lower())
                    evidence.setdefault("identifier_source", f"jsonld:{key}")
                    break

    for tag in soup.find_all("meta"):
        name = tag.get("name") or tag.get("property") or tag.get("itemprop") or ""
        content = tag.get("content") or ""
        if re.search(r"gtin|upc|ean|barcode", name, re.I):
            value = _first_identifier_from_values(content)
            if value:
                evidence.setdefault("identifier", value)
                evidence.setdefault("identifier_type", name.lower())
                evidence.setdefault("identifier_source", f"meta:{name}")
                break

    for tag in soup.find_all(string=re.compile(r"UPC|GTIN|EAN|Barcode", re.I)):
        text = str(tag)
        if hasattr(tag, "parents"):
            for parent in tag.parents:
                if getattr(parent, "name", None) not in {"li", "tr", "td", "div", "section", "span", "p"}:
                    continue
                candidate_text = parent.get_text(" ", strip=True)
                if len(candidate_text) <= 500:
                    text = candidate_text
                    break
            else:
                continue
        value = _first_identifier_from_values(text)
        if value:
            evidence.setdefault("identifier", value)
            evidence.setdefault("identifier_type", "visible_label")
            evidence.setdefault("identifier_source", "visible_label")
            break

    if not evidence.get("identifier"):
        for script in soup.find_all("script"):
            text = script.string or script.get_text(" ", strip=True)
            if not text or not re.search(r"gtin|upc|ean|barcode", text, re.I):
                continue
            for match in re.finditer(
                r"(?:gtin(?:8|12|13|14)?|upc|ean|barcode)[\w-]*[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']",
                text,
                re.I,
            ):
                value = _first_identifier_from_values(match.group(1))
                if value:
                    evidence.setdefault("identifier", value)
                    evidence.setdefault("identifier_type", match.group(0).split(":", 1)[0].lower())
                    evidence.setdefault("identifier_source", "script_identifier_field")
                    break
            if evidence.get("identifier"):
                break

    return evidence


def _assign_identifier_from_evidence(product: dict, evidence: dict) -> bool:
    value = evidence.get("identifier")
    if not value or product.get("upc") or product.get("ean") or product.get("gtin"):
        return False

    source_type = str(evidence.get("identifier_type") or "").lower()
    digits = re.sub(r"\D", "", value)
    if "ean" in source_type or len(digits) == 13:
        target = "ean"
    elif "gtin" in source_type or len(digits) == 14:
        target = "gtin"
    else:
        target = "upc"

    product[target] = value
    product["barcode_raw"] = value
    product["identifier_type"] = target
    product["upc_source"] = evidence.get("identifier_source", "supplier_page")
    return True


def _openai_page_limit() -> int:
    try:
        return max(1, int(os.environ.get("SCRAPEBUDDY_OPENAI_PAGE_LIMIT", "100") or "100"))
    except Exception:
        return 100


def _openai_detail_limit() -> int:
    try:
        return max(0, int(os.environ.get("SCRAPEBUDDY_OPENAI_DETAIL_LIMIT", "1000") or "1000"))
    except Exception:
        return 1000


def _openai_detail_concurrency() -> int:
    try:
        return min(12, max(1, int(os.environ.get("SCRAPEBUDDY_OPENAI_DETAIL_CONCURRENCY", "8") or "8")))
    except Exception:
        return 8


def _openai_detail_llm_enabled() -> bool:
    return os.environ.get("SCRAPEBUDDY_OPENAI_DETAIL_LLM", "0").strip().lower() in {
        "1", "true", "yes", "on", "always"
    }


def _detect_openai_pagination(html: str) -> dict:
    soup = BeautifulSoup(html or "", "html.parser")
    text = soup.get_text(" ", strip=True)
    total_products = None
    total_pages = None

    for pattern in (
        r"\b\d+\s*-\s*\d+\s+of\s+([\d,]+)\b",
        r"\b([\d,]+)\s+(?:items?|products?|results?)\b",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            try:
                total_products = int(match.group(1).replace(",", ""))
                break
            except Exception:
                pass

    page_numbers = []
    for candidate in soup.find_all(["a", "button"]):
        value = candidate.get_text(" ", strip=True)
        if re.fullmatch(r"\d{1,4}", value or ""):
            try:
                page_numbers.append(int(value))
            except Exception:
                pass
    if len(page_numbers) >= 2:
        total_pages = max(page_numbers)

    return {"total_products": total_products, "total_pages": total_pages}


def _next_page_candidates(current_url: str, next_page: int) -> list[str]:
    parsed = urlparse(current_url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    candidates = []
    for name in ("page", "p", "pg", "pageNumber", "Page", "currentPage"):
        next_params = dict(params)
        next_params[name] = [str(next_page)]
        candidates.append(parsed._replace(query=urlencode(next_params, doseq=True)).geturl())

    path = parsed.path or ""
    for pattern, repl in (
        (r"(/page/)\d+(/?)$", rf"\g<1>{next_page}\g<2>"),
        (r"(/p/)\d+(/?)$", rf"\g<1>{next_page}\g<2>"),
    ):
        next_path = re.sub(pattern, repl, path, flags=re.I)
        if next_path != path:
            candidates.append(parsed._replace(path=next_path).geturl())

    seen = set()
    unique = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def _find_next_link(html: str, current_url: str, visited: set[str]) -> str | None:
    soup = BeautifulSoup(html or "", "html.parser")
    base_host = urlparse(current_url).netloc
    for link in soup.find_all("a", href=True):
        text = link.get_text(" ", strip=True).lower()
        rel = " ".join(link.get("rel") or []).lower()
        aria = str(link.get("aria-label") or "").lower()
        if not (rel == "next" or text in {"next", "next >", ">", "›", "»"} or "next" in aria):
            continue
        candidate = urljoin(current_url, link["href"])
        if urlparse(candidate).netloc != base_host or candidate in visited:
            continue
        return candidate
    return None


def _extract_openai_listing_products(html: str, url: str) -> list[dict]:
    return llm_extractor.extract_from_text(_compact_listing_evidence(html, url), url, content_type="listing_evidence")


def _collect_openai_listing_pages(start_html: str, start_url: str, state_file: str | None = None) -> tuple[list[dict], dict]:
    visited = {start_url}
    current_url = start_url
    current_html = start_html
    products = []
    page_limit = _openai_page_limit()
    detected = _detect_openai_pagination(start_html)
    estimated_total_pages = detected.get("total_pages")
    pages_visited = 0

    while pages_visited < page_limit:
        pages_visited += 1
        logging.info(f"[OpenAIOnly] Listing page {pages_visited}: {current_url}")
        page_products = _extract_openai_listing_products(current_html, current_url)
        products.extend(page_products)

        if not estimated_total_pages and detected.get("total_products") and page_products:
            estimated_total_pages = math.ceil(detected["total_products"] / len(page_products))

        next_url = _find_next_link(current_html, current_url, visited)
        if not next_url and estimated_total_pages and pages_visited < estimated_total_pages:
            for candidate in _next_page_candidates(current_url, pages_visited + 1):
                if candidate not in visited:
                    next_url = candidate
                    break

        if not next_url:
            break

        visited.add(next_url)
        try:
            current_html, _ = _fetch_html_for_openai(next_url, state_file=state_file)
            current_url = next_url
        except Exception as e:
            logging.warning(f"[OpenAIOnly] Could not fetch listing page {next_url}: {e}")
            break

    products, removed = dedup_products(products)
    return products, {
        "pages_visited": pages_visited,
        "expected_pages": estimated_total_pages,
        "expected_products": detected.get("total_products"),
        "duplicates_removed": removed,
    }


def _compact_listing_evidence(html: str, url: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag_name in ("script", "style", "svg", "iframe", "noscript"):
        for tag in soup.find_all(tag_name):
            tag.decompose()

    chunks = []
    title = soup.find("title")
    if title:
        chunks.append(f"PAGE TITLE: {title.get_text(' ', strip=True)}")

    for selector in (
        "[class*='product']", "[class*='item']", "[class*='card']",
        "[class*='sku']", "[class*='price']", "table", "main",
    ):
        for tag in soup.select(selector)[:160]:
            text = tag.get_text(" ", strip=True)
            if len(text) < 20:
                continue
            links = []
            for a in tag.find_all("a", href=True)[:4]:
                href = urljoin(url, a["href"])
                label = a.get_text(" ", strip=True)
                links.append(f"{label} -> {href}".strip())
            chunk = text[:1800]
            if links:
                chunk += "\nLinks: " + " | ".join(links)
            chunks.append(chunk)
            if len("\n\n".join(chunks)) > 180_000:
                return "\n\n---\n\n".join(chunks)

    if len(chunks) < 4:
        chunks.append(soup.get_text("\n", strip=True)[:180_000])
    return "\n\n---\n\n".join(chunks)


def _compact_detail_evidence(html: str, url: str, product: dict) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    identifier_evidence = _identifier_evidence_from_detail_html(html)
    chunks = [
        "TASK: Extract only the product matching the known listing SKU/name below. Ignore related, recommended, sponsored, and recently viewed products.",
        f"DETAIL URL: {url}",
        f"KNOWN LISTING PRODUCT: {product.get('product_name', '')}",
        f"KNOWN SKU: {product.get('sku', '')}",
        f"KNOWN PRICE: {product.get('price', '')}",
    ]
    if identifier_evidence.get("identifier"):
        chunks.append(
            f"BARCODE EVIDENCE ({identifier_evidence.get('identifier_source', 'page')}): "
            f"{identifier_evidence['identifier']}"
        )

    for tag in soup.find_all(["title", "h1", "h2"]):
        text = tag.get_text(" ", strip=True)
        if text:
            chunks.append(text)

    for tag in soup.find_all("meta"):
        name = tag.get("name") or tag.get("property") or tag.get("itemprop")
        content = tag.get("content")
        if name and content and re.search(r"title|description|sku|upc|gtin|ean|barcode|image|price", name, re.I):
            chunks.append(f"META {name}: {content}")

    for script in soup.find_all("script"):
        text = script.string or script.get_text(" ", strip=True)
        if not text:
            continue
        if re.search(r"upc|gtin|ean|barcode|sku|productid|product_id|mfr|manufacturer", text, re.I):
            for match in re.finditer(r".{0,700}(?:upc|gtin|ean|barcode|sku|productid|product_id|mfr|manufacturer).{0,1200}", text, re.I | re.S):
                chunks.append("SCRIPT: " + re.sub(r"\s+", " ", match.group(0)).strip())
                if len("\n\n".join(chunks)) > 180_000:
                    return "\n\n---\n\n".join(chunks)

    for tag in soup.find_all(string=re.compile(r"UPC|GTIN|EAN|Barcode|SKU|Manufacturer|Item #", re.I)):
        parent = tag.find_parent(["li", "tr", "div", "section", "span"]) if hasattr(tag, "find_parent") else None
        text = parent.get_text(" ", strip=True) if parent else str(tag)
        if text:
            chunks.append(text[:2000])

    return "\n\n---\n\n".join(chunks)[:200_000]


def _openai_enrich_detail_pages(products: list[dict], state_file: str | None = None) -> int:
    if not llm_extractor.enabled():
        return 0

    max_details = _openai_detail_limit()
    candidates = [
        product for product in products
        if product.get("product_url")
        and not (product.get("upc") or product.get("ean") or product.get("gtin"))
    ][:max_details]
    enriched = 0

    def enrich_one(index: int, product: dict) -> bool:
        url = product.get("product_url")
        try:
            logging.info(f"[OpenAIOnly] Detail extraction [{index}/{len(candidates)}] {url}")
            content_type = "html"
            if state_file:
                html, content_type = _fetch_html_for_openai(url, state_file=state_file)
            else:
                try:
                    html = fetch_html(url)
                except Exception:
                    html = ""
            identifier_evidence = _identifier_evidence_from_detail_html(html)

            if not identifier_evidence.get("identifier") and (state_file or _html_needs_browser_crawler(html)):
                html, content_type = _fetch_html_for_openai(url, state_file=state_file)
                identifier_evidence = _identifier_evidence_from_detail_html(html)

            changed = False
            changed = _assign_identifier_from_evidence(product, identifier_evidence)

            needs_core_fields = not (
                product.get("product_name")
                and product.get("sku")
                and product.get("price")
            )
            if _openai_detail_llm_enabled() or (not changed and needs_core_fields):
                evidence = _compact_detail_evidence(html, url, product)
                detail_products = llm_extractor.extract_from_text(
                    evidence,
                    url,
                    content_type=f"detail_evidence_{content_type}",
                )
                best_detail = _choose_best_detail_product(product, detail_products)
                if best_detail:
                    changed = _merge_detail_fields_from_openai(product, best_detail) or changed
            return changed
        except Exception as e:
            logging.warning(f"[OpenAIOnly] Detail extraction failed for {url}: {e}")
            return False

    if not candidates:
        return 0

    workers = min(_openai_detail_concurrency(), len(candidates))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(enrich_one, index, product)
            for index, product in enumerate(candidates, start=1)
        ]
        for future in as_completed(futures):
            if future.result():
                enriched += 1
    return enriched


def _run_openai_only_scrape(url: str, html: str = "", state_file: str | None = None) -> dict | None:
    if not llm_extractor.enabled():
        raise RuntimeError("OPENAI_API_KEY is not set, so OpenAI-only extraction cannot run.")

    content_type = "html"
    if not html or state_file or _html_needs_browser_crawler(html):
        html, content_type = _fetch_html_for_openai(url, state_file=state_file)

    products, crawl_diagnostics = _collect_openai_listing_pages(html, url, state_file=state_file)

    if products:
        detail_wins = _openai_enrich_detail_pages(products, state_file=state_file)
    else:
        detail_wins = 0

    products = normalize_products(products)
    return {
        "strategy_id": 70,
        "strategy_name": "OpenAI Product Extraction",
        "reason": "OpenAI extracted product rows from page content and detail pages",
        "products": products,
        "_skip_external_upc_enrichment": False,
        "_crawl_diagnostics": {
            "openai_only": True,
            "content_type": content_type,
            "openai_detail_pages_enriched": detail_wins,
            **crawl_diagnostics,
            "stop_reason": "OpenAI-only extraction completed",
        },
    }


def _run_scrape_worker(run_id: int, url: str, html: str, use_playwright: bool = False) -> None:
    """
    Run scraping strategies + enrichment in a background thread.
    Calls database.complete_run() on success or database.fail_run() on error.
    """
    try:
        if _openai_only_mode():
            logging.info(f"[Job {run_id}] OpenAI-only extraction enabled")
            result = _run_openai_only_scrape(url, html=html)
            result = _maybe_recover_with_browser_catalog(result, url)
        else:
            result = feed_exporter.run(url)
        if result:
            logging.info(
                f"[Job {run_id}] {result.get('strategy_name', 'Extraction')} found "
                f"{len(result.get('products', []))} row(s)"
            )
            feed_diag = (result.get("diagnostics") or {}).get("feed")
            if isinstance(feed_diag, dict):
                crawl_diag = dict(feed_diag)
                crawl_diag.setdefault("stop_reason", result.get("reason", ""))
                result.setdefault("_crawl_diagnostics", crawl_diag)
        if result is None and not html:
            try:
                html = fetch_html(url)
            except Exception as e:
                logging.warning(
                    f"[Job {run_id}] requests fetch failed after feed-first miss: {e}"
                )
                html = ""
            use_playwright = _html_needs_browser_crawler(html)

        browser_crawl_attempt = {}

        firecrawl_mode = _firecrawl_mode()
        firecrawl_first = firecrawl_mode in {"first", "primary", "always", "firecrawl_first"}
        hosted_before_browser = (
            use_playwright
            and firecrawl_fallback.enabled()
            and firecrawl_mode not in {"0", "off", "disabled", "none", "never", "local_first"}
        )
        if result is None and firecrawl_first and firecrawl_fallback.enabled():
            try:
                firecrawl_products = firecrawl_fallback.run(url)
                if firecrawl_products:
                    logging.info(
                        f"[Job {run_id}] Firecrawl primary scrape found "
                        f"{len(firecrawl_products)} product(s)"
                    )
                    result = {
                        "strategy_id": firecrawl_fallback.ID,
                        "strategy_name": firecrawl_fallback.NAME,
                        "reason": "Firecrawl primary extraction is enabled",
                        "products": firecrawl_products,
                    }
                else:
                    logging.info(
                        f"[Job {run_id}] Firecrawl primary scrape returned no products; "
                        "continuing with local stack"
                    )
            except Exception as e:
                logging.warning(
                    f"[Job {run_id}] Firecrawl primary scrape failed ({e}); "
                    "continuing with local stack"
                )

        if result is None and hosted_before_browser:
            try:
                firecrawl_products = firecrawl_fallback.run(url)
                if firecrawl_products:
                    logging.info(
                        f"[Job {run_id}] Firecrawl hosted render found "
                        f"{len(firecrawl_products)} product(s) before local browser crawl"
                    )
                    result = {
                        "strategy_id": firecrawl_fallback.ID,
                        "strategy_name": firecrawl_fallback.NAME,
                        "reason": (
                            "Firecrawl hosted rendered extraction returned a "
                            "usable baseline before local browser crawl"
                        ),
                        "products": firecrawl_products,
                        "_crawl_diagnostics": {
                            "hosted_render_first": True,
                            "stop_reason": (
                                "Hosted rendered extraction succeeded; skipped local browser crawl "
                                "to avoid long-running public JS scrape"
                            ),
                        },
                    }
                else:
                    logging.info(
                        f"[Job {run_id}] Firecrawl hosted render returned no products; "
                        "trying local browser crawler"
                    )
            except Exception as e:
                logging.warning(
                    f"[Job {run_id}] Firecrawl hosted render failed ({e}); "
                    "trying local browser crawler"
                )

        if result is None and use_playwright:
            logging.info(
                f"[Job {run_id}] JS/browser signals detected — trying full "
                "Playwright catalog crawler first"
            )
            try:
                result, browser_crawl_attempt = _run_public_browser_catalog(url)
                if result:
                    logging.info(
                        f"[Job {run_id}] Browser catalog crawler found "
                        f"{len(result['products'])} product(s)"
                    )
                else:
                    logging.info(
                        f"[Job {run_id}] Browser catalog crawler returned no "
                        "products; falling back to HTML strategy stack"
                    )
                if result and _should_try_firecrawl_supplement(
                    result.get("products", []),
                    browser_crawl_attempt,
                ):
                    try:
                        firecrawl_products = firecrawl_fallback.run(url)
                        if firecrawl_products:
                            before = len(result.get("products", []))
                            merged = _merge_products(result.get("products", []), firecrawl_products)
                            if len(merged) > before:
                                result["products"] = merged
                                result["reason"] = (
                                    result.get("reason", "")
                                    + f"; Firecrawl supplemented {len(firecrawl_products)} hosted extraction row(s)"
                                )
                                result.setdefault("_crawl_diagnostics", {})[
                                    "firecrawl_supplement_products"
                                ] = len(firecrawl_products)
                                logging.info(
                                    f"[Job {run_id}] Firecrawl supplement increased "
                                    f"browser result from {before} to {len(merged)} product(s)"
                                )
                    except Exception as e:
                        logging.warning(f"[Job {run_id}] Firecrawl supplement failed: {e}")
            except Exception as e:
                browser_crawl_attempt = _catalog_crawl_diagnostics()
                logging.warning(
                    f"[Job {run_id}] Browser catalog crawler failed ({e}); "
                    "falling back to HTML strategy stack"
                )

        if result is None:
            if use_playwright and _html_needs_browser_crawler(html):
                if (
                    firecrawl_fallback.enabled()
                    and firecrawl_mode not in {"0", "off", "disabled", "none", "never"}
                ):
                    try:
                        firecrawl_products = firecrawl_fallback.run(url)
                        if firecrawl_products:
                            result = {
                                "strategy_id": firecrawl_fallback.ID,
                                "strategy_name": firecrawl_fallback.NAME,
                                "reason": "Firecrawl fallback after browser crawler returned no products",
                                "products": firecrawl_products,
                                "_crawl_diagnostics": browser_crawl_attempt,
                            }
                    except Exception as e:
                        logging.warning(f"[Job {run_id}] Firecrawl fallback failed: {e}")
                if result is not None:
                    pass
                else:
                    diagnostics = _build_product_diagnostics(
                        [],
                        playwright_catalog.NAME,
                        browser_crawl_attempt,
                    )
                    diagnostics.setdefault("warnings", []).append(
                        "Browser crawler found no products, and the static HTML looked blocked or JavaScript-rendered; skipped static fallback to avoid navigation rows."
                    )
                    database.complete_run(
                        run_id=run_id,
                        strategy_id=playwright_catalog.ID,
                        strategy_name=playwright_catalog.NAME,
                        products=[],
                        diagnostics=diagnostics,
                    )
                    logging.info(
                        f"[Job {run_id}] Completed with no products after browser "
                        "crawler attempt; skipped blocked/JS static fallback"
                    )
                    return
            if result is None and not html:
                diagnostics = _build_product_diagnostics(
                    [],
                    playwright_catalog.NAME,
                    browser_crawl_attempt,
                )
                database.complete_run(
                    run_id=run_id,
                    strategy_id=playwright_catalog.ID,
                    strategy_name=playwright_catalog.NAME,
                    products=[],
                    diagnostics=diagnostics,
                )
                logging.info(f"[Job {run_id}] Completed with no products after browser crawler attempt")
                return
            if result is not None:
                pass
            else:
                result = run_best_strategy(html, url, use_playwright=use_playwright)

        crawl_diagnostics = result.pop("_crawl_diagnostics", {}) or {}
        skip_external_upc = bool(result.pop("_skip_external_upc_enrichment", False)) or (
            result.get("strategy_id") == firecrawl_fallback.ID
        )
        enrich_all_pack(result["products"])
        if skip_external_upc:
            _mark_external_upc_skipped(
                result["products"],
                "external UPC lookup skipped for hosted rendered extraction; visible supplier identifiers were preserved",
            )
        else:
            result["products"] = upc_enrichment.enrich_products_upc(
                result["products"], providers=default_providers()
            )
        result["products"] = normalize_products(result["products"])
        result["diagnostics"] = _build_product_diagnostics(
            result["products"],
            result.get("strategy_name", ""),
            crawl_diagnostics,
        )
        if browser_crawl_attempt and not crawl_diagnostics:
            result["diagnostics"]["browser_crawl_attempt"] = dict(browser_crawl_attempt)

        # ── Data quality metrics ──────────────────────────────────────────
        products = result["products"]
        total = len(products)
        if total > 0:
            has_name = sum(1 for p in products if p.get("product_name"))
            has_price = sum(1 for p in products if p.get("price"))
            has_identifier = sum(1 for p in products if p.get("upc") or p.get("ean") or p.get("gtin") or p.get("gtin_case") or p.get("barcode_raw"))

            name_pct = has_name / total * 100
            price_pct = has_price / total * 100

            quality_warnings = []
            if name_pct < 50:
                quality_warnings.append(f"Only {name_pct:.0f}% of products have names")
            if price_pct < 50:
                quality_warnings.append(f"Only {price_pct:.0f}% of products have prices")

            # Detect "same price" bug: if >80% of products share the same price
            if has_price >= 3:
                from collections import Counter
                price_counts = Counter(p.get("price", "") for p in products if p.get("price"))
                most_common_price, most_common_count = price_counts.most_common(1)[0]
                if most_common_count / has_price > 0.8 and has_price > 5:
                    quality_warnings.append(
                        f"WARNING: {most_common_count}/{has_price} products share the same price "
                        f"({most_common_price}) — likely a scraping bug"
                    )

            if quality_warnings:
                logging.warning(
                    f"[Job {run_id}] Data quality issues detected:\n  " +
                    "\n  ".join(quality_warnings)
                )

            logging.info(
                f"[Job {run_id}] Quality: {has_name}/{total} names ({name_pct:.0f}%), "
                f"{has_price}/{total} prices ({price_pct:.0f}%), "
                f"{has_identifier}/{total} UPC/EAN/GTINs ({has_identifier/total*100:.0f}%)"
            )

        database.complete_run(
            run_id=run_id,
            strategy_id=result["strategy_id"],
            strategy_name=result["strategy_name"],
            products=result["products"],
            diagnostics=result.get("diagnostics", {}),
        )
        logging.info(
            f"[Job {run_id}] Completed — {len(result['products'])} product(s)"
        )
    except Exception as e:
        logging.exception(f"[Job {run_id}] Scrape worker failed")
        database.fail_run(run_id, str(e), diagnostics=build_error_report(e, stage="worker"))


def _run_auth_scrape_worker(
    run_id: int, url: str, state_file: str, session_id: str
) -> None:
    """
    Run an authenticated Playwright catalog scrape in a background thread.

    Important: keep the authenticated supplier pipeline based on supplier-visible
    data only. Do not run external UPC enrichment here because it can overwrite
    or mask the real detail-page values during Nassau/login hardening.
    """
    try:
        products = []
        strategy_id = playwright_catalog.ID
        strategy_name = playwright_catalog.NAME
        crawl_diagnostics = {}
        auth_firecrawl_mode = _auth_firecrawl_mode()
        firecrawl_first = auth_firecrawl_mode in {"first", "primary", "always", "firecrawl_first"}

        if _openai_only_mode():
            feed_result = _run_openai_only_scrape(url, state_file=state_file)
        else:
            feed_result = feed_exporter.run(url, state_file=state_file)
        if feed_result:
            products = feed_result.get("products", [])
            strategy_id = feed_result.get("strategy_id", feed_exporter.ID)
            strategy_name = feed_result.get("strategy_name", feed_exporter.NAME)
            crawl_diagnostics = (
                feed_result.get("_crawl_diagnostics")
                or feed_result.get("diagnostics", {}).get("feed", {})
                or {}
            )
            logging.info(
                f"[Job {run_id}] Auth {strategy_name} found "
                f"{len(products)} row(s)"
            )

        if not products and not _openai_only_mode() and firecrawl_first and firecrawl_fallback.enabled():
            try:
                products = firecrawl_fallback.run_authenticated(url, state_file)
                if products:
                    strategy_id = firecrawl_fallback.ID
                    strategy_name = f"{firecrawl_fallback.NAME} (Authenticated)"
                    crawl_diagnostics = {
                        "authenticated_firecrawl": True,
                        "stop_reason": "Firecrawl authenticated extraction succeeded",
                    }
                    logging.info(
                        f"[Job {run_id}] Firecrawl authenticated scrape found "
                        f"{len(products)} product(s)"
                    )
            except Exception as e:
                logging.warning(f"[Job {run_id}] Firecrawl authenticated scrape failed: {e}")

        if not products and not _openai_only_mode():
            products = playwright_catalog.run(state_file, url)
            crawl_diagnostics = getattr(playwright_catalog, "LAST_CRAWL_DIAGNOSTICS", {}) or {}

        should_try_auth_firecrawl = (
            firecrawl_fallback.enabled()
            and not _openai_only_mode()
            and not firecrawl_first
            and auth_firecrawl_mode not in {"0", "off", "disabled", "none", "never"}
            and (not products or auth_firecrawl_mode in {"supplement", "always"})
        )
        if should_try_auth_firecrawl:
            try:
                firecrawl_products = firecrawl_fallback.run_authenticated(url, state_file)
                if firecrawl_products:
                    crawl_diagnostics["authenticated_firecrawl_products"] = len(firecrawl_products)
                    if products:
                        before = len(products)
                        products = _merge_products(products, firecrawl_products)
                        if len(products) > before:
                            strategy_name = f"{strategy_name} + Firecrawl Auth Supplement"
                            crawl_diagnostics["authenticated_firecrawl_added"] = len(products) - before
                            logging.info(
                                f"[Job {run_id}] Firecrawl authenticated supplement increased "
                                f"local result from {before} to {len(products)} product(s)"
                            )
                    else:
                        products = firecrawl_products
                        strategy_id = firecrawl_fallback.ID
                        strategy_name = f"{firecrawl_fallback.NAME} (Authenticated)"
                        crawl_diagnostics["authenticated_firecrawl"] = True
                        crawl_diagnostics["stop_reason"] = (
                            "Firecrawl authenticated extraction succeeded after local crawler returned no products"
                        )
                        logging.info(
                            f"[Job {run_id}] Firecrawl authenticated fallback found "
                            f"{len(products)} product(s)"
                        )
            except Exception as e:
                logging.warning(f"[Job {run_id}] Firecrawl authenticated fallback failed: {e}")

        enrich_all_pack(products)
        products = normalize_products(products)
        diagnostics = _build_product_diagnostics(
            products,
            strategy_name,
            crawl_diagnostics,
        )
        database.complete_run(
            run_id=run_id,
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            products=products,
            diagnostics=diagnostics,
        )
        logging.info(
            f"[Job {run_id}] Auth scrape completed - {len(products)} product(s)"
        )
    except Exception as e:
        logging.exception(f"[Job {run_id}] Auth scrape worker failed")
        database.fail_run(run_id, str(e), diagnostics=build_error_report(e, stage="authenticated_worker"))
    finally:
        browser_login.finish_session(session_id)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scrape", methods=["POST"])
def scrape():
    """
    Start a background scrape job and return immediately.

    Request body:
        { "url": "https://...", "label": "Optional label" }

    Response (immediate — job still running):
        { "run_id": 123, "label": "...", "status": "running" }

    The history entry appears right away; poll GET /api/history for updates.
    """
    data = request.get_json(silent=True)
    if not data or not data.get("url"):
        return jsonify({"error": "Missing 'url' in request body."}), 400

    url = data["url"].strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return jsonify({"error": "URL must start with http:// or https://"}), 400

    label = (data.get("label") or "").strip() or _auto_label(url)

    run_id = database.create_run(label=label, source_url=url)
    threading.Thread(
        target=_run_scrape_worker,
        args=(run_id, url, "", False),
        daemon=True,
        name=f"scrape-{run_id}",
    ).start()
    return jsonify({"run_id": run_id, "label": label, "status": "running"})

@app.route("/api/history", methods=["GET"])
def history_list():
    """Return all scrape runs, newest first (no product rows)."""
    return jsonify(database.get_history())


@app.route("/api/history/<int:run_id>", methods=["GET"])
def history_get(run_id):
    """Return a single scrape run including all its product rows."""
    run = database.get_run(run_id)
    if run is None:
        return jsonify({"error": "Run not found."}), 404
    return jsonify(run)


@app.route("/api/history/<int:run_id>", methods=["PATCH"])
def history_rename(run_id):
    """
    Rename a scrape run.
    Request body: { "label": "New label" }
    """
    data = request.get_json(silent=True)
    if not data or not data.get("label", "").strip():
        return jsonify({"error": "Missing 'label'."}), 400
    updated = database.update_label(run_id, data["label"])
    if not updated:
        return jsonify({"error": "Run not found."}), 404
    return jsonify({"ok": True})


@app.route("/api/history/<int:run_id>", methods=["DELETE"])
def history_delete(run_id):
    """Delete a scrape run and all its products."""
    deleted = database.delete_run(run_id)
    if not deleted:
        return jsonify({"error": "Run not found."}), 404
    return jsonify({"ok": True})


@app.route("/api/export/<int:run_id>", methods=["GET"])
def export_xlsx(run_id):
    """
    Download a scrape run as an .xlsx file.
    The filename is derived from the run label.
    """
    run = database.get_run(run_id)
    if run is None:
        return jsonify({"error": "Run not found."}), 404

    buf = _build_xlsx(run)

    # Build a safe filename from the label
    safe_label = "".join(c if c.isalnum() or c in " -_" else "_" for c in run["label"])
    filename = f"{safe_label[:60]}.xlsx"

    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/api/auth/start", methods=["POST"])
def auth_start():
    """
    Open a visible browser at the login URL so the user can log in manually.

    Request body: { "login_url": "https://supplier.com/login" }
    Response:     { "session_id": "abc123", "status": "waiting" }
    """
    data = request.get_json(silent=True)
    if not data or not data.get("login_url"):
        return jsonify({"error": "Missing 'login_url'."}), 400

    login_url = data["login_url"].strip()
    if urlparse(login_url).scheme not in ("http", "https"):
        return jsonify({"error": "login_url must start with http:// or https://"}), 400

    try:
        session_id = browser_login.start_session(login_url)
        return jsonify({"session_id": session_id, "status": "waiting"})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Failed to open browser: {e}"}), 500


@app.route("/api/auth/scrape", methods=["POST"])
def auth_scrape():
    """
    Confirm the login session and run a full Playwright-based catalog crawl.

    Confirm the login session, then launch the Playwright catalog crawl in
    the background and return immediately.

    Flow:
      1. confirm_session() — synchronous: browser thread saves cookies to a
         temp file and closes the visible login browser (typically 2–5 s).
      2. create_run()      — history entry appears in the sidebar right away.
      3. Background thread — runs playwright_catalog.run(), enrichment, and
         complete_run() / fail_run(). Session cleanup always happens here.
      4. Returns immediately with { run_id, label, status: "running" }.

    Request body: { "session_id": "...", "url": "https://...", "label": "Optional" }
    Response:     { "run_id": 123, "label": "...", "status": "running" }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Missing request body."}), 400

    session_id = (data.get("session_id") or "").strip()
    url         = (data.get("url")        or "").strip()
    label       = (data.get("label")      or "").strip()

    if not session_id:
        return jsonify({"error": "Missing 'session_id'."}), 400
    if not url:
        return jsonify({"error": "Missing 'url'."}), 400
    if urlparse(url).scheme not in ("http", "https"):
        return jsonify({"error": "URL must start with http:// or https://"}), 400

    # ── Phase: save session state ─────────────────────────────────────────────
    # confirm_session() tells the browser thread to export storage state to a
    # temp file and close the login browser.  Returns a file path string —
    # NOT a live Playwright object — so no cross-thread greenlet issues are
    # possible.
    state_file = browser_login.confirm_session(session_id)
    if state_file is None:
        session_status = browser_login.get_session_status(session_id)
        detail = session_status.get("error") or "unknown error"
        browser_login.finish_session(session_id)
        return jsonify({
            "error": (
                f"Could not save login session state ({detail}). "
                "Make sure you are fully logged in before clicking Continue, "
                "then start a new login session."
            )
        }), 400

    label = label or _auto_label(url)

    # ── Phase 2: create history record immediately ────────────────────────────
    run_id = database.create_run(label=label, source_url=url)

    # ── Phase 3: launch Playwright scrape in background ───────────────────────
    # The worker owns session cleanup (finish_session) regardless of outcome.
    threading.Thread(
        target=_run_auth_scrape_worker,
        args=(run_id, url, state_file, session_id),
        daemon=True,
        name=f"auth-scrape-{run_id}",
    ).start()

    return jsonify({"run_id": run_id, "label": label, "status": "running"})


@app.route("/api/debug", methods=["POST"])
def debug():
    """Diagnostic info for a URL — does not save to history."""
    data = request.get_json(silent=True)
    if not data or not data.get("url"):
        return jsonify({"error": "Missing 'url'."}), 400
    url = data["url"].strip()
    if urlparse(url).scheme not in ("http", "https"):
        return jsonify({"error": "URL must start with http:// or https://"}), 400
    return jsonify(debug_scrape(url))


@app.route("/api/chat", methods=["POST"])
def chat():
    """AI support chat — stateless, no history stored."""
    data = request.get_json(silent=True)
    if not data or not data.get("message", "").strip():
        return jsonify({"error": "Missing 'message'."}), 400

    global openai_client
    if not os.environ.get("OPENAI_API_KEY"):
        return jsonify({"error": "OPENAI_API_KEY is not set."}), 500

    # Lazy-create client if it wasn't available at startup
    if openai_client is None:
        openai_client = OpenAI()

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": CHAT_SYSTEM_PROMPT},
                {"role": "user",   "content": data["message"].strip()},
            ],
            max_tokens=400,
            temperature=0.4,
        )
        return jsonify({"reply": response.choices[0].message.content.strip()})
    except Exception as e:
        return jsonify({"error": f"AI request failed: {e}"}), 500


if __name__ == "__main__":
    # threaded=True (default) is required for background scrape jobs to work
    # alongside concurrent HTTP requests.  use_reloader=False prevents the
    # dev-server reloader from forking a second process that loses background threads.
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("RAILWAY_ENVIRONMENT") is None  # debug only locally
    app.run(debug=debug, host="0.0.0.0", port=port, threaded=True, use_reloader=False)
