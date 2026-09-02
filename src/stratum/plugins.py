"""Plugin resolution. Spec: docs/specs/04-cost-functions.md section 7.

A `ref` is an entry-point name when one is registered, or `module:Class` otherwise; every hook
accepts both forms.
"""
from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from typing import Any

GROUPS = {
    "scorer": "stratum.scorers",
    "reducer": "stratum.reducers",
    "mask": "stratum.masks",
    "filter": "stratum.filters",
    "mapper": "stratum.mappers",
    "reader": "stratum.readers",
    "source": "stratum.sources",
}


def registered(kind: str) -> dict[str, str]:
    """name -> 'module:attr' for every registered plugin of one kind."""
    return {ep.name: ep.value for ep in entry_points(group=GROUPS[kind])}


def resolve(kind: str, ref: str) -> type[Any]:
    """Entry-point name first, then module:Class. Raises LookupError with both attempts named."""
    eps = {ep.name: ep for ep in entry_points(group=GROUPS[kind])}
    if ref in eps:
        return eps[ref].load()
    if ":" in ref:
        module, _, attr = ref.partition(":")
        return getattr(importlib.import_module(module), attr)
    raise LookupError(f"no {kind} named {ref!r} is registered under {GROUPS[kind]!r}, "
                      "and it is not a module:Class reference")
