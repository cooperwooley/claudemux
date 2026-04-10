# fn-1-discord-orchestrator-bot-for-claude.3 Discord bot core with all slash commands

## Description
The Discord bot itself: client setup, slash commands, channel/category management, and the on_message handler that routes messages to pipes.

**Size:** M
**Files:** discord_claude/bot.py, discord_claude/__main__.py (update entry point)

## Approach
- discord.py Client with app_commands.CommandTree
- Intents: default + message_content (privileged)
- tree.sync() in setup_hook, per-guild for fast dev iteration
- Commands: /claude-attach, /claude-start, /claude-list (all use interaction.response.defer())
- Channel/category creation: get_or_create pattern — find existing category by name, create if missing
- on_message: check if channel is in active mappings, route to pipe's input queue
- Terminal mode: check `$ ` prefix + user in allowlist before routing

## Key context
- tree.sync() has 200/day global limit — sync per-guild during development
- message_content intent must also be toggled in Discord Developer Portal
- Use interaction.followup.send() after defer() for long operations
## Acceptance
- [ ] Bot connects with message_content intent
- [ ] /claude-attach: finds existing tmux session or creates new, creates Discord channel in project category
- [ ] /claude-start: always creates new session, errors if exists
- [ ] /claude-list: embed showing all active session↔channel mappings
- [ ] Categories auto-created per project name
- [ ] on_message routes to correct pipe input queue
- [ ] Terminal mode ($ prefix) restricted to allowed user IDs
- [ ] Commands use defer() + followup for long operations
- [ ] tree.sync() called once in setup_hook (per-guild)
## Done summary
TBD

## Evidence
- Commits:
- Tests:
- PRs:
