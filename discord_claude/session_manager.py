"""Tmux session lifecycle management and state persistence.

Every tmux interaction goes through asyncio.create_subprocess_exec —
never subprocess.run — so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
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

        rc, _, stderr = await self._run(
            "new-session", "-d",
            "-s", name,
            "-c", workspace,
            "-x", "220", "-y", "50",
            "claude",
        )
        if rc != 0:
            raise RuntimeError(f"tmux new-session failed: {stderr.strip()}")

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

    async def send_keys(self, name: str, text: str) -> None:
        """Send keystrokes to a tmux session pane."""
        if not await self.has_session(name):
            raise RuntimeError(f"tmux session '{name}' does not exist")
        await self._run("send-keys", "-t", name, text, "Enter")

    async def capture_pane(self, name: str) -> str:
        """Return the current visible content of a tmux pane."""
        rc, stdout, stderr = await self._run(
            "capture-pane", "-t", name, "-p", "-e",
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
