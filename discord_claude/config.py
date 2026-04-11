from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# Sanitisation pattern: only lowercase alphanumeric and hyphens
_SAFE_NAME = re.compile(r"[^a-z0-9-]")


def sanitize_name(raw: str) -> str:
    """Collapse a raw string into a tmux-safe slug (a-z 0-9 -)."""
    return _SAFE_NAME.sub("-", raw.lower()).strip("-") or "unnamed"


# ── Workspace resolution ────────────────────────────────────────────
_WORKSPACES_FILE = Path(".workspaces.json")


class WorkspaceRegistry:
    """Manages base directories and resolves slash-separated project paths.

    Base dirs are top-level directories like ~/projects, ~/work, ~/personal.
    A project path like "work/backend/api-server" is resolved by
    walking base dirs to find a matching filesystem path.
    """

    def __init__(self, path: Path = _WORKSPACES_FILE) -> None:
        self._path = path
        self._base_dirs: list[Path] = []
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self._base_dirs = [Path(p) for p in data.get("base_dirs", [])]
                log.info("Loaded %d base dirs from %s", len(self._base_dirs), self._path)
            except (json.JSONDecodeError, OSError):
                log.exception("Failed to load workspaces from %s", self._path)
        if not self._base_dirs:
            # No defaults — user must register base dirs via /claude-workspace add
            pass

    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps(
                {"base_dirs": [str(p) for p in self._base_dirs]},
                indent=2,
            ))
        except OSError:
            log.exception("Failed to save workspaces to %s", self._path)

    @property
    def base_dirs(self) -> list[Path]:
        return list(self._base_dirs)

    def add_base_dir(self, path: Path) -> bool:
        """Add a base directory. Returns False if already registered or doesn't exist."""
        path = path.expanduser().resolve()
        if not path.is_dir():
            return False
        if path in self._base_dirs:
            return False
        self._base_dirs.append(path)
        self._save()
        return True

    def remove_base_dir(self, path: Path) -> bool:
        """Remove a base directory. Returns False if not found."""
        path = path.expanduser().resolve()
        if path not in self._base_dirs:
            return False
        self._base_dirs.remove(path)
        self._save()
        return True

    def resolve(self, project_path: str) -> Path | None:
        """Resolve a slash-separated project path against base directories.

        Examples:
            "work/backend/api-server"
              → checks ~/work/backend/api-server (if ~/work is a base dir)
              → or checks ~/projects/work/backend/api-server, etc.

            "backend/api-server"
              → checks ~/work/backend/api-server
              → checks ~/projects/backend/api-server

            "api-server"
              → searches all base dirs recursively (max 3 levels)
        """
        parts = project_path.strip("/").split("/")

        for base in self._base_dirs:
            # If the first part matches the base dir name, strip it
            # e.g. "work/backend" with base ~/work → ~/work/backend
            if parts[0] == base.name:
                candidate = base / "/".join(parts[1:])
            else:
                candidate = base / "/".join(parts)

            if candidate.is_dir():
                return candidate

        # Fallback: search all base dirs for a leaf match (single name like "texpilot-ide")
        if len(parts) == 1:
            target = parts[0]
            for base in self._base_dirs:
                for depth in range(1, 4):  # max 3 levels deep
                    for match in base.glob("/".join(["*"] * depth)):
                        if match.name == target and match.is_dir():
                            return match

        return None

    def category_name(self, project_path: str) -> str:
        """Derive a Discord category name from the project path.

        Uses the first path component (the top-level group).
        "work/backend/api-server" → "Work"
        "personal/my-project"     → "Personal"
        "api-server"              → tries to find which base dir it's in
        """
        parts = project_path.strip("/").split("/")

        if len(parts) >= 2:
            # First segment is the group
            return parts[0].replace("-", " ").title()

        # Single name — figure out which base dir contains it
        resolved = self.resolve(project_path)
        if resolved:
            for base in self._base_dirs:
                try:
                    resolved.relative_to(base)
                    return base.name.replace("-", " ").title()
                except ValueError:
                    continue

        return parts[0].replace("-", " ").title()

    def channel_name(self, project_path: str, feature: str) -> str:
        """Derive a Discord channel name.

        "work/backend/api-server", "auth-refactor"
          → "api-server-auth-refactor"
        """
        parts = project_path.strip("/").split("/")
        # Use the leaf project name + feature
        leaf = sanitize_name(parts[-1])
        feat = sanitize_name(feature)
        return f"{leaf}-{feat}"


# ── Bot settings ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class Settings:
    bot_token: str = ""
    guild_id: int = 0
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    state_file: Path = Path(".sessions.json")
    poll_interval: float = 1.0       # seconds between capture-pane polls
    quiet_timeout: float = 3.0       # seconds of silence before finalising message
    tmux_prefix: str = "claude"      # session names: {prefix}-{project}-{feature}

    @classmethod
    def from_env(cls) -> Settings:
        token = os.getenv("DISCORD_BOT_TOKEN", "")
        guild = int(os.getenv("DISCORD_GUILD_ID", "0"))
        raw_ids = os.getenv("ALLOWED_USER_IDS", "")
        allowed = frozenset(
            int(uid.strip()) for uid in raw_ids.split(",") if uid.strip().isdigit()
        )
        return cls(bot_token=token, guild_id=guild, allowed_user_ids=allowed)


def session_name(prefix: str, project: str, feature: str) -> str:
    """Build a deterministic tmux session name."""
    return f"{prefix}-{sanitize_name(project)}-{sanitize_name(feature)}"
