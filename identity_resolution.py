import re
from dataclasses import dataclass
from typing import Any


@dataclass
class IdentifierResolution:
    upc_12: str = ""
    native_identifier: str = ""
    native_identifier_type: str = ""
    normalized_identifier: str = ""
    identifier_status: str = ""
    needs_resolution: str = "0"
    resolution_source: str = ""
    resolution_method: str = ""
    resolution_confidence: str = ""
    resolution_color: str = ""
    resolution_reason: str = ""
    resolution_status: str = ""
    raw_identifier: str = ""


def _digits(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _valid_upc12(value: str) -> bool:
    return bool(re.fullmatch(r"\d{12}", value or ""))


def _infer_identifier_type(raw: str, gtin_case: str = "") -> str:
    d = _digits(raw)
    if not d:
        return ""
    if gtin_case and d == _digits(gtin_case):
        return "gtin_case"
    if len(d) == 12:
        return "upc_12"
    if len(d) == 13:
        return "gtin_13"
    if len(d) == 14:
        return "gtin_14"
    if len(d) == 8:
        return "gtin_8"
    return "unknown_numeric"


def resolve_supplier_identity(product: dict[str, Any]) -> dict[str, Any]:
    upc = _digits(product.get("upc", ""))
    gtin_case = _digits(product.get("gtin_case", ""))
    raw_identifier = upc or gtin_case
    ident_type = _infer_identifier_type(raw_identifier, gtin_case)

    result = IdentifierResolution(
        raw_identifier=raw_identifier,
        native_identifier=raw_identifier,
        native_identifier_type=ident_type,
        normalized_identifier=raw_identifier,
    )

    if _valid_upc12(upc):
        result.upc_12 = upc
        result.identifier_status = "native_upc"
        result.needs_resolution = "0"
        result.resolution_source = "supplier_page"
        result.resolution_method = "native_12_digit"
        result.resolution_confidence = "high"
        result.resolution_color = "green"
        result.resolution_reason = "supplier provided a valid 12-digit UPC"
        result.resolution_status = "native"
        return result.__dict__

    if len(upc) == 14 and upc.startswith("00") and _valid_upc12(upc[2:]):
        result.upc_12 = upc[2:]
        result.identifier_status = "converted_padded_gtin14"
        result.needs_resolution = "0"
        result.resolution_source = "supplier_page"
        result.resolution_method = "strip_gtin14_padding"
        result.resolution_confidence = "high"
        result.resolution_color = "green"
        result.resolution_reason = "14-digit supplier identifier started with 00 and normalized cleanly to UPC-12"
        result.resolution_status = "converted"
        return result.__dict__

    if upc:
        result.identifier_status = ident_type or "supplier_identifier_present"
        result.needs_resolution = "1"
        result.resolution_status = "needs_external_resolution"
        result.resolution_reason = "supplier identifier exists but is not directly usable as a trusted 12-digit UPC"
    else:
        result.identifier_status = "missing_identifier"
        result.needs_resolution = "1"
        result.resolution_status = "needs_external_resolution"
        result.resolution_reason = "no supplier-native UPC-12 available"

    return result.__dict__


def enrich_identity(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for product in products:
        product.update(resolve_supplier_identity(product))
    return products
