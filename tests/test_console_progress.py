"""Plain-output progress: what a detached run's log shows while a stage is under way."""
from __future__ import annotations

from stratum import console


def test_plain_progress_reports_occasionally_and_at_the_end(monkeypatch, capsys) -> None:
    """A bar is noise in a log, but a stage of thousands of items must not be silent between
    its start and its end: a line at most every `PLAIN_PROGRESS_SECONDS`, and one at the last
    item whatever the time."""
    import time

    monkeypatch.setattr(console, "styled", lambda: False)
    clock = iter([0.0, 10.0, 31.0, 40.0, 45.0])       # started, then one reading per item
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    with console.progress("resolve", 4, lines=True) as advance:
        for _ in range(4):
            advance()
    captured = capsys.readouterr()
    assert captured.err.splitlines() == ["resolve: 2/4 items, 31 s", "resolve: 4/4 items, 45 s"]
    assert captured.out == "", "progress on stdout would corrupt `plan --json`"


def test_an_empty_stage_reports_nothing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(console, "styled", lambda: False)
    with console.progress("publish", 0, lines=True) as advance:
        advance()
    with console.progress("reading class tables", 3) as advance:     # not asked for: silent
        advance()
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
