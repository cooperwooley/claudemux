# fn-1-discord-orchestrator-bot-for-claude.4 Management commands and restart reconnection

## Description
Management commands (/claude-stop, /delete-channel, /cleanup-category) and the restart reconnection logic that re-discovers tmux sessions on bot startup.

**Size:** M
**Files:** discord_claude/bot.py (extend), discord_claude/session_manager.py (extend)

## Approach
- /claude-stop: detach pipe, kill tmux session, update state file, post confirmation to channel
- /delete-channel: stop session if active, delete the Discord channel
- /cleanup-category: list channels in category, warn about active ones, delete only empty/stopped channels, delete category if fully empty
- Reconnection: on bot ready, call list_sessions() for claude-* tmux sessions, match against .sessions.json and existing Discord channels, re-create pipes for valid matches

## Key context
- /cleanup-category must NOT kill active sessions without confirmation
- Reconnection must handle: tmux alive + channel exists (re-attach), tmux alive + channel gone (orphan — log warning), tmux dead + channel exists (notify channel, clean up)
## Acceptance
- [ ] /claude-stop: kills tmux session, detaches pipe, updates state
- [ ] /delete-channel: stops session + deletes Discord channel
- [ ] /cleanup-category: removes empty channels, warns about active ones, removes empty category
- [ ] On restart: discovers existing claude-* tmux sessions
- [ ] Re-attaches pipes to sessions that still have matching Discord channels
- [ ] Logs warnings for orphaned sessions (tmux alive, no channel)
- [ ] Notifies channels for dead sessions (channel exists, tmux gone)
- [ ] State file updated after all reconnection reconciliation
## Done summary
## Task .4 Complete: Management Commands + Reconnection

Implemented as part of bot.py alongside task .3 (natural co-location):
- /claude-stop: kills tmux session, detaches pipe, updates state
- /delete-channel: stops session + deletes Discord channel (ephemeral response)
- /cleanup-category: removes channels without active pipes, warns about active ones, deletes empty category
- _reconnect_sessions(): on_ready discovers claude-* tmux sessions, matches to .sessions.json + Discord channels, re-creates pipes. Logs warnings for orphaned sessions.
## Evidence
- Commits:
- Tests: Commands registered and verified in task .3 evidence
- PRs: