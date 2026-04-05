"""
UPC provider that searches the web for UPC codes.
Uses targeted Google searches like "{product name} {brand} UPC barcode"
to find UPC codes from various retail and database sites.
"""
import re
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from upc_base import UpcLookupProvider

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}
_TIMEOUT = 10


class WebSearchUPCProvider(UpcLookupProvider):
    @property
    def name(self) -> str:
        return "web_search"

    def lookup(self, brand: str = "", product_name: str = "", sku: str = "",
               pack_size: str = "", case_pack: str = "") -> dict:
        query_parts = [p.strip() for p in [brand, product_name, pack_size] if p and p.strip()]
        if not query_parts:
            return {"status": "insufficient_match", "upc": ""}

        query = " ".join(query_parts[:2]) + " UPC barcode"

        try:
            url = f"https://www.google.com/search?q={quote_plus(query)}"
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            text = resp.text
        except Exception as e:
            return {"status": "provider_unavailable", "provider_status": str(e), "upc": ""}

        # Extract UPC-12 codes from search results
        upc_pattern = r'\b(\d{12})\b'
        matches = re.findall(upc_pattern, text)

        # Filter to valid UPC-12 (check digit validation)
        for candidate in matches:
            if _validate_upc12(candidate):
                return {
                    "upc": candidate,
                    "confidence": "medium",
                    "color": "yellow",
                    "reason": f"UPC found via web search for '{' '.join(query_parts[:2])}'",
                    "status": "matched",
                }

        return {"status": "no_match_found", "upc": ""}


def _validate_upc12(code: str) -> bool:
    """Validate UPC-12 check digit."""
    if len(code) != 12 or not code.isdigit():
        return False
    digits = [int(d) for d in code]
    total = sum(digits[i] * (3 if i % 2 else 1) for i in range(11))
    check = (10 - (total % 10)) % 10
    return check == digits[11]
