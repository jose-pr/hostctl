"""The stream helpers a third-party executor needs are public.

`hostctl/AGENTS.md` states that only names in `__all__` are stable and anything
else "may change without notice".  An executor implemented outside hostctl --
pytruenas 0.4.0's web shell is the real case -- must reproduce hostctl's own
stdout/stderr and stdin semantics exactly, because a `SystemHost` can dispatch
the same call through different providers on different attempts.  Providers
that disagree about output handling produce results that differ by which
transport happened to win.

That argument is already written into `normalize_input`'s docstring as the
reason every executor must share it.  These tests pin the corollary: sharing is
only possible if the helpers are reachable without importing a private module.
"""

from __future__ import annotations

import io

import hostctl.executor as executor


def test_stream_helpers_are_exported():
    """All four helpers are public, not just `capture_streams`.

    Regression test for a real gap: `capture_streams` was exported while
    `write_output`, `normalize_input`, and `dispatch_output` -- its neighbours
    in `_common.py`, used on the same call path -- were not, so consumers
    imported them from `hostctl.executor._common`.
    """

    for name in (
        "capture_streams",
        "dispatch_output",
        "normalize_input",
        "write_output",
    ):
        assert name in executor.__all__, f"{name} is missing from __all__"
        assert callable(getattr(executor, name)), f"{name} is not importable"


def test_exported_helpers_are_the_implementations_themselves():
    """The public names must not be re-wrapped copies of the private ones.

    A shim would drift from the original, which is the outcome the shared
    helpers exist to prevent.
    """

    from hostctl.executor import _common

    for name in (
        "capture_streams",
        "dispatch_output",
        "normalize_input",
        "write_output",
    ):
        assert getattr(executor, name) is getattr(_common, name)


def test_write_output_survives_a_stream_mode_mismatch():
    """Pin the bytes<->str fallback an external executor would have to clone.

    This is why reimplementing `write_output` is a trap rather than a chore:
    the fallback is invisible until a transport hands text to a binary sink.
    """

    binary = io.BytesIO()
    executor.write_output(binary, "text into a binary sink", encoding=None, errors=None)
    assert binary.getvalue() == b"text into a binary sink"

    text = io.StringIO()
    executor.write_output(text, b"bytes into a text sink", encoding=None, errors=None)
    assert text.getvalue() == "bytes into a text sink"


def test_normalize_input_matches_the_stream_mode():
    """Pin the conversion whose absence deadlocks `subprocess`.

    Handing `bytes` to a text-mode stdin kills the writer thread without
    closing the pipe, so the child never sees EOF and `timeout=` never fires.
    """

    assert executor.normalize_input(b"payload", text_mode=True) == "payload"
    assert executor.normalize_input("payload", text_mode=False) == b"payload"
    assert executor.normalize_input(None, text_mode=True) is None
