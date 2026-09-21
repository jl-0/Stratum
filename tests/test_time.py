"""Spec 11 section 4 and 09 section 5 in executable form: durations, epochs, delivery periods."""

from __future__ import annotations

from datetime import UTC, date, datetime
from itertools import pairwise

import pytest

from stratum.time import (
    Duration,
    as_utc,
    check_delivery,
    delivery_periods,
    epochs_between,
    is_partial,
    parse_duration,
)


def d(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_parse_duration_forms():
    assert parse_duration("P13M") == Duration(months=13)
    assert parse_duration("P1Y") == Duration(years=1)
    assert parse_duration("P1W") == Duration(days=7)
    assert parse_duration("P1Y2M3D") == Duration(years=1, months=2, days=3)
    assert str(parse_duration("P13M")) == "P13M"
    for bad in ("P", "1M", "PT1H", "P0D", ""):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_ratio_and_multiple_of():  # 09 section 5
    assert Duration(years=1).ratio(Duration(months=1)) == 12
    assert Duration(months=13).ratio(Duration(months=1)) == 13
    assert Duration(months=13).ratio(Duration(months=2)) is None
    assert Duration(days=14).multiple_of(Duration(days=7))
    assert not Duration(months=1).multiple_of(Duration(days=1))  # incommensurable
    assert not Duration(months=1).multiple_of(Duration(months=2))  # shorter, not a multiple
    assert Duration(months=2, days=2).ratio(Duration(months=1, days=1)) == 2


def test_month_add_clamps_and_does_not_compound():
    jan31 = d(2023, 1, 31)
    m = Duration(months=1)
    assert m.add(jan31) == d(2023, 2, 28)
    assert m.add(jan31, 2) == d(2023, 3, 31)  # from Jan 31, not from Feb 28
    assert m.add(d(2024, 1, 31)) == d(2024, 2, 29)  # leap year
    assert Duration(years=1).add(d(2024, 2, 29)) == d(2025, 2, 28)


def test_epochs_are_half_open_from_start_and_truncate_at_end():  # 11 section 4
    ep = epochs_between(d(2022, 8, 1), d(2026, 7, 31), Duration(months=1))
    assert len(ep) == 48
    assert (ep[0].start, ep[0].end) == (d(2022, 8, 1), d(2022, 9, 1))
    assert ep[-1].end == d(2026, 7, 31)  # truncated
    for a, b in pairwise(ep):
        assert a.end == b.start  # no gap, no overlap
    assert ep[0].contains(d(2022, 8, 31, 23, 59)) and not ep[0].contains(ep[0].end)


def test_epochs_across_month_ends():
    ep = epochs_between(d(2023, 1, 31), d(2023, 6, 1), Duration(months=1))
    starts = [e.start for e in ep]
    assert starts == [
        d(2023, 1, 31),
        d(2023, 2, 28),
        d(2023, 3, 31),
        d(2023, 4, 30),
        d(2023, 5, 31),
    ]
    assert ep[-1].end == d(2023, 6, 1)


def test_calendar_alignment_truncates_first_epoch():
    ep = epochs_between(d(2023, 1, 15), d(2023, 3, 10), Duration(months=1), align="calendar")
    assert [(e.start, e.end) for e in ep] == [
        (d(2023, 1, 15), d(2023, 2, 1)),
        (d(2023, 2, 1), d(2023, 3, 1)),
        (d(2023, 3, 1), d(2023, 3, 10)),
    ]


def test_epochs_accept_dates_and_naive_datetimes():
    assert as_utc(date(2022, 8, 1)) == d(2022, 8, 1)
    naive = datetime(2022, 8, 1, 12)  # noqa: DTZ001 - naive input is what is under test
    assert as_utc(naive) == d(2022, 8, 1, 12)
    with pytest.raises(ValueError):
        epochs_between(d(2022, 8, 1), d(2022, 8, 1), Duration(months=1))


def test_delivery_rules():  # 09 section 5
    m1, m13, y1 = Duration(months=1), Duration(months=13), Duration(years=1)
    check_delivery(m1, y1, y1, "exact")
    check_delivery(m1, m1, m13, "center")
    # An epoch that does not divide the delivery is allowed when every period is the SAME
    # fixed window - the straggler is dropped (09 section 5). P2M into a P13M window gives six
    # whole epochs covering 12 of its 13 months.
    check_delivery(Duration(months=2), m13, m13, "exact")
    # ... but a ROLLING window would slide by a non-whole number of epochs, so no two products
    # would carry comparable evidence. That is still refused, and the message says why.
    with pytest.raises(ValueError, match="rolling window"):
        check_delivery(Duration(months=2), m1, m13, "center")
    with pytest.raises(ValueError, match="not a whole multiple"):
        check_delivery(Duration(days=7), y1, y1, "center")
    with pytest.raises(ValueError, match="shorter than"):
        check_delivery(m1, y1, m1, "trailing")
    with pytest.raises(ValueError, match="exact"):
        check_delivery(m1, m1, m13, "exact")


def test_annual_delivery_is_shorthand_for_exact():
    """This window ends 2026-07-31, one day shy of a clean 48 months, so the final July is a
    30-day epoch. It is DROPPED rather than allowed to vote on less evidence than the other 47
    (`is_partial`), which is why the last period carries 11. The fix is `time.end`, and
    `stratum plan` says so - see test_a_partial_epoch_is_dropped_and_reported."""
    m1, y1 = Duration(months=1), Duration(years=1)
    periods = delivery_periods(d(2022, 8, 1), d(2026, 7, 31), m1, y1, y1, "exact")
    assert len(periods) == 4
    assert periods[0].bounds == ("2022-08-01T00:00:00+00:00", "2023-08-01T00:00:00+00:00")
    assert [len(p.epochs) for p in periods] == [12, 12, 12, 11]
    assert periods[-1].end == d(2026, 7, 1)  # the last WHOLE epoch's end
    assert periods[-1].epochs[-1].end == d(2026, 7, 1)


def test_a_weekly_epoch_delivers_annually_by_dropping_the_straggler():
    """The case this was built for: a year is 52 weeks and a day, so counting epochs cannot
    place the period. It is placed in time instead and given the whole weeks inside it."""
    w1, y1 = Duration(days=7), Duration(years=1)
    all_weeks = epochs_between(d(2025, 1, 1), d(2026, 1, 1), w1)
    whole = epochs_between(d(2025, 1, 1), d(2026, 1, 1), w1, drop_partial=True)
    assert len(all_weeks) == 53 and len(whole) == 52
    assert is_partial(w1, all_weeks[-1]) and not any(is_partial(w1, e) for e in whole)

    periods = delivery_periods(d(2025, 1, 1), d(2026, 1, 1), w1, y1, y1, "exact")
    assert len(periods) == 1
    assert len(periods[0].epochs) == 52
    assert periods[0].epochs[0].start == d(2025, 1, 1)
    assert periods[0].epochs[-1].end == d(2025, 12, 31)  # the 53rd week is not in the product


def test_a_partial_epoch_is_dropped_and_reported():
    """Dropping silently would be the bug. `Manifest.epochs()` excludes it so resolve does no
    work reduce cannot read, and `partial_epochs()` is what plan reports."""
    from stratum.time import Epoch

    m1 = Duration(months=1)
    whole = epochs_between(d(2022, 8, 1), d(2026, 7, 31), m1, drop_partial=True)
    every = epochs_between(d(2022, 8, 1), d(2026, 7, 31), m1)
    assert len(every) == 48 and len(whole) == 47
    dropped = [e for e in every if e not in set(whole)]
    assert len(dropped) == 1
    assert dropped[0] == Epoch(d(2026, 7, 1), d(2026, 7, 31))


def test_rolling_13_month_window_delivered_monthly_centered():  # 11 section 4 table
    m1, m13 = Duration(months=1), Duration(months=13)
    periods = delivery_periods(d(2022, 1, 1), d(2024, 1, 1), m1, m1, m13, "center")
    assert len(periods) == 24
    mid = periods[12]  # Jan 2023
    assert (mid.start, mid.end) == (d(2023, 1, 1), d(2023, 2, 1))
    assert len(mid.epochs) == 13
    assert mid.epochs[0].start == d(2022, 7, 1)  # Jul 2022 .. Jul 2023 inclusive
    assert mid.epochs[-1].end == d(2023, 8, 1)
    assert len(periods[0].epochs) == 7  # truncated at start: self + 6 after
    assert len(periods[-1].epochs) == 7  # truncated at end: 6 before + self
    assert all(p.epochs[0].start >= d(2022, 1, 1) for p in periods)


def test_trailing_and_leading_windows():
    m1 = Duration(months=1)
    trailing = delivery_periods(
        d(2022, 1, 1), d(2022, 7, 1), m1, m1, Duration(months=3), "trailing"
    )
    assert [len(p.epochs) for p in trailing] == [1, 2, 3, 3, 3, 3]
    assert trailing[3].epochs[-1].end == trailing[3].end
    leading = delivery_periods(d(2022, 1, 1), d(2022, 7, 1), m1, m1, Duration(months=3), "leading")
    assert [len(p.epochs) for p in leading] == [3, 3, 3, 3, 2, 1]
    assert leading[0].epochs[0].start == leading[0].start


def test_partial_last_period_when_every_does_not_divide_range():
    m1 = Duration(months=1)
    periods = delivery_periods(
        d(2022, 1, 1), d(2022, 5, 1), m1, Duration(months=3), Duration(months=3), "exact"
    )
    assert [len(p.epochs) for p in periods] == [3, 1]
    assert periods[1].bounds == ("2022-04-01T00:00:00+00:00", "2022-05-01T00:00:00+00:00")
