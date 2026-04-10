from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Sanitisation pattern: only lowercase alphanumeric and hyphens
_SAFE_NAME = re.compile(r"[^a-z0-9-]")


def sanitize_name(raw: str) -> str:
    """Collapse a raw string into a tmux-safe slug (a-z 0-9 -)."""
    return _SAFE_NAME.sub("-", raw.lower()).strip("-") or "unnamed"


# ── Workspace mapping ────────────────────────────────────────────────
WORKSPACES: dict[str, Path] = {
    "tex-pilot": Path.home() / "personal" / "tex-pilot",
    "handoff": Path.home() / "personal" / "handoff",
    "forge": Path.home() / "personal" / "forge",
    "accessibility": Path.home() / "personal" / "accessibility",
}


def resolve_workspace(project: str) -> Path | None:
    """Return the local path for a known project, or None."""
    return WORKSPACES.get(sanitize_name(project))


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
