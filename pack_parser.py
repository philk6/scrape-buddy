"""
pack_parser.py — Reusable pack size / case pack extraction and normalization

Parses compact supplier strings like 4/1GAL, 6/2.5LB, 12/32OZ, 24/1CT
into structured output usable across all supplier sites.

Output fields added to each product:
    raw_pack_text     (str)  — original text the parse was sourced from
    case_pack         (str)  — number of units per case, e.g. "4"
    pack_size         (str)  — size of each unit, e.g. "1 GAL"
    unit_measure      (str)  — canonical unit only, e.g. "GAL"
    pack_confidence   (str)  — "high" | "medium" | "low" | ""

Usage:
    from pack_parser import enrich_all, enrich_product_pack, parse_pack_string

    products = enrich_all(products)                  # bulk
    product  = enrich_product_pack(product)          # single
    result   = parse_pack_string("4/1GAL")           # raw parse
"""

import re
import logging

logger = logging.getLogger(__name__)

# ── Unit normalization ─────────────────────────────────────────────────────────

_UNIT_MAP = {
    # Fluid / liquid
    "fl oz":   "FL OZ",
    "floz":    "FL OZ",
    "fl_oz":   "FL OZ",
    "oz":      "OZ",
    "gal":     "GAL",
    "gallon":  "GAL",
    "gallons": "GAL",
    "qt":      "QT",
    "quart":   "QT",
    "quarts":  "QT",
    "pt":      "PT",
    "pint":    "PT",
    "pints":   "PT",
    "ml":      "ML",
    "l":       "L",
    "liter":   "L",
    "litre":   "L",
    "liters":  "L",
    "litres":  "L",
    # Weight
    "lb":      "LB",
    "lbs":     "LB",
    "pound":   "LB",
    "pounds":  "LB",
    "g":       "G",
    "gram":    "G",
    "grams":   "G",
    "kg":      "KG",
    "kilogram":"KG",
    # Count / pack
    "ct":      "CT",
    "count":   "CT",
    "ea":      "EA",
    "each":    "EA",
    "pc":      "CT",
    "pcs":     "CT",
    "pk":      "PK",
    "pack":    "PK",
    "packs":   "PK",
    "cs":      "CS",
    "case":    "CS",
}

# Regex that matches a unit token (longest to shortest to avoid "g" eating "gal")
_UNIT_RE = re.compile(
    r"\b(fl\s*oz|fl_oz|gallon[s]?|gallons?|quarts?|pints?|liters?|litres?|"
    r"lbs?|pounds?|grams?|kilograms?|kg|ml|oz|gal|qt|pt|l|ct|count|ea|each|"
    r"pcs?|packs?|pk|cs|case)\b",
    re.IGNORECASE,
)


def _norm_unit(raw: str) -> str:
    """Normalize a raw unit string to a canonical uppercase unit, or return raw.upper()."""
    key = raw.strip().lower().replace("-", " ").replace("_", " ")
    return _UNIT_MAP.get(key, raw.upper())


def _fmt_pack_size(qty: str, unit: str) -> str:
    """Format pack_size as '1 GAL', '32 OZ', etc. Strips trailing .0 from ints."""
    qty = qty.strip()
    try:
        fval = float(qty)
        qty = str(int(fval)) if fval == int(fval) else qty
    except ValueError:
        pass
    return f"{qty} {unit}".strip() if unit else qty


# ── Core pattern matching ──────────────────────────────────────────────────────

# Number pattern: integer or decimal
_NUM = r"(\d+(?:\.\d+)?)"
# Optional whitespace
_SP  = r"\s*"

# Layer 1 — EA/CS split: "1 GAL EA, 4/CS"  or  "1 GAL, 4/CS"
_EACS_RE = re.compile(
    r"(\d+(?:\.\d+)?)" + _SP + r"(" + _UNIT_RE.pattern + r")" + _SP +
    r"(?:ea|each)?" + _SP + r"[,;]?" + _SP +
    r"(\d+)" + _SP + r"[/]" + _SP + r"(?:cs|case)\b",
    re.IGNORECASE,
)

# Layer 2 — Compact slash with size+unit: "4/1GAL", "6/2.5LB", "12/32OZ"
_COMPACT_RE = re.compile(
    r"(\d+)" + _SP + r"[/\\]" + _SP + _NUM + _SP + r"(" + _UNIT_RE.pattern + r")",
    re.IGNORECASE,
)

# Layer 3 — Count-only slash: "60/EA", "24/CS"
_COUNT_ONLY_RE = re.compile(
    r"(\d+)" + _SP + r"[/\\]" + _SP + r"(ea|each|cs|case|ct|count|pk|pack)\b",
    re.IGNORECASE,
)

