import re
from typing import Any

from pack_parser import _parse_from_title

_GENERIC_SUPPLIER_BRANDS = {
    'rego trading inc',
    'supplier',
    'wholesaler',
}

_CPG_BRAND_PATTERNS = [
    r'^(TOOTSIE ROLL(?: POP DROPS)?|TOOTSIE POPS?)\b',
    r'^(WHATCHAMACALLIT)\b',
    r'^(WHOPPERS)\b',
    r'^(WACKY WAFERS)\b',
    r'^(WACK-O-WAX)\b',
    r'^(SWEDISH FISH)\b',
    r'^(ZOTZ)\b',
    r'^(ZEBRA BARS)\b',
    r'^(SUGAR DADDY)\b',
    r'^(SUPER MARIO)\b',
    # Wholesale/CPG brands
    r'^(KRAFT|NESTL[EÉ]|GENERAL MILLS|KELLOGG|MONDELEZ|TYSON|HORMEL|HEINZ|PEPSI|COCA[\s\-]?COLA)\b',
    r'^(SYSCO|US FOODS|FOOD SERVICE|RESTAURANT DEPOT)\b',
    r'^(PROCTER[\s\-]?GAMBLE|P[\s\-]?&[\s\-]?G)\b',
    r'^(UNILEVER|JOHNSON[\s\-]?&[\s\-]?JOHNSON|J[\s\-]?&[\s\-]?J)\b',
    r'^(CAMPBELL|DELMONTEFOODS|CONAGRA)\b',
    r'^(CLOROX|LYSOL|BOUNTY|CHARMIN)\b',
    r'^(FRITO[\s\-]?LAY|LAY[\s\-]?S|DORITOS|CHEETOS|TOSTITOS)\b',
    r'^(RED BULL|MONSTER|ENERGY|GATORADE|POWERADE)\b',
]

_CASE_PATTERNS = [
    r'\b(?:case of|pack of|count of)\s*(\d+)\b',
    r'\b(\d+)\s*(?:pack|pk|count|ct)\b',
]


def _clean_spaces(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text or '')).strip(' -|,/')


def _title_case_brand(text: str) -> str:
    return ' '.join(word.upper() if len(word) <= 4 or word.isupper() else word.title() for word in text.split())


def _extract_brand_from_title(title: str) -> str:
    title = _clean_spaces(title).upper()
    if not title:
        return ''
    for pat in _CPG_BRAND_PATTERNS:
        m = re.search(pat, title, re.IGNORECASE)
        if m:
            return _title_case_brand(_clean_spaces(m.group(1)))
    m = re.match(r'^([A-Z0-9&\-\.\' ]{3,30})\b', title)
    if not m:
        return ''
    candidate = _clean_spaces(m.group(1))
    if len(candidate.split()) > 4:
        return ''
    return _title_case_brand(candidate)


def _normalize_title(title: str) -> str:
    title = _clean_spaces(title)
    title = re.sub(r'\bReGo Trading Inc\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s+', ' ', title).strip(' -|,/')
    return title


def _extract_case_count(title: str) -> str:
    title = str(title or '')
    for pat in _CASE_PATTERNS:
        m = re.search(pat, title, re.IGNORECASE)
        if m:
            return m.group(1)
    return ''


def _infer_brand_from_sku(sku: str) -> str:
    """
    Try to infer brand from SKU patterns.
    Common wholesale/supplier SKU prefixes and patterns.
    """
    if not sku:
        return ''
    sku = sku.strip().upper()

    # Sysco SKUs often start with specific prefixes
    if sku.startswith('SYS'):
        return 'SYSCO'

    # US Foods prefixes
    if sku.startswith('USF'):
        return 'US FOODS'

    # Some distributors encode brand codes
    if sku.startswith('K-'):
        return 'KRAFT'

    if sku.startswith('N-'):
        return 'NESTLE'

    # If SKU is all numeric and substantial, likely from a major supplier
    if sku.isdigit() and len(sku) >= 8:
        # Could be UPC-based or supplier-based, try common patterns
        pass

    return ''


def _promote_pack_size_to_unit_size(product: dict[str, Any]) -> None:
    if (product.get('unit_size') or '').strip():
        return
    pack_size = (product.get('pack_size') or '').strip()
    if re.fullmatch(r'\d+(?:\.\d+)?\s*(?:OZ|FL OZ|LB|G|CT|ML)', pack_size, re.IGNORECASE):
        product['unit_size'] = pack_size.upper().replace('  ', ' ')


def strengthen_identity(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for product in products:
        before = {
            'brand': product.get('brand', ''),
            'product_name': product.get('product_name', ''),
            'unit_size': product.get('unit_size', ''),
            'case_pack': product.get('case_pack', ''),
            'pack_size': product.get('pack_size', ''),
        }

        title = _normalize_title(product.get('product_name', ''))
        product['product_name'] = title

        brand = _clean_spaces(product.get('brand', ''))
        if brand.lower() in _GENERIC_SUPPLIER_BRANDS or not brand:
            inferred = _extract_brand_from_title(title)
            if not inferred:
                # Try to infer brand from SKU if name extraction didn't work
                sku = (product.get('sku') or '').strip()
                inferred = _infer_brand_from_sku(sku)
            if inferred:
                product['brand'] = inferred
        elif brand:
            product['brand'] = _title_case_brand(brand)

        parsed = _parse_from_title(title) if title else None
        if parsed:
            if not (product.get('unit_size') or '').strip() and parsed.get('pack_size'):
                product['unit_size'] = parsed['pack_size']
            if not (product.get('case_pack') or '').strip() and parsed.get('case_pack'):
                product['case_pack'] = parsed['case_pack']
            if not (product.get('pack_size') or '').strip() and parsed.get('pack_size'):
                product['pack_size'] = parsed['pack_size']

        if not (product.get('case_pack') or '').strip():
            inferred_case = _extract_case_count(title)
            if inferred_case:
                product['case_pack'] = inferred_case

        _promote_pack_size_to_unit_size(product)

        after = {
            'brand': product.get('brand', ''),
            'product_name': product.get('product_name', ''),
            'unit_size': product.get('unit_size', ''),
            'case_pack': product.get('case_pack', ''),
            'pack_size': product.get('pack_size', ''),
        }
        product['identity_before'] = before
        product['identity_after'] = after
    return products
