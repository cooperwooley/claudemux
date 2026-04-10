# fn-1-discord-orchestrator-bot-for-claude.2 Bidirectional pipe with rate-limit-safe output

## Description
The async bidirectional pipe engine that bridges tmux panes and Discord channels. Handles output buffering, ANSI stripping, rate-limit-safe message editing, and input queuing.

**Size:** M
**Files:** discord_claude/pipe.py

## Approach
- One `OutputPipe` class per active session: polls `capture_pane()` on interval, diffs against last snapshot
- ANSI stripping via precompiled regex covering CSI, OSC, SGR, charset sequences
- Rate limit strategy: maintain a single "live" Discord message per response, edit it on a 1.5s timer
- When no new output for 3s, finalize the live message and prepare for next response
- Input: asyncio.Queue per session, consumer sends keys sequentially (prevents interleaving)
- Chunking: split output at 1900 chars (leave room for code block markers)
- Terminal mode (`$ ` prefix) sends raw command via send_keys, no special handling needed

## Key context
- discord.py handles 429 retries automatically, but edit-in-place avoids triggering them
- `message.edit()` shares the same rate bucket as `channel.send()` — still better because 1 edit vs N sends
- Use `asyncio.create_task()` for polling loops, track per session for cleanup
## Acceptance
- [ ] OutputPipe class polls tmux pane at configurable interval (~1s)
- [ ] Diff-based detection: only pushes to Discord when content changes
- [ ] ANSI escape codes stripped (CSI, OSC, SGR, charset sequences)
- [ ] Edit-in-place: maintains one "live" message, edits on timer
- [ ] Finalizes message after 3s of no new output
- [ ] Output chunked at ~1900 chars with code block formatting
- [ ] Input queue: messages processed sequentially per session
- [ ] Polling task created/cancelled cleanly per session lifecycle
- [ ] Handles tmux session death gracefully (notifies channel)
## Done summary
## Task .2 Complete: Bidirectional Pipe

**Files created:** `discord_claude/pipe.py`

**Features:**
- `strip_ansi()` — comprehensive regex covering CSI, OSC, SGR, charset sequences
- `chunk_output()` — splits at ~1900 chars with code block formatting
- `SessionPipe` — full async bidirectional bridge:
  - Output polling via `_poll_loop` with diff-based change detection
  - Edit-in-place: maintains one "live" Discord message, edits on timer
  - Finalizes after quiet_timeout (3s) of no new output
  - Input queue with sequential delivery (prevents interleaving)
  - Session death detection and notification
- `PipeRegistry` — lookup by channel_id or session_name, clean teardown

**Verified:** ANSI stripping, chunking, imports, registry operations all tested.
## Evidence
- Commits:
- Tests: ANSI stripping (CSI, OSC, SGR), chunk_output short/long/empty, SessionPipe+PipeRegistry import, Registry lookup/create
- PRs: