"""Bidirectional pipe between a tmux session and a Discord channel.

Handles:
- Output polling via capture-pane with diff-based change detection
- ANSI escape code stripping
- Rate-limit-safe output via a rolling-page model: edit one "active" page
  in place; when it would overflow, freeze it and start a new page so
  the full transcript stays scrollable in Discord.
- Input queuing (sequential send-keys to prevent interleaving)
- Graceful session death detection
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord

    from .session_manager import SessionManager

log = logging.getLogger(__name__)

# ── ANSI stripping ───────────────────────────────────────────────────
# Covers CSI sequences, OSC (title sets), SGR, and charset switches.
_ANSI_RE = re.compile(
    r"(\x9B|\x1B\[)[0-?]*[ -/]*[@-~]"   # CSI (includes SGR)
    r"|\x1B\][^\x07]*\x07"                # OSC (terminated by BEL)
    r"|\x1B\][^\x1B]*\x1B\\"              # OSC (terminated by ST)
    r"|\x1B[()][AB012]"                    # Charset switches
    r"|\x1B[=>NH]"                         # Misc single-char escapes
)

MAX_MSG_LEN = 1900  # leave room for code block markers + buffer

# Headroom inside a single Discord message after the surrounding code-block
# fence (```\n…\n```) is added. Splitting at MAX_BODY keeps the rendered
# message under MAX_MSG_LEN.
MAX_BODY = MAX_MSG_LEN - len("```\n\n```")  # ≈ 1892

# Cap on how much of *prev* we suffix-search against *curr* in _compute_new_text.
# 4 KB is far larger than typical single-poll deltas yet keeps the search cheap.
_DIFF_PREV_TAIL = 4096

# Minimum overlap length before we trust a suffix-of-prev / substring-of-curr match.
# A short match (e.g. "$ ") can collide with a repeated shell prompt and silently
# drop output between the two prompts; below this floor we treat as no-overlap.
_DIFF_MIN_OVERLAP = 32

# Lines made entirely of box-drawing / decoration characters (with optional whitespace)
_DECORATION_LINE = re.compile(
    r"^\s*[─━═╌╍┄┅╴╶╸╺│┃║╎╏┆┇╵╷╹╻"
    r"┌┍┎┏┐┑┒┓└┕┖┗┘┙┚┛├┝┞┟┠┡┢┣┤┥┦┧┨┩┪┫"
    r"┬┭┮┯┰┱┲┳┴┵┶┷┸┹┺┻┼┽┾┿╀╁╂╃╄╅╆╇╈╉╊╋"
    r"╔╗╚╝╠╣╦╩╬╭╮╯╰╱╲╳]+\s*$"
)

# Claude Code status bar patterns (bottom of pane)
_STATUS_PATTERNS = [
    re.compile(r"^\s*⏵⏵\s"),                        # bypass permissions indicator
    re.compile(r"^\s*❯\s*$"),                         # empty input prompt
    re.compile(r"\(shift\+tab to cycle\)"),            # mode switcher hint
    re.compile(r"\(ctrl\+o to expand\)"),              # expandable section hint
    re.compile(r"^\s*\d+[.,]\d+[kKmM]?\s+tokens"),    # token counter
    re.compile(r"^\s*claude-?\d"),                     # model name line
]


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def clean_tui_chrome(text: str) -> str:
    """Remove Claude Code TUI decorations that look bad in Discord."""
    lines = text.splitlines()
    cleaned: list[str] = []

    for line in lines:
        # Skip pure decoration lines (horizontal rules)
        if _DECORATION_LINE.match(line):
            continue
        # Skip status bar patterns
        if any(p.search(line) for p in _STATUS_PATTERNS):
            continue
        cleaned.append(line)

    result = "\n".join(cleaned)
    # Collapse runs of blank lines
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    return result.strip()


def _strip_trailing_blank_lines(text: str) -> str:
    """Drop trailing empty lines (tmux pads to pane height) without changing
    the content we care about. Keeps a single trailing newline if the original
    body was non-empty so identical content with/without padding compares equal.
    """
    stripped = text.rstrip("\n")
    if not stripped:
        return ""
    return stripped


def _compute_new_text(prev: str, curr: str) -> str:
    """Return the suffix of *curr* that should be appended to *prev*.

    Pure function — no Discord/tmux dependencies. Behavior:

    - identical (after stripping trailing blank lines) → ``""``
    - shrink (``len(curr) < len(prev)``) → ``""`` (TUI redraw, not new content)
    - empty *prev* → return *curr* (with trailing blank lines normalized)
    - prefix match (``curr.startswith(prev)``) → everything in *curr* past *prev*
    - otherwise: find the longest suffix of ``prev`` (capped to the last
      ``_DIFF_PREV_TAIL`` bytes) that also appears as a substring of *curr*;
      append everything in *curr* after that overlap.
    - the overlap must be at least ``_DIFF_MIN_OVERLAP`` chars; shorter matches
      risk colliding with a repeated shell prompt and dropping content between.
    - no overlap found → return *curr* in full and log a WARN.

    Trailing blank lines on either side are ignored for diff purposes (tmux
    pads to pane height, which would otherwise cause spurious diffs). The
    returned suffix may begin with a newline; callers that split across page
    boundaries should ``lstrip("\\n")`` at the boundary, not on every append.
    """
    prev_n = _strip_trailing_blank_lines(prev)
    curr_n = _strip_trailing_blank_lines(curr)

    if curr_n == prev_n:
        return ""
    if len(curr_n) < len(prev_n):
        return ""
    if not prev_n:
        return curr_n
    if curr_n.startswith(prev_n):
        return curr_n[len(prev_n):]

    # Suffix-overlap search: try the longest suffix of prev (up to the last
    # _DIFF_PREV_TAIL bytes) that occurs anywhere in curr; everything in curr
    # after the *last* such occurrence is the new content.
    tail = prev_n[-_DIFF_PREV_TAIL:]
    max_overlap = min(len(tail), len(curr_n))

    for size in range(max_overlap, _DIFF_MIN_OVERLAP - 1, -1):
        candidate = tail[-size:]
        idx = curr_n.rfind(candidate)
        if idx == -1:
            continue
        # Append everything in curr after the matched overlap.
        return curr_n[idx + size:]

    log.warning(
        "No suffix overlap >= %d chars between prev (%d) and curr (%d); "
        "appending full snapshot",
        _DIFF_MIN_OVERLAP, len(prev_n), len(curr_n),
    )
    return curr_n


# ── Session pipe ─────────────────────────────────────────────────────
class SessionPipe:
    """Manages bidirectional I/O between one tmux session and one Discord channel."""

    def __init__(
        self,
        session_name: str,
        channel: discord.TextChannel,
        manager: SessionManager,
        *,
        poll_interval: float = 1.0,
        quiet_timeout: float = 3.0,
        notify_user_ids: frozenset[int] = frozenset(),
    ) -> None:
        self.session_name = session_name
        self.channel = channel
        self.manager = manager
        self.poll_interval = poll_interval
        self.quiet_timeout = quiet_timeout
        self._notify_user_ids = notify_user_ids

        # State
        self._last_snapshot: str = ""
        # Accumulated cleaned output for the current turn (since the last
        # finalize). Useful for diagnostics; not currently re-rendered.
        self._transcript: str = ""
        # The Discord message currently being edited in place. Frozen pages
        # (older messages in the channel) are never referenced again.
        self._active_page: discord.Message | None = None
        # Body text rendered into _active_page (without code-block fences),
        # capped at MAX_BODY before we freeze and roll to a new page.
        self._active_page_text: str = ""
        self._last_change: float = 0.0
        self._input_queue: asyncio.Queue[str] = asyncio.Queue()
        self._poll_task: asyncio.Task | None = None
        self._input_task: asyncio.Task | None = None
        self._stopped = False

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        """Spin up the polling and input consumer loops."""
        if self._poll_task is not None:
            return
        self._stopped = False
        self._poll_task = asyncio.create_task(self._poll_loop(), name=f"poll-{self.session_name}")
        self._input_task = asyncio.create_task(self._input_loop(), name=f"input-{self.session_name}")
        log.info("Pipe started for %s → #%s", self.session_name, self.channel.name)

    async def stop(self) -> None:
        """Cancel loops and clean up."""
        self._stopped = True
        for task in (self._poll_task, self._input_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._poll_task = None
        self._input_task = None
        # Lock the in-flight active page (if any) and ping users.
        await self._finalize_turn()
        log.info("Pipe stopped for %s", self.session_name)

    # ── Input (Discord → tmux) ───────────────────────────────────────

    async def enqueue_input(self, text: str) -> None:
        """Add a message to the input queue for sequential delivery."""
        await self._input_queue.put(("text", text))

    async def enqueue_special_keys(self, *keys: str) -> None:
        """Add special key presses (e.g. Down, Escape) to the input queue."""
        await self._input_queue.put(("keys", keys))

    async def _input_loop(self) -> None:
        """Consume queued messages and send to tmux one at a time."""
        try:
            while not self._stopped:
                item = await self._input_queue.get()
                try:
                    kind, payload = item
                    if kind == "keys":
                        await self.manager.send_special_keys(
                            self.session_name, *payload,
                        )
                    else:
                        await self.manager.send_keys(self.session_name, payload)
                except RuntimeError as e:
                    log.error("send_keys failed for %s: %s", self.session_name, e)
                    await self._notify_death(str(e))
                    return
                except Exception as e:
                    log.exception("Unexpected error in input loop for %s", self.session_name)
        except asyncio.CancelledError:
            return

    # ── Output (tmux → Discord) ──────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Poll tmux pane, detect changes, push to Discord via rolling pages.

        Strategy: capture the full visible pane (plus scrollback) each tick.
        When content changes, compute the new suffix vs the previous snapshot
        and append it to the active Discord page. If the page would exceed
        MAX_BODY, freeze it (no further edits) and start a new active page.
        After *quiet_timeout* seconds of no change the active page is locked
        in place and a single ping is sent in a trailing message.
        """
        try:
            while not self._stopped:
                await asyncio.sleep(self.poll_interval)
                try:
                    raw = await self.manager.capture_pane(self.session_name)
                except RuntimeError as e:
                    if not self._stopped:
                        log.error("Poll capture failed for %s: %s", self.session_name, e)
                        await self._notify_death("tmux session ended")
                    return

                clean = clean_tui_chrome(strip_ansi(raw))

                if clean == self._last_snapshot:
                    # No change — check if we should finalize the active page.
                    if (
                        self._active_page_text
                        and self._last_change > 0
                        and (time.monotonic() - self._last_change) >= self.quiet_timeout
                    ):
                        await self._finalize_turn()
                    continue

                new = _compute_new_text(self._last_snapshot, clean)
                # Always advance _last_snapshot — handles shrink + no-net-content.
                self._last_snapshot = clean
                if not new:
                    continue

                self._transcript += new
                await self._append_to_active(new)
                self._last_change = time.monotonic()

        except asyncio.CancelledError:
            return

    async def _append_to_active(self, new: str) -> None:
        """Append *new* to the active page, rolling to fresh pages as needed.

        Splits at the last newline before MAX_BODY when one exists; falls back
        to a byte-boundary cut only when no newline appears in the would-be
        page tail. After freezing a page, the leading newline that joined the
        old content to the new content is stripped so the next page does not
        start with a blank line.
        """
        # Roll forward as long as the next chunk would overflow the active page.
        while self._active_page_text and len(self._active_page_text) + len(new) > MAX_BODY:
            take = MAX_BODY - len(self._active_page_text)
            cut = new.rfind("\n", 0, take)
            if cut <= 0:
                # No newline in the tail — fall back to byte boundary.
                cut = take
            self._active_page_text += new[:cut]
            await self._flush_active(final=True)
            self._active_page = None
            self._active_page_text = ""
            new = new[cut:].lstrip("\n")
            if not new:
                return

        # First-page case: an empty active_page_text and a single delta larger
        # than MAX_BODY (rare, but possible on the very first poll of a long
        # turn). Slice it the same way before opening the page.
        while len(new) > MAX_BODY:
            take = MAX_BODY
            cut = new.rfind("\n", 0, take)
            if cut <= 0:
                cut = take
            self._active_page_text = new[:cut]
            await self._flush_active(final=True)
            self._active_page = None
            self._active_page_text = ""
            new = new[cut:].lstrip("\n")
            if not new:
                return

        self._active_page_text += new
        await self._flush_active(final=False)

    async def _flush_active(self, *, final: bool) -> None:
        """Render ``_active_page_text`` to Discord as the active page.

        - If no active page exists yet, send a new message and store it.
        - Otherwise edit the existing message in place.
        - ``final`` is informational; the caller is responsible for clearing
          ``_active_page`` after a ``final=True`` flush so the next append
          opens a fresh message.
        - Discord errors during edit/send are caught and logged. Forbidden
          (channel access revoked) stops the pipe; transient HTTP errors are
          left for the next poll to retry. discord.py auto-respects 429
          retry-after on both edit and send — we deliberately do not stack a
          second backoff on top.
        """
        if not self._active_page_text.strip():
            return

        # Defer the discord import to keep the module test-friendly.
        import discord

        content = f"```\n{self._active_page_text}\n```"
        try:
            if self._active_page is None:
                self._active_page = await self.channel.send(content)
            else:
                await self._active_page.edit(content=content)
        except discord.Forbidden:
            log.error(
                "Discord Forbidden while flushing page in #%s — stopping pipe",
                getattr(self.channel, "name", self.channel.id),
            )
            self._stopped = True
        except discord.NotFound:
            # Channel or message gone; stop trying. Drop the message handle so
            # any retry would attempt a fresh send rather than edit a tombstone.
            log.error(
                "Discord NotFound while flushing page in #%s — stopping pipe",
                getattr(self.channel, "name", self.channel.id),
            )
            self._active_page = None
            self._stopped = True
        except discord.HTTPException:
            log.exception(
                "Discord HTTP error while flushing page in #%s",
                getattr(self.channel, "name", self.channel.id),
            )

    async def _finalize_turn(self) -> None:
        """Lock the active page in place and ping users for this turn.

        - The active page is *not* re-edited here (no marker, no reflow per
          spec) — we simply stop touching it.
        - If the channel has notify users configured, one ping is sent in a
          trailing message below the final page (Discord delivers a real
          notification only on send, not on edit).
        - All per-turn state (_transcript, _active_page, _active_page_text,
          _last_change) is reset. _last_snapshot is intentionally NOT reset
          so the next turn's diff still anchors on what is currently on
          screen — turns are conceptual, tmux is continuous.
        """
        # Defer import; same rationale as _flush_active.
        import discord

        had_active = bool(self._active_page_text.strip())
        if had_active and self._notify_user_ids:
            mentions = " ".join(f"<@{uid}>" for uid in sorted(self._notify_user_ids))
            try:
                await self.channel.send(mentions, silent=False)
            except (discord.Forbidden, discord.NotFound):
                log.error(
                    "Discord Forbidden/NotFound while sending ping in #%s",
                    getattr(self.channel, "name", self.channel.id),
                )
            except discord.HTTPException:
                log.exception(
                    "Discord HTTP error while sending ping in #%s",
                    getattr(self.channel, "name", self.channel.id),
                )

        self._transcript = ""
        self._active_page = None
        self._active_page_text = ""
        self._last_change = 0.0

    async def _notify_death(self, reason: str) -> None:
        """Send a notification that the tmux session has died."""
        try:
            await self.channel.send(f"**Session ended:** {reason}")
        except Exception:
            log.exception("Failed to send death notification to #%s", self.channel.name)
        self._stopped = True


