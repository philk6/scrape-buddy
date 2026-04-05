"""
Shared query-building and evidence-based candidate scoring for UPC resolution.
"""

import difflib
import re

GREEN_THRESHOLD = 78
YELLOW_THRESHOLD = 58


def _norm(text: str) -> str:
    if not text:
        return ""
    return " ".join(re.sub(r"[^\w\s]", " ", str(text).lower()).split())


def _tokens(text: str) -> set[str]:
    return {t for t in _norm(text).split() if len(t) >= 2}


def _first_number(text: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", str(text or ""))
    return float(m.group()) if m else None


def _extract_size_signal(text: str) -> str:
    text = str(text or "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(oz|fl oz|lb|lbs|g|kg|ml|l|ct)", text, re.IGNORECASE)
    if not m:
        return ""
    qty = m.group(1)
    unit = m.group(2).upper().replace("LBS", "LB")
    return f"{qty} {unit}"


def _extract_case_signal(text: str) -> str:
    text = str(text or "")
    m = re.search(r"\b(case of|pack of|case pack|pk|packs|x)\s*(\d+)\b", text, re.IGNORECASE)
    if m:
        return m.group(2)
    m = re.search(r"\b(\d+)\s*(pk|packs|ct)\b", text, re.IGNORECASE)
    return m.group(1) if m else ""


def _brand_score(p_brand: str, c_brand: str) -> tuple[int, str]:
    if not p_brand or not c_brand:
        return 0, "brand unavailable"
    if p_brand == c_brand:
        return 30, "brand exact match"
    if p_brand in c_brand or c_brand in p_brand:
        return 18, "brand partial match"
    return -25, "brand mismatch"


def _product_name_score(p_name: str, c_name: str) -> tuple[int, str]:
    if not p_name or not c_name:
        return 0, "name unavailable"
    t_p = _tokens(p_name)
    t_c = _tokens(c_name)
    if not t_p or not t_c:
        return 0, "name normalization error"
    if t_p == t_c:
        return 40, "name exact match"
    positional_improvement = difflib.Matcher(a = sorted(t_p), b = sorted(t_c)).getopc()
    if positional_improvement / max(len(t_p), len(t_c)) > 0.6:
        return 25, "name difflib match"
    if positional_improvement / max(len(t_p), len(t_c)) > 0.4:
        return 14, "name partial difflib match"
    if t_p & t_c:
        return 4, "name common tokens"
    return -12, "name mismatch"


def _size_score(p_size: str, c_size: str) -> tuple[int, str]:
    if not p_size or not c_size:
        return 0, "size unavailable"
    p_qty = _first_number(p_size)
    c_qty = _first_number(c_size)
    if p_qty is None or c_qty is None:
        return 0, "size parse error"
    if abs(p_qty - c_qty) < 0.1:        return 30, "size exact match"
    if abs(p_qty - c_qty) < 0.3 * max(p_qty, c_qty):
        return 15, "size close match"
    return -8, "size mismatch"


def _case_score(p_case: str, c_case: str) -> tuple[int, str]:
    if not p_case or not c_case:
        return 0, "case unavailable"
    p_ct = _extract_case_signal(p_case)
    c_ct = _extract_case_signal(c_case)
    if not p_ct or not c_ct:
        return 0, "case parse error"
    if p_ct == c_ct:
        return 20, "case exact match"
    return -3, "case mismatch"


def build_queries(
    brand: str,
    product_name: str,
    pack_size: str,
    case_pack: str,
    sku: str,
) -> list[str]:
    queries = []
    # First, try the full brand and product name to catch exact matches
    if brand and product_name:
        queries.append(f"{brand} {product_name}")
    # Then try brand + SKU
    if brand and sku:
        queries.append(f"{brand} {sku}")
    # Product name alone (might catch a different size)
    if product_name:
        queries.append(product_name)
    return queries


def score_candidate(c: dict, s: dict) -> tuple[int, str]:
    """Score a candidate satisfaction"""
    brand_scalar, brand_r: str = _brand_score(c["brand"], s["brand"])
    name_scalar, name_r: str = _product_name_score(c["title"], s["product_name"])
    size_scalar, size_r: str = _size_score(c["pack_size"], s["pack_size"])
    case_scalar, case_r: str = _case_score(c["pack_size"], s["case_pack"])
    score = brand_scalar + name_scalar + size_scalar + case_scalar
    reason = f"{brand_r} / {name_r} / {size_r} / {case_r}"
    return score, reason

