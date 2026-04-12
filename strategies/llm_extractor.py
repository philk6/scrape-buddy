"""
strategies/llm_extractor.py — Tier 2: LLM-Assisted Product Extraction

When structured data (JSON-LD, microdata, OG tags) and CSS heuristics fail,
this module sends cleaned/truncated HTML to an LLM and asks it to extract
products like a human would.

This makes Scrape Buddy truly universal — the LLM reads any layout.

Cost controls:
  - HTML is cleaned (scripts, styles, nav, footer removed) before sending
  - Truncated to MAX_HTML_CHARS (~50k chars ≈ ~12k tokens)
  - Uses gpt-4o-mini by default (cheap, fast, good at structured extraction)
  - Response limited to max_tokens=4096

Public API:
  extract(html, url) -> list[dict]
    Returns a list of product dicts. Empty list if extraction fails.

Environment:
  Requires OPENAI_API_KEY in environment (already used by app.py).
"""

import json
import logging
import os
import re
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

# ── Configuration ───────────────────────────────────────────────────────────────

# Maximum characters of cleaned HTML to send to the LLM.
# ~50k chars ≈ ~12k tokens with gpt-4o-mini tokenizer.
MAX_HTML_CHARS = 50_000

# Model to use — gpt-4o-mini is cheap ($0.15/1M input, $0.60/1M output)
# and excellent at structured extraction tasks.
LLM_MODEL = "gpt-4o-mini"

# Maximum tokens for the LLM response
MAX_RESPONSE_TOKENS = 4096

# ── HTML cleaning ───────────────────────────────────────────────────────────────

# Tags to remove entirely (content and all)
_REMOVE_TAGS = [
    "script", "style", "noscript", "svg", "iframe", "video", "audio",
    "canvas", "map", "object", "embed",
]

# Tags that are site chrome, not product content
_CHROME_TAGS = ["nav", "header", "footer", "aside", "menu"]

# Attributes to strip (reduce token waste on styling/tracking)
_STRIP_ATTRS = [
    "style", "class", "id", "data-gtm", "data-analytics", "data-track",
    "data-testid", "data-cy", "data-test", "aria-label", "aria-describedby",
    "onclick", "onload", "onerror",
]


def _clean_html(html: str) -> str:
    """
    Clean HTML to reduce token count while preserving product-relevant content.

    Removes: scripts, styles, SVGs, navigation chrome, tracking attributes.
    Preserves: product cards, prices, images, links, text content.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    # Remove non-content tags
    for tag_name in _REMOVE_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Remove chrome/navigation elements
    for tag_name in _CHROME_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Strip noisy attributes from all remaining elements
    for tag in soup.find_all(True):
        for attr in _STRIP_ATTRS:
            if attr in tag.attrs:
                del tag.attrs[attr]

    # Remove HTML comments
    from bs4 import Comment
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    # Collapse whitespace
    text = str(soup)
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)

    return text.strip()


# ── LLM extraction ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a product data extraction assistant. You will receive HTML from an e-commerce or wholesale product listing page. Extract ALL products visible on the page.

Return a JSON array of objects. Each object must have these fields (use empty string "" if not found):
- "product_name": the product title/description
- "price": the price including currency symbol (e.g. "$9.99")
- "sku": the SKU, item number, part number, or model number
- "image_url": the product image URL (absolute or relative)
- "product_url": the link to the product detail page (absolute or relative)
- "brand": the brand or manufacturer name
- "upc": the UPC/EAN/GTIN barcode number if visible

Rules:
- Extract EVERY product on the page, not just a sample
- Return ONLY the JSON array, no other text or markdown
- If no products are found, return an empty array: []
- For prices, include the currency symbol
- For URLs, preserve the exact href value from the HTML
- Do not invent or guess data — only extract what's visible"""


