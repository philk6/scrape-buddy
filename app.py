"""
app.py — The Syndicate Amazon Mastery UPC Scraper — Flask server

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

# ── Local imports (wrapped for boot resilience) ──────────────────────────────
_import_errors = []
try:
    import database
except Exception as e:
    database = None; _import_errors.append(f"database: {e}")
try:
    import browser_login
except Exception as e:
    browser_login = None; _import_errors.append(f"browser_login: {e}")
try:
    import upc_enrichment
except Exception as e:
    upc_enrichment = None; _import_errors.append(f"upc_enrichment: {e}")
try:
    from scraper import fetch_html, debug_scrape, make_auth_fetch_fn
except Exception as e:
    fetch_html = debug_scrape = make_auth_fetch_fn = None; _import_errors.append(f"scraper: {e}")
try:
    from strategies import run_best_strategy
except Exception as e:
    run_best_strategy = None; _import_errors.append(f"strategies: {e}")
try:
    from strategies.detail import run as detail_run
except Exception as e:
    detail_run = None; _import_errors.append(f"strategies.detail: {e}")
try:
    from strategies import playwright_catalog
except Exception as e:
    playwright_catalog = None; _import_errors.append(f"playwright_catalog: {e}")
try:
    from upc_providers import default_providers
except Exception as e:
    default_providers = None; _import_errors.append(f"upc_providers: {e}")
try:
    from pack_parser import enrich_all as enrich_all_pack
except Exception as e:
    enrich_all_pack = None; _import_errors.append(f"pack_parser: {e}")
if _import_errors:
    import sys
    for err in _import_errors:
        print(f"[BOOT WARNING] Import failed: {err}", file=sys.stderr)

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
if database:
    database.init_db()
else:
    logging.warning('database module not available - skipping init_db')

# ── OpenAI ────────────────────────────────────────────────────────────────────
try:
    openai_client = OpenAI()
except Exception as e:
    openai_client = None
    logging.warning(f'OpenAI client init failed: {e}')

CHAT_SYSTEM_PROMPT = """You are a helpful support assistant for The Syndicate Amazon Mastery UPC Scraper.

This tool lets users paste a supplier category page URL and scrape product data from it.
It works by detecting /products/ links on the listing page and visiting each product detail page.
It extracts: product_name, brand, sku, upc, price, pack_size, case_pack, image_url, product_url.

Key behaviours:
- Strongly prefers /products/ URLs; rejects /collections/ and navigation links.
- Uses a two-pass approach: context-aware link collection first, URL-only fallback if needed.
- Saves every scrape to a local SQLite database with a label and timestamp.
- Results can be exported to .xlsx from the results header or the History sidebar.

Help users with:
- How to use the tool (paste URL, optional label, click Scrape)
- Why scraping might return no results (JavaScript-rendered sites, bot protection, unusual layouts)
- What each extracted field means (UPC, SKU, pack size, case pack)
- How to export results to Excel
- How to view, rename, and delete saved scrapes in the History sidebar
- Common issues (timeouts, empty results, missing UPC or case pack data)

