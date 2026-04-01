"""
debug_row_inspector.py — Concrete row/click-target inspection for structured wholesale pages.

Usage:
    python debug_row_inspector.py https://pricekingwholesale.com/all-products.html

Inspects the first 3 product rows of a structured catalog page and produces a
human-readable report covering:
  1. Detected layout type and column headers
  2. Per-row cell texts mapped to detected columns
  3. All clickable candidates inside each row with scores
  4. Detail-page validation for the top 2 candidates per row
  5. What the scraper currently extracts vs. what it should extract
"""

import sys
import re
import textwrap
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# -- Add project root to path so we can import existing strategy code -----------
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategies.row_extractor import (
    detect_layout,
    extract_products_from_layout,
    _extract_product_from_row,
    _get_direct_children_tags,
    _best_img_src,
    _extract_image_from_cell,
    _href_from_cell,
    _all_hrefs_from_row,
    _map_header_row,
    _match_column_meaning,
    _normalise_header,
)
from strategies.target_selector import (
    discover_candidates,
    validate_rich_detail,
    _score_candidate,
    Candidate,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
ROWS_TO_INSPECT = 3
CANDIDATES_PER_ROW = 5     # how many candidates to show per row
DETAIL_VALIDATE_TOP_N = 2  # how many top candidates to actually fetch + validate


# -- Helpers -------------------------------------------------------------------

def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text


def hr(char="=", width=80):
    print(char * width)


def section(title: str):
    print()
    hr("=")
    print(f"  {title}")
    hr("=")


def sub(title: str):
    print()
    hr("-")
    print(f"  {title}")
    hr("-")


def wrap(text: str, indent=4, width=76) -> str:
    prefix = " " * indent
    return textwrap.fill(str(text), width=width, initial_indent=prefix,
                         subsequent_indent=prefix)


def truncate(s: str, n=120) -> str:
    s = str(s).strip()
    return s[:n] + "…" if len(s) > n else s


def cell_text(cell, max_len=80) -> str:
    t = cell.get_text(separator=" ", strip=True)
    return truncate(t, max_len)


def get_cells(row, layout_type: str):
    if layout_type == "table":
        cells = row.find_all(["td", "th"])
    else:
        cells = _get_direct_children_tags(row)
    return cells


# -- Section 1: Layout detection -----------------------------------------------

def report_layout(html: str, url: str):
    section("1. LAYOUT DETECTION")
    soup = BeautifulSoup(html, "html.parser")
    layout = detect_layout(soup, url)

    if layout is None:
        print("\n  *** NO STRUCTURED LAYOUT DETECTED ***")
        print("  detect_layout() returned None — row_extractor will not run.")
        print("  The scraper falls back to generic CSS class heuristics.")
        return None

    print(f"\n  Layout type  : {layout.layout_type}")
    print(f"  Confidence   : {layout.confidence:.2f}")
    print(f"  Data rows    : {len(layout.data_rows)}")
    print(f"\n  Columns detected ({len(layout.columns)}):")
    for col in layout.columns:
        print(f"    [{col.index:2d}]  meaning={col.meaning:<20s}  header_text={col.header_text!r}")

    return layout


# -- Section 2: Header row raw text --------------------------------------------

def report_raw_header(html: str, url: str):
    sub("2. RAW HEADER ROW TEXT (all cells)")
    soup = BeautifulSoup(html, "html.parser")

    # Look at the first table's first row for raw header text
    for table in soup.find_all("table"):
        thead = table.find("thead")
        if thead:
            rows = thead.find_all("tr")
        else:
            rows = table.find_all("tr")[:3]  # sample first 3 rows

        if not rows:
            continue

        print(f"\n  Table found. Showing first {min(len(rows), 3)} row(s) for header candidates:\n")
        for ri, row in enumerate(rows[:3]):
            cells = row.find_all(["th", "td"])
            print(f"  Row {ri}  ({len(cells)} cells):")
            for ci, c in enumerate(cells):
                raw = c.get_text(separator=" ", strip=True)
                norm = _normalise_header(raw)
                meaning = _match_column_meaning(raw)
                match_tag = f"  >> matches '{meaning}'" if meaning else ""
                print(f"    Cell[{ci:2d}]  raw={raw!r:<30s}  norm={norm!r:<30s}{match_tag}")
        break  # only inspect the first table


# -- Section 3: Per-row cell texts + column mapping ----------------------------

def report_row_cells(layout, url: str):
    section("3. FIRST 3 DATA ROWS — cell texts mapped to columns")
    col_by_index = {c.index: c.meaning for c in layout.columns}
    base_netloc = urlparse(url).netloc

    for row_i, row in enumerate(layout.data_rows[:ROWS_TO_INSPECT], 1):
        sub(f"Row {row_i}")
        cells = get_cells(row, layout.layout_type)
        print(f"\n  Total cells in row: {len(cells)}\n")

        for ci, cell in enumerate(cells):
            txt = cell_text(cell)
            meaning = col_by_index.get(ci, "(unmapped)")
            imgs = cell.find_all("img")
            links = cell.find_all("a", href=True)

            img_info = ""
            if imgs:
                src = _best_img_src(imgs[0]) or "(no src)"
                img_info = f"  IMG: {truncate(src, 60)}"

            link_info = ""
            if links:
                hrefs = [a.get("href", "") for a in links[:2]]
                link_info = f"  LINKS: {hrefs}"

            print(f"  Cell[{ci:2d}]  col={meaning:<20s}  text={txt!r}{img_info}{link_info}")


# -- Section 4: Clickable targets per row --------------------------------------

def report_clickable_targets(layout, url: str):
    section("4. CLICKABLE TARGETS PER ROW")
    col_by_index = {c.index: c.meaning for c in layout.columns}
    base_netloc = urlparse(url).netloc

    for row_i, row in enumerate(layout.data_rows[:ROWS_TO_INSPECT], 1):
        sub(f"Row {row_i} — all clickable candidates")
        cells = get_cells(row, layout.layout_type)

        # Collect every <a href> in the row, annotated with which cell it's in
        print(f"\n  {'TAG':<8} {'CELL':<6} {'COL MEANING':<22} {'TEXT':<35} HREF")
        print(f"  {'-'*7} {'-'*5} {'-'*21} {'-'*34} {'-'*30}")

        for ci, cell in enumerate(cells):
            col_meaning = col_by_index.get(ci, "(unmapped)")
            for a in cell.find_all("a", href=True):
                href = a.get("href", "").strip()
                text = a.get_text(separator=" ", strip=True)
                abs_url = urljoin(url, href)
                has_img = "IMG-WRAP" if a.find("img") else ""
                print(f"  {'<a>':<8} [{ci:2d}]   {col_meaning:<22} {truncate(text,33):<35} {truncate(abs_url, 60)}  {has_img}")

            # also check onclick
            for el in cell.find_all(attrs={"onclick": True}):
                onclick = el.get("onclick", "")
                tag = el.name or "?"
                text = el.get_text(separator=" ", strip=True)
                m = re.search(r"['\"]([/][^'\"?#]+)['\"]", onclick)
                href = m.group(1) if m else "(no url)"
                print(f"  <{tag:<6}> [{ci:2d}]   {col_meaning:<22} {truncate(text,33):<35} onclick→{href}")

        # Also show target_selector candidate scores
        print(f"\n  --- target_selector.discover_candidates() scores for Row {row_i} ---\n")
        candidates = discover_candidates(row, url)
        if not candidates:
            print("  (no candidates found by target_selector)")
        for rank, c in enumerate(candidates[:CANDIDATES_PER_ROW], 1):
            chosen_marker = " << CHOSEN" if rank == 1 else ""
            print(f"  [{rank}] score={c.score:+3d}  source={c.source:<14} wraps_img={str(c.wraps_image):<5} "
                  f"text={truncate(c.text,30)!r:<33} reasons={c.reasons}")
            print(f"       url={truncate(c.url, 70)}{chosen_marker}")


# -- Section 5: What scraper currently extracts --------------------------------

def report_current_extraction(layout, url: str):
    section("5. CURRENT SCRAPER EXTRACTION vs. EXPECTED")

    for row_i, row in enumerate(layout.data_rows[:ROWS_TO_INSPECT], 1):
        sub(f"Row {row_i}")
        cells = get_cells(row, layout.layout_type)

        # What the scraper currently extracts
        extracted = _extract_product_from_row(row, layout.columns, url, row_index=row_i)
        print(f"\n  Scraper extracted:")
        if extracted:
            for k, v in extracted.items():
                print(f"    {k:<25} = {truncate(str(v), 80)!r}")
        else:
            print("    (None — row was discarded as empty)")

        # What the visible row data LOOKS like — ground truth from all cells
        print(f"\n  Visible row data (ground truth):")
        col_by_index = {c.index: c.meaning for c in layout.columns}
        for ci, cell in enumerate(cells):
            txt = cell.get_text(separator=" ", strip=True)
            imgs = cell.find_all("img")
            img_src = _best_img_src(imgs[0]) if imgs else ""
            links = [urljoin(url, a["href"]) for a in cell.find_all("a", href=True)]
            meaning = col_by_index.get(ci, "(unmapped)")
            if txt or img_src or links:
                print(f"    Cell[{ci}] {meaning:<20} text={truncate(txt,40)!r}", end="")
                if img_src:
                    print(f"  img={truncate(img_src,50)!r}", end="")
                if links:
                    print(f"  links={[truncate(l,50) for l in links[:2]]}", end="")
                print()


# -- Section 6: Detail-page validation for top 2 candidates per row ------------

def report_detail_validation(layout, url: str):
    section("6. DETAIL-PAGE VALIDATION — top 2 candidates per row")

    for row_i, row in enumerate(layout.data_rows[:ROWS_TO_INSPECT], 1):
        sub(f"Row {row_i} — fetching top {DETAIL_VALIDATE_TOP_N} candidates")
        candidates = discover_candidates(row, url)
        viable = [c for c in candidates if c.score >= 0][:DETAIL_VALIDATE_TOP_N]

        if not viable:
            print("  No viable candidates.")
            continue

        for rank, c in enumerate(viable, 1):
            print(f"\n  Candidate [{rank}]  score={c.score:+d}  url={c.url}")
            try:
                detail_html = fetch(c.url)
                detail_soup = BeautifulSoup(detail_html, "html.parser")
                title_tag = detail_soup.find("title")
                page_title = title_tag.get_text(strip=True) if title_tag else "(no title)"
                page_text = detail_soup.get_text(separator=" ", strip=True).lower()
                final_url = c.url  # requests follows redirects; real url would need response.url

                rich = validate_rich_detail(detail_html)

                # Check for specific useful signals
                signals = {
                    "barcode/upc/gtin"         : any(kw in page_text for kw in ["barcode", "upc", "gtin", "ean"]),
                    "item-no/sku"               : any(kw in page_text for kw in ["item no", "item number", "item#", "sku"]),
                    "master case / case pack"   : any(kw in page_text for kw in ["master case", "case pack", "units per case"]),
                    "pricing table"             : any(kw in page_text for kw in ["price table", "quantity break", "price tier", "volume price"]),
                    "structured product details": bool(detail_soup.find("dl") or
                                                       len(detail_soup.find_all("dt")) >= 2 or
                                                       any(len(t.find_all("tr")) >= 3 for t in detail_soup.find_all("table"))),
                    "product image"             : bool(detail_soup.find("img")),
                }

                print(f"    Title       : {page_title}")
                print(f"    Final URL   : {final_url}")
                print(f"    Rich score  : {rich.score}  is_rich={rich.is_rich}  signals={rich.signals_found}")
                print(f"    Field signals:")
                for sig_name, present in signals.items():
                    mark = "YES" if present else " no"
                    print(f"      [{mark}]  {sig_name}")

            except Exception as e:
                print(f"    FETCH FAILED: {e}")


# -- Section 7: Root-cause summary ---------------------------------------------

def report_root_cause_prompt(layout, url: str):
    section("7. ROOT CAUSE ANALYSIS INPUTS")

    if layout is None:
        print("\n  detect_layout() returned None.")
        print("  Possible causes:")
        print("  - Table header row not identified (_is_header_row returned False for all rows)")
        print("  - Column header texts do not match any keyword in _COLUMN_KEYWORDS")
        print("  - Fewer than _MIN_RECGGNIZED_COLS (2) columns matched")
        print("  - Fewer than _MIN_DATA_ROWS (2) data rows found")
        print("  - Page uses div-grid layout but header element contains images")
        print()
        print("  Next step: check raw header row text in section 2.")
        return

    col_meanings = {c.meaning for c in layout.columns}
    has_image_col = "image" in col_meanings
    has_sku_col = "sku" in col_meanings
    has_order_col = "order_target" in col_meanings
    has_price_col = "price" in col_meanings
    has_name_col = "product_name" in col_meanings

    print(f"\n  Columns found:     {sorted(col_meanings)}")
    print(f"  IMAGE column:      {'YES' if has_image_col else 'MISSINE — image_url will NOT be extracted from correct cell'}")
    print(f"  DESCRIPTION col:   {'YES' if has_name_col else 'MISSING — product_name extraction may fail'}")
    print(f"  ITEM/SKU column:   {'YES' if has_sku_col else 'MISSING'}")
    print(f"  ORDER column:      {'YES' if has_order_col else 'MISSING — clickable target falls back to description link'}")
    print(f"  PRICE column:      {'YES' if has_price_col else 'MISSING'}")
    print()
    print("  Click-target priority (row_extractor._extract_product_from_row):")
    print("    1. order_target column link")
    print("    2. sku column link")
    print("    3. product_name column link")
    print("    4. first non-image link in row (fallback)")
    print()
    if not has_order_col:
        print("  WARNING: No ORDER column detected — scraper falls back to description link")
        print("           or first non-image link, which may not be the correct target.")


# -- Main ----------------------------------------------------------------------

def main(url: str):
    print(f"\n  Fetching: {url}")
    html = fetch(url)
    print(f"  HTML length: {len(html):,} chars")

    # Run all sections
    layout = report_layout(html, url)
    report_raw_header(html, url)

    if layout is not None and layout.data_rows:
        report_row_cells(layout, url)
        report_clickable_targets(layout, url)
        report_current_extraction(layout, url)
        report_detail_validation(layout, url)

    report_root_cause_prompt(layout, url)

    section("DONE")
    print()


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "https://pricekingwholesale.com/all-products.html"
    main(target)
