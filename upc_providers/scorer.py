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
    m = re.search(r"\b(case of|pack of|case pack|pk|pack|x)\s*(\d+)\b", text, re.IGNORECASE)
    if m:
        return m.group(2)
    m = re.search(r"\b(\d+)\s*(pk|pack|ct)\b", text, re.IGNORECASE)
    return m.group(1) if m else ""


def _brand_score(p_brand: str, c_brand: str) -> tuple[int, str]:
    if not p_brand or not c_brand:
        return 0, "brand unavailable"
    if p_brand == c_brand:
        return 30, "brand exact match"
    if p_brand in c_brand or c_brand in p_brand:
        return 18, "brand partial match"
    return -25, "brand mismatch"


def _title_score(p_name: str, c_title: str) -> tuple[int, str]:
    if not p_name or not c_title:
        return 0, "title unavailable"
    ratio = difflib.SequenceMatcher(None, p_name, c_title).ratio()
    token_overlap = len(_tokens(p_name) & _tokens(c_title))
    score = round(ratio * 38)
    if token_overlap >= 4:
        score += 8
    elif token_overlap >= 2:
        score += 4
    if ratio < 0.45 and token_overlap < 2:
        score -= 20
    reason = f"title ratio {ratio:.2f}; shared tokens {token_overlap}"
    return score, reason


def _size_score(product: dict, candidate: dict) -> tuple[int, str]:
    p_size = _extract_size_signal(product.get("unit_size") or product.get("pack_size") or product.get("product_name"))
    c_size = _extract_size_signal(candidate.get("pack_size") or candidate.get("title"))
    if not p_size:
        return 0, "size unavailable on supplier"
    if not c_size:
        return -8, "candidate missing size while supplier has size"
    if _norm(p_size) == _norm(c_size):
        return 22, "unit size match"
    return -18, f"unit size mismatch ({p_size} vs {c_size})"


def _case_score(product: dict, candidate: dict) -> tuple[int, str]:
    p_case = str(product.get("case_pack") or "").strip() or _extract_case_signal(product.get("product_name"))
    c_case = str(candidate.get("case_pack") or "").strip() or _extract_case_signal(candidate.get("pack_size") or candidate.get("title"))
    if not p_case:
        return 0, "case pack unavailable on supplier"
    if not c_case:
        return -4, "candidate missing case pack"
    if p_case == c_case:
        return 12, "case pack match"
    return -12, f"case pack mismatch ({p_case} vs {c_case})"


def _sku_support(product: dict, candidate: dict) -> tuple[int, str]:
    sku = _norm(product.get("sku", ""))
    title = _norm(candidate.get("title", ""))
    if sku and sku in title:
        return 8, "sku token present in candidate title"
    return 0, "no sku support"


def score_candidate(product: dict, candidate: dict) -> tuple[int, str]:
    p_name = _norm(product.get("product_name", ""))
    c_title = _norm(candidate.get("title", ""))
    p_brand = _norm(product.get("brand", ""))
    c_brand = _norm(candidate.get("brand", ""))

    parts = []
    score = 0
    for fn in (_brand_score, _title_score):
        pts, reason = fn(p_brand, c_brand) if fn is _brand_score else fn(p_name, c_title)
        score += pts
        parts.append(reason)
    for fn in (_size_score, _case_score, _sku_support):
        pts, reason = fn(product, candidate)
        score += pts
        parts.append(reason)

    if p_brand and c_brand and p_brand != c_brand and p_brand not in c_brand and c_brand not in p_brand:
        score -= 10
    if p_name and c_title and len(_tokens(p_name) & _tokens(c_title)) == 0:
        score -= 20

    score = max(0, min(100, score))
    return score, "; ".join(parts)


def score_to_confidence(score: int) -> tuple[str, str]:
    if score >= GREEN_THRESHOLD:
        return "high", "green"
    if score >= YELLOW_THRESHOLD:
        return "medium", "yellow"
    return "", ""


def build_queries(brand: str, product_name: str, pack_size: str, case_pack: str = "", sku: str = "") -> list[str]:
    b = (brand or "").strip()
    n = (product_name or "").strip()
    p = (pack_size or "").strip()
    c = (case_pack or "").strip()
    s = (sku or "").strip()

    raw = []
    if b and n and p:
        raw.append(f"{b} {n} {p}")
    if b and n and c:
        raw.append(f"{b} {n} case {c}")
    if b and n:
        raw.append(f"{b} {n}")
    if n and p:
        raw.append(f"{n} {p}")
    if n and c:
        raw.append(f"{n} case {c}")
    if b and s and n:
        raw.append(f"{b} {s} {n}")
    if n:
        raw.append(n)
    if b and s:
        raw.append(f"{b} {s}")

    seen, deduped = set(), []
    for q in raw:
        qn = q.strip()
        if qn and qn not in seen:
            seen.add(qn)
            deduped.append(qn)
    return deduped