# Layer 4 — Reversed slash: "5.7OZ/12PK", "64OZ/4PK"
_REVERSED_RE = re.compile(
    _NUM + _SP + r"(" + _UNIT_RE.pattern + r")" + _SP + r"[/\\]" + _SP +
    r"(\d+)" + _SP + r"(?:pk|pack|cs|case|ct)?\b",
    re.IGNORECASE,
)

# Layer 5 — Cross-multiply: "4 x 1 gal", "4×1GAL", "4 * 1 gal"
_CROSS_RE = re.compile(
    r"(\d+)" + _SP + r"[x×*]" + _SP + _NUM + _SP + r"(" + _UNIT_RE.pattern + r")?",
    re.IGNORECASE,
)

# Layer 6 — Dash-separated: "4-1gal"
_DASH_RE = re.compile(
    r"(\d+)-" + _NUM + r"(" + _UNIT_RE.pattern + r")",
    re.IGNORECASE,
)

# Layer 7 — "case of N" / "case pack: N" / "case pack N"
_CASE_OF_RE = re.compile(
    r"case\s+(?:of|pack:?)\s+(\d+)",
    re.IGNORECASE,
)

# Layer 8 — "N per case"
_PER_CASE_RE = re.compile(
    r"(\d+)\s+per\s+case",
    re.IGNORECASE,
)

# Layer 9 — Plain size only: "1 GAL", "32 OZ"
_PLAIN_SIZE_RE = re.compile(
    _NUM + _SP + r"(" + _UNIT_RE.pattern + r")\b",
    re.IGNORECASE,
)


def parse_pack_string(text: str) -> dict | None:
    """
    Parse a supplier pack string into structured fields.

    Returns a dict with keys:
        raw_pack_text, case_pack, pack_size, unit_measure, pack_confidence, pack_method

    Returns None if no pattern matches.
    """
    if not text:
        return None

    t = text.strip()

    # ── Layer 1: EA/CS split ("1 GAL EA, 4/CS") ──────────────────────────────
    m = _EACS_RE.search(t)
    if m:
        qty  = m.group(1)
        unit = _norm_unit(m.group(2))
        case = m.group(len(m.groups()))  # last group is the case count
        return {
            "raw_pack_text":   t,
            "case_pack":       case,
            "pack_size":       _fmt_pack_size(qty, unit),
            "unit_measure":    unit,
            "pack_confidence": "high",
            "pack_method":     "eacs_split",
        }

    # ── Layer 2: Compact slash with size+unit ("4/1GAL") ─────────────────────
    m = _COMPACT_RE.search(t)
    if m:
        case = m.group(1)
        qty  = m.group(2)
        unit = _norm_unit(m.group(3))
        return {
            "raw_pack_text":   t,
            "case_pack":       case,
            "pack_size":       _fmt_pack_size(qty, unit),
            "unit_measure":    unit,
            "pack_confidence": "high",
            "pack_method":     "compact_slash",
        }

    # ── Layer 3: Count-only slash ("60/EA", "24/CS") ─────────────────────────
    m = _COUNT_ONLY_RE.search(t)
    if m:
        case = m.group(1)
        unit = _norm_unit(m.group(2))
        return {
            "raw_pack_text":   t,
            "case_pack":       case,
            "pack_size":       "",
            "unit_measure":    unit,
            "pack_confidence": "medium",
            "pack_method":     "count_only_slash",
        }

    # ── Layer 4: Reversed slash ("5.7OZ/12PK") ───────────────────────────────
    m = _REVERSED_RE.search(t)
    if m:
        qty  = m.group(1)
        unit = _norm_unit(m.group(2))
        case = m.group(len(m.groups()))  # last group is case count
        return {
            "raw_pack_text":   t,
            "case_pack":       case,
            "pack_size":       _fmt_pack_size(qty, unit),
            "unit_measure":    unit,
            "pack_confidence": "high",
            "pack_method":     "reversed_slash",
        }

    # ── Layer 5: Cross-multiply ("4 x 1 gal") ────────────────────────────────
    m = _CROSS_RE.search(t)
    if m:
        case = m.group(1)
        qty  = m.group(2)
        raw_unit = m.group(3) or ""
        unit = _norm_unit(raw_unit) if raw_unit else ""
        return {
            "raw_pack_text":   t,
            "case_pack":       case,
            "pack_size":       _fmt_pack_size(qty, unit),
            "unit_measure":    unit,
            "pack_confidence": "high" if unit else "medium",
            "pack_method":     "cross_multiply",
        }

    # ── Layer 6: Dash ("4-1gal") ─────────────────────────────────────────────
    m = _DASH_RE.search(t)
    if m:
        case = m.group(1)
        qty  = m.group(2)
        unit = _norm_unit(m.group(3))
        return {
            "raw_pack_text":   t,
            "case_pack":       case,
            "pack_size":       _fmt_pack_size(qty, unit),
            "unit_measure":    unit,
            "pack_confidence": "high",
            "pack_method":     "dash_separator",
        }

    # ── Layer 7: "case of N" ──────────────────────────────────────────────────
    m = _CASE_OF_RE.search(t)
    if m:
        return {
            "raw_pack_text":   t,
            "case_pack":       m.group(1),
            "pack_size":       "",
            "unit_measure":    "",
            "pack_confidence": "medium",
            "pack_method":     "case_of",
        }

    # ── Layer 8: "N per case" ─────────────────────────────────────────────────
    m = _PER_CASE_RE.search(t)
    if m:
        return {
            "raw_pack_text":   t,
            "case_pack":       m.group(1),
            "pack_size":       "",
            "unit_measure":    "",
            "pack_confidence": "medium",
            "pack_method":     "per_case",
        }

    # ── Layer 9: Plain size only ("1 GAL", "32 OZ") ──────────────────────────
    m = _PLAIN_SIZE_RE.search(t)
    if m:
        qty  = m.group(1)
        unit = _norm_unit(m.group(2))
        return {
            "raw_pack_text":   t,
            "case_pack":       "",
            "pack_size":       _fmt_pack_size(qty, unit),
            "unit_measure":    unit,
            "pack_confidence": "low",
            "pack_method":     "plain_size",
        }

    return None