def _call_llm(cleaned_html: str) -> str | None:
    """
    Send cleaned HTML to the LLM and get the extraction response.
    Returns the response text, or None if the call fails.
    """
    try:
        from openai import OpenAI
    except ImportError:
        logger.error("[Tier2] openai package not installed")
        return None

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.error("[Tier2] OPENAI_API_KEY not set")
        return None

    client = OpenAI(api_key=api_key)

    # Truncate HTML to stay within token limits
    if len(cleaned_html) > MAX_HTML_CHARS:
        logger.info(
            f"[Tier2] Truncating HTML from {len(cleaned_html)} to {MAX_HTML_CHARS} chars"
        )
        cleaned_html = cleaned_html[:MAX_HTML_CHARS]

    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Extract all products from this HTML page.\n\n"
                        f"<html>\n{cleaned_html}\n</html>"
                    ),
                },
            ],
            max_tokens=MAX_RESPONSE_TOKENS,
            temperature=0,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"[Tier2] LLM API call failed: {e}")
        return None


def _parse_llm_response(response_text: str, base_url: str) -> list[dict]:
    """
    Parse the LLM's JSON response into a list of product dicts.
    Handles common LLM output quirks (markdown fences, trailing commas).
    """
    if not response_text:
        return []

    # Strip markdown code fences if present
    text = response_text.strip()
    if text.startswith("```"):
        # Remove opening fence (```json or ```)
        text = re.sub(r"^```\w*\n?", "", text)
        # Remove closing fence
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()

    # Try to find a JSON array in the response
    # Sometimes the LLM wraps it in an object like {"products": [...]}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to extract JSON array from the text
        match = re.search(r"\[[\s\S]*\]", text)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                logger.warning("[Tier2] Could not parse LLM response as JSON")
                return []
        else:
            logger.warning("[Tier2] No JSON array found in LLM response")
            return []

    # Handle {"products": [...]} wrapper
    if isinstance(data, dict):
        for key in ["products", "items", "results", "data"]:
            if key in data and isinstance(data[key], list):
                data = data[key]
                break
        else:
            return []

    if not isinstance(data, list):
        return []

    # Normalize each product
    products = []
    for item in data:
        if not isinstance(item, dict):
            continue

        product = {}

        # Map LLM field names to our standard fields
        name = (
            item.get("product_name", "")
            or item.get("name", "")
            or item.get("title", "")
        )
        if not name:
            continue
        product["product_name"] = str(name).strip()

        # Price
        price = item.get("price", "")
        if price and price != "":
            product["price"] = str(price).strip()

        # SKU
        sku = (
            item.get("sku", "")
            or item.get("item_number", "")
            or item.get("part_number", "")
            or item.get("model", "")
        )
        if sku and sku != "":
            product["sku"] = str(sku).strip()

        # Image URL — resolve relative URLs
        img = item.get("image_url", "") or item.get("image", "")
        if img and img != "":
            product["image_url"] = urljoin(base_url, str(img).strip())

        # Product URL — resolve relative URLs
        purl = item.get("product_url", "") or item.get("url", "") or item.get("link", "")
        if purl and purl != "":
            product["product_url"] = urljoin(base_url, str(purl).strip())

        # Brand
        brand = item.get("brand", "") or item.get("manufacturer", "")
        if brand and brand != "":
            product["brand"] = str(brand).strip()

        # UPC
        upc = item.get("upc", "") or item.get("gtin", "") or item.get("ean", "")
        if upc and upc != "":
            product["upc"] = str(upc).strip()

        product["_source"] = "llm"
        products.append(product)

    return products


# ── Public API ──────────────────────────────────────────────────────────────────

def extract(html: str, url: str) -> list[dict]:
    """
    Tier 2: Extract products using LLM-assisted analysis.

    Cleans and truncates the HTML, sends it to gpt-4o-mini, and parses
    the structured JSON response.

    Returns:
        List of product dicts. Empty list if extraction fails or finds nothing.
    """
    logger.info(f"[Tier2] Starting LLM extraction for {url}")

    # Clean HTML to reduce tokens
    cleaned = _clean_html(html)
    logger.info(
        f"[Tier2] Cleaned HTML: {len(html)} -> {len(cleaned)} chars "
        f"({100 - len(cleaned) * 100 // max(len(html), 1)}% reduction)"
    )

    if len(cleaned) < 100:
        logger.warning("[Tier2] Cleaned HTML too short — likely empty page")
        return []

    # Call LLM
    response_text = _call_llm(cleaned)
    if not response_text:
        logger.warning("[Tier2] LLM returned no response")
        return []

    # Parse response
    products = _parse_llm_response(response_text, url)
    logger.info(f"[Tier2] LLM extracted {len(products)} product(s)")

    return products
