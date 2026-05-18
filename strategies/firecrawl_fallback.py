"""
Firecrawl hosted extraction strategy.

When FIRECRAWL_API_KEY is configured, Scraper Buddy can use Firecrawl as a
hosted rendering pass. By default we request markdown and links, then parse
product cards locally; Firecrawl's schema JSON extraction can be enabled with
SCRAPEBUDDY_FIRECRAWL_JSON=1, but it is slower and can time out on large
catalog pages.
"""

from __future__ import annotations

import logging
import json
import os
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from .product_quality import normalize_products

logger = logging.getLogger(__name__)

ID = 40
NAME = "Firecrawl Rendered Extraction"

API_URL = "https://api.firecrawl.dev/v2/scrape"


PRODUCT_SCHEMA = {
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
                },
                "required": ["product_name"],
            },
        }
    },
    "required": ["products"],
}

PROMPT = (
    "Extract every visible product from this wholesale, distributor, retailer, "
    "or online catalog page. For each product, capture product_name, brand, sku, "
    "UPC, EAN, GTIN, price, pack_size, case_pack, image_url, and product_url. "
    "Only return real products, not category headings, navigation, ads, filters, "
    "or result-count summary text. Preserve barcode digits exactly when shown."
)


AUTH_PROMPT = (
    PROMPT
    + " This page may be inside an authenticated supplier account. Use the "
    "current authenticated session state and extract the products visible to "
    "that logged-in account."
)


_PRODUCT_URL_HINTS = (
    "/product/",
    "/products/",
    "/item/",
    "/items/",
    "/detail/",
    "/details/",
    "/pd/",
    "/p/",
    "/sku/",
    "/prod/",
    "/catalog/product/",
    "/shop/product/",
    "/gp/product/",
    "/dp/",
)

_NON_PRODUCT_URL_FRAGMENTS = (
    "/account",
    "/about",
    "/basket",
    "/blog",
    "/brand",
    "/brands",
    "/cart",
    "/category",
    "/categories",
    "/checkout",
    "/collection",
    "/collections",
    "/contact",
    "/customer",
    "/faq",
    "/help",
    "/login",
    "/logout",
    "/my-account",
    "/privacy",
    "/register",
    "/registration",
    "/search",
    "/signin",
    "/signup",
    "/terms",
    "/wishlist",
)

_MARKDOWN_CARD_RE = re.compile(
    r"(?ms)^\s*\d+\.\s+\[!\[(?P<image_alt>[^\]]*)\]\((?P<image_url>[^)]+)\)\]\((?P<url>[^)]+)\)"
    r"(?P<body>.*?)(?=^\s*\d+\.\s+\[!\[|\Z)"
)

_MARKDOWN_HEADING_LINK_RE = re.compile(
    r"(?m)^\s*#{1,4}\s+\[(?P<name>[^\]]+)\]\((?P<url>[^)]+)\)"
)


def enabled() -> bool:
    return bool(api_key()) and os.environ.get("SCRAPEBUDDY_FIRECRAWL_DISABLED") != "1"


def api_key() -> str:
    return os.environ.get("FIRECRAWL_API_KEY", "").strip()


def _timeout_seconds() -> int:
    try:
        value = int(os.environ.get("SCRAPEBUDDY_FIRECRAWL_TIMEOUT", "120"))
        return max(15, value)
    except Exception:
        return 120


def _json_extraction_enabled() -> bool:
    return os.environ.get("SCRAPEBUDDY_FIRECRAWL_JSON", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
    }


def _walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item)


