"""Epochs and delivery periods. Spec: docs/specs/11-types.md section 4, 09 section 2.

An epoch is one vote; a delivery period is one product, reduced from the epochs inside its
window. Both are generated from `time.start` by calendar arithmetic, half-open, UTC.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from stratum.types import Epoch

Align = Literal["exact", "center", "trailing", "leading"]
EpochAlign = Literal["start", "calendar"]

_DURATION = re.compile(
    r"^P(?:(?P<years>\d+)Y)?(?:(?P<months>\d+)M)?(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?$"
)


@dataclass(frozen=True)
class Duration:
    """A calendar duration: whole years, months and days (11 section 4).

    Years and months are added on the calendar, so `P1M` from 31 January lands on 28 or 29
    February - the day of month is clamped to the target month's length. Days are added after
    the calendar step. Weeks parse to days (`P1W` == `P7D`).
    """

    years: int = 0
    months: int = 0
    days: int = 0

    def __post_init__(self) -> None:
        if min(self.years, self.months, self.days) < 0:
            raise ValueError("a Duration is non-negative")
        if self.years == self.months == self.days == 0:
            raise ValueError("a Duration must be positive (P0D has no epochs)")

    @property
    def total_months(self) -> int:
        return self.years * 12 + self.months

    def add(self, dt: datetime, n: int = 1) -> datetime:
        """`dt` plus `n` of this duration, computed in one step from `dt` so the month-end clamp
        never compounds: 31 Jan + 2 x P1M is 31 Mar, not 28 Mar."""
        months = self.total_months * n
        if months:
            total = dt.year * 12 + (dt.month - 1) + months
            year, month = divmod(total, 12)
            month += 1
            day = min(dt.day, calendar.monthrange(year, month)[1])
            dt = dt.replace(year=year, month=month, day=day)
        return dt + timedelta(days=self.days * n)

    def ratio(self, other: Duration) -> int | None:
        """The whole k such that self == k * other, else None. Month-based and day-based
        durations are incommensurable: P1M is never a multiple of P1D."""
        if other.total_months:
            k, rem = divmod(self.total_months, other.total_months)
            if rem or k < 1 or self.days != k * other.days:
                return None
            return k
        if self.total_months:
            return None
        k, rem = divmod(self.days, other.days)
        return None if rem or k < 1 else k

    def multiple_of(self, other: Duration) -> bool:
        return self.ratio(other) is not None

    def __str__(self) -> str:
        parts = [f"{v}{u}" for v, u in ((self.years, "Y"), (self.months, "M"), (self.days, "D"))
                 if v]
        return "P" + "".join(parts)


def parse_duration(text: str | Duration) -> Duration:
    """ISO 8601 date periods: P#Y, P#M, P#W, P#D and combinations. No time components."""
    if isinstance(text, Duration):
        return text
    m = _DURATION.match(str(text).strip()) if text else None
    if not m or not any(m.groupdict().values()):
        raise ValueError(f"not an ISO 8601 date period: {text!r} (expected e.g. P1M, P13M, P1Y)")
    g = {k: int(v) if v else 0 for k, v in m.groupdict().items()}
    return Duration(years=g["years"], months=g["months"], days=g["days"] + 7 * g["weeks"])


def as_utc(when: datetime | date) -> datetime:
    """A date is midnight UTC; a naive datetime is taken as UTC; an aware one is converted."""
    if isinstance(when, datetime):
        return when.astimezone(UTC) if when.tzinfo else when.replace(tzinfo=UTC)
    return datetime(when.year, when.month, when.day, tzinfo=UTC)


def _calendar_anchor(start: datetime, epoch: Duration) -> datetime:
    """Floor `start` to the epoch's coarsest calendar unit (`align: calendar`, 11 section 4)."""
    if epoch.years and not epoch.months:
        return start.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if epoch.total_months:
        return start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.replace(hour=0, minute=0, second=0, microsecond=0)


def epochs_between(start: datetime, end: datetime, epoch: Duration, *,
                   align: EpochAlign = "start") -> list[Epoch]:
    """Half-open epochs covering [start, end), generated from `start` by `epoch` (11 section 4).

    The last epoch is truncated at `end`. With `align="calendar"` the lattice is anchored on the
    calendar unit instead, so the first epoch is truncated at `start` as well.
    """
    start, end = as_utc(start), as_utc(end)
    if end <= start:
        raise ValueError(f"time.end {end.isoformat()} must be after time.start {start.isoformat()}")
    anchor = _calendar_anchor(start, epoch) if align == "calendar" else start
    out: list[Epoch] = []
    i = 0
    while True:
        s, e = epoch.add(anchor, i), epoch.add(anchor, i + 1)
        i += 1
        if e <= start:
            continue
        if s >= end:
            break
        out.append(Epoch(max(s, start), min(e, end)))
    return out


@dataclass(frozen=True)
class DeliveryPeriod:
    """One product: the period it is delivered for, and the epochs its window draws on."""

    start: datetime
    end: datetime
    epochs: tuple[Epoch, ...]

    @property
    def bounds(self) -> tuple[str, str]:
        return (self.start.isoformat(), self.end.isoformat())


def check_delivery(epoch: Duration, every: Duration, window: Duration, align: Align) -> None:
    """The plan-time rules of 09 section 5: every and window are whole multiples of epoch,
    window >= every, and align is exact only when window == every."""
    ke, kw = every.ratio(epoch), window.ratio(epoch)
    if ke is None:
        raise ValueError(f"deliver.every {every} is not a whole multiple of epoch {epoch}")
    if kw is None:
        raise ValueError(f"deliver.window {window} is not a whole multiple of epoch {epoch}")
    if kw < ke:
        raise ValueError(f"deliver.window {window} is shorter than deliver.every {every}")
    if align == "exact" and kw != ke:
        raise ValueError(f"deliver.align 'exact' needs window == every; got {window} vs {every}"
                         " (use center, trailing or leading)")


def delivery_periods(start: datetime, end: datetime, epoch: Duration, every: Duration,
                     window: Duration, align: Align, *,
                     epoch_align: EpochAlign = "start") -> list[DeliveryPeriod]:
    """Products at `every`, each reduced from the `window` of epochs placed by `align`.

    All placement is in whole epochs, so a window always holds entire epochs. `center` puts
    the surplus epochs half before and half after the period, the odd one after. Windows are
    truncated at `start`/`end`, so edge products carry fewer epochs (11 section 4).
    """
    check_delivery(epoch, every, window, align)
    epochs = epochs_between(start, end, epoch, align=epoch_align)
    ke, kw = every.ratio(epoch), window.ratio(epoch)
    assert ke is not None and kw is not None
    n = len(epochs)
    out: list[DeliveryPeriod] = []
    for k in range(0, n, ke):
        p0, p1 = k, min(k + ke, n)
        if align == "exact" or align == "leading":
            w0, w1 = p0, p0 + kw
        elif align == "trailing":
            w0, w1 = p0 + ke - kw, p0 + ke
        else:  # center
            before = (kw - ke) // 2
            w0, w1 = p0 - before, p0 - before + kw
        w0, w1 = max(w0, 0), min(w1, n)
        out.append(DeliveryPeriod(epochs[p0].start, epochs[p1 - 1].end, tuple(epochs[w0:w1])))
    return out
