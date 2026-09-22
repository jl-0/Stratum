"""Terminal rendering for the report the plan stage writes.

`report.md` is an artifact and stays markdown: it is written into a run directory, read later and
diffed. What was ugly is echoing it raw to a terminal - unrendered pipe tables, sha256 at full
length, absolute paths wider than the window.

Rendering is `rich`. The one thing it does not know about is our values: a sha256 and an absolute
path are noise in a terminal and meaningful in the file, so `abbreviate` shortens them on the way
to the screen only.

Plain output whenever stdout is not a terminal, `NO_COLOR` is set, or `--plain` is passed. That is
a contract, not a nicety: pipes, CI and Lambda logs get exactly the bytes they got before this
module existed, and `tests/test_console.py` holds it.
"""
from __future__ import annotations

import io
import os
import re
import sys
from collections.abc import Iterable, Sequence

_PLAIN = False


def set_plain(plain: bool) -> None:
    """`--plain`: force unstyled output even on a terminal."""
    global _PLAIN
    _PLAIN = plain


def styled() -> bool:
    """True when it is safe and useful to emit styled output."""
    if _PLAIN or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def abbreviate(value: str, keep: int = 12) -> str:
    """`sha256:<64 hex>` to a readable prefix, a long absolute path to its last two segments.
    The report keeps both in full; only the terminal view is shortened."""
    m = re.fullmatch(r"(sha\d+):([0-9a-f]{16,})", value)
    if m:
        return f"{m.group(1)}:{m.group(2)[:keep]}…"
    if "/" in value and len(value) > 48:
        parts = value.rstrip("/").split("/")
        return "…/" + "/".join(parts[-2:]) if len(parts) > 2 else value
    return value


def _console(width: int | None = None):
    """A console that renders to a STRING. Everything in this module that returns `str` uses it;
    it writes nowhere, so it must never be handed to a log handler or a progress bar."""
    from rich.console import Console
    return Console(file=io.StringIO(), force_terminal=True, width=width or _width(),
                   soft_wrap=False, highlight=False)


_OUT = None


def out_console():
    """The one console that actually writes to the terminal, shared by the progress bar and the
    log handler.

    Sharing matters: rich coordinates a live display with anything printed through the same
    console, so a warning arriving mid-stage is drawn ABOVE the bar instead of tearing through
    it. Two consoles - or a log handler on stderr and a bar on stdout - garble each other.
    """
    global _OUT
    if _OUT is None:
        from rich.console import Console
        _OUT = Console(width=_width(), soft_wrap=False, highlight=False)
    return _OUT


def _width(default: int = 100) -> int:
    import shutil
    return min(shutil.get_terminal_size((default, 24)).columns, 120)


def _render(renderable) -> str:
    con = _console()
    con.print(renderable)
    return con.file.getvalue()


def render_markdown(text: str) -> str:
    """The report, rendered. Backticked spans are abbreviated first - that is the part rich
    cannot do, because it does not know a digest from any other code span."""
    if not styled():
        return text
    from rich.markdown import Markdown
    shortened = re.sub(r"`([^`]+)`", lambda m: f"`{abbreviate(m.group(1))}`", text)
    return _render(Markdown(shortened))


def summary_table(rows: Iterable[Sequence[object]], head: Sequence[str]) -> str:
    rows = [[abbreviate(str(c)) for c in r] for r in rows]
    if not styled():
        widths = [max(len(str(r[i])) for r in [list(head), *rows]) for i in range(len(head))]
        out = ["  ".join(h.ljust(w) for h, w in zip(head, widths)).rstrip()]
        out += ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
        return "\n".join(out)
    from rich.table import Table
    table = Table(show_edge=False, box=None, pad_edge=False)
    for i, h in enumerate(head):
        table.add_column(h, justify="right" if i else "left", style="" if i else "bold")
    for r in rows:
        table.add_row(*r)
    return _render(table).rstrip("\n")


def rule(label: str = "") -> str:
    if not styled():
        return f"== {label} ==" if label else "=" * 40
    from rich.rule import Rule
    return _render(Rule(label, align="left", style="dim")).rstrip("\n")


def _style(text: str, style: str) -> str:
    if not styled():
        return text
    from rich.text import Text
    return _render(Text(text, style=style)).rstrip("\n")


def ok(text: str) -> str:
    return _style(text, "green")


def warn(text: str) -> str:
    return _style(text, "yellow")


def fail(text: str) -> str:
    return _style(text, "red")


def emphasis(text: str) -> str:
    return _style(text, "bold cyan")


def configure_logging(plain: bool = False, verbose: bool = False) -> None:
    """Give `logging` somewhere to go, so log records look like the rest of the output.

    Without this, nothing configures logging at all and records fall through to
    `logging.lastResort`: a bare `StreamHandler` on stderr, unformatted, fixed at WARNING. That
    is why a styled progress bar used to be followed by an unstyled warning - two output systems,
    one of them unconfigured - and why every `log.info` in the codebase was invisible.

    Handlers go on the `stratum` logger rather than the root, so a dependency's logging (botocore
    especially) is not adopted along with ours. Idempotent: calling it twice does not double up.
    """
    import logging

    root = logging.getLogger("stratum")
    root.setLevel(logging.INFO if verbose else logging.WARNING)
    root.propagate = False
    for existing in list(root.handlers):
        root.removeHandler(existing)
    if plain or not styled():
        handler: logging.Handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    else:
        from rich.logging import RichHandler
        handler = RichHandler(console=out_console(), show_path=False, show_time=False,
                              markup=False, rich_tracebacks=True)
        handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)


def progress(description: str, total: int):
    """A progress bar over one stage's work items, or a no-op when output is plain.

    Returns a context manager yielding `advance()`. The executors call it as each item finishes,
    which is the difference between a silent three-minute fan-out and a visible one.

    NOT transient: the finished bar stays on screen. It used to erase itself, which looked like
    a glitch - pretty output that flashed and vanished, leaving plain text behind - and threw
    away the one record of how long a stage took and how many items it covered.
    """
    import contextlib
    if not styled() or total <= 0:
        @contextlib.contextmanager
        def _noop():
            yield lambda: None
        return _noop()

    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    @contextlib.contextmanager
    def _bar():
        with Progress(SpinnerColumn(), TextColumn("[bold]{task.description}"), BarColumn(),
                      MofNCompleteColumn(), TimeElapsedColumn(), transient=False,
                      console=out_console()) as prog:
            task = prog.add_task(description, total=total)
            yield lambda: prog.advance(task)
    return _bar()