# ── Pipe registry ────────────────────────────────────────────────────
class PipeRegistry:
    """Tracks all active SessionPipe instances.

    Keyed by Discord channel ID for fast on_message lookup.
    """

    def __init__(self) -> None:
        self._by_channel: dict[int, SessionPipe] = {}
        self._by_session: dict[str, SessionPipe] = {}

    async def register(self, pipe: SessionPipe) -> None:
        # Stop any existing pipe for this session to prevent duplicates
        old = self._by_session.get(pipe.session_name)
        if old is not None:
            log.warning(
                "Replacing existing pipe for session %s — stopping old pipe",
                pipe.session_name,
            )
            await old.stop()
        self._by_channel[pipe.channel.id] = pipe
        self._by_session[pipe.session_name] = pipe

    def get_by_channel(self, channel_id: int) -> SessionPipe | None:
        return self._by_channel.get(channel_id)

    def get_by_session(self, session_name: str) -> SessionPipe | None:
        return self._by_session.get(session_name)

    async def remove(self, session_name: str) -> None:
        pipe = self._by_session.pop(session_name, None)
        if pipe:
            self._by_channel.pop(pipe.channel.id, None)
            await pipe.stop()

    async def stop_all(self) -> None:
        for pipe in list(self._by_session.values()):
            await pipe.stop()
        self._by_channel.clear()
        self._by_session.clear()

    def all_pipes(self) -> dict[str, SessionPipe]:
        return dict(self._by_session)
