"""Discord bot: slash commands, channel management, message routing."""

from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord import app_commands

from .config import Settings, WorkspaceRegistry, sanitize_name, session_name
from .pipe import PipeRegistry, SessionPipe
from .session_manager import SessionManager

log = logging.getLogger(__name__)


class ClaudeBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.manager = SessionManager(settings)
        self.pipes = PipeRegistry()
        self.workspaces = WorkspaceRegistry()
        self._guild_obj: discord.Object | None = (
            discord.Object(id=settings.guild_id) if settings.guild_id else None
        )

        self._register_commands()
        self._register_error_handler()

    # ── Global error handler ───────────────────────────────────────

    def _register_error_handler(self) -> None:
        @self.tree.error
        async def on_app_command_error(
            interaction: discord.Interaction,
            error: app_commands.AppCommandError,
        ) -> None:
            original = getattr(error, "original", error)
            log.error("Command '%s' failed: %s", interaction.command and interaction.command.name, original)

            msg = f"**Error:** {original}"
            if isinstance(original, discord.Forbidden):
                msg = (
                    "**Missing Permissions.** The bot needs **Manage Channels**, "
                    "**Send Messages**, **Read Message History**, **Embed Links**, "
                    "and **Add Reactions**. Re-invite with the correct permissions."
                )

            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except Exception:
                log.exception("Failed to send error response")

    # ── Setup ────────────────────────────────────────────────────────

    async def setup_hook(self) -> None:
        if self._guild_obj:
            self.tree.copy_global_to(guild=self._guild_obj)
            await self.tree.sync(guild=self._guild_obj)
            log.info("Synced commands to guild %s", self.settings.guild_id)
        else:
            await self.tree.sync()
            log.info("Synced commands globally")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)  # type: ignore[union-attr]
        await self._reconnect_sessions()

    # ── Message routing ──────────────────────────────────────────────

    # Map of ^-prefix names to tmux key names
    _SPECIAL_KEYS: dict[str, str] = {
        "esc": "Escape",
        "escape": "Escape",
        "up": "Up",
        "down": "Down",
        "left": "Left",
        "right": "Right",
        "tab": "Tab",
        "enter": "Enter",
        "space": "Space",
        "backspace": "BSpace",
        "y": "y",
        "n": "n",
    }

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        pipe = self.pipes.get_by_channel(message.channel.id)
        if pipe is None:
            return

        text = message.content.strip()
        if not text:
            return

        # Terminal mode: `$ <command>` restricted to allowed users
        if text.startswith("$ "):
            if message.author.id not in self.settings.allowed_user_ids:
                await message.reply("You are not authorised to run shell commands.")
                return
            await pipe.enqueue_input(text[2:])
            await message.add_reaction("\u2705")
            return

        # Special-key mode: `^<key>` for interactive prompts
        if text.startswith("^"):
            arg = text[1:].strip().lower()

            # ^1-^9 → select menu option N (send N-1 Down arrows + Enter)
            if arg.isdigit() and 1 <= int(arg) <= 9:
                n = int(arg)
                keys = ["Down"] * (n - 1) + ["Enter"]
                await pipe.enqueue_special_keys(*keys)
                await message.add_reaction("\U0001f3af")  # dart
                return

            # Named keys: ^esc, ^up, ^down, ^tab, etc.
            tmux_key = self._SPECIAL_KEYS.get(arg)
            if tmux_key:
                await pipe.enqueue_special_keys(tmux_key)
                await message.add_reaction("\U0001f3af")  # dart
                return

            await message.reply(
                "Unknown key. Use `^1`-`^9` for menu selection, "
                "or `^esc` `^up` `^down` `^tab` `^enter` `^y` `^n`."
            )
            return

        # Normal mode: send to Claude
        await pipe.enqueue_input(text)
        await message.add_reaction("\u2709")  # envelope

    # ── Helpers ──────────────────────────────────────────────────────

    async def _get_or_create_category(
        self, guild: discord.Guild, name: str
    ) -> discord.CategoryChannel:
        """Find or create a category by display name."""
        for cat in guild.categories:
            if cat.name.lower() == name.lower():
                return cat
        return await guild.create_category(name)

    async def _create_pipe(
        self,
        session_name_str: str,
        channel: discord.TextChannel,
    ) -> SessionPipe:
        pipe = SessionPipe(
            session_name=session_name_str,
            channel=channel,
            manager=self.manager,
            poll_interval=self.settings.poll_interval,
            quiet_timeout=self.settings.quiet_timeout,
            notify_user_ids=self.settings.allowed_user_ids,
        )
        self.pipes.register(pipe)
        pipe.start()
        return pipe

    async def _reconnect_sessions(self) -> None:
        """On startup, re-discover tmux sessions and re-attach pipes."""
        live_sessions = await self.manager.list_sessions()
        guild = self.get_guild(self.settings.guild_id) if self.settings.guild_id else None
        if not guild:
            log.warning("Guild %s not found — skipping reconnection", self.settings.guild_id)
            return

        for sname in live_sessions:
            info = self.manager.get_info(sname)
            if info is None or info.channel_id == 0:
                log.warning("Orphaned tmux session %s (no channel mapping)", sname)
                continue

            channel = guild.get_channel(info.channel_id)
            if channel is None or not isinstance(channel, discord.TextChannel):
                log.warning(
                    "Channel %s for session %s no longer exists",
                    info.channel_id, sname,
                )
                continue

            await self._create_pipe(sname, channel)
            log.info("Reconnected pipe: %s → #%s", sname, channel.name)

        log.info("Reconnection complete: %d pipes active", len(self.pipes.all_pipes()))

    # ── Slash commands ───────────────────────────────────────────────

    def _register_commands(self) -> None:
        @self.tree.command(
            name="claude-attach",
            description="Attach to an existing Claude session or create a new one",
        )
        @app_commands.describe(
            project="Project path (e.g. myorg/backend, tools/cli-app, personal/my-project)",
            feature="Feature/task name for the channel",
        )
        async def claude_attach(
            interaction: discord.Interaction,
            project: str,
            feature: str,
        ) -> None:
            await interaction.response.defer()

            guild = interaction.guild
            if guild is None:
                await interaction.followup.send("This command only works in a server.")
                return

            # Resolve workspace path
            resolved = self.workspaces.resolve(project)
            if resolved is None:
                await interaction.followup.send(
                    f"Could not find project `{project}` in any base directory.\n"
                    f"Registered base dirs: {', '.join(f'`{p}`' for p in self.workspaces.base_dirs)}\n"
                    f"Use `/claude-workspace add <path>` to register a new base directory."
                )
                return

            workspace_str = str(resolved)
            project_slug = sanitize_name(project.replace("/", "-"))
            feature_slug = sanitize_name(feature)
            sname = session_name(self.settings.tmux_prefix, project_slug, feature_slug)

            # Discord structure
            cat_name = self.workspaces.category_name(project)
            ch_name = self.workspaces.channel_name(project, feature)

            category = await self._get_or_create_category(guild, cat_name)
            existing_ch = discord.utils.get(category.text_channels, name=ch_name)
            channel = existing_ch or await guild.create_text_channel(ch_name, category=category)

            # Attach or create tmux session
            info = await self.manager.attach_session(project_slug, feature_slug, workspace_str)
            self.manager.update_channel_id(sname, channel.id)

            # Create pipe
            if self.pipes.get_by_session(sname):
                await self.pipes.remove(sname)
            await self._create_pipe(sname, channel)

            attached = await self.manager.has_session(sname)
            status = "Attached to existing" if attached else "Created new"
            await interaction.followup.send(
                f"{status} session `{sname}` → {channel.mention}\n"
                f"Workspace: `{workspace_str}`"
            )

        @self.tree.command(
            name="claude-start",
            description="Start a new Claude session (errors if one already exists)",
        )
        @app_commands.describe(
            project="Project path (e.g. myorg/backend, personal/my-project)",
            feature="Feature/task name for the channel",
        )
        async def claude_start(
            interaction: discord.Interaction,
            project: str,
            feature: str,
        ) -> None:
            await interaction.response.defer()

            guild = interaction.guild
            if guild is None:
                await interaction.followup.send("This command only works in a server.")
                return

            resolved = self.workspaces.resolve(project)
            if resolved is None:
                await interaction.followup.send(
                    f"Could not find project `{project}` in any base directory.\n"
                    f"Use `/claude-workspace add <path>` to register a new base directory."
                )
                return

            workspace_str = str(resolved)
            project_slug = sanitize_name(project.replace("/", "-"))
            feature_slug = sanitize_name(feature)
            sname = session_name(self.settings.tmux_prefix, project_slug, feature_slug)

            if await self.manager.has_session(sname):
                await interaction.followup.send(
                    f"Session `{sname}` already exists. Use `/claude-attach` instead."
                )
                return

            cat_name = self.workspaces.category_name(project)
            ch_name = self.workspaces.channel_name(project, feature)

            category = await self._get_or_create_category(guild, cat_name)
            channel = await guild.create_text_channel(ch_name, category=category)

            info = await self.manager.create_session(project_slug, feature_slug, workspace_str)
            self.manager.update_channel_id(sname, channel.id)
            await self._create_pipe(sname, channel)

            await interaction.followup.send(
                f"Started session `{sname}` → {channel.mention}\n"
                f"Workspace: `{workspace_str}`"
            )

        @self.tree.command(
            name="claude-list",
            description="List all active Claude sessions",
        )
        async def claude_list(interaction: discord.Interaction) -> None:
            await interaction.response.defer()

            pipes = self.pipes.all_pipes()
            if not pipes:
                await interaction.followup.send("No active sessions.")
                return

            embed = discord.Embed(
                title="Active Claude Sessions",
                color=discord.Color.blue(),
            )
            for sname, pipe in pipes.items():
                info = self.manager.get_info(sname)
                ws = info.workspace if info else "unknown"
                embed.add_field(
                    name=sname,
                    value=f"Channel: {pipe.channel.mention}\nWorkspace: `{ws}`",
                    inline=False,
                )

            await interaction.followup.send(embed=embed)

        @self.tree.command(
            name="claude-stop",
            description="Stop a Claude session and detach the pipe",
        )
        @app_commands.describe(
            session="Session name (e.g. claude-backend-auth-refactor)",
        )
        async def claude_stop(
            interaction: discord.Interaction,
            session: str,
        ) -> None:
            await interaction.response.defer()

            pipe = self.pipes.get_by_session(session)
            if pipe is None:
                await interaction.followup.send(f"No active pipe for `{session}`.")
                return

            await self.pipes.remove(session)
            await self.manager.kill_session(session)
            await interaction.followup.send(f"Stopped session `{session}`.")

        @self.tree.command(
            name="delete-channel",
            description="Stop the session and delete the Discord channel",
        )
        @app_commands.describe(
            channel="The channel to delete",
        )
        async def delete_channel(
            interaction: discord.Interaction,
            channel: discord.TextChannel,
        ) -> None:
            await interaction.response.defer(ephemeral=True)

            pipe = self.pipes.get_by_channel(channel.id)
            if pipe:
                await self.pipes.remove(pipe.session_name)
                await self.manager.kill_session(pipe.session_name)

            channel_name = channel.name
            await channel.delete(reason="Cleaned up by Claude orchestrator")
            await interaction.followup.send(
                f"Deleted channel #{channel_name} and stopped its session.",
                ephemeral=True,
            )

        @self.tree.command(
            name="cleanup-category",
            description="Remove empty/stopped channels from a project category",
        )
        @app_commands.describe(
            project="Project name (category to clean up)",
        )
        async def cleanup_category(
            interaction: discord.Interaction,
            project: str,
        ) -> None:
            await interaction.response.defer()

            guild = interaction.guild
            if guild is None:
                await interaction.followup.send("This command only works in a server.")
                return

            display = sanitize_name(project).replace("-", " ").title()
            category = discord.utils.get(guild.categories, name=display)
            if category is None:
                await interaction.followup.send(f"Category '{display}' not found.")
                return

            deleted = []
            active = []
            for ch in category.text_channels:
                pipe = self.pipes.get_by_channel(ch.id)
                if pipe:
                    active.append(ch.name)
                else:
                    await ch.delete(reason="Cleanup: no active session")
                    deleted.append(ch.name)

            if not active and not category.text_channels:
                await category.delete(reason="Cleanup: empty category")
                msg = f"Deleted category '{display}' and {len(deleted)} channel(s)."
            else:
                msg_parts = []
                if deleted:
                    msg_parts.append(f"Deleted {len(deleted)} channel(s): {', '.join(deleted)}")
                if active:
                    msg_parts.append(
                        f"Skipped {len(active)} active channel(s): {', '.join(active)}"
                    )
                msg = "\n".join(msg_parts) or "Nothing to clean up."

            await interaction.followup.send(msg)

        # ── Workspace management ─────────────────────────────────────

        workspace_group = app_commands.Group(
            name="claude-workspace",
            description="Manage project base directories",
        )

        @workspace_group.command(
            name="add",
            description="Register a base directory for project discovery",
        )
        @app_commands.describe(path="Absolute path to a base directory (e.g. /home/you/projects)")
        async def workspace_add(interaction: discord.Interaction, path: str) -> None:
            p = Path(path).expanduser().resolve()
            if self.workspaces.add_base_dir(p):
                await interaction.response.send_message(
                    f"Added base directory: `{p}`\n"
                    f"All base dirs: {', '.join(f'`{d}`' for d in self.workspaces.base_dirs)}"
                )
            else:
                if not p.is_dir():
                    await interaction.response.send_message(f"`{p}` is not a valid directory.")
                else:
                    await interaction.response.send_message(f"`{p}` is already registered.")

        @workspace_group.command(
            name="remove",
            description="Remove a base directory from project discovery",
        )
        @app_commands.describe(path="Path to remove")
        async def workspace_remove(interaction: discord.Interaction, path: str) -> None:
            p = Path(path).expanduser().resolve()
            if self.workspaces.remove_base_dir(p):
                await interaction.response.send_message(
                    f"Removed base directory: `{p}`\n"
                    f"Remaining: {', '.join(f'`{d}`' for d in self.workspaces.base_dirs) or 'none'}"
                )
            else:
                await interaction.response.send_message(f"`{p}` is not a registered base directory.")

        @workspace_group.command(
            name="list",
            description="Show all registered base directories and discoverable projects",
        )
        async def workspace_list(interaction: discord.Interaction) -> None:
            await interaction.response.defer()

            dirs = self.workspaces.base_dirs
            if not dirs:
                await interaction.followup.send("No base directories registered.")
                return

            embed = discord.Embed(
                title="Workspace Base Directories",
                color=discord.Color.green(),
            )
            for base in dirs:
                # List immediate children as discoverable projects
                if base.is_dir():
                    children = sorted(
                        p.name for p in base.iterdir()
                        if p.is_dir() and not p.name.startswith(".")
                    )
                    value = ", ".join(f"`{c}`" for c in children[:15]) if children else "*empty*"
                    if len(children) > 15:
                        value += f" ... +{len(children) - 15} more"
                else:
                    value = "*directory not found*"
                embed.add_field(name=str(base), value=value, inline=False)

            await interaction.followup.send(embed=embed)

        self.tree.add_command(workspace_group)


def run_bot(settings: Settings) -> None:
    """Create and run the bot (blocking)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    bot = ClaudeBot(settings)
    bot.run(settings.bot_token, log_handler=None)
