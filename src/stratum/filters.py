"""Built-in granule filters (04 section 2, 02 section 4).

Every filter is vectorised over the index frame and never sees a granule. Missing metadata is a
declared policy - `reject | keep | fail` - never the accidental result of a truth test, and the
chain reports what each filter removed.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from stratum.hooks import GranuleFilter
from stratum.plugins import resolve

if TYPE_CHECKING:
    from stratum.manifest.models import Manifest

OnMissing = str  # "reject" | "keep" | "fail"


class FilterError(ValueError):
    """`on_missing: fail` met missing metadata, or a frame lacks what a filter needs."""


@dataclass(frozen=True)
class FilterReport:
    """One line of the run report: what the filter was, how many it removed, its policy."""

    describe: str
    removed: int
    on_missing: OnMissing | None


def _ids(frame: pd.DataFrame, mask: pd.Series) -> str:
    ids = frame["granule_id"][mask].astype(str).tolist() if "granule_id" in frame else []
    shown = ", ".join(ids[:10]) + (f" ... and {len(ids) - 10} more" if len(ids) > 10 else "")
    return shown or f"{int(mask.sum())} row(s)"


def _column(frame: pd.DataFrame, name: str) -> pd.Series:
    """The column, or an all-null series when the frame lacks it - which is 'missing'."""
    if name in frame:
        return frame[name]
    return pd.Series([None] * len(frame), index=frame.index, dtype=object)


def _decide(ok: pd.Series, missing: pd.Series, frame: pd.DataFrame, filt: GranuleFilter,
            policy: OnMissing) -> np.ndarray:
    """Apply the on_missing policy to rows whose predicate could not be evaluated."""
    if missing.any():
        if policy == "fail":
            raise FilterError(f"{filt.describe()}: {int(missing.sum())} granule(s) lack the "
                              f"metadata it needs: {_ids(frame, missing)}. Set on_missing to "
                              "reject or keep to decide for them (02 section 4)")
        ok = ok.where(~missing, policy == "keep")
    return ok.fillna(False).to_numpy(dtype=bool)


def _attribute(attrs: Any, key: str) -> Any:
    """One value from a row's `attributes`: a dict, or a list of (key, value) pairs as a
    pyarrow map arrives; anything else is missing."""
    if isinstance(attrs, Mapping):
        return attrs.get(key)
    if isinstance(attrs, (list, tuple)):
        for item in attrs:
            if isinstance(item, (list, tuple)) and len(item) == 2 and item[0] == key:
                return item[1]
    return None


class MaxCloudFraction:
    """`cloud_fraction <= threshold`; null is missing (02 section 4)."""

    def __init__(self, threshold: float, on_missing: OnMissing = "fail") -> None:
        self.threshold = float(threshold)
        self.on_missing = on_missing

    def keep(self, granules: pd.DataFrame) -> np.ndarray:
        cf = pd.to_numeric(_column(granules, "cloud_fraction"), errors="coerce")
        missing = cf.isna()
        return _decide(cf <= self.threshold, missing, granules, self, self.on_missing)

    def describe(self) -> str:
        return f"cloud_fraction <= {self.threshold:g} (on_missing: {self.on_missing})"


class MaxSolarZenith:
    """`attributes["SOLAR_ZENITH"] <= threshold`, from the source's verbatim attributes."""

    key = "SOLAR_ZENITH"

    def __init__(self, threshold: float, on_missing: OnMissing = "fail") -> None:
        self.threshold = float(threshold)
        self.on_missing = on_missing

    def keep(self, granules: pd.DataFrame) -> np.ndarray:
        raw = _column(granules, "attributes").map(lambda a: _attribute(a, self.key))
        sz = pd.to_numeric(raw, errors="coerce")
        return _decide(sz <= self.threshold, sz.isna(), granules, self, self.on_missing)

    def describe(self) -> str:
        return f"{self.key} <= {self.threshold:g} (on_missing: {self.on_missing})"


class MonthIn:
    """A recurring seasonal window on acquisition `datetime` (04 section 2)."""

    on_missing = None

    def __init__(self, months: Sequence[int]) -> None:
        self.months = tuple(sorted({int(m) for m in months}))

    def keep(self, granules: pd.DataFrame) -> np.ndarray:
        if "datetime" not in granules:
            raise FilterError("month_in: the frame has no datetime column")
        when = pd.to_datetime(granules["datetime"], utc=True)
        return when.dt.month.isin(self.months).to_numpy(dtype=bool)

    def describe(self) -> str:
        return f"month in {list(self.months)}"


class ColumnIn:
    """Equality on a string column: build_version, product_version, collection_version,
    day_night. Null is missing, and so is the empty string: the index stores an absent header
    attribute as `""` (its version columns are non-nullable, 02 section 2), and a granule
    lacking the attribute must meet `on_missing`, never a silent equality miss (02 section 4)."""

    def __init__(self, column: str, values: str | Sequence[str],
                 on_missing: OnMissing = "fail") -> None:
        self.column = column
        self.values = (values,) if isinstance(values, str) else tuple(str(v) for v in values)
        self.on_missing = on_missing

    def keep(self, granules: pd.DataFrame) -> np.ndarray:
        col = _column(granules, self.column)
        text = col.astype(str).str.strip()
        missing = col.isna() | (text == "")
        ok = text.isin(self.values)
        return _decide(ok, missing, granules, self, self.on_missing)

    def describe(self) -> str:
        vals = self.values[0] if len(self.values) == 1 else list(self.values)
        return f"{self.column} == {vals!r} (on_missing: {self.on_missing})"


def build_filters(manifest: Manifest) -> list[GranuleFilter]:
    """Instantiate `granule_filter` in manifest order. `{ref, params}` resolves through the
    plugin registry (04 section 7); the plugin's own `on_missing` is whatever it accepts."""
    out: list[GranuleFilter] = []
    for spec in manifest.granule_filter:
        p = spec.predicate
        if p == "max_cloud_fraction":
            out.append(MaxCloudFraction(spec.max_cloud_fraction, spec.policy))
        elif p == "max_solar_zenith":
            out.append(MaxSolarZenith(spec.max_solar_zenith, spec.policy))
        elif p == "month_in":
            out.append(MonthIn(spec.month_in))
        elif p == "ref":
            params = dict(spec.params)
            if spec.on_missing is not None:
                params.setdefault("on_missing", spec.on_missing)
            out.append(resolve("filter", spec.ref)(**params))
        else:  # build_version, product_version, collection_version, day_night
            out.append(ColumnIn(p, getattr(spec, p), spec.policy))
    return out


def apply_filters(frame: pd.DataFrame,
                  filters: Sequence[GranuleFilter]) -> tuple[pd.DataFrame, list[FilterReport]]:
    """Run the chain in order; each report counts what that filter removed from what reached it."""
    reports: list[FilterReport] = []
    for f in filters:
        keep = np.asarray(f.keep(frame), dtype=bool)
        if keep.shape != (len(frame),):
            raise FilterError(f"{f.describe()}: keep() returned shape {keep.shape} for "
                              f"{len(frame)} granules")
        reports.append(FilterReport(f.describe(), int((~keep).sum()),
                                    getattr(f, "on_missing", None)))
        frame = frame[keep]
    return frame, reports
