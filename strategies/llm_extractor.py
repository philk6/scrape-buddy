"""
OpenAI-assisted product extraction.

This tier is the "human eyes" layer for Scraper Buddy. It does not browse a
site by itself; it reads rendered HTML or Firecrawl markdown that another layer
already fetched, then returns structured product rows.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from urllib.parse import urljoin

from .product_quality import normalize_products

logger = logging.getLogger(__name__)

MAX_CONTENT_CHARS = 90_000
MAX_RESPONSE_TOKENS = 16_000
DEFAULT_MODEL = "gpt-5.4-mini"

PRODUCT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "product_name": {"type": "string"},
                    "brand": {"type": "string"},
                    "sku": {"type": "string"},
                    "upc": {"type": "string"},
                    "ean": {"type": "string"},
                    "gtin": {"type": "string"},
                    "price": {"type": "string"},
                    "pack_size": {"type": "string"},
                    "case_pack": {"type": "string"},
                    "image_url": {"type": "string"},
                    "product_url": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["product_name"],
            },
        }
    },
    "required": ["products"],
}

SYSTEM_PROMPT = """You extract product data from e-commerce, wholesale, distributor, and catalog pages.

Return JSON with a top-level "products" array. Extract every real product visible in the provided content.

For each product, capture:
product_name, brand, sku, upc, ean, gtin, price, pack_size, case_pack, image_url, product_url, confidence.

