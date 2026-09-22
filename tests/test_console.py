"""The terminal renderer. Spec: none - this is presentation, and the contract that matters is
that it changes NOTHING when stdout is not a terminal, so pipes, CI and Lambda logs are byte
identical to what they were before rendering existed."""
from __future__ import annotations

import pytest

from stratum import console

REPORT = """# Stratum plan report: demo-1234

- manifest: `/a/very/long/path/to/some/example/manifest.yaml`
- manifest_hash: `sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef`

## Selection

| filter | removed |
|---|---|
| meets a tile | 0 |
| has every role | 2 |
"""


@pytest.fixture(autouse=True)
def _plain_by_default():
    console.set_plain(False)
    yield
    console.set_plain(False)


def test_not_a_tty_passes_the_markdown_through_unchanged(monkeypatch):
    monkeypatch.setattr(console.sys.stdout, "isatty", lambda: False, raising=False)
    assert console.render_markdown(REPORT) == REPORT


def test_plain_flag_passes_through_even_on_a_tty(monkeypatch):
    monkeypatch.setattr(console, "styled", lambda: False)
    console.set_plain(True)
    assert console.render_markdown(REPORT) == REPORT


def test_no_color_env_disables_styling(monkeypatch):
    monkeypatch.setattr(console.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    assert console.styled() is False


def test_rendered_output_drops_pipes_and_shortens_hashes(monkeypatch):
    monkeypatch.setattr(console, "styled", lambda: True)
    out = console.render_markdown(REPORT)
    assert "|" not in out, "pipe tables become aligned columns"
    assert "0123456789abcdef0123456789abcdef" not in out, "the full digest is abbreviated"
    assert "sha256:0123456789ab" in out
    assert "meets a tile" in out and "has every role" in out, "no row is lost"


def test_abbreviate_keeps_short_values_intact():
    assert console.abbreviate("001") == "001"
    assert console.abbreviate("EMITL2BMIN") == "EMITL2BMIN"
    assert console.abbreviate("sha256:" + "a" * 64).endswith("…")
    assert console.abbreviate("/" + "x/" * 40).startswith("…/")


def test_plain_summary_table_is_aligned_and_untrailed(monkeypatch):
    monkeypatch.setattr(console, "styled", lambda: False)
    out = console.summary_table([("a", 0), ("bbbb", 12)], ("stage", "items"))
    assert all(line == line.rstrip() for line in out.splitlines())
    assert "\x1b[" not in out, "no escape codes when output is plain"


def test_progress_is_a_noop_when_plain(monkeypatch):
    monkeypatch.setattr(console, "styled", lambda: False)
    with console.progress("regrid", 10) as advance:
        advance()   # must not raise, and must not print


def test_progress_yields_an_advance_when_styled(monkeypatch):
    monkeypatch.setattr(console, "styled", lambda: True)
    with console.progress("regrid", 3) as advance:
        for _ in range(3):
            advance()


# ----------------------------------------------------------------------- logging, and the seam
# Two output systems used to run side by side: `console.*` through rich, and `log.*` through a
# `logging` that nothing configured. So a styled progress bar was followed by an unstyled
# warning, the bar erased itself on the way out, and every `log.info` was invisible.
def test_logging_is_configured_on_the_stratum_logger_and_is_idempotent():
    import logging

    console.configure_logging(plain=True, verbose=False)
    root = logging.getLogger("stratum")
    assert len(root.handlers) == 1
    assert root.level == logging.WARNING
    assert not root.propagate, "a dependency's root handler must not also print our records"

    console.configure_logging(plain=True, verbose=True)
    assert len(root.handlers) == 1, "calling twice must not stack handlers"
    assert root.level == logging.INFO, "-v surfaces the four log.info calls in the codebase"


def test_an_unconfigured_logger_would_have_dropped_info_entirely():
    """Why -v exists at all. `logging.lastResort` is fixed at WARNING, so before this every
    `log.info` went nowhere no matter what the user asked for."""
    import logging

    assert logging.lastResort.level == logging.WARNING
    assert logging.lastResort.formatter is None, "and unformatted, hence the bare line"


def test_the_bar_and_the_log_share_one_console(monkeypatch):
    """Rich only interleaves a live display with printed output when both go through the SAME
    console: a warning mid-stage is then drawn ABOVE the bar instead of tearing through it.
    `_console()` renders to a StringIO and writes nowhere, so handing it to either would have
    silently swallowed the output."""
    import io
    import logging

    assert console.out_console() is console.out_console(), "memoised, so Live can coordinate"
    assert not isinstance(console.out_console().file, io.StringIO)
    assert isinstance(console._console().file, io.StringIO)

    monkeypatch.setattr(console, "styled", lambda: True)
    console.configure_logging(plain=False, verbose=False)
    handler = logging.getLogger("stratum").handlers[0]
    assert handler.console is console.out_console()
