# upc_providers — External UPC lookup providers
#
# Usage in app.py:
#   from upc_providers import default_providers
#   products = upc_enrichment.enrich_products_upc(products, providers=default_providers())
#
# Providers are instantiated fresh per call so they carry no state between runs.

from .openfoodfacts import OpenFoodFactsProvider
from .upcitemdb import UPCItemDBProvider
from .go_upc import GoUPCProvider
from .upcdatabase import UPCDatabaseProvider
from .web_search import WebSearchUPCProvider

__all__ = ["OpenFoodFactsProvider", "UPCItemDBProvider", "GoUPCProvider", "UPCDatabaseProvider", "WebSearchUPCProvider", "default_providers"]


def default_providers() -> list:
    """
    Return the default ordered provider list used by all scrape routes.

    Order matters: enrichment tries every provider, then picks the best result.
    Open Food Facts, UPCItemDB, Go-UPC, and UPCDatabase provide diversified
    free-text search paths. WebSearch is used as a final fallback.
    """
    return [OpenFoodFactsProvider(), UPCItemDBProvider(), GoUPCProvider(), UPCDatabaseProvider(), WebSearchUPCProvider()]
