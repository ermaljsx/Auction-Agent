"""Adapter registry. Every module in this package is imported, so subclasses register
themselves through ``AuctionAdapter.__init_subclass__``."""
from __future__ import annotations

import importlib
import pkgutil
from typing import Optional

from .base import AuctionAdapter

_loaded = False


def load_all() -> dict[str, type[AuctionAdapter]]:
    global _loaded
    if not _loaded:
        for module in pkgutil.iter_modules(__path__):
            if module.name != "base" and not module.name.startswith("_"):
                importlib.import_module(f"{__name__}.{module.name}")
        _loaded = True
    return dict(AuctionAdapter._registry)


def get_adapter_class(slug: str) -> type[AuctionAdapter]:
    registry = load_all()
    if slug not in registry:
        raise KeyError(f"Unknown auction house {slug!r}. Known: {', '.join(sorted(registry))}")
    return registry[slug]


def adapter_class_for_url(url: str) -> Optional[type[AuctionAdapter]]:
    for cls in load_all().values():
        if any(p.search(url) for p in cls.url_patterns):
            return cls
    return None
