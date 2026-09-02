"""The reader registry (12 section 3 rule 1): keyed on `collection`, populated by the
`stratum.readers` entry-point group, overridable from the manifest's `inputs.readers`.

A reader is selected from what a role DECLARES, never from what a file is called. Entry-point
names are collection names (`EMITL2BMIN = "stratum_emit.readers:L2BMin"` in pyproject).
"""
from __future__ import annotations

from collections.abc import Mapping

from stratum.plugins import registered, resolve
from stratum.types import GranuleReader

_INSTANCES: dict[tuple[str, str], GranuleReader] = {}


def reader_ref(collection: str, overrides: Mapping[str, str] | None = None) -> str:
    """The plugin ref that serves `collection`: the manifest override when one is given, else the
    entry point of that name. Unknown -> LookupError naming the collection and what IS
    registered."""
    if overrides and collection in overrides:
        return overrides[collection]
    eps = registered("reader")
    if collection in eps:
        return collection
    known = ", ".join(sorted(eps)) or "nothing"
    raise LookupError(
        f"no reader is registered for collection {collection!r}; registered under "
        f"'stratum.readers': {known}. Add an entry point or an inputs.readers override "
        "(12 section 3)")


def reader_for(collection: str, overrides: Mapping[str, str] | None = None) -> GranuleReader:
    """One reader instance per (collection, ref) per process. Readers hold no per-file state -
    that lives in the ReaderContext - so sharing an instance is safe."""
    ref = reader_ref(collection, overrides)
    key = (collection, ref)
    if key not in _INSTANCES:
        cls = resolve("reader", ref)
        _INSTANCES[key] = cls()
    return _INSTANCES[key]


def clear_cache() -> None:
    """Drop the per-process instances (tests, or after an override changes)."""
    _INSTANCES.clear()