def _extract_link_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("href", "url", "link"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return ""


def _normalize_url_for_key(value: str) -> str:
    parsed = urlparse(value or "")
    path = re.sub(r"/+$", "", parsed.path or "/")
    return parsed._replace(path=path, fragment="").geturl()


def _name_from_url(value: str) -> str:
    path = urlparse(value or "").path
    slug = re.sub(r"\.(?:html?|aspx?|php)$", "", path.rstrip("/").split("/")[-1], flags=re.I)
    slug = re.sub(r"[-_]+", " ", slug).strip()
    if not slug or slug.lower() in {"product", "products", "item", "items", "detail", "details"}:
        return ""
    return slug.title()


def _clean_markdown_text(value: str) -> str:
    value = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", value or "")
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"[*_`#>]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _same_host(value: str, base_url: str) -> bool:
    parsed = urlparse(value or "")
    base = urlparse(base_url or "")
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and (
        not base.netloc or parsed.netloc.lower() == base.netloc.lower()
    )


def _allowed_markdown_product_url(value: str, base_url: str) -> bool:
    normalized = _normalize_url_for_key(value).lower()
    if not _same_host(value, base_url):
        return False
    if normalized == _normalize_url_for_key(base_url).lower():
        return False
    return not any(fragment in normalized for fragment in _NON_PRODUCT_URL_FRAGMENTS)


def _extract_sku_from_card_body(value: str) -> str:
    for raw_line in (value or "").splitlines():
        line = _clean_markdown_text(raw_line)
        if not line or line.lower() in {"add to wish list", "in cart", "add to cart"}:
            continue
        if re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{1,30}", line, re.I):
            return line
    return ""


def _extract_price_from_text(value: str) -> str:
    match = re.search(r"(?<!\w)\$\s?\d[\d,]*(?:\.\d{2})?", value or "")
    return re.sub(r"\s+", "", match.group(0)) if match else ""


def _looks_like_product_url(value: str, base_url: str) -> bool:
    parsed = urlparse(value or "")
    base = urlparse(base_url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if base.netloc and parsed.netloc.lower() != base.netloc.lower():
        return False

    normalized = _normalize_url_for_key(value).lower()
    if normalized == _normalize_url_for_key(base_url).lower():
        return False
    if any(fragment in normalized for fragment in _NON_PRODUCT_URL_FRAGMENTS):
        return False
    return any(hint in normalized for hint in _PRODUCT_URL_HINTS)


def _product_link_candidates_from_response(payload: dict, base_url: str) -> list[dict]:
    data = payload.get("data") if isinstance(payload, dict) else None
    links = data.get("links") if isinstance(data, dict) else None
    if not isinstance(links, list):
        return []

    products: list[dict] = []
    seen: set[str] = set()
    for raw_link in links:
        href = _extract_link_value(raw_link).strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urljoin(base_url, href)
        if not _looks_like_product_url(absolute, base_url):
            continue
        key = _normalize_url_for_key(absolute)
        if key in seen:
            continue
        seen.add(key)
        products.append(
            {
                "product_name": _name_from_url(absolute),
                "product_url": absolute,
            }
        )
    return products


def _dedupe_merge_products(products: list[dict]) -> list[dict]:
    merged: list[dict] = []
    index: dict[str, int] = {}
    for product in products:
        name = str(product.get("product_name") or "").strip()
        url = str(product.get("product_url") or "").strip()
        sku = str(product.get("sku") or "").strip()
        price = str(product.get("price") or "").strip()
        key = _normalize_url_for_key(url) if url else sku or f"{name.lower()}|{price}"
        if not key:
            continue
        if key in index:
            existing = merged[index[key]]
            for field, value in product.items():
                if value and not existing.get(field):
                    existing[field] = value
            continue
        index[key] = len(merged)
        merged.append(product)
    return merged


def _merge_product_lists(primary: list[dict], supplemental: list[dict]) -> list[dict]:
    return _dedupe_merge_products((primary or []) + (supplemental or []))


def _extract_products_from_markdown(payload: dict, base_url: str) -> list[dict]:
    data = payload.get("data") if isinstance(payload, dict) else None
    markdown = data.get("markdown") if isinstance(data, dict) else None
    if not isinstance(markdown, str) or not markdown.strip():
        return []

    products: list[dict] = []
    seen_urls: set[str] = set()
    heading_names_by_url: dict[str, str] = {}
    for heading in _MARKDOWN_HEADING_LINK_RE.finditer(markdown):
        absolute = urljoin(base_url, heading.group("url").strip())
        heading_names_by_url[_normalize_url_for_key(absolute)] = _clean_markdown_text(
            heading.group("name")
        )

    for match in _MARKDOWN_CARD_RE.finditer(markdown):
        absolute = urljoin(base_url, match.group("url").strip())
        if not _allowed_markdown_product_url(absolute, base_url):
            continue
        key = _normalize_url_for_key(absolute)
        if key in seen_urls:
            continue
        seen_urls.add(key)
        body = match.group("body") or ""
        name = heading_names_by_url.get(key) or _clean_markdown_text(match.group("image_alt"))
        if not name:
            name = _name_from_url(absolute)
        if not name or re.search(r"^\d[\d,]*\s+(?:results?|items?|products?)\s+for\b", name, re.I):
            continue
        products.append(
            {
                "product_name": name,
                "sku": _extract_sku_from_card_body(body),
                "price": _extract_price_from_text(body),
                "image_url": urljoin(base_url, match.group("image_url").strip()),
                "product_url": absolute,
            }
        )

    return products


def _markdown_from_response(payload: dict) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    markdown = data.get("markdown") if isinstance(data, dict) else ""
    return markdown if isinstance(markdown, str) else ""


def _openai_mode() -> str:
    return os.environ.get("SCRAPEBUDDY_FIRECRAWL_OPENAI", "auto").strip().lower()


def _should_try_openai(payload: dict, products: list[dict]) -> bool:
    mode = _openai_mode()
    if mode in {"0", "off", "disabled", "none", "never"}:
        return False
    if not _markdown_from_response(payload):
        return False
    try:
        from . import llm_extractor
        if not llm_extractor.enabled():
            return False
    except Exception:
        return False
    if mode in {"1", "true", "yes", "on", "always"}:
        return True
    if not products:
        return True
    useful_rows = [
        p for p in products
        if p.get("product_name") and (p.get("product_url") or p.get("sku") or p.get("price"))
    ]
    return len(useful_rows) < 3


def _extract_products_with_openai(payload: dict, base_url: str) -> list[dict]:
    try:
        from . import llm_extractor
        markdown = _markdown_from_response(payload)
        if not markdown:
            return []
        return llm_extractor.extract_from_text(
            markdown,
            base_url,
            content_type="firecrawl_markdown",
        )
    except Exception as e:
        logger.warning(f"[Firecrawl] OpenAI markdown extraction failed: {e}")
        return []


def _extract_products_from_response(payload: dict, base_url: str) -> list[dict]:
    candidates: list[dict] = []

    for obj in _walk_json(payload):
        products = obj.get("products")
        if isinstance(products, list):
            candidates.extend(p for p in products if isinstance(p, dict))

    if not candidates:
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            json_data = data.get("json") or data.get("extract") or data.get("structuredData")
            if isinstance(json_data, dict):
                products = json_data.get("products")
                if isinstance(products, list):
                    candidates.extend(p for p in products if isinstance(p, dict))

    cleaned: list[dict] = []
    seen: set[str] = set()
    for item in candidates:
        product = {
            "product_name": item.get("product_name") or item.get("name") or item.get("title") or "",
            "brand": item.get("brand") or "",
            "sku": item.get("sku") or item.get("item_number") or item.get("itemNumber") or "",
            "upc": item.get("upc") or "",
            "ean": item.get("ean") or "",
            "gtin": item.get("gtin") or "",
            "price": item.get("price") or "",
            "pack_size": item.get("pack_size") or item.get("packSize") or "",
            "case_pack": item.get("case_pack") or item.get("casePack") or "",
            "image_url": item.get("image_url") or item.get("imageUrl") or "",
            "product_url": item.get("product_url") or item.get("productUrl") or item.get("url") or "",
        }
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
        cleaned.append(product)

    cleaned = _dedupe_merge_products(cleaned + _extract_products_from_markdown(payload, base_url))

    if not cleaned:
        cleaned.extend(_product_link_candidates_from_response(payload, base_url))

    return normalize_products(cleaned)


def _domain_matches(cookie_domain: str, host: str) -> bool:
    cookie_domain = (cookie_domain or "").lstrip(".").lower()
    host = (host or "").lower()
    return bool(cookie_domain and host) and (
        host == cookie_domain or host.endswith("." + cookie_domain)
    )


def _cookie_header_from_state_file(state_file: str, url: str) -> str:
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        logger.warning(f"[Firecrawl] Could not read auth state file: {e}")
        return ""

    host = urlparse(url).hostname or ""
    cookie_parts: list[str] = []
    for cookie in state.get("cookies", []) if isinstance(state, dict) else []:
        domain = cookie.get("domain") or ""
        name = cookie.get("name") or ""
        value = cookie.get("value") or ""
        if not name or value is None:
            continue
        if _domain_matches(domain, host):
            cookie_parts.append(f"{name}={value}")
    return "; ".join(cookie_parts)


def _request_body(url: str, *, authenticated: bool = False) -> dict:
    # Firecrawl's markdown and links formats are much more reliable for large
    # catalog pages than schema JSON extraction. JSON stays opt-in because it
    # can time out on product grids that render successfully.
    formats: list[Any] = ["markdown", "links"]
    if _json_extraction_enabled():
        formats.append(
            {
                "type": "json",
                "schema": PRODUCT_SCHEMA,
                "prompt": AUTH_PROMPT if authenticated else PROMPT,
            }
        )

    return {
        "url": url,
        "formats": formats,
        "onlyMainContent": False,
        "maxAge": 0,
        "waitFor": 5000,
        "timeout": _timeout_seconds() * 1000,
        "removeBase64Images": True,
        "blockAds": True,
        "proxy": "auto",
        "actions": [
            {"type": "wait", "milliseconds": 2000},
            {"type": "scroll"},
        ],
    }


def scrape_payload(
    url: str,
    *,
    headers_override: dict | None = None,
    authenticated: bool = False,
) -> dict | None:
    key = api_key()
    if not key:
        logger.info("[Firecrawl] FIRECRAWL_API_KEY not set; skipping")
        return None

    body = _request_body(url, authenticated=authenticated)
    if headers_override:
        body["headers"] = headers_override

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    try:
        logger.info(f"[Firecrawl] Scraping with hosted JSON extraction: {url}")
        response = requests.post(
            API_URL,
            json=body,
            headers=headers,
            timeout=_timeout_seconds(),
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.warning(f"[Firecrawl] Request failed: {e}")
        return None


def extract_products_from_payload(payload: dict, url: str) -> list[dict]:
    products = _extract_products_from_response(payload, url)
    if _should_try_openai(payload, products):
        openai_products = _extract_products_with_openai(payload, url)
        if openai_products:
            before = len(products)
            products = _merge_product_lists(products, openai_products)
            logger.info(
                f"[Firecrawl] OpenAI markdown extraction added "
                f"{len(products) - before} product(s)"
            )
    return products


def run(url: str, *, headers_override: dict | None = None, authenticated: bool = False) -> list[dict]:
    payload = scrape_payload(
        url,
        headers_override=headers_override,
        authenticated=authenticated,
    )
    if not payload:
        return []

    products = extract_products_from_payload(payload, url)
    warning = (payload.get("data") or {}).get("warning") if isinstance(payload, dict) else ""
    if warning:
        logger.info(f"[Firecrawl] API warning: {warning}")
    logger.info(f"[Firecrawl] Extracted {len(products)} product(s)")
    return products


def run_authenticated(url: str, state_file: str) -> list[dict]:
    cookie_header = _cookie_header_from_state_file(state_file, url)
    if not cookie_header:
        logger.info("[Firecrawl] No matching authenticated cookies found; skipping auth scrape")
        return []
    headers_override = {
        "Cookie": cookie_header,
        "Referer": url,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    logger.info("[Firecrawl] Trying authenticated hosted extraction with session cookies")
    return run(url, headers_override=headers_override, authenticated=True)
