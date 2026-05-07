"""Public interface for the strategy system.

Keep this package import lightweight. Tests and utility modules should be able
to import strategies.product_quality without loading scraper/network deps.
"""

__all__ = ["run_best_strategy"]


def __getattr__(name):
    if name == "run_best_strategy":
        from .router import run_best_strategy
        return run_best_strategy
    raise AttributeError(name)
