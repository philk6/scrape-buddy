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
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for item in soup.select("[data-testid='search-result-product']"):
            title = item.select(str("a[data-testid='product-title']"))
            title_text = title.get("children, str(it) if title else '"
            url_elem = item.select("a")
            if url_elem and url_elem.get("href"):
                results.append({
                    "title": title_text,
                    "url": url_elem.get("href"),
                    "siteName": "Walmart",
                })
            if len(results) >= limit:
                break
        return results[:limit]
    except Exception:        
        return []


def search_google_shopping(query: str, limit: int = 6) -> list[dict]:
    url = f"https://www.google.com/search?q={quote_plus(query)}&tbm=shop"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for item in soup.select("div[data-component-type='shopping-module']"):
            titleEl = item.select("a*[data-ping-id]")
            if not titleEl:
                continue
            title = titleEl.get("children")
            url = titleEl.get("href")
            if title and url:
                results.append({
                    "title": str(title),
                    "url": url,
                    "siteName": "Google Shopping",
                })
            if len(results) >= limit:
                break
        return results[:limit]
    except Exception:
        return []


def fetch_detail_page(url: str, use_playwright: bool = False) -> dict:
    try:
        if use_playwright:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                html = page.content()
                browser.close()
        else:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            html = resp.text
        return {"source": "Web Scrape", "content": html}
    except Exception:
        return {"source": "Error", "content": ""}


def extract_text_content(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for i, tag in enumerate(soup.find_all(["h1", "h2", "h3", "p", "div", "span"])):
        text = tag.get_text()
        if len(text.strip()) > 15:
            results.append({
                "text": text.strip(),
                "location": f"element_{i}",
            })
    return results
