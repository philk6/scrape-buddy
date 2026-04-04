# strategies/__init__.py
#
# Public interface for the strategy system.
# Import run_best_strategy from here — don't import from the individual strategy
# files directly, so the router stays as the single decision point.

from .router import run_best_strategy
from .universal_pipeline import run_pipeline

__all__ = ["run_best_strategy", "run_pipeline"]
