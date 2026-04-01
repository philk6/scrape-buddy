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
    YELLOWWTHRESHOLD,
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
