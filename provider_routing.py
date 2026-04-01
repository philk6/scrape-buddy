import re
from typing import Any


def row_strength(product: dict[str, Any]) -> int:
    score = 0
    if (product.get("brand") or "").strip():
        score += 3
    if (product.get("unit_size") or product.get("pack_size") or "").strip():
        score += 2
    if (product.get("case_pack") or "").strip():
        score += 1
    if len((product.get("product_name") or "").strip()) >= 12:
        score += 2
    if (product.get("sku") or "").strip():
        score += 1
    return score


def classify_row(product: dict[str, Any]) -> str:
    title = (product.get("product_name") or "").lower()
    brand = (product.get("brand") or "").strip()
    name = (product.get("product_name") or "").strip()
    sku = (product.get("sku") or "").strip()
    strength = row_strength(product)

    candy_tokens = [
        'candy', 'gum', 'taffy', 'tootsie', 'whoppers', 'smarties', 'wafers',
        'bar', 'lollipop', 'mint', 'snack', 'theater box', 'soda'
    ]
    has_candy_signal = any(tok in title for tok in candy_tokens)

    # If product has a reasonable name (8+ chars), it's always queryable
    # If product has name + brand or name + sku, it's always queryable
    if len(name) >= 8:
        # Product name is substantial enough to query
        pass
    elif (name and brand) or (name and sku):
        # Product name + brand or name + sku is queryable
        pass
    elif strength < 3:
        # Not enough info to query
        return 'too_weak_to_query'

    if has_candy_signal and brand and (product.get('unit_size') or '').strip():
        return 'consumer_candy_strong'
    if has_candy_signal:
        return 'consumer_candy_weak'
    if brand and (product.get('unit_size') or product.get('pack_size') or '').strip():
        return 'strong_identity'
    if brand or sku:
        return 'partial_identity'
    return 'weak_identity'


def route_providers(product: dict[str, Any], providers: list[Any]) -> list[Any]:
    group = classify_row(product)
    by_name = {p.name: p for p in providers}

    orders = {
        'consumer_candy_strong': ['upcdatabase', 'go_upc', 'open_food_facts', 'upcitemdb'],
        'consumer_candy_weak': ['upcdatabase', 'go_upc', 'open_food_facts'],
        'strong_identity': ['upcitemdb', 'upcdatabase', 'open_food_facts', 'go_upc'],
        'partial_identity': ['upcdatabase', 'upcitemdb', 'go_upc', 'open_food_facts'],
        'weak_identity': ['upcdatabase', 'go_upc'],
        'too_weak_to_query': [],
    }
    routed = [by_name[name] for name in orders.get(group, []) if name in by_name]
    return routed
