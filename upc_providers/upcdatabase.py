import logging
import time
from urllib.parse import quote_plus

import requests

from resolution_runtime import (
    get_cached_query,
    mark_provider_status,
    provider_available,
    set_cached_query,
)
from upc_base import UpcLookupProvider
from upc_providers.scorer import (
    GREEN_THRESHOLD,
    YELLOW_THRESHOLD,
    build_queries,
    score_candidate,
    score_to_confidence,
)

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.upcdatabase.org/search/"
_TIMEOUT = 10
_HEADERS = {
    "Authorization": "Bearer THISISALIVEDEMOAPIKEY19651D54X47",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0",
}
_RETRYABLE = {429, 503}


class UPCDatabaseProvider(UpcLookupProvider):
    @property
    def name(self) -> str:
        return "upcdatabase"

    def lookup(self, brand: str, product_name: str, sku: str, pack_size: str, case_pack: str) -> dict:
        available, status = provider_available(self.name)
        if not available:
            return {"status": "provider_unavailable", "provider_status": status, "reason": f"provider cooldown active: {status}"}

        product = {
            "brand": brand,
            "product_name": product_name,
            "sku": sku,
            "pack_size": pack_size,
            "case_pack": case_pack,
        }
        queries = build_queries(brand, product_name, pack_size, case_pack, sku)
        best_score = 0
        best_result = {}
        saw_no_result = False

        for query in queries:
            payload = self._search(query)
            status = payload.get("status", "")
            if status == "provider_unavailable":
                return payload
            candidates = payload.get("candidates", [])
            if not candidates:
                saw_no_result = True
            for candidate in candidates:
                score, reason = score_candidate(product, candidate)
                if score > best_score:
                    best_score = score
                    confidence, color = score_to_confidence(score)
                    best_result = {
                        "upc": candidate["upc"],
                        "confidence": confidence,
                        "color": color,
                        "reason": reason,
                        "status": "matched",
                    }
            if best_score >= GREEN_THRESHOLD:
                break

        if best_score < YELLOW_THRESHOLD:
            return {
                "status": "no_match_found" if saw_no_result else "insufficient_match",
                "reason": "no provider candidate met the confidence threshold",
            }
        return best_result

    def _search(self, query: str) -> dict:
        cached = get_cached_query(self.name, query)
        if cached:
            return cached

        url = f"{_SEARCH_URL}?query={quote_plus(query)}&page=1"
        for attempt in range(2):
            try:
                resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
                if resp.status_code in _RETRYABLE:
                    if attempt == 0:
                        time.sleep(1.5)
                        continue
                    status_name = "rate_limited" if resp.status_code == 429 else "service_unavailable"
                    mark_provider_status(self.name, status_name, cooldown_seconds=600)
                    payload = {"status": "provider_unavailable", "provider_status": str(resp.status_code), "reason": f"UPCDatabase returned {resp.status_code}"}
                    set_cached_query(self.name, query, payload)
                    return payload
                if resp.status_code in {400, 403}:
                    payload = {"status": "provider_unavailable", "provider_status": str(resp.status_code), "reason": f"UPCDatabase returned {resp.status_code}"}
                    set_cached_query(self.name, query, payload)
                    return payload
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                if attempt == 0:
                    time.sleep(1.5)
                    continue
                payload = {"status": "provider_unavailable", "provider_status": "exception", "reason": str(e)}
                set_cached_query(self.name, query, payload)
                return payload
        else:
            payload = {"status": "provider_unavailable", "provider_status": "unknown", "reason": "request loop exhausted"}
            set_cached_query(self.name, query, payload)
            return payload

        items = data.get("items", []) if isinstance(data, dict) else []
        candidates = []
        for item in items:
            upc = str(item.get("barcode") or "").strip()
            if not upc.isdigit() or not (8 <= len(upc) <= 14):
                continue
            candidates.append({
                "upc": upc,
                "title": str(item.get("title") or "").strip(),
                "brand": str(item.get("brand") or "").strip(),
                "pack_size": str(item.get("description") or "").strip(),
            })
        payload = {"status": "ok" if candidates else "no_results", "candidates": candidates}
        set_cached_query(self.name, query, payload)
        return payload