# ── Title parsing (Layer 4 of product enrichment) ─────────────────────────────

_TITLE_DELIMITERS = re.compile(r"[,|;\u2013\u2014]|--|\s{2,}")


def _parse_from_title(title: str) -> dict | None:
    """
    Split title on delimiters and try parse_pack_string() on each segment.
    Confidence is capped at 'medium' since this is a fallback source.
    """
    if not title:
        return None

    segments = _TITLE_DELIMITERS.split(title)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        result = parse_pack_string(seg)
        if result:
            # downgrade high→medium since we're parsing a title
            if result["pack_confidence"] == "high":
                result["pack_confidence"] = "medium"
            result["pack_method"] = f"title:{result['pack_method']}"
            return result
    return None


# ── Single-product enrichment ──────────────────────────────────────────────────

def enrich_product_pack(product: dict) -> dict:
    """
    Add pack enrichment fields to a single product dict (modified in place).

    Source priority:
      1. pack_size field already contains a parseable string
      2. case_pack field already contains a parseable string
      3. pack_size + case_pack combined
      4. product_name (title parsing, medium confidence cap)

    If no source yields a result, all pack fields are set to empty strings.
    """
    pack_size_raw  = (product.get("pack_size")  or "").strip()
    case_pack_raw  = (product.get("case_pack")  or "").strip()
    product_name   = (product.get("product_name") or "").strip()

    result = None

    # Layer 1 — pack_size field
    if pack_size_raw:
        result = parse_pack_string(pack_size_raw)
        if result:
            logger.debug(
                f"[PackParser] pack_size field matched ({result['pack_method']}): "
                f"'{pack_size_raw}'"
            )

    # Layer 2 — case_pack field
    if not result and case_pack_raw:
        result = parse_pack_string(case_pack_raw)
        if result:
            logger.debug(
                f"[PackParser] case_pack field matched ({result['pack_method']}): "
                f"'{case_pack_raw}'"
            )

    # Layer 3 — combined fields
    if not result and (pack_size_raw or case_pack_raw):
        combined = " ".join(filter(None, [case_pack_raw, pack_size_raw]))
        result = parse_pack_string(combined)
        if result:
            logger.debug(
                f"[PackParser] combined fields matched ({result['pack_method']}): "
                f"'{combined}'"
            )

    # Layer 4 — product_name title parsing
    if not result and product_name:
        result = _parse_from_title(product_name)
        if result:
            logger.debug(
                f"[PackParser] title parsed ({result['pack_method']}): "
                f"'{product_name[:60]}'"
            )

    if result:
        # Only overwrite pack_size / case_pack if the parse found them
        if result.get("pack_size"):
            product["pack_size"] = result["pack_size"]
        if result.get("case_pack"):
            product["case_pack"] = result["case_pack"]

        product["raw_pack_text"]   = result["raw_pack_text"]
        product["unit_measure"]    = result["unit_measure"]
        product["pack_confidence"] = result["pack_confidence"]
    else:
        product.setdefault("raw_pack_text",   "")
        product.setdefault("unit_measure",    "")
        product.setdefault("pack_confidence", "")

    return product


# ── Bulk enrichment ────────────────────────────────────────────────────────────

def enrich_all(products: list) -> list:
    """
    Add pack enrichment fields to every product in the list.
    Modifies in place and returns the same list.
    """
    high = medium = low = missing = 0

    for product in products:
        enrich_product_pack(product)
        conf = product.get("pack_confidence", "")
        if conf == "high":
            high += 1
        elif conf == "medium":
            medium += 1
        elif conf == "low":
            low += 1
        else:
            missing += 1

    logger.info(
        f"[PackParser] Complete — "
        f"{high} high | {medium} medium | {low} low | {missing} no match"
    )
    return products
