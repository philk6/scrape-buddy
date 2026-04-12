import json
import re
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

USER_AGENT = "Mozilla/5.0"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}


def search_walmart_candidates(query: str, limit: int = 8) -> list[dict]:
    url = f"https://www.walmart.com/search?q={quote_plus(query)}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        resp.raise_for_status()
    except Exception:
        return []

    html = resp.text
    candidates = []
    for m in re.finditer(r'"canonicalUrl":"([^"]+/ip/[^"]+)"', html):
        path = m.group(1).replace('\\/', '/')
        if not path.startswith('http'):
            path = 'https://www.walmart.com' + path
        candidates.append({'url': path, 'source': 'walmart', 'title': '', 'description': ''})
    deduped, seen = [], set()
    for c in candidates:
        if c['url'] not in seen:
            seen.add(c['url'])
            deduped.append(c)
    return deduped[:limit]


def fetch_detail_page(url: str) -> dict:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.ok and len(resp.text) > 500:
            return {'url': url, 'source': 'http', 'content': resp.text}
    except Exception:
        pass

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until='domcontentloaded', timeout=30000)
            page.wait_for_timeout(2500)
            html = page.content()
            browser.close()
            return {'url': url, 'source': 'playwright', 'content': html}
    except Exception:
        return {'url': url, 'source': 'failed', 'content': ''}


def _extract_structured_texts(soup: BeautifulSoup) -> list[tuple[str, str]]:
    chunks = []

    for meta in soup.find_all('meta'):
        content = meta.get('content') or ''
        attrs = ' '.join(f'{k}={v}' for k, v in meta.attrs.items())
        if any(tok in attrs.lower() for tok in ['upc', 'gtin', 'barcode', 'product']) and content:
            chunks.append(('meta', f'{attrs} {content}'))

    for el in soup.find_all(attrs=True):
        attr_blob = ' '.join(f'{k}={v}' for k, v in el.attrs.items())
        if any(tok in attr_blob.lower() for tok in ['upc', 'gtin', 'barcode']):
            chunks.append(('data_attr', attr_blob))

    for script in soup.find_all('script', type='application/ld+json'):
        txt = script.get_text(' ', strip=True)
        if txt:
            chunks.append(('json_ld', txt))

    for script in soup.find_all('script'):
        txt = script.get_text(' ', strip=True)
        lowered = txt.lower()
        if any(tok in lowered for tok in ['upc', 'gtin', 'barcode', '__initial_state__', '__next_data__']):
            chunks.append(('script', txt))

    for table in soup.find_all(['table', 'dl', 'ul', 'div', 'section']):
        txt = table.get_text(' ', strip=True)
        lowered = txt.lower()
        if any(tok in lowered for tok in ['upc', 'gtin', 'barcode', 'item id', 'model number']):
            chunks.append(('dom_specs', txt))

    body_text = soup.get_text(' ', strip=True)
    if body_text:
        chunks.append(('body_text', body_text))

    return chunks


def search_google_shopping(query: str, limit: int = 8) -> list[dict]:
    """Search Google Shopping for product candidates with UPC information."""
    url = f"https://www.google.com/search?q={quote_plus(query)}+UPC&tbm=shop"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        resp.raise_for_status()
    except Exception:
        return []

    html = resp.text
    candidates = []
    # Extract product links from Google Shopping results
    soup = BeautifulSoup(html, 'html.parser')
    for link in soup.find_all('a', href=True):
        href = link.get('href', '')
        text = link.get_text(strip=True)
        if '/url?q=' in href and text and len(text) > 5:
            import urllib.parse
            parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            actual_url = parsed.get('q', [''])[0]
            if actual_url and any(site in actual_url for site in ['walmart.com', 'target.com', 'amazon.com', 'kroger.com', 'instacart.com']):
                candidates.append({
                    'url': actual_url,
                    'source': 'google_shopping',
                    'title': text,
                    'description': '',
                })

    deduped, seen = [], set()
    for c in candidates:
        if c['url'] not in seen:
            seen.add(c['url'])
            deduped.append(c)
    return deduped[:limit]


def extract_text_content(html: str) -> list[dict]:
    soup = BeautifulSoup(html or '', 'html.parser')
    chunks = _extract_structured_texts(soup)
    return [{'location': loc, 'text': text} for loc, text in chunks]
