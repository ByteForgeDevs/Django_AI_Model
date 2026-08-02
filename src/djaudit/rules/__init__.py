"""Built-in rule modules.

Modules in this package are imported dynamically, so adding a rule file is the
only step needed to register its rules -- there is no central list to keep in
sync and therefore no way to forget.
"""

from __future__ import annotations

import importlib
import pkgutil


def load_all() -> None:
    """Import every rule module in this package, triggering registration."""
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{module_info.name}")