Rules:
- Do not invent UPC, EAN, GTIN, price, SKU, or brand values.
- Preserve barcode digits exactly when shown.
- Use empty strings for missing fields.
- Ignore navigation, filters, category names, account links, wishlists, carts, ads, and result-count summaries.
- If content only shows product links or cards without barcodes, still return product rows with the fields that are visible.
- If no real products are present, return {"products": []}.
"""


def enabled() -> bool:
    return bool(api_key()) and os.environ.get("SCRAPEBUDDY_OPENAI_DISABLED") != "1"


def api_key() -> str:
    return os.environ.get("OPENAI_API_KEY", "").strip()


def model_name() -> str:
    return os.environ.get("SCRAPEBUDDY_OPENAI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
        return value if value > 0 else default
    except Exception:
        return default


def _clean_html(html: str) -> str:
    from bs4 import BeautifulSoup, Comment

    soup = BeautifulSoup(html or "", "html.parser")

    for tag_name in (
        "script",
        "style",
        "noscript",
        "svg",
        "iframe",
        "video",
        "audio",
        "canvas",
        "map",
        "object",
        "embed",
    ):
        for tag in soup.find_all(tag_name):
            tag.decompose()

    for tag_name in ("nav", "header", "footer", "aside", "menu"):
        for tag in soup.find_all(tag_name):
            tag.decompose()

    noisy_attrs = (
        "style",
        "class",
        "id",
        "data-gtm",
        "data-analytics",
        "data-track",
        "data-testid",
        "data-cy",
        "data-test",
        "aria-label",
        "aria-describedby",
        "onclick",
        "onload",
        "onerror",
    )
    for tag in soup.find_all(True):
        for attr in noisy_attrs:
            tag.attrs.pop(attr, None)

    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    text = str(soup)
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _trim_content(content: str) -> str:
    max_chars = _positive_int_env("SCRAPEBUDDY_LLM_MAX_CHARS", MAX_CONTENT_CHARS)
    if len(content) <= max_chars:
        return content
    logger.info(f"[OpenAI] Trimming extraction input from {len(content)} to {max_chars} chars")
    return content[:max_chars]


def _response_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text

    chunks: list[str] = []
    for output in getattr(response, "output", []) or []:
        for content in getattr(output, "content", []) or []:
            value = getattr(content, "text", None)
            if isinstance(value, str):
                chunks.append(value)
    return "\n".join(chunks).strip()


def _call_responses_api(content: str, url: str, content_type: str) -> str | None:
    try:
        from openai import OpenAI
    except ImportError:
        logger.error("[OpenAI] openai package is not installed")
        return None

    if not enabled():
        logger.info("[OpenAI] OPENAI_API_KEY not set or OpenAI extraction disabled")
        return None

    client = OpenAI(api_key=api_key())
    prompt = (
        f"Source URL: {url}\n"
        f"Content type: {content_type}\n\n"
        f"Extract products from this content:\n\n{content}"
    )

    try:
        response = client.responses.create(
            model=model_name(),
            instructions=SYSTEM_PROMPT,
            input=prompt,
            max_output_tokens=_positive_int_env(
                "SCRAPEBUDDY_OPENAI_MAX_OUTPUT_TOKENS",
                MAX_RESPONSE_TOKENS,
            ),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "product_extraction",
                    "schema": PRODUCT_RESPONSE_SCHEMA,
                    "strict": False,
                }
            },
            store=False,
            timeout=_positive_int_env("SCRAPEBUDDY_OPENAI_TIMEOUT", 120),
        )
        return _response_text(response)
    except Exception as e:
        logger.warning(f"[OpenAI] Responses API extraction failed: {e}")
        return _call_chat_completions(content, url, content_type)


def _call_chat_completions(content: str, url: str, content_type: str) -> str | None:
    try:
        from openai import OpenAI
    except ImportError:
        return None

    try:
        client = OpenAI(api_key=api_key())
        response = client.chat.completions.create(
            model=model_name(),
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Source URL: {url}\n"
                        f"Content type: {content_type}\n\n"
                        f"Extract products from this content:\n\n{content}"
                    ),
                },
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=_positive_int_env(
                "SCRAPEBUDDY_OPENAI_MAX_OUTPUT_TOKENS",
                MAX_RESPONSE_TOKENS,
            ),
            timeout=_positive_int_env("SCRAPEBUDDY_OPENAI_TIMEOUT", 120),
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        logger.warning(f"[OpenAI] Chat Completions extraction failed: {e}")
        return None


def _loads_json(text: str) -> Any:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        object_match = re.search(r"\{[\s\S]*\}", text)
        array_match = re.search(r"\[[\s\S]*\]", text)
        for match in (object_match, array_match):
            if not match:
                continue
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
    return None


def _products_from_parsed_json(value: Any) -> list[dict]:
    if isinstance(value, dict):
        for key in ("products", "items", "results", "data"):
            if isinstance(value.get(key), list):
                return [item for item in value[key] if isinstance(item, dict)]
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _parse_llm_response(response_text: str, base_url: str) -> list[dict]:
    parsed = _loads_json(response_text)
    raw_products = _products_from_parsed_json(parsed)
    if not raw_products:
        return []

    products: list[dict] = []
    seen: set[str] = set()
    for item in raw_products:
        product = {
            "product_name": item.get("product_name") or item.get("name") or item.get("title") or "",
            "brand": item.get("brand") or item.get("manufacturer") or "",
            "sku": (
                item.get("sku")
                or item.get("item_number")
                or item.get("itemNumber")
                or item.get("part_number")
                or item.get("model")
                or ""
            ),
            "upc": item.get("upc") or "",
            "ean": item.get("ean") or "",
            "gtin": item.get("gtin") or "",
            "price": item.get("price") or "",
            "pack_size": item.get("pack_size") or item.get("packSize") or "",
            "case_pack": item.get("case_pack") or item.get("casePack") or "",
            "image_url": item.get("image_url") or item.get("imageUrl") or item.get("image") or "",
            "product_url": item.get("product_url") or item.get("productUrl") or item.get("url") or "",
            "_source": "openai_llm",
        }

        for key in list(product.keys()):
            if isinstance(product[key], str):
                product[key] = re.sub(r"\s+", " ", product[key]).strip()

        if product["image_url"]:
            product["image_url"] = urljoin(base_url, str(product["image_url"]))
        if product["product_url"]:
            product["product_url"] = urljoin(base_url, str(product["product_url"]))

        name = str(product["product_name"] or "").strip()
        if not name or re.search(r"^\d[\d,]*\s+(?:results?|items?|products?)\s+for\b", name, re.I):
            continue

        key = product["product_url"] or product["sku"] or f"{name}|{product['price']}"
        if key in seen:
            continue
        seen.add(key)
        products.append(product)

    return normalize_products(products)


def extract_from_text(content: str, url: str, *, content_type: str = "text") -> list[dict]:
    logger.info(f"[OpenAI] Starting LLM extraction for {url} ({content_type})")
    content = _trim_content(content or "")
    if len(content.strip()) < 80:
        logger.info("[OpenAI] Content too short for extraction")
        return []

    response_text = _call_responses_api(content, url, content_type)
    if not response_text:
        return []

    products = _parse_llm_response(response_text, url)
    logger.info(f"[OpenAI] Extracted {len(products)} product(s)")
    return products


def extract(html: str, url: str) -> list[dict]:
    cleaned = _clean_html(html)
    logger.info(
        f"[OpenAI] Cleaned HTML: {len(html or '')} -> {len(cleaned)} chars"
    )
    return extract_from_text(cleaned, url, content_type="cleaned_html")
