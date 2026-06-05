"""Handler modules for the FC Mobile League bot.

This package is being introduced incrementally — Phase 1 only carries the
small, self-contained helpers from ``bot.py``. Subsequent phases will move
domain handlers (admin, tournament, match, profile, …) into their own
modules. ``bot.py`` re-exports everything from here so external imports
keep working.
"""

from . import common  # noqa: F401  (re-exported for "from handlers import common")
from . import tours   # noqa: F401

__all__ = ["common", "tours"]
