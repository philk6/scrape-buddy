from typing import Any

from retail_match_provider import accept_retail_match, rank_retail_candidates
from retail_retrieval import extract_text_content, fetch_detail_page, search_walmart_candidates, search_google_shopping


def build_retail_queries(product: dict[str, Any]) -> list[str]:
    brand = (product.get('brand') or '').strip()
    name = (product.get('product_name') or '').strip()
    size = (product.get('unit_size') or product.get('pack_size') or '').strip()
    case = (product.get('case_pack') or '').strip()

    queries = []
    base = ' '.join(x for x in [brand, name, size] if x).strip()
    if base:
        queries.append(base)
    if brand and name:
        queries.append(f"{brand} {name}")
    if name and size:
        queries.append(f"{name} {size}")
    if case and name:
        queries.append(f"{name} {case} count")
    if name:
        queries.append(name)
    return list(dict.fromkeys(q for q in queries if q))


def resolve_with_retail_candidates(product: dict[str, Any]) -> dict[str, Any]:
    all_results = []
    queries = build_retail_queries(product)
    for q in queries:
        all_results.extend(search_walmart_candidates(q, limit=8))

    # If Walmart didn't find enough results, try Google Shopping
    if len(all_results) < 3:
        try:
            for q in queries[:2]:
                google_results = search_google_shopping(q, limit=6)
                all_results.extend(google_results)
        except Exception:
            pass

    ranked = rank_retail_candidates(product, all_results, limit=6)
    for candidate in ranked:
        detail = fetch_detail_page(candidate['source_url'])
        detail_chunks = extract_text_content(detail.get('content', ''))
        accepted = accept_retail_match(product, candidate, detail_chunks)
        if accepted:
            accepted['detail_fetch_source'] = detail.get('source', '')
            return accepted
    return {}
