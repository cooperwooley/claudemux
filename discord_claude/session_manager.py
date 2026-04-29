"""Tmux session lifecycle management and state persistence.

Every tmux interaction goes through asyncio.create_subprocess_exec —
never subprocess.run — so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings, sanitize_name, session_name

log = logging.getLogger(__name__)


# ── Data ─────────────────────────────────────────────────────────────
@dataclass
class SessionInfo:
    """One tracked mapping between a tmux session and a Discord channel."""

    session_name: str
    project: str
    feature: str
    workspace: str          # absolute path string
    channel_id: int = 0     # Discord channel snowflake (0 = not yet created)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SessionInfo:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ── Manager ──────────────────────────────────────────────────────────
class SessionManager:
    """Create / attach / destroy tmux sessions and persist state."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sessions: dict[str, SessionInfo] = {}  # keyed by session_name
        self._load_state()

    # ── tmux primitives ──────────────────────────────────────────────

    async def _run(self, *args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "tmux", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        return (
            proc.returncode or 0,
            stdout_b.decode(errors="replace"),
            stderr_b.decode(errors="replace"),
        )

    async def has_session(self, name: str) -> bool:
        rc, _, _ = await self._run("has-session", "-t", name)
        return rc == 0

    async def create_session(
        self,
        project: str,
        feature: str,
        workspace: str,
    ) -> SessionInfo:
        """Spawn a detached tmux session running `claude` in *workspace*."""
        name = session_name(self._settings.tmux_prefix, project, feature)

        if await self.has_session(name):
            raise RuntimeError(f"tmux session '{name}' already exists")

        claude_bin = shutil.which("claude")
        if claude_bin is None:
            raise RuntimeError("'claude' not found on PATH")

        rc, _, stderr = await self._run(
            "new-session", "-d",
            "-s", name,
            "-c", workspace,
            "-x", "220", "-y", "50",
            claude_bin, "--dangerously-skip-permissions",
        )
        if rc != 0:
            raise RuntimeError(f"tmux new-session failed: {stderr.strip()}")

        # Bump per-session history-limit so capture-pane -S -<N> can
        # actually return N lines of scrollback. Failure is non-fatal —
        # we just get a smaller scrollback window than intended.
        rc_hl, _, stderr_hl = await self._run(
            "set-option", "-t", name, "history-limit", "5000",
        )
        if rc_hl != 0:
            log.warning(
                "could not set history-limit on %s: %s",
                name, stderr_hl.strip(),
            )

        info = SessionInfo(
            session_name=name,
            project=sanitize_name(project),
            feature=sanitize_name(feature),
            workspace=workspace,
        )
        self._sessions[name] = info
        self._save_state()
        log.info("Created tmux session %s in %s", name, workspace)
        return info

    async def attach_session(
        self,
        project: str,
        feature: str,
        workspace: str,
    ) -> SessionInfo:
        """Attach to an existing session, or create one if it doesn't exist."""
        name = session_name(self._settings.tmux_prefix, project, feature)

        if await self.has_session(name):
            info = self._sessions.get(name) or SessionInfo(
                session_name=name,
                project=sanitize_name(project),
                feature=sanitize_name(feature),
                workspace=workspace,
            )
            self._sessions[name] = info
            self._save_state()
            log.info("Attached to existing tmux session %s", name)
            return info

        return await self.create_session(project, feature, workspace)

    async def send_keys(self, name: str, text: str, *, enter: bool = True) -> None:
        """Send keystrokes to a tmux session pane.

        Uses -l (literal) so tmux never interprets message text as key
        names (e.g. "Escape", "Space").  Enter is sent as a separate
        command so the TUI has time to process the text first.
        """
        if not await self.has_session(name):
            raise RuntimeError(f"tmux session '{name}' does not exist")
        await self._run("send-keys", "-t", name, "-l", text)
        if enter:
            await self._run("send-keys", "-t", name, "Enter")

    async def send_special_keys(self, name: str, *keys: str) -> None:
        """Send raw tmux key names (Down, Up, Escape, Enter, etc.)."""
        if not await self.has_session(name):
            raise RuntimeError(f"tmux session '{name}' does not exist")
        for key in keys:
            await self._run("send-keys", "-t", name, key)

    async def capture_pane(
        self, name: str, *, scrollback_lines: int = 2000,
    ) -> str:
        """Return pane content including the last *scrollback_lines* of history.

        tmux silently clamps ``-S -<N>`` to whatever ``history-limit`` is set
        to, so callers must ensure the session was created with a large
        enough buffer (see :meth:`create_session`).
        """
        rc, stdout, stderr = await self._run(
            "capture-pane", "-t", name, "-p", "-e",
            "-S", f"-{scrollback_lines}",
        )
        if rc != 0:
            raise RuntimeError(f"capture-pane failed: {stderr.strip()}")
        return stdout

    async def kill_session(self, name: str) -> None:
        """Destroy a tmux session."""
        rc, _, stderr = await self._run("kill-session", "-t", name)
        if rc != 0:
            log.warning("kill-session %s failed: %s", name, stderr.strip())
        self._sessions.pop(name, None)
        self._save_state()
        log.info("Killed tmux session %s", name)

    async def list_sessions(self) -> list[str]:
        """Return names of all tmux sessions matching our prefix."""
        rc, stdout, _ = await self._run(
            "list-sessions", "-F", "#{session_name}",
        )
        if rc != 0:
            return []
        prefix = f"{self._settings.tmux_prefix}-"
        return [
            line for line in stdout.strip().splitlines()
            if line.startswith(prefix)
        ]

    # ── State persistence ────────────────────────────────────────────

    def get_info(self, name: str) -> SessionInfo | None:
        return self._sessions.get(name)

    def all_sessions(self) -> dict[str, SessionInfo]:
        return dict(self._sessions)

    def update_channel_id(self, name: str, channel_id: int) -> None:
        if name in self._sessions:
            self._sessions[name].channel_id = channel_id
            self._save_state()

    def _save_state(self) -> None:
        """Atomic write: tmp file → rename."""
        path = self._settings.state_file
        data = {k: v.to_dict() for k, v in self._sessions.items()}
        try:
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            with open(fd, "w") as f:
                json.dump(data, f, indent=2)
            Path(tmp).replace(path)
        except OSError:
            log.exception("Failed to save state to %s", path)

    def _load_state(self) -> None:
        path = self._settings.state_file
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text())
            self._sessions = {
                k: SessionInfo.from_dict(v) for k, v in raw.items()
            }
            log.info("Loaded %d sessions from %s", len(self._sessions), path)
        except (json.JSONDecodeError, OSError):
            log.exception("Failed to load state from %s", path)
