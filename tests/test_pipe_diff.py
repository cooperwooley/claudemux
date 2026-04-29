"""Unit tests for ``_compute_new_text`` (pipe.py).

These tests are pure-Python and do not require Discord, tmux, or asyncio.
"""

from __future__ import annotations

import logging

import pytest

from discord_claude.pipe import (
    _DIFF_MIN_OVERLAP,
    _compute_new_text,
)


def test_identical_returns_empty() -> None:
    """Same content on both sides yields no append."""
    text = "hello\nworld\n"
    assert _compute_new_text(text, text) == ""


def test_shrink_returns_empty() -> None:
    """If curr is shorter than prev, treat as TUI redraw (no new content)."""
    prev = "line one\nline two\nline three\n"
    curr = "line one\n"  # spinner collapsed, status line removed, etc.
    assert _compute_new_text(prev, curr) == ""


def test_prefix_append_returns_suffix() -> None:
    """Pure append case: curr starts with prev, return the new tail.

    Trailing blank-line padding is normalized off both sides, so the returned
    suffix begins with the newline that joins the old content to the new line.
    The caller is responsible for ``lstrip("\\n")`` at page boundaries.
    """
    prev = "alpha\nbeta\n"
    curr = "alpha\nbeta\ngamma\n"
    assert _compute_new_text(prev, curr) == "\ngamma"


def test_empty_prev_returns_full_curr() -> None:
    """First poll: prev is empty, append everything."""
    curr = "first line\nsecond line\n"
    assert _compute_new_text("", curr) == "first line\nsecond line"


def test_suffix_overlap_after_repaint() -> None:
    """Classic TUI repaint: the bottom of prev re-appears mid-curr; the diff
    is everything in curr after that overlap.

    The returned suffix may begin with a newline (the one that joined the
    overlap to the new content) — callers strip it at page boundaries.
    """
    # Build an overlap that comfortably exceeds _DIFF_MIN_OVERLAP.
    overlap = "the quick brown fox jumps over the lazy dog\n" * 2
    prev = "earlier output that scrolled away\n" + overlap
    new_tail = "freshly produced line A\nfreshly produced line B\n"
    curr = overlap + new_tail
    result = _compute_new_text(prev, curr)
    # Strip a possible leading newline for the content assertion; the helper
    # is allowed to include it (depends on where the overlap match lands).
    assert result.lstrip("\n") == "freshly produced line A\nfreshly produced line B"


def test_no_overlap_logs_warning_and_returns_curr(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If no suffix of prev (>= min overlap) matches anywhere in curr,
    fall back to appending the full snapshot and log a WARN.
    """
    prev = "A" * 200
    curr = "B" * 200  # no overlap at all
    with caplog.at_level(logging.WARNING, logger="discord_claude.pipe"):
        result = _compute_new_text(prev, curr)
    assert result == curr
    assert any(
        "No suffix overlap" in rec.message and rec.levelno == logging.WARNING
        for rec in caplog.records
    )


def test_repeated_prompt_does_not_false_match() -> None:
    """Two `$ ` prompts must not anchor a tiny overlap that drops content
    between them. With min overlap = 32, the short ``$ `` shouldn't match.

    prev ends with prompt; curr ends with prompt again preceded by useful output.
    The function must not "find" the prompt in the middle of curr and drop the
    output before it.
    """
    prev = "running command\noutput line 1\noutput line 2\n$ "
    curr = (
        "running command\noutput line 1\noutput line 2\n$ "
        "next-command\nthat-produced-output\n$ "
    )
    result = _compute_new_text(prev, curr)
    # The legitimate prefix-append path applies here — curr starts with prev.
    assert result.startswith("next-command")
    assert "that-produced-output" in result
    # And the ``$ `` token by itself (length 2) must never act as a usable anchor.
    assert _DIFF_MIN_OVERLAP > len("$ ")


def test_trailing_blank_line_padding_no_diff() -> None:
    """tmux pads pane to its row height with blank lines. Pure-padding
    differences must not register as new content.
    """
    body = "alpha\nbeta\ngamma"
    prev = body + "\n\n\n\n\n\n"  # padded to pane height
    curr = body + "\n"             # different padding, same content
    assert _compute_new_text(prev, curr) == ""


def test_short_overlap_below_min_treated_as_no_overlap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An overlap shorter than _DIFF_MIN_OVERLAP must not anchor; the
    helper falls back to the full-curr append + warning path.
    """
    # Build prev/curr where the only common substring is shorter than the floor
    # AND curr does not start with prev (so prefix path can't apply).
    short_common = "xy"  # 2 chars, well under the 32-char floor
    prev = "P" * 100 + short_common
    curr = "Q" * 100 + short_common + "Z" * 50
    with caplog.at_level(logging.WARNING, logger="discord_claude.pipe"):
        result = _compute_new_text(prev, curr)
    # We append the full curr because no usable overlap exists.
    assert result == curr
    assert any("No suffix overlap" in rec.message for rec in caplog.records)
