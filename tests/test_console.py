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
