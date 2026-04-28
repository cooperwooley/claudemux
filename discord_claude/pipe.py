"""Bidirectional pipe between a tmux session and a Discord channel.

Handles:
- Output polling via capture-pane with diff-based change detection
- ANSI escape code stripping
- Rate-limit-safe output via edit-in-place (one "live" message)
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


def chunk_output(text: str) -> list[str]:
    """Split *text* into chunks that fit inside a Discord code block."""
    if not text:
        return []

    chunks: list[str] = []
    while text:
        if len(text) <= MAX_MSG_LEN:
            chunks.append(f"```\n{text}\n```")
            break
        # Find a newline near the boundary to avoid mid-line splits
        cut = text.rfind("\n", 0, MAX_MSG_LEN)
        if cut == -1:
            cut = MAX_MSG_LEN
        chunks.append(f"```\n{text[:cut]}\n```")
        text = text[cut:].lstrip("\n")
    return chunks


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
        self._live_message: discord.Message | None = None
        self._live_buffer: str = ""
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
        # Finalize any pending live message
        await self._finalize_message()
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
        """Poll tmux pane, detect changes, push to Discord.

        Strategy: capture the full visible pane each tick.  When content
        changes, replace the live message with the latest pane snapshot
        (trimmed to the last ~1900 chars).  After *quiet_timeout* seconds
        of no change the message is finalised and the next change starts
        a fresh message.
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
                    # No change — check if we should finalize
                    if (
                        self._live_buffer
                        and self._last_change > 0
                        and (time.monotonic() - self._last_change) >= self.quiet_timeout
                    ):
                        await self._finalize_message()
                    continue

                self._last_snapshot = clean
                # Show the tail of the pane (most relevant output)
                self._live_buffer = clean
                self._last_change = time.monotonic()
                await self._update_live_message()

        except asyncio.CancelledError:
            return

    async def _update_live_message(self) -> None:
        """Edit the live message with the latest pane snapshot."""
        if not self._live_buffer.strip():
            return

        # Trim to tail to fit Discord's limit
        text = self._live_buffer.strip()
        if len(text) > MAX_MSG_LEN:
            text = "...\n" + text[-(MAX_MSG_LEN - 5):]
        display = f"```\n{text}\n```"

        try:
            if self._live_message is not None:
                await self._live_message.edit(content=display)
            else:
                self._live_message = await self.channel.send(display)
        except Exception:
            log.exception("Failed to update live message in #%s", self.channel.name)

    async def _finalize_message(self) -> None:
        """Finalize the current live message and reset for next response."""
        if not self._live_buffer.strip():
            self._live_buffer = ""
            self._live_message = None
            return

        # Send all chunks as final messages
        chunks = chunk_output(self._live_buffer.strip())
        try:
            if self._live_message is not None and chunks:
                # Update the live message with the first chunk
                await self._live_message.edit(content=chunks[0])
                # Send remaining chunks as new messages
                for chunk in chunks[1:]:
                    await self.channel.send(chunk)
            elif chunks:
                for chunk in chunks:
                    await self.channel.send(chunk)
        except Exception:
            log.exception("Failed to finalize message in #%s", self.channel.name)

        # Ping users that the response is ready
        if self._notify_user_ids:
            mentions = " ".join(f"<@{uid}>" for uid in self._notify_user_ids)
            try:
                await self.channel.send(mentions, silent=False)
            except Exception:
                log.exception("Failed to send ping in #%s", self.channel.name)

        self._live_buffer = ""
        self._live_message = None
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
