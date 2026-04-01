import logging
import time

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
    YELLOWWTHRESHOLD,
    build_queries,
    score_candidate,
    score_to_confidence,
)

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
_HEADERS = {"User-Agent": "TheSyndicateUPCScraper/1.0 (upc-lookup-bot)"}
_TIMEOUT = 8
_PAGE_SIZE = 5
_RETRYABLE = {503}


class OpenFoodFactsProvider(UpcLookupProvider):
    @property
    def name(self) -> str:
        return "open_food_facts"

    def lookup(self, brand: str, product_name: str, sku: str, pack_size: str, case_pack: str) -> dict:
        available, status = provider_available(self.name)
        if not available:
            return {
                "status": "provider_unavailable",
                "provider_status": status,
                "reason": f"provider cooldown active: {status}",
            }

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

        logger.info(
            f"[OpenFoodFacts] score={best_score} upc={best_result.get('upc')} reason={best_result.get('reason')!r}"
        )
        return best_result

    def _search(self, query: str) -> dict:
        cached = get_cached_query(self.name, query)
        if cached:
            return cached

        for attempt in range(2):
            try:
                resp = requests.get(
                    _SEARCH_URL,
                    params={
                        "action": "process",
                        "search_terms": query,
                        "json": 1,
                        "page_size": _PAGE_SIZE,
                        "fields": "code,product_name,brands,quantity",
                    },
                    headers=_HEADERS,
                    timeout=_TIMEOUT,
                )
                if resp.status_code in _RETRYABLE:
                    if attempt == 0:
                        time.sleep(1.5)
                        continue
                    mark_provider_status(self.name, "service_unavailable", cooldown_seconds=300)
                    payload = {"status": "provider_unavailable", "provider_status": "503", "reason": "Open Food Facts returned 503"}
                    set_cached_query(self.name, query, payload)
                    return payload
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                if attempt == 0:
                    time.sleep(1.5)
                    continue
                logger.warning(f"[OpenFoodFacts] Search error for {query!r}: {e}")
                payload = {"status": "provider_unavailable", "provider_status": "exception", "reason": str(e)}
                set_cached_query(self.name, query, payload)
                return payload
        else:
            payload = {"status": "provider_unavailable", "provider_status": "unknown", "reason": "request loop exhausted"}
            set_cached_query(self.name, query, payload)
            return payload

        candidates = []
        for item in data.get("products", []):
            raw_code = str(item.get("code") or "").strip()
            if not raw_code.isdigit() or not (8 <= len(raw_code) <= 14):
                continue
            raw_brand = str(item.get("brands") or "").split(",")[0].strip()
            candidates.append({
                "upc": raw_code,
                "title": str(item.get("product_name") or "").strip(),
                "brand": raw_brand,
                "pack_size": str(item.get("quantity") or "").strip(),
            })

        payload = {"status": "ok" if candidates else "no_results", "candidates": candidates}
        set_cached_query(self.name, query, payload)
        return payload
