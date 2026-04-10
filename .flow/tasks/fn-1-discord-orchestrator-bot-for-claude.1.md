# fn-1-discord-orchestrator-bot-for-claude.1 Project scaffolding, config, and tmux session manager

## Description
Project scaffolding and the two foundational modules: config and tmux session manager.

**Size:** M
**Files:** pyproject.toml, .gitignore, .env.example, discord_claude/__init__.py, discord_claude/__main__.py, discord_claude/config.py, discord_claude/session_manager.py

## Approach
- Use `pyproject.toml` with discord.py and python-dotenv as dependencies
- `config.py`: dataclass-based settings loaded from env vars + hardcoded workspace map
- `session_manager.py`: all tmux interaction via `asyncio.create_subprocess_exec` (never subprocess.run)
- State persistence: `.sessions.json` file with atomic writes (write to temp, rename)
- Session naming: `claude-{project}-{feature}`, sanitized to `[a-z0-9-]` only

## Key context
- tmux `has-session` returns exit code 0/1 — use returncode, not stdout
- `capture-pane -p` prints to stdout; use `-e` to include escape sequences for later stripping
- Session names must be sanitized against shell injection (tmux send-keys is a vector)
## Acceptance
- [ ] pyproject.toml with discord.py, python-dotenv dependencies
- [ ] .gitignore covers __pycache__, .env, .sessions.json
- [ ] .env.example with DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, ALLOWED_USER_IDS
- [ ] config.py loads settings from env, has WORKSPACES dict for tex-pilot/handoff/forge/accessibility
- [ ] session_manager.py: create_session(project, feature) spawns detached tmux with claude CLI
- [ ] session_manager.py: has_session(name) checks existence
- [ ] session_manager.py: send_keys(name, text) pipes input
- [ ] session_manager.py: capture_pane(name) returns current pane content
- [ ] session_manager.py: kill_session(name) destroys session
- [ ] session_manager.py: list_sessions() returns all claude-* sessions
- [ ] session_manager.py: save/load state to .sessions.json
- [ ] All tmux calls use asyncio.create_subprocess_exec
- [ ] Session names sanitized to [a-z0-9-]
## Done summary
## Task .1 Complete: Project Scaffolding + Config + Session Manager

**Files created:**
- `pyproject.toml` — discord.py + python-dotenv deps
- `.gitignore` — covers __pycache__, .env, .sessions.json, .venv
- `.env.example` — DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, ALLOWED_USER_IDS
- `discord_claude/__init__.py` — package init
- `discord_claude/__main__.py` — entry point
- `discord_claude/config.py` — Settings dataclass, workspace mapping, name sanitization
- `discord_claude/session_manager.py` — full tmux lifecycle via asyncio

**Verified:** config loads, sanitize_name works, session_name builds correctly, workspace mapping resolves, tmux create/has/capture/kill/list all work via integration test.
## Evidence
- Commits:
- Tests: config import + settings load, sanitize_name('My Feature!!') → 'my-feature', session_name → 'claude-tex-pilot-auth-refactor', resolve_workspace('tex-pilot') → correct path, tmux create/has/capture/kill/list lifecycle
- PRs: