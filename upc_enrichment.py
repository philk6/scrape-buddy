"""
upc_enrichment.py — UPC enrichment + product identity resolution pipeline
"""

import logging
import os
import time

from identity_resolution import enrich_identity
from identity_strengthening import strengthen_identity
from provider_routing import classify_row, route_providers, row_strength
from retail_resolution import resolve_with_retail_candidates
from upc_base import UpcLookupProvider

logger = logging.getLogger(__name__)


_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1, "": 0}


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
        return value if value > 0 else default
    except Exception:
        return default


def _confidence_ok(confidence: str, min_confidence: str) -> bool:
    return _CONFIDENCE_RANK.get(confidence, 0) >= _CONFIDENCE_RANK.get(min_confidence, 3)


def enrich_products_upc(products: list, providers: list = None, min_confidence: str = "medium") -> list:
    if providers is None:
        providers = []

    strengthen_identity(products)
    enrich_identity(products)

    enriched_count = 0
    missing_count = 0
    already_count = 0
    provider_unavailable_count = 0
    insufficient_data_count = 0
    skipped_budget_count = 0
    max_seconds = _positive_int_env("SCRAPEBUDDY_UPC_ENRICHMENT_MAX_SECONDS", 45)
    max_rows = _positive_int_env("SCRAPEBUDDY_UPC_ENRICHMENT_MAX_ROWS", 20)
    started_at = time.monotonic()
    external_rows_attempted = 0

    ordered_products = sorted(products, key=row_strength, reverse=True)

    for product in ordered_products:
        resolved_upc12 = (product.get("upc_12") or "").strip()
        existing_upc = (product.get("upc") or "").strip()

        if resolved_upc12:
            product["upc"] = resolved_upc12
            product["upc_source"] = product.get("resolution_source", "supplier_page") or "supplier_page"
            product["upc_enriched"] = "0"
            product["upc_match_confidence"] = product.get("resolution_confidence", "high") or "high"
            product["upc_confidence_color"] = product.get("resolution_color", "green") or "green"
            product["upc_match_reason"] = product.get("resolution_reason", "supplier provided a valid UPC-12") or "supplier provided a valid UPC-12"
            product["missing_upc"] = "0"
            already_count += 1
            continue

        if existing_upc:
            product["upc_source"] = "supplier_page"
            product["upc_enriched"] = "0"
            product["upc_match_confidence"] = "high"
            product["upc_confidence_color"] = "green"
            product["upc_match_reason"] = "supplier provided a trusted identifier"
            product["missing_upc"] = "0"
            already_count += 1
            continue

        row_group = classify_row(product)
        product["resolution_row_group"] = row_group
        active_providers = route_providers(product, providers)

        if row_group == "too_weak_to_query" or not active_providers:
            product["missing_upc"] = "1"
            product["resolution_status"] = "insufficient_product_data"
            product["resolution_reason"] = "insufficient product identity data for external UPC resolution"
            insufficient_data_count += 1
            continue

        if (
            external_rows_attempted >= max_rows
            or time.monotonic() - started_at > max_seconds
        ):
            product["upc_enriched"] = "0"
            product["missing_upc"] = "1"
            product["resolution_status"] = "external_lookup_skipped_budget"
            product["resolution_reason"] = (
                "external UPC lookup skipped because the run reached its "
                "configured enrichment budget"
            )
            skipped_budget_count += 1
            continue
        external_rows_attempted += 1

        candidates = []
        provider_failures = []
        saw_no_match = False

        retail_result = {}
        try:
            retail_result = resolve_with_retail_candidates(product)
        except Exception as e:
            logger.warning(f"[UpcEnrichment] Retail resolution error for '{product.get('product_name', '')[:40]}': {e}")

        if retail_result.get('upc') and _confidence_ok(retail_result.get('confidence', ''), min_confidence):
            candidates.append((retail_result.get('source', 'retail_match'), retail_result))
        else:
            for provider in active_providers:
                try:
                    result = provider.lookup(
                        brand=product.get("brand", ""),
                        product_name=product.get("product_name", ""),
                        sku=product.get("sku", ""),
                        pack_size=product.get("pack_size", "") or product.get("unit_size", ""),
                        case_pack=product.get("case_pack", ""),
                    )
                    status = result.get("status", "") if result else ""
                    if status == "provider_unavailable":
                        provider_failures.append(f"{provider.name}:{result.get('provider_status', '')}")
                        continue
                    if status in {"no_match_found", "insufficient_match", "no_results", ""} and (not result or not result.get("upc")):
                        saw_no_match = True
                        continue
                    if not result or not result.get("upc"):
                        continue
                    confidence = result.get("confidence", "")
                    if not _confidence_ok(confidence, min_confidence):
                        saw_no_match = True
                        continue
                    candidates.append((provider.name, result))
                except Exception as e:
                    provider_failures.append(f"{provider.name}:exception")
                    logger.warning(f"[UpcEnrichment] Provider {provider.name} error for '{product.get('product_name', '')[:40]}': {e}")

        if candidates:
            provider_name, best = max(candidates, key=lambda x: _CONFIDENCE_RANK.get(x[1].get("confidence", ""), 0))
            product["upc"] = str(best["upc"]).strip()
            product["upc_12"] = product["upc"]
            product["upc_source"] = provider_name
            product["upc_enriched"] = "1"
            product["upc_match_confidence"] = best.get("confidence", "")
            product["upc_confidence_color"] = best.get("color", "")
            product["upc_match_reason"] = best.get("reason", "")
            product["missing_upc"] = "0"
            product["resolution_source"] = provider_name
            product["resolution_method"] = "external_lookup"
            product["resolution_confidence"] = best.get("confidence", "")
            product["resolution_color"] = best.get("color", "")
            product["resolution_reason"] = best.get("reason", "")
            product["resolution_status"] = "externally_resolved"
            enriched_count += 1
        else:
            product["upc_enriched"] = "0"
            product["missing_upc"] = "1"
            if provider_failures and not saw_no_match:
                product["resolution_status"] = "provider_unavailable"
                product["resolution_reason"] = "; ".join(provider_failures)
                provider_unavailable_count += 1
            elif provider_failures and saw_no_match:
                product["resolution_status"] = "provider_partially_unavailable"
                product["resolution_reason"] = "; ".join(provider_failures)
                provider_unavailable_count += 1
            else:
                product["resolution_status"] = "no_match_found"
                product["resolution_reason"] = "no provider candidate met the confidence threshold"
                missing_count += 1

    logger.info(
        f"[UpcEnrichment] Complete - {already_count} supplier/native UPCs | {enriched_count} externally resolved | {provider_unavailable_count} provider unavailable | {insufficient_data_count} insufficient data | {missing_count} no match"
        f" | {skipped_budget_count} skipped by budget"
    )
    return products
