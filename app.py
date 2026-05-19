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
from datetime import datetime, timezone
from urllib.parse import urlparse

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
from scraper import fetch_html, debug_scrape, make_auth_fetch_fn
from strategies import run_best_strategy
from strategies.detail import run as detail_run
from strategies import playwright_catalog, firecrawl_fallback, feed_exporter
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


def _run_scrape_worker(run_id: int, url: str, html: str, use_playwright: bool = False) -> None:
    """
    Run scraping strategies + enrichment in a background thread.
    Calls database.complete_run() on success or database.fail_run() on error.
    """
    try:
        result = feed_exporter.run(url)
        if result:
            logging.info(
                f"[Job {run_id}] Feed-first export found "
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
            has_upc = sum(1 for p in products if p.get("upc"))

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
                f"{has_upc}/{total} UPCs ({has_upc/total*100:.0f}%)"
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

        feed_result = feed_exporter.run(url, state_file=state_file)
        if feed_result:
            products = feed_result.get("products", [])
            strategy_id = feed_result.get("strategy_id", feed_exporter.ID)
            strategy_name = feed_result.get("strategy_name", feed_exporter.NAME)
            crawl_diagnostics = feed_result.get("diagnostics", {}).get("feed", {})
            logging.info(
                f"[Job {run_id}] Auth feed-first export found "
                f"{len(products)} row(s)"
            )

        if not products and firecrawl_first and firecrawl_fallback.enabled():
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

        if not products:
            products = playwright_catalog.run(state_file, url)
            crawl_diagnostics = getattr(playwright_catalog, "LAST_CRAWL_DIAGNOSTICS", {}) or {}

        should_try_auth_firecrawl = (
            firecrawl_fallback.enabled()
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
