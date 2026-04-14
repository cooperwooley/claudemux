# claudemux

Multiplex [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI sessions through Discord via tmux.

## What is this?

claudemux is a self-hosted Discord bot that bridges your Discord server to real Claude Code CLI instances running on your machine. Each conversation gets its own Discord channel backed by a dedicated tmux session, giving you full Claude Code capabilities — file editing, bash execution, tool use, MCP servers, hooks, skills — all accessible from Discord on any device.

## Why not Claude Channels?

Anthropic's [Claude Channels](https://support.anthropic.com/en/articles/11092633-using-claude-in-discord-and-slack) integration lets you chat with Claude directly in Discord. It's great for general conversation, but it's **API Claude** — not **Claude Code**. That distinction matters:

| Capability | Claude Channels | claudemux |
|---|---|---|
| **Filesystem access** | None — Claude can't see or edit your code | Full — each session runs in your actual project directory |
| **Tool use** | Limited to what the API supports | Everything Claude Code supports: Read, Edit, Write, Bash, Grep, Glob, etc. |
| **Bash execution** | No | Yes — Claude Code runs commands in your real shell environment |
| **Multiple concurrent sessions** | One conversation per channel | Unlimited sessions across any number of projects, each in its own channel |
| **MCP servers** | No | Yes — your local MCP configuration is available |
| **Hooks and skills** | No | Yes — your Claude Code hooks, skills, and CLAUDE.md files are all active |
| **Git integration** | No | Full — Claude Code can commit, branch, create PRs |
| **Session persistence** | Conversations reset with context limits | tmux sessions survive bot restarts; the bot reconnects automatically |
| **Project organization** | Flat channel list | Auto-organized: channels grouped into Discord categories by project |
| **Cost model** | Per-seat Anthropic subscription | Your own Claude Code subscription — no per-channel fees |
| **Privacy** | Messages route through Anthropic's Discord integration | Messages stay between your Discord server and your machine |

**In short:** Claude Channels gives you a chatbot in Discord. claudemux gives you a full Claude Code workstation in Discord — the same Claude Code you use in your terminal, accessible from your phone, tablet, or any device with Discord.

## How it works

```
Discord channel  <──messages──>  claudemux bot  <──tmux pipe──>  Claude Code CLI
     #project-feature               (Python)                    (tmux session)
```

1. You run `/claude-start` or `/claude-attach` with a project and feature name
2. The bot creates a tmux session running `claude` in your project directory
3. A Discord channel is created (organized under a project category)
4. Messages you send in the channel are piped to Claude Code's stdin
5. Claude Code's terminal output is captured and relayed back to Discord
6. When Claude finishes responding, you get a ping

## Prerequisites

- **Python 3.11+**
- **tmux** installed and on PATH
- **Claude Code CLI** (`claude`) installed and authenticated
- A **Discord bot** with these permissions: Manage Channels, Send Messages, Read Message History, Embed Links, Add Reactions
- A **Discord server** where you've invited the bot

## Setup

1. Clone the repo and install:
   ```bash
   git clone https://github.com/cooperwooley/claudemux.git
   cd claudemux
   python -m venv .venv
   source .venv/bin/activate
   pip install -e .
   ```

2. Copy the example env file and fill in your values:
   ```bash
   cp .env.example .env
   ```

   | Variable | Description |
   |---|---|
   | `DISCORD_BOT_TOKEN` | Your Discord bot token |
   | `DISCORD_GUILD_ID` | The server (guild) ID where the bot operates |
   | `ALLOWED_USER_IDS` | Comma-separated Discord user IDs allowed to run shell commands |

3. Register your project directories:
   ```
   /claude-workspace add /home/you/projects
   ```

4. Run:
   ```bash
   claudemux
   ```

## Commands

| Command | Description |
|---|---|
| `/claude-start <project> <feature>` | Start a new Claude Code session in a project directory |
| `/claude-attach <project> <feature>` | Attach to an existing session (or create one if it doesn't exist) |
| `/claude-list` | List all active sessions |
| `/claude-stop <session>` | Stop a session and detach the pipe |
| `/delete-channel <channel>` | Stop the session and delete the Discord channel |
| `/cleanup-category <project>` | Remove empty/stopped channels from a project category |
| `/claude-workspace add <path>` | Register a base directory for project discovery |
| `/claude-workspace remove <path>` | Remove a registered base directory |
| `/claude-workspace list` | Show all registered base directories |

## Message routing

- **Normal messages** in a session channel are sent to Claude Code as prompts
- **`$ <command>`** prefix runs a raw shell command in the tmux session (restricted to allowed users)
- **`^<key>`** prefix sends special keys for navigating interactive TUI prompts (menus, selections, etc.)
- **Bot messages** are ignored to prevent loops

### Special key commands

Interactive prompts (like arrow-key menus) can't be navigated with normal text messages. Use the `^` prefix to send special keys:

| Command | Action |
|---|---|
| `^` | Enter (confirm/submit) |
| `^1` through `^9` | Select menu option N (sends N-1 Down arrows + Enter) |
| `^esc` | Escape (quit a menu or prompt) |
| `^up` / `^down` | Arrow keys (manual navigation) |
| `^left` / `^right` | Arrow keys |
| `^tab` | Tab |
| `^enter` | Enter (confirm default selection) |
| `^space` | Space (toggle checkboxes) |
| `^y` / `^n` | Answer yes/no prompts |
| `^backspace` | Backspace |

## Architecture

```
discord_claude/
  __main__.py        # Entry point
  config.py          # Settings, workspace resolution, naming conventions
  bot.py             # Discord client, slash commands, message routing
  pipe.py            # Bidirectional tmux<->Discord pipe with rate-limit-safe output
  session_manager.py # tmux session lifecycle, state persistence
```

- **SessionManager** handles all tmux interactions (create, attach, capture-pane, send-keys, kill) via async subprocess — the event loop is never blocked
- **SessionPipe** polls tmux pane content, diffs against the last snapshot, and updates a single "live" Discord message that gets edited in place (avoiding rate limits). After a quiet period, the message is finalized and a new one starts on the next change
- **WorkspaceRegistry** resolves slash-separated project paths (like `myorg/repo-name`) against registered base directories, supporting nested project structures
- **State persistence** via `.sessions.json` enables automatic reconnection after bot restarts

## Security notes

- The bot runs Claude Code with `--dangerously-skip-permissions` so it can operate non-interactively in tmux. This means Claude Code will execute tools without confirmation. Only run this on machines where you trust the prompts coming through Discord.
- The `$ <command>` prefix gives direct shell access — it's gated by `ALLOWED_USER_IDS` but you should treat those user IDs as root-equivalent on the host machine.
- Keep your `.env` file private. Never commit it.
- The bot is designed for personal/private Discord servers. Do not deploy it to public servers.

## License

MIT
