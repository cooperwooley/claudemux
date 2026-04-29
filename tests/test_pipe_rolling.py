"""Async unit tests for the rolling-page output model in ``SessionPipe``.

These tests use a stub Discord channel + message and only exercise
``_append_to_active`` / ``_flush_active`` / ``_finalize_turn``. They do not
spin up the real Discord client or tmux.
"""

from __future__ import annotations

import pytest

from discord_claude.pipe import MAX_BODY, SessionPipe


class _StubMessage:
    """Stand-in for ``discord.Message``. Tracks edits in order."""

    _next_id: int = 1

    def __init__(self, content: str) -> None:
        self.id = _StubMessage._next_id
        _StubMessage._next_id += 1
        self.content = content
        self.edits: list[str] = []

    async def edit(self, *, content: str) -> None:
        self.content = content
        self.edits.append(content)


class _StubChannel:
    """Stand-in for ``discord.TextChannel``. Records sends in order."""

    def __init__(self) -> None:
        self.id = 12345
        self.name = "stub-channel"
        self.sent: list[_StubMessage] = []
        self.ping_sends: list[str] = []

    async def send(self, content: str, *, silent: bool = False) -> _StubMessage:
        # Detect ping sends (no code-block fence) so finalize-turn can be
        # asserted separately from page sends.
        if not content.startswith("```"):
            self.ping_sends.append(content)
        msg = _StubMessage(content)
        self.sent.append(msg)
        return msg


def _make_pipe(channel: _StubChannel, *, notify: frozenset[int] = frozenset()) -> SessionPipe:
    """Build a SessionPipe with a stub channel and a no-op manager."""
    pipe = SessionPipe.__new__(SessionPipe)
    pipe.session_name = "test-session"
    pipe.channel = channel  # type: ignore[assignment]
    pipe.manager = None  # type: ignore[assignment]
    pipe.poll_interval = 1.0
    pipe.quiet_timeout = 3.0
    pipe._notify_user_ids = notify
    pipe._last_snapshot = ""
    pipe._transcript = ""
    pipe._active_page = None
    pipe._active_page_text = ""
    pipe._last_change = 0.0
    pipe._input_queue = None  # type: ignore[assignment]
    pipe._poll_task = None
    pipe._input_task = None
    pipe._stopped = False
    return pipe


@pytest.mark.asyncio
async def test_short_append_uses_single_page() -> None:
    """A small append should send once then edit in place — no roll."""
    channel = _StubChannel()
    pipe = _make_pipe(channel)

    await pipe._append_to_active("hello world")
    await pipe._append_to_active("\nmore content")

    # Exactly one Discord message was created.
    assert len(channel.sent) == 1
    msg = channel.sent[0]
    # Initial send + one edit (the second append).
    assert len(msg.edits) == 1
    assert "hello world" in msg.content
    assert "more content" in msg.content
    # Wrapped in a code-block fence.
    assert msg.content.startswith("```\n")
    assert msg.content.endswith("\n```")


@pytest.mark.asyncio
async def test_long_append_splits_into_multiple_pages() -> None:
    """A >5 KB delta must produce multiple pages, not overwrite earlier ones."""
    channel = _StubChannel()
    pipe = _make_pipe(channel)

    # ~6 KB of content, mostly distinct lines so we can verify nothing was lost.
    lines = [f"line-{i:04d}-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" for i in range(120)]
    body = "\n".join(lines)
    assert len(body) > 5 * 1024

    await pipe._append_to_active(body)

    # Multiple pages must have been created (each well under MAX_BODY).
    assert len(channel.sent) >= 2
    for msg in channel.sent:
        assert msg.content.startswith("```\n")
        assert msg.content.endswith("\n```")
        # The body inside the fence must respect MAX_BODY.
        body_only = msg.content[len("```\n"):-len("\n```")]
        assert len(body_only) <= MAX_BODY

    # Concatenating all page bodies (with newlines) must contain every
    # original line — no truncation, no '...'.
    all_bodies = "\n".join(
        m.content[len("```\n"):-len("\n```")] for m in channel.sent
    )
    for line in lines:
        assert line in all_bodies, f"missing line {line!r}"


