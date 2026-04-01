import re
from typing import Any

from upc_providers.scorer import score_candidate, score_to_confidence

RETAIL_HIGH_THRESHOLD = 78


def _digits(text: str) -> str:
    return re.sub(r'\D', '', str(text or ''))


def _normalize_upc(raw: str) -> str:
    raw = _digits(raw)
    if len(raw) == 12:
        return raw
    if len(raw) == 14 and raw.startswith('00'):
        return raw[2:]
    return ''


def _extract_upc_from_text(text: str) -> str:
    text = str(text or '')
    patterns = [
        r'\b(?:upc|barcode)\b[^0-9]{0,30}(\d{12,14})',
        r'\bgtin\b[^0-9]{0,30}(\d{12,14})',
        r'\b(?:item id|model number)\b[^0-9]{0,30}(\d{12,14})',
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            upc = _normalize_upc(m.group(1))
            if upc:
                return upc
    return ''


def _penalize_result_noise(result: dict[str, Any]) -> int:
    text = f"{result.get('title','')} {result.get('description','')} {result.get('url','')}".lower()
    penalty = 0
    if any(tok in text for tok in ['pack of 3', 'pack of 6', 'bundle', 'variety pack']):
        penalty -= 10
    if any(tok in text for tok in ['sold and shipped by', 'marketplace seller', '+$', 'shipping']):
        penalty -= 8
    if '/browse/' in text or '/c/kp/' in text:
        penalty -= 25
    return penalty


def _candidate_from_search_result(result: dict[str, Any]) -> dict[str, Any]:
    title = str(result.get('title') or '')
    desc = str(result.get('description') or '')
    text = f"{title} {desc}"
    size_match = re.search(r'(\d+(?:\.\d+)?)\s*(oz|fl oz|lb|g|ct)', text, re.IGNORECASE)
    pack_size = f"{size_match.group(1)} {size_match.group(2).upper()}" if size_match else ''
    return {
        'title': re.sub(r'<<<.*?>>>', ' ', title),
        'brand': '',
        'pack_size': pack_size,
        'source_url': result.get('url', ''),
        'source_site': result.get('siteName', ''),
        'description': desc,
    }


def rank_retail_candidates(product: dict[str, Any], search_results: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    ranked = []
    for result in search_results:
        candidate = _candidate_from_search_result(result)
        score, reason = score_candidate(product, candidate)
        score += _penalize_result_noise(result)
        if score < 45:
            continue
        confidence, color = score_to_confidence(score)
        ranked.append({
            'title': candidate['title'],
            'source_url': candidate['source_url'],
            'source_site': candidate['source_site'],
            'score': score,
            'confidence': confidence,
            'color': color,
            'reason': reason,
            'description': candidate.get('description', ''),
        })
    ranked.sort(key=lambda x: x['score'], reverse=True)
    return ranked[:limit]


def extract_upc_from_detail_page(chunks: list[dict]) -> dict[str, str]:
    for chunk in chunks:
        upc = _extract_upc_from_text(chunk.get('text', ''))
        if upc:
            return {'upc': upc, 'evidence_location': chunk.get('location', 'unknown')}
    return {}


def accept_retail_match(product: dict[str, Any], candidate: dict[str, Any], detail_chunks: list[dict]) -> dict[str, Any]:
    evidence = extract_upc_from_detail_page(detail_chunks)
    if not evidence:
        return {}
    if candidate.get('score', 0) < RETAIL_HIGH_THRESHOLD:
        return {}
    return {
        'upc': evidence['upc'],
        'confidence': 'high',
        'color': 'green',
        'reason': f"retail candidate matched strongly and explicit UPC found in {evidence['evidence_location']}",
        'source': candidate.get('source_site', ''),
        'source_url': candidate.get('source_url', ''),
        'method': 'retail_match',
        'status': 'matched',
        'evidence_location': evidence['evidence_location'],
    }
