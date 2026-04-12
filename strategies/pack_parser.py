"""
strategies/pack_parser.py — Title-abbreviation pack/case parser.

Parses compact packaging abbreviations from product titles such as:
  "Sour Patch Kids - 20oz/24pk"   -> unit_size="20 OZ", case_pack="24"
  "Clorox Wipes - 75ct/6pk"       -> unit_size="75 CT", case_pack="6"
  "Neutrogena Gel Cream - 16oz/3pk" -> unit_size="16 OZ", case_pack="3"
  "Tide Pods 4pk"                 -> case_pack="4"

Public API:
  parse_pack_from_title(title: str) -> dict
    Returns a dict with any combination of:
      unit_size       str    e.g. "20 OZ"
      case_pack       str    e.g. "24"
      raw_pack_text   str    the matched text fragment from the title
      pack_confidence float  0.0–1.0
    Returns {} if no confident match is found.

Priority:
  1. unit/pack compound (e.g. 16oz/3pk)  — confidence 0.95
  2. standalone pack count (e.g. 4pk)    — confidence 0.75 (0.80 if unit also found)
  3. unit size only (e.g. 16oz)          — confidence 0.60  (unit_size only, no case_pack)
"""

import re
import logging

logger = logging.getLogger(__name__)

# ── Unit type normalisation map ────────────────────────────────────────────────
# Keys are lowercase canonical forms; values are the output string.
_UNIT_ALIASES: dict[str, str] = {
    "fl oz":  "FL OZ",
    "fl. oz": "FL OZ",
    "floz":   "FL OZ",
    "fl.oz":  "FL OZ",
    "oz":     "OZ",
    "ct":     "CT",
    "cnt":    "CT",
    "count":  "CT",
    "lb":     "LB",
    "lbs":    "LB",
    "lb.":    "LB",
    "lbs.":   "LB",
    "gal":    "GAL",
    "gl":     "GAL",
    "ml":     "ML",
    "l":      "L",
    "g":      "G",
    "kg":     "KG",
    "pk":     "PK",
    "pack":   "PK",
}

# Alternation of all recognised unit strings.  Longer alternatives first to
# prevent "oz" matching before "fl oz".  No outer group — callers wrap as needed.
_UNIT_ALTS = (
    r"fl\.?\s*oz"              # fl oz / fl.oz — before bare "oz"
    r"|floz"
    r"|oz"
    r"|ct|cnt|count"
    r"|lbs?\.?"                # lb / lbs / lb. / lbs.
    r"|gal|gl"
    r"|ml"
    r"|kg"
    r"|l(?=\s|/|\d|$)"        # bare "l" only when followed by space/slash/digit/end
    r"|g(?=\s|/|\d|$)"        # bare "g" only when followed by space/slash/digit/end
)

# ── Core patterns ──────────────────────────────────────────────────────────────

# Priority 1: "16oz/3pk", "20 oz / 24 pk", "75CT/6PK", "110 ct / 3 pk"
# Capturing groups: (1) unit qty, (2) unit type, (3) pack qty.
_UNIT_SLASH_PACK_RE = re.compile(
    r"(\d+(?:\.\d+)?)"         # (1) unit quantity  e.g. 16, 20, 75.5
    r"\s*"
    r"(" + _UNIT_ALTS + r")"   # (2) unit type
    r"\s*/\s*"                 # slash separator (spaces optional)
    r"(\d+)"                   # (3) pack quantity  e.g. 3, 24, 6
    r"\s*(?:pk|pack)\b",       # "pk" or "pack" keyword (non-capturing)
    re.IGNORECASE,
)

# Priority 2: standalone pack count — "4pk", "24 pk", "12-pack"
# Negative lookbehind prevents matching the Npk part inside a unit/pack string.
_PACK_ONLY_RE = re.compile(
    r"(?<!/)"                  # not immediately after a slash (already caught by pattern 1)
    r"(?<!\d)"                 # not immediately after a digit
    r"(\d+)"                   # (1) pack count
    r"\s*(?:pk|pack)\b",
    re.IGNORECASE,
)

# Priority 3 / unit-size side search — "16oz", "32 fl oz", "1 gal"
# Capturing groups: (1) unit qty, (2) unit type.
_UNIT_ONLY_RE = re.compile(
    r"(\d+(?:\.\d+)?)"         # (1) unit quantity
    r"\s*"
    r"(" + _UNIT_ALTS + r")",  # (2) unit type
    re.IGNORECASE,
)


def _normalise_unit(raw: str) -> str:
    """Return a normalised uppercase unit string (e.g. 'oz' -> 'OZ')."""
    key = re.sub(r"\s+", " ", raw.lower().strip().rstrip("."))
    return _UNIT_ALIASES.get(key, raw.upper().strip())


def parse_pack_from_title(title: str) -> dict:
    """
    Parse pack/case information from a product title string.

    Returns a dict (possibly empty) with keys:
      unit_size, case_pack, raw_pack_text, pack_confidence.

    Only returns a result when confidence is >= 0.60 — never fabricates values.
    Never raises.
    """
    if not title or not isinstance(title, str):
        return {}

    try:
        # ── Priority 1: unit/pack compound  e.g. "16oz/3pk" ──────────────────
        m = _UNIT_SLASH_PACK_RE.search(title)
        if m:
            unit_qty  = m.group(1)
            unit_raw  = m.group(2)    # the unit token captured by _UNIT_TOKEN
            pack_qty  = m.group(3)
            unit_norm = _normalise_unit(unit_raw)
            result = {
                "unit_size":       f"{unit_qty} {unit_norm}",
                "case_pack":       pack_qty,
                "raw_pack_text":   m.group(0),
                "pack_confidence": 0.95,
            }
            logger.debug(
                f"[PackParser] title compound match: "
                f"unit_size={result['unit_size']!r} "
                f"case_pack={result['case_pack']!r} "
                f"from {m.group(0)!r}"
            )
            return result

        # ── Priority 2: standalone pack count  e.g. "4pk" ────────────────────
        m2 = _PACK_ONLY_RE.search(title)
        if m2:
            pack_qty  = m2.group(1)
            confidence = 0.75
            result: dict = {
                "case_pack":     pack_qty,
                "raw_pack_text": m2.group(0),
            }
            # Also try to extract unit size from elsewhere in the title
            m3 = _UNIT_ONLY_RE.search(title)
            if m3:
                unit_qty  = m3.group(1)
                unit_raw  = m3.group(2)
                unit_norm = _normalise_unit(unit_raw)
                result["unit_size"] = f"{unit_qty} {unit_norm}"
                confidence = 0.80
            result["pack_confidence"] = confidence
            logger.debug(
                f"[PackParser] standalone pack match: "
                f"case_pack={pack_qty!r} "
                + (f"unit_size={result.get('unit_size')!r} " if "unit_size" in result else "")
                + f"from {m2.group(0)!r}"
            )
            return result

        # ── Priority 3: unit size only  e.g. "16oz" ──────────────────────────
        m3 = _UNIT_ONLY_RE.search(title)
        if m3:
            unit_qty  = m3.group(1)
            unit_raw  = m3.group(2)
            unit_norm = _normalise_unit(unit_raw)
            result = {
                "unit_size":       f"{unit_qty} {unit_norm}",
                "raw_pack_text":   m3.group(0),
                "pack_confidence": 0.60,
            }
            logger.debug(
                f"[PackParser] unit-only match: "
                f"unit_size={result['unit_size']!r} "
                f"from {m3.group(0)!r}"
            )
            return result

    except Exception as e:
        logger.debug(f"[PackParser] parse error for {title!r}: {e}")

    return {}
