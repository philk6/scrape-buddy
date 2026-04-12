"""
strategies/row_extractor.py — Structured row/table catalog extraction

Highest-priority extraction layer for wholesale B2B supplier pages.

When a page exposes a clearly structured catalog layout — HTML tables with
labeled headers, or div-based grids with column labels — this module extracts
product data DIRECTLY from that structure instead of relying on generic CSS
class heuristics.

Hierarchy (caller is responsible for ordering):
  1. Structured row/table detection (this module)     ← highest priority
  2. Row-level target selection (target_selector.py)
  3. Generic CSS heuristics (scraper.py)              ← fallback only

Public API
──────────
  detect_layout(soup, base_url)
      → StructuredLayout | None
        Detects whether the page has a recognizable table or div-grid layout.

  extract_products_from_layout(layout, base_url)
      → list[dict]
        Extracts product rows from an already-detected layout.

  extract_products_from_page(html, base_url)
      → list[dict] | None
        One-shot helper: detect + extract.
        Returns None (not []) if no structured layout was found — this signals
        the caller to fall through to generic heuristics.

  extract_row_links(soup, base_url)
      → list[tuple[str, list[str]]]
        Returns (primary_url, [alt_urls]) tuples for detail-page link
        collection. Used in detail.py Pass -1 — bypasses the /products/ URL
        requirement so it works with non-Shopify URL schemes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── Column header keyword mapping ─────────────────────────────────────────────
# Maps column meaning → list of lowercase keywords that identify it.
# Multi-word keywords listed before shorter ones to reduce false matches.

_COLUMN_KEYWORDS: dict[str, list[str]] = {
    "image": [
        "image", "img", "photo", "pic", "picture", "thumbnail", "thumb",
    ],
    "product_name": [
        "description", "product description", "item description",
        "product name", "item name", "name", "product", "title",
    ],
    "sku": [
        "item no", "item number", "item #", "item-no", "item code",
        "part number", "part no", "part #", "catalog number", "catalog #",
        "model number", "model #", "vendor part",
        "sku", "item", "part", "model", "code",
    ],
    # qty MUST come before order_target in this dict so that compound headers
    # like "Qty Order" and "Qty per Order" are matched as qty rather than being
    # caught by the shorter "order" keyword in order_target via substring match.
    "qty": [
        "qty order", "qty per order", "quantity order", "order qty",
        "order quantity", "min order qty", "min order quantity",
        "min order", "min qty", "minimum order",
        "qty", "quantity",
    ],
    "order_target": [
        "order", "quick order", "add to order", "buy", "view detail",
        "view details", "detail", "details", "more info", "info",
    ],
    "price": [
        "unit price", "each price", "price each", "price per unit",
        "list price", "your price", "sale price", "net price",
        "price", "cost", "each", "ea", "rate", "amount",
    ],
    "case_pack": [
        "case pack", "master case", "units per case", "qty per case",
        "case qty", "inner pack", "pack size",
        "case", "pack",
    ],
    "brand": [
        "manufacturer", "vendor", "brand name",
        "brand", "mfg", "make",
    ],
    "upc": [
        "upc code", "upc/ean",
        "upc", "barcode", "gtin", "ean",
    ],
    "extension": [
        "extension", "extention", "ext price", "extended price",
        "ext", "total",
    ],
}

# Minimum recognized columns to accept a layout
_MIN_RECOGNIZED_COLS = 2

# Minimum data rows to accept a layout
_MIN_DATA_ROWS = 2

# Confidence threshold (ratio of recognized cols / total cols)
_CONFIDENCE_THRESHOLD = 0.25


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class ColumnMapping:
    index:       int    # 0-based position in the row
    meaning:     str    # one of the keys in _COLUMN_KEYWORDS
    header_text: str    # original header text (for logging)


@dataclass
class StructuredLayout:
    layout_type:  str                     # "table" | "div_grid"
    columns:      list[ColumnMapping]     # recognized columns, by index
    data_rows:    list                    # BS4 Tag objects (the product rows)
    confidence:   float                  # 0.0–1.0


# ── Header text normalisation ──────────────────────────────────────────────────

def _normalise_header(text: str) -> str:
    """Lowercase, collapse punctuation and extra spaces."""
    text = text.lower()
    text = re.sub(r"[:\-_/\\|]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _match_column_meaning(header_text: str) -> str | None:
    """
    Return the meaning (e.g. "image", "sku") that best matches header_text.
    Longer / more-specific keywords are checked first.
    Returns None if no match.

    Uses word-boundary matching (not bare substring) so that short keywords
    like "ea" (each) or "ext" do not fire on unrelated words such as
    "cleaning", "cleaners", "extension", etc.
    """
    norm = _normalise_header(header_text)
    for meaning, keywords in _COLUMN_KEYWORDS.items():
        for kw in keywords:           # list already ordered specific→generic
            if kw == norm:
                return meaning
            if re.search(r'\b' + re.escape(kw) + r'\b', norm):
                return meaning
    return None


def _map_header_row(header_cells: list) -> list[ColumnMapping]:
    """
    Given a list of header cell elements (th or td), return a list of
    ColumnMapping for every cell whose text matches a known column keyword.
    """
    mappings = []
    for idx, cell in enumerate(header_cells):
        text = cell.get_text(separator=" ", strip=True)
        meaning = _match_column_meaning(text)
        if meaning:
            mappings.append(ColumnMapping(index=idx, meaning=meaning, header_text=text))
    return mappings


# ── Table layout detection ─────────────────────────────────────────────────────

def _is_header_row(cells: list) -> bool:
    """
    Heuristic: a row looks like a header if:
      - It contains <th> elements, OR
      - At least 2 cells have text matching known column keywords, AND
        the row contains no <img> tags and no price-like values.
    """
    if any(c.name == "th" for c in cells):
        return True
    texts = [c.get_text(strip=True) for c in cells]
    hits = sum(1 for t in texts if _match_column_meaning(t) is not None)
    has_images = any(c.find("img") for c in cells)
    has_prices = any(re.search(r"\$[\d,]+\.\d{2}", c.get_text()) for c in cells)
    return hits >= _MIN_RECOGNIZED_COLS and not has_images and not has_prices


def _detect_table_layout(soup: BeautifulSoup) -> StructuredLayout | None:
    """
    Scan for HTML tables with recognized column headers.
    Returns the best-matching StructuredLayout, or None.
    """
    best: StructuredLayout | None = None
    best_score = 0.0

    for table in soup.find_all("table"):
        # Gather all rows from thead + tbody, or just all tr
        thead = table.find("thead")
        tbody = table.find("tbody")

        if thead and tbody:
            header_rows = thead.find_all("tr")
            data_rows   = tbody.find_all("tr")
        else:
            all_rows = table.find_all("tr", recursive=False)
            if not all_rows:
                all_rows = table.find_all("tr")
            header_rows = []
            data_rows   = []
            # Walk rows and split at first non-header row
            for row in all_rows:
                cells = row.find_all(["th", "td"])
                if not cells:
                    continue
                if not header_rows and _is_header_row(cells):
                    header_rows.append(row)
                else:
                    data_rows.append(row)

        if not header_rows or len(data_rows) < _MIN_DATA_ROWS:
            continue

        # Use last header row (handles multi-level headers)
        header_cells = header_rows[-1].find_all(["th", "td"])
        if not header_cells:
            continue

        columns = _map_header_row(header_cells)
        if len(columns) < _MIN_RECOGNIZED_COLS:
            continue

        confidence = len(columns) / max(len(header_cells), 1)
        if confidence < _CONFIDENCE_THRESHOLD:
            continue

        if confidence > best_score:
            best_score = confidence
            best = StructuredLayout(
                layout_type="table",
                columns=columns,
                data_rows=data_rows,
                confidence=confidence,
            )

    return best


# ── Div-grid layout detection ─────────────────────────────────────────────────

def _get_direct_children_tags(el) -> list:
    return [c for c in el.children if getattr(c, "name", None) is not None]


def _detect_div_layout(soup: BeautifulSoup) -> StructuredLayout | None:
    """
    Scan for div-based grid layouts where a "header row" container holds
    column labels and subsequent sibling containers are product rows.

    Pattern:
      <div class="header-row">
        <div>IMAGE</div><div>DESCRIPTION</div><div>ITEM</div><div>PRICE</div>
      </div>
      <div class="product-row"><div><img></div><div>Name</div>...</div>
      <div class="product-row">...</div>
    """
    best: StructuredLayout | None = None
    best_score = 0.0

    # Minimum columns that MUST be present for a real catalog header.
    # A legitimate product catalog always has at least one identifying column
    # (image, product_name, or sku).  Without one, 2-column matches like
    # "Quantity: / 1 Each" are false positives.
    _IDENTITY_MEANINGS = {"image", "product_name", "sku"}

    # Walk every block-level element looking for a potential header row
    for el in soup.find_all(["div", "li", "ul", "section", "article"]):
        children = _get_direct_children_tags(el)
        if len(children) < _MIN_RECOGNIZED_COLS:
            continue

        # Must not contain images itself (a header row is text-only)
        if el.find("img"):
            continue

        child_texts = [c.get_text(separator=" ", strip=True) for c in children]
        meanings = [_match_column_meaning(t) for t in child_texts]
        recognized = sum(1 for m in meanings if m is not None)

        if recognized < _MIN_RECOGNIZED_COLS:
            continue

        # Require at least one identity column (image, product_name, or sku)
        # to distinguish real catalog headers from spurious attribute labels
        meaning_set = {m for m in meanings if m is not None}
        if not meaning_set & _IDENTITY_MEANINGS:
            continue

        # Find data rows: later siblings of el with the same tag name
        parent = el.parent
        if parent is None:
            continue

        data_rows = []
        found_header = False
        for sibling in parent.children:
            if sibling is el:
                found_header = True
                continue
            if not found_header:
                continue
            if getattr(sibling, "name", None) != el.name:
                continue
            # Data rows should have a similar number of children
            sib_children = _get_direct_children_tags(sibling)
            if abs(len(sib_children) - len(children)) <= 1:
                data_rows.append(sibling)

        if len(data_rows) < _MIN_DATA_ROWS:
            continue

        # Verify data rows actually contain data (image, text, price)
        sample = data_rows[0]
        has_content = bool(
            sample.find("img")
            or re.search(r"\$[\d,]+", sample.get_text())
            or len(sample.get_text(strip=True)) > 5
        )
        if not has_content:
            continue

        # Build column map
        columns = [
            ColumnMapping(index=i, meaning=m, header_text=child_texts[i])
            for i, m in enumerate(meanings)
            if m is not None
        ]
        confidence = recognized / max(len(children), 1)

        if confidence > best_score:
            best_score = confidence
            best = StructuredLayout(
                layout_type="div_grid",
                columns=columns,
                data_rows=data_rows,
                confidence=confidence,
            )

    return best


# ── Layout detection (public) ─────────────────────────────────────────────────

def detect_layout(soup: BeautifulSoup, base_url: str) -> StructuredLayout | None:
    """
    Detect whether the page has a structured row/table catalog layout.

    Tries table detection first (most explicit), then div-grid.
    Returns the best match, or None if no structured layout is found.
    """
    try:
        layout = _detect_table_layout(soup)
        if layout:
            col_summary = ", ".join(
                f"{c.meaning}[{c.index}]='{c.header_text}'"
                for c in layout.columns
            )
            logger.info(
                f"[RowExtractor] Table layout detected — "
                f"confidence={layout.confidence:.2f}, "
                f"{len(layout.data_rows)} data rows, "
                f"columns: {col_summary}"
            )
            return layout
    except Exception as e:
        logger.warning(f"[RowExtractor] Table detection error: {e}")

    try:
        layout = _detect_div_layout(soup)
        if layout:
            col_summary = ", ".join(
                f"{c.meaning}[{c.index}]='{c.header_text}'"
                for c in layout.columns
            )
            logger.info(
                f"[RowExtractor] Div-grid layout detected — "
                f"confidence={layout.confidence:.2f}, "
                f"{len(layout.data_rows)} data rows, "
                f"columns: {col_summary}"
            )
            return layout
    except Exception as e:
        logger.warning(f"[RowExtractor] Div-grid detection error: {e}")

    logger.info("[RowExtractor] No structured layout detected — caller should use generic heuristics")
    return None


# ── Image extraction helpers ───────────────────────────────────────────────────

def _best_img_src(img_tag) -> str:
    """Try img src attributes in priority order, skip data URIs."""
    for attr in ["src", "data-src", "data-lazy-src", "data-original",
                 "data-img", "data-image", "data-full-src"]:
        val = (img_tag.get(attr) or "").strip()
        if val and not val.startswith("data:"):
            return val
    return ""


def _extract_image_from_cell(cell, base_url: str) -> str:
    """
    Extract the best image URL from an IMAGE column cell.

    Priority:
      1. <img> src / data-src  (explicit image element)
      2. background-image in style attribute
      3. Largest img by dimension attributes (if multiple)
    """
    imgs = cell.find_all("img")
    if imgs:
        # Pick the img with the largest declared size if sizes differ,
        # otherwise just take the first non-tiny one
        best_src = ""
        best_area = -1
        for img in imgs:
            src = _best_img_src(img)
            if not src:
                continue
            try:
                w = int(img.get("width") or 0)
                h = int(img.get("height") or 0)
                area = w * h
            except (ValueError, TypeError):
                area = 0
            if area > best_area:
                best_area = area
                best_src = src
        if best_src:
            return urljoin(base_url, best_src)

    # Fallback: background-image in style
    for el in cell.find_all(style=True):
        m = re.search(
            r"background(?:-image)?\s*:\s*url\(['\"]?([^'\")\s]+)['\"]?\)",
            el.get("style", ""),
            re.IGNORECASE,
        )
        if m:
            return urljoin(base_url, m.group(1))

    return ""


# ── Link extraction helpers ────────────────────────────────────────────────────

def _href_from_cell(cell, base_url: str, base_netloc: str) -> str:
    """Return the first valid same-domain href found in a cell."""
    for a in cell.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        url = urljoin(base_url, href)
        if urlparse(url).netloc == base_netloc:
            return url
    return ""


def _all_hrefs_from_row(row, base_url: str, base_netloc: str) -> list[str]:
    """Return all valid same-domain hrefs from a row element."""
    seen: set[str] = set()
    hrefs: list[str] = []
    for a in row.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        url = urljoin(base_url, href)
        if urlparse(url).netloc == base_netloc and url not in seen:
            seen.add(url)
            hrefs.append(url)
    return hrefs


# ── Price normalization helper ────────────────────────────────────────────────

def _normalize_price_from_cell(text: str) -> str:
    """
    Normalize raw price cell text to a single price value.

    Handles:
    - Simple prices: "$9.99" → "$9.99"
    - Tiered pricing: "Price2/Min: $8.50 / 24 ea+Price1/Min: $9.50 / 6 ea+" → "$8.50"
    - Multiple prices: extracts all $X.XX patterns and returns the lowest

    Returns the lowest price found (as a string), or empty string if no prices found.
    """
    text = text.strip()
    if not text:
        return ""

    # Extract all dollar amounts from the text
    all_prices = re.findall(r'\$\s*\d[\d,]*(?:\.\d{2})?', text)
    if not all_prices:
        return ""

    # If only one price, return it normalized
    if len(all_prices) == 1:
        normalized = all_prices[0].replace(' ', '').replace('$', '$')
        # Clean up any lingering spaces
        return re.sub(r'\s+', '', normalized)

    # Multiple prices: find the lowest
    try:
        prices_with_values = []
        for price_str in all_prices:
            # Extract numeric value for comparison
            numeric = price_str.replace('$', '').replace(',', '').replace(' ', '')
            price_float = float(numeric)
            prices_with_values.append((price_float, price_str))

        # Sort by numeric value and return the lowest
        prices_with_values.sort(key=lambda x: x[0])
        lowest_price = prices_with_values[0][1]
        return lowest_price.replace(' ', '').strip()
    except (ValueError, IndexError):
        # If conversion fails, return first price
        return re.sub(r'\s+', '', all_prices[0])


# ── Row data extraction ────────────────────────────────────────────────────────

def _extract_product_from_row(
    row,
    columns: list[ColumnMapping],
    base_url: str,
    row_index: int,
) -> dict | None:
    """
    Extract a product dict from a single data row using the column mapping.

    Column priority for product_url:
      order_target > sku > product_name > any link in row

    Image priority:
      image column > any img in row

    Returns None if the row contains no useful data.
    """
    base_netloc = urlparse(base_url).netloc

    # Get all cells from the row
    cells = row.find_all(["td", "th"])
    if not cells:
        # Div-grid row — children ARE the cells
        cells = _get_direct_children_tags(row)
    if not cells:
        return None

    product: dict = {}

    # Build a dict-of-lists so that multiple columns sharing the same meaning
    # (e.g. "Order" AND "Qty Order" both matching order_target) are all
    # preserved.  Using a plain dict would let the last column silently
    # overwrite earlier ones, causing the wrong column to be used.
    # Columns are appended in their original index order, so [0] is always
    # the lowest-index (leftmost) column for that meaning.
    cols_by_meaning: dict[str, list[ColumnMapping]] = {}
    for c in columns:
        cols_by_meaning.setdefault(c.meaning, []).append(c)

    def _cell(meaning: str):
        """Return the first (lowest-index) cell for a given column meaning."""
        col_list = cols_by_meaning.get(meaning)
        if not col_list:
            return None
        col = col_list[0]
        if col.index >= len(cells):
            return None
        return cells[col.index]

    # ── IMAGE ────────────────────────────────────────────────────────────────
    img_cell = _cell("image")
    if img_cell is not None:
        img_url = _extract_image_from_cell(img_cell, base_url)
        if img_url:
            product["image_url"] = img_url
            logger.debug(f"[RowExtractor] Row {row_index}: image from IMAGE column: {img_url!r}")

    # ── PRODUCT NAME ─────────────────────────────────────────────────────────
    name_cell = _cell("product_name")
    if name_cell is not None:
        text = name_cell.get_text(separator=" ", strip=True)
        if text:
            product["product_name"] = text

    # ── SKU / ITEM NUMBER ─────────────────────────────────────────────────────
    sku_cell = _cell("sku")
    if sku_cell is not None:
        text = sku_cell.get_text(separator=" ", strip=True)
        if text:
            product["sku"] = text

    # ── SKU fallback: extract numeric item ID from ORDER column text ──────────
    # Many wholesale order-form pages embed item/part numbers directly in the
    # ORDER column (e.g. "ORDER 6273528", "Item #A-1042").  When no dedicated
    # SKU column exists, scan the order_target cell text for a standalone digit
    # sequence (5–10 digits) and use it as the SKU.
    if "sku" not in product:
        for col in cols_by_meaning.get("order_target", []):
            if col.index >= len(cells):
                continue
            order_text = cells[col.index].get_text(separator=" ", strip=True)
            m = re.search(r'\b(\d{5,10})\b', order_text)
            if m:
                product["sku"] = m.group(1)
                logger.debug(
                    f"[RowExtractor] Row {row_index}: "
                    f"sku={m.group(1)!r} from ORDER column text {order_text!r}"
                )
                break

    # ── PRICE ─────────────────────────────────────────────────────────────────
    price_cell = _cell("price")
    if price_cell is not None:
        text = price_cell.get_text(separator=" ", strip=True)
        if text:
            # Normalize: extract first $X.XX pattern, handle tiered pricing
            normalized = _normalize_price_from_cell(text)
            if normalized:
                product["price"] = normalized
                if len(normalized) < len(text):
                    # Store the full raw text if it was normalized
                    product["raw_price_text"] = text
            else:
                product["price"] = text

    # ── CASE PACK ─────────────────────────────────────────────────────────────
    case_cell = _cell("case_pack")
    if case_cell is not None:
        text = case_cell.get_text(separator=" ", strip=True)
        if text:
            product["case_pack"] = text

    # ── BRAND ─────────────────────────────────────────────────────────────────
    brand_cell = _cell("brand")
    if brand_cell is not None:
        text = brand_cell.get_text(separator=" ", strip=True)
        if text:
            product["brand"] = text

    # ── UPC ───────────────────────────────────────────────────────────────────
    upc_cell = _cell("upc")
    if upc_cell is not None:
        text = upc_cell.get_text(separator=" ", strip=True)
        digits = re.sub(r"[\s\-]", "", text)
        if re.fullmatch(r"\d{8,14}", digits):
            product["upc"] = digits

    # ── QTY (used as pack_size fallback) ──────────────────────────────────────
    qty_cell = _cell("qty")
    if qty_cell is not None and "case_pack" not in product:
        text = qty_cell.get_text(separator=" ", strip=True)
        if text:
            product["pack_size"] = text

    # ── PRODUCT URL — priority: order_target > sku link > name link > any ────
    # Iterate ALL columns for each meaning (not just the first) so that if
    # e.g. "Qty Order" maps to order_target and has no link, we still try the
    # real "Order" column which does have the product-detail link.
    all_row_links = _all_hrefs_from_row(row, base_url, base_netloc)
    product_url = ""
    chosen_source = ""

    for priority_meaning, label in [
        ("order_target", "ORDER column"),
        ("sku",          "ITEM/SKU column"),
        ("product_name", "DESCRIPTION column"),
    ]:
        for col in cols_by_meaning.get(priority_meaning, []):
            if col.index >= len(cells):
                continue
            url = _href_from_cell(cells[col.index], base_url, base_netloc)
            if url:
                product_url = url
                chosen_source = f"{label} (col[{col.index}]='{col.header_text}')"
                break
        if product_url:
            break

    if not product_url and all_row_links:
        # Exclude image-only links (links that contain only an img tag)
        non_img_links = []
        for a in row.find_all("a", href=True):
            if not a.find("img"):   # skip pure image links as product target
                href = (a.get("href") or "").strip()
                if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
                    continue
                url = urljoin(base_url, href)
                if urlparse(url).netloc == base_netloc:
                    non_img_links.append(url)
        if non_img_links:
            product_url = non_img_links[0]
            chosen_source = "first non-image link in row"
        elif all_row_links:
            product_url = all_row_links[0]
            chosen_source = "first link in row (image link)"

    if product_url:
        product["product_url"] = product_url
        logger.debug(
            f"[RowExtractor] Row {row_index}: "
            f"product_url from {chosen_source}: {product_url!r}"
        )

    # ── If no image from column, try anywhere in the row ─────────────────────
    if "image_url" not in product:
        for img in row.find_all("img"):
            src = _best_img_src(img)
            if src and not src.startswith("data:"):
                product["image_url"] = urljoin(base_url, src)
                logger.debug(
                    f"[RowExtractor] Row {row_index}: "
                    f"image from row fallback (no IMAGE column): {src!r}"
                )
                break

    # ── Discard empty rows ────────────────────────────────────────────────────
    has_data = (
        product.get("product_name")
        or product.get("sku")
        or product.get("product_url")
    )
    if not has_data:
        return None

    return product


# ── Public extraction functions ───────────────────────────────────────────────

def extract_products_from_layout(
    layout: StructuredLayout,
    base_url: str,
) -> list[dict]:
    """
    Extract product dicts from an already-detected StructuredLayout.

    Logs per-row extraction results and summary statistics.
    """
    products = []
    skipped = 0

    for i, row in enumerate(layout.data_rows, 1):
        try:
            product = _extract_product_from_row(row, layout.columns, base_url, row_index=i)
            if product:
                products.append(product)
            else:
                skipped += 1
        except Exception as e:
            logger.warning(f"[RowExtractor] Row {i} extraction error: {e}")
            skipped += 1

    logger.info(
        f"[RowExtractor] Extracted {len(products)} product(s) "
        f"({skipped} empty/skipped rows) from {layout.layout_type} layout"
    )
    return products


def extract_products_from_page(html: str, base_url: str) -> list[dict] | None:
    """
    Detect structured layout and extract products in one call.

    Returns list[dict] if a layout was detected (may be empty if all rows
    were blank).  Returns None if no structured layout was detected — this
    signals the caller to fall through to generic heuristics.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        layout = detect_layout(soup, base_url)
        if layout is None:
            return None
        products = extract_products_from_layout(layout, base_url)
        return products
    except Exception as e:
        logger.warning(f"[RowExtractor] extract_products_from_page error: {e}")
        return None


