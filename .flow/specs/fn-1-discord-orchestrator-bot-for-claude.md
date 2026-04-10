# Discord Orchestrator Bot for Claude Code CLI

## Overview
Discord bot that manages multiple local Claude Code CLI instances via tmux sessions. Creates Discord channels mapped 1:1 to tmux sessions, organized by project category. Supports attaching to pre-existing tmux sessions, bidirectional piping, and shell command passthrough.

## Scope
- Slash commands: /claude-attach, /claude-start, /claude-stop, /claude-list, /delete-channel, /cleanup-category
- Bidirectional pipe: Discord ↔ tmux via asyncio polling
- Smart workspace mapping for known projects
- Terminal mode (`$ ` prefix) with user allowlist
- Session state persistence via JSON + tmux discovery
- Rate-limit-safe output via edit-in-place strategy

## Approach

### Architecture (4 modules)
- `config.py` — workspace mappings, bot settings, user allowlist
- `session_manager.py` — tmux lifecycle (create/attach/destroy/list), state persistence
- `pipe.py` — async bidirectional bridge, ANSI stripping, output buffering, rate-limit handling
- `bot.py` — discord.py bot, slash commands, channel/category management, on_message

### Rate Limit Strategy (CRITICAL)
Discord enforces 5 messages/5s/channel. Solution: **edit a single "live" message** instead of sending new ones. Buffer output, edit on a 1-2s timer. When Claude goes quiet (no new output for 3s), finalize the message and create a new "live" message for the next response.

### State Management
- Primary: `.sessions.json` file mapping session_name → {channel_id, project, workspace_path}
- Recovery: On restart, scan tmux sessions matching `claude-*` pattern, reconcile with Discord channels
- Concurrent input: asyncio.Queue per session, processed sequentially

### Security
- Terminal mode (`$ `) restricted to configured user IDs
- tmux session names sanitized (alphanumeric + hyphens only)
- Bot requires: Manage Channels, Send Messages, Read Message History, Message Content intent

## Quick commands
```bash
# Install
pip install -e .

# Run
cp .env.example .env  # add bot token
python -m discord_claude

# Test tmux integration
tmux new-session -d -s claude-test -c /tmp
tmux has-session -t claude-test && echo "works"
tmux kill-session -t claude-test
```

## Risks
- Discord category limit: 50 channels per category — warn user when approaching
- Claude CLI interactive prompts (Y/n) must be forwarded as-is
- Claude process death (OOM, token limit) — detect via tmux session status, notify channel
- Stale mappings if channels deleted manually — reconcile on message events

## Acceptance
- [ ] /claude-attach finds existing tmux sessions or creates new ones
- [ ] Discord channels auto-organized into Categories by project
- [ ] Bidirectional pipe: Discord → Claude input, Claude output → Discord
- [ ] Edit-in-place output strategy avoids rate limits
- [ ] Terminal mode ($ prefix) works with user allowlist
- [ ] /claude-list shows all active mappings
- [ ] /claude-stop, /delete-channel, /cleanup-category work correctly
- [ ] Bot restart re-discovers and re-attaches to existing sessions
- [ ] ANSI codes stripped from Discord output
- [ ] State persisted to .sessions.json

## References
- discord.py 2.x docs: https://discordpy.readthedocs.io/en/stable/
- Discord rate limits: https://docs.discord.com/developers/topics/rate-limits
- Python asyncio subprocess: https://docs.python.org/3/library/asyncio-subprocess.html
- tmux man page: capture-pane, send-keys, has-session, list-sessions
