import logging
import os
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
    YELLOW_THRESHOLD,
    build_queries,
    score_candidate,
    score_to_confidence,
)

logger = logging.getLogger(__name__)

_TRIAL_URL = "https://api.upcitemdb.com/prod/trial/search"
_PAID_URL = "https://api.upcitemdb.com/prod/v1/search"
_TIMEOUT = 8
_MAX_ITEMS = 5


def _search_url() -> str:
    return _PAID_URL if os.environ.get("UPCITEMDB_USER_KEY") else _TRIAL_URL


def _headers() -> dict:
    key = os.environ.get("UPCITEMDB_USER_KEY")
    h = {"Accept": "application/json"}
    if key:
        h["user_key"] = key
    return h


class UPCItemDBProvider(UpcLookupProvider):
    @property
    def name(self) -> str:
        return "upcitemdb"

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
            f"[UPCItemDB] score={best_score} upc={best_result.get('upc')} reason={best_result.get('reason')!r}"
        )
        return best_result

    def _search(self, query: str) -> dict:
        cached = get_cached_query(self.name, query)
        if cached:
            return cached

        for attempt in range(2):
            try:
                resp = requests.get(
                    _search_url(),
                    params={"s": query, "type": "product"},
                    headers=_headers(),
                    timeout=_TIMEOUT,
                )
                if resp.status_code == 429:
                    mark_provider_status(self.name, "rate_limited", cooldown_seconds=1800)
                    payload = {"status": "provider_unavailable", "provider_status": "429", "reason": "UPCItemDB rate limit reached"}
                    set_cached_query(self.name, query, payload)
                    return payload
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                if attempt == 0:
                    time.sleep(1.5)
                    continue
                logger.warning(f"[UPCItemDB] Search error for {query!r}: {e}")
                payload = {"status": "provider_unavailable", "provider_status": "exception", "reason": str(e)}
                set_cached_query(self.name, query, payload)
                return payload
        else:
            payload = {"status": "provider_unavailable", "provider_status": "unknown", "reason": "request loop exhausted"}
            set_cached_query(self.name, query, payload)
            return payload

        candidates = []
        for item in data.get("items", [])[:_MAX_ITEMS]:
            upc = _extract_upc(item)
            if not upc:
                continue
            pack_hint = str(item.get("description") or "").strip()
            candidates.append({
                "upc": upc,
                "title": str(item.get("title") or "").strip(),
                "brand": str(item.get("brand") or "").strip(),
                "pack_size": pack_hint,
            })

        payload = {"status": "ok" if candidates else "no_results", "candidates": candidates}
        set_cached_query(self.name, query, payload)
        return payload


def _extract_upc(item: dict) -> str:
    raw = item.get("upc") or item.get("upcs") or ""
    candidates = [raw] if isinstance(raw, str) else list(raw)
    for u in candidates:
        u = str(u).strip()
        if u.isdigit() and 8 <= len(u) <= 14:
            return u
    return ""