@pytest.mark.asyncio
async def test_split_prefers_last_newline_before_limit() -> None:
    """Page splits must occur at the last newline before MAX_BODY."""
    channel = _StubChannel()
    pipe = _make_pipe(channel)

    # Build a body whose total length exceeds MAX_BODY but whose last newline
    # before MAX_BODY is at a known offset.
    chunk = "X" * 200 + "\n"          # 201 chars per "row"
    rows = [chunk] * 12               # 2412 chars total — exceeds MAX_BODY
    body = "".join(rows)
    assert len(body) > MAX_BODY

    await pipe._append_to_active(body)

    assert len(channel.sent) >= 2
    first_body = channel.sent[0].content[len("```\n"):-len("\n```")]
    # The split should land on a newline boundary, so the first page must end
    # without truncating mid-row (the body before the cut ends with a row).
    assert first_body.endswith("X" * 200)
    # And no row is truncated mid-X.
    for msg in channel.sent:
        body_only = msg.content[len("```\n"):-len("\n```")]
        for ln in body_only.split("\n"):
            if ln:  # skip blank
                assert ln == "X" * 200, f"row was split mid-line: {ln!r}"


@pytest.mark.asyncio
async def test_split_falls_back_to_byte_boundary_when_no_newline() -> None:
    """If the tail has no newline, split at the byte boundary."""
    channel = _StubChannel()
    pipe = _make_pipe(channel)

    # No newlines anywhere — must still split.
    body = "Y" * (MAX_BODY * 2 + 50)
    await pipe._append_to_active(body)

    assert len(channel.sent) >= 2
    for msg in channel.sent:
        body_only = msg.content[len("```\n"):-len("\n```")]
        assert len(body_only) <= MAX_BODY


@pytest.mark.asyncio
async def test_finalize_turn_sends_ping_and_resets() -> None:
    """Finalize sends a single ping in a trailing message and resets state."""
    channel = _StubChannel()
    pipe = _make_pipe(channel, notify=frozenset({111, 222}))

    await pipe._append_to_active("hello\nworld")
    assert len(channel.sent) == 1  # one page created

    await pipe._finalize_turn()

    # The ping is a separate trailing message (no code-block fence).
    assert len(channel.ping_sends) == 1
    ping = channel.ping_sends[0]
    assert "<@111>" in ping
    assert "<@222>" in ping

    # Per-turn state was reset.
    assert pipe._active_page is None
    assert pipe._active_page_text == ""
    assert pipe._transcript == ""
    assert pipe._last_change == 0.0


@pytest.mark.asyncio
async def test_finalize_turn_no_active_page_no_ping() -> None:
    """Finalize on an empty turn must NOT send a ping (or anything)."""
    channel = _StubChannel()
    pipe = _make_pipe(channel, notify=frozenset({333}))

    await pipe._finalize_turn()
    assert channel.sent == []
    assert channel.ping_sends == []


@pytest.mark.asyncio
async def test_finalize_does_not_clear_last_snapshot() -> None:
    """Next turn's diff must still anchor on the prior pane content."""
    channel = _StubChannel()
    pipe = _make_pipe(channel)

    pipe._last_snapshot = "previously visible content"
    await pipe._append_to_active("page text")
    await pipe._finalize_turn()

    assert pipe._last_snapshot == "previously visible content"


@pytest.mark.asyncio
async def test_active_page_edited_not_recreated_within_limit() -> None:
    """Successive small appends edit the same Discord message, not new ones."""
    channel = _StubChannel()
    pipe = _make_pipe(channel)

    for i in range(5):
        await pipe._append_to_active(f"chunk-{i}\n")

    assert len(channel.sent) == 1
    # Initial send + 4 edits.
    assert len(channel.sent[0].edits) == 4


@pytest.mark.asyncio
async def test_flush_active_skips_whitespace_only() -> None:
    """A page body of only whitespace must not produce a Discord send."""
    channel = _StubChannel()
    pipe = _make_pipe(channel)

    pipe._active_page_text = "   \n\n  "
    await pipe._flush_active(final=False)

    assert channel.sent == []