def extract_row_links(
    soup: BeautifulSoup,
    base_url: str,
) -> list[tuple[str, list[str]]]:
    """
    Extract (primary_url, [alt_urls]) tuples from a structured layout for
    use in detail-page link collection.

    Unlike the URL-pattern-based passes in detail.py, this uses the
    explicit column structure and therefore works on non-Shopify sites
    whose product URLs don't contain /products/.

    Returns [] if no structured layout is detected (caller uses next pass).
    """
    try:
        layout = detect_layout(soup, base_url)
        if layout is None:
            return []

        base_netloc = urlparse(base_url).netloc
        results: list[tuple[str, list[str]]] = []
        # Use dict-of-lists so multiple columns with the same meaning are all
        # preserved — same fix as in _extract_product_from_row.
        cols_by_meaning: dict[str, list[ColumnMapping]] = {}
        for c in layout.columns:
            cols_by_meaning.setdefault(c.meaning, []).append(c)
        seen_primary: set[str] = set()

        for i, row in enumerate(layout.data_rows, 1):
            try:
                cells = row.find_all(["td", "th"])
                if not cells:
                    cells = _get_direct_children_tags(row)
                if not cells:
                    continue

                all_links = _all_hrefs_from_row(row, base_url, base_netloc)
                if not all_links:
                    continue

                # Choose primary using column priority — try ALL columns for
                # each meaning so that empty columns (e.g. "Qty Order") don't
                # block the real link column (e.g. "Order").
                primary = ""
                for priority_meaning in ("order_target", "sku", "product_name"):
                    for col in cols_by_meaning.get(priority_meaning, []):
                        if col.index >= len(cells):
                            continue
                        url = _href_from_cell(cells[col.index], base_url, base_netloc)
                        if url:
                            primary = url
                            break
                    if primary:
                        break

                if not primary:
                    # Fall back to first non-image link
                    for a in row.find_all("a", href=True):
                        if a.find("img"):
                            continue
                        href = (a.get("href") or "").strip()
                        if href and not href.startswith(("javascript:", "mailto:", "tel:", "#")):
                            url = urljoin(base_url, href)
                            if urlparse(url).netloc == base_netloc:
                                primary = url
                                break

                if not primary and all_links:
                    primary = all_links[0]

                if not primary or primary in seen_primary:
                    continue

                seen_primary.add(primary)
                alts = [u for u in all_links if u != primary]
                results.append((primary, alts))

            except Exception as e:
                logger.warning(f"[RowExtractor] extract_row_links row {i} error: {e}")
                continue

        if results:
            logger.info(
                f"[RowExtractor] extract_row_links: "
                f"{len(results)} (primary, alts) tuples from {layout.layout_type} layout"
            )
        return results

    except Exception as e:
        logger.warning(f"[RowExtractor] extract_row_links error: {e}")
        return []