Keep answers short and practical. Do not refer to the app as Scrape Buddy — it is The Syndicate Amazon Mastery UPC Scraper."""

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
]


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
    headers = [f.replace("_", " ").title() for f in PRODUCT_FIELDS]
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

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ── Background workers ────────────────────────────────────────────────────────

def _run_scrape_worker(run_id: int, url: str, html: str, use_playwright: bool = False) -> None:
    """
    Run scraping strategies + enrichment in a background thread.
    Calls database.complete_run() on success or database.fail_run() on error.
    """
    try:
        result = run_best_strategy(html, url, use_playwright=use_playwright)
        enrich_all_pack(result["products"])
        result["products"] = upc_enrichment.enrich_products_upc(
            result["products"], providers=default_providers()
        )

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
        )
        logging.info(
            f"[Job {run_id}] Completed — {len(result['products'])} product(s)"
        )
    except Exception as e:
        logging.exception(f"[Job {run_id}] Scrape worker failed")
        database.fail_run(run_id, str(e))


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
        products = playwright_catalog.run(state_file, url)
        enrich_all_pack(products)
        database.complete_run(
            run_id=run_id,
            strategy_id=playwright_catalog.ID,
            strategy_name=playwright_catalog.NAME,
            products=products,
        )
        logging.info(
            f"[Job {run_id}] Auth scrape completed - {len(products)} product(s)"
        )
    except Exception as e:
        logging.exception(f"[Job {run_id}] Auth scrape worker failed")
        database.fail_run(run_id, str(e))
    finally:
        browser_login.finish_session(session_id)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    missing = [e.split(":")[0] for e in _import_errors] if _import_errors else []
    return jsonify({"status": "ok", "missing_modules": missing}), 200


@app.route("/")
def index():
    try:
        return render_template("index.html")
    except Exception:
        return "<h1>Scrape Buddy</h1><p>Service running. UI loading...</p>", 200


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

    # Fetch HTML synchronously — fast network call, not the slow part.
    # If requests fails or HTML looks empty/blocked, try Playwright as fallback.
    html = None
    fetch_error = None

    try:
        html = fetch_html(url)
    except Exception as e:
        fetch_error = e
        logging.warning(f"[Scrape] requests fetch failed: {e} — will try Playwright")

    # Determine if we need Playwright (fetch failed, blocked, or JS-rendered)
    used_playwright = False
    needs_playwright = html is None
    if html:
        lower_html = html.lower()
        soup_check = BeautifulSoup(html, 'html.parser')
        body_text_len = len(soup_check.get_text(strip=True)) if soup_check.body else 0
        is_blocked = any(phrase in lower_html for phrase in [
            "access denied", "403 forbidden", "captcha", "are you a robot",
            "please enable javascript", "checking your browser",
        ])
        # Check for JS framework that renders products client-side
        is_js_rendered = any(sig in html for sig in [
            'data-bind=', 'ko.applyBindings', 'ng-app', 'ng-controller',
            '__NEXT_DATA__', 'data-reactroot',
        ])
        # If products exist in HTML but prices are missing, JS rendering is likely needed
        has_product_classes = bool(soup_check.find(attrs={"class": lambda c: c and any(
            kw in ' '.join(c).lower() for kw in ["product", "item", "card"]
        ) if isinstance(c, list) else False}))
        has_prices = bool(soup_check.find(string=lambda s: s and '$' in s))
        products_but_no_prices = has_product_classes and not has_prices

        if body_text_len < 500 or is_blocked or is_js_rendered or products_but_no_prices:
            needs_playwright = True

    if needs_playwright:
        try:
            from scraper import fetch_html_playwright
            pw_html = fetch_html_playwright(url, wait_ms=4000)
            if pw_html and (html is None or len(pw_html) > len(html or '') + 200):
                logging.info(
                    f"[Scrape] Playwright produced {'initial' if html is None else 'more'} content "
                    f"({len(pw_html)} chars{f' vs {len(html)} from requests' if html else ''})"
                )
                html = pw_html
                used_playwright = True
                fetch_error = None
        except Exception as e:
            logging.warning(f"[Scrape] Playwright fallback failed: {e}")

    # If we still have no HTML, return error
    if not html:
        if fetch_error:
            if 'Timeout' in type(fetch_error).__name__:
                return jsonify({"error": "Request timed out."}), 504
            return jsonify({"error": f"Failed to fetch page: {fetch_error}"}), 502
        return jsonify({"error": "Could not fetch page content."}), 502

    # Create the history record immediately so it shows up in the sidebar
    run_id = database.create_run(label=label, source_url=url)

    # Launch the slow work (parsing + enrichment + DB write) in the background
    threading.Thread(
        target=_run_scrape_worker,
        args=(run_id, url, html, used_playwright),
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

    if not os.environ.get("OPENAI_API_KEY"):
        return jsonify({"error": "OPENAI_API_KEY is not set."}), 500

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
