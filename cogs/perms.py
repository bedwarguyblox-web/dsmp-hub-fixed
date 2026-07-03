"""
perms.py — /giveperms, /removeperms, /listperms commands.
Only the bot Owner can grant or revoke per-user/per-role command access.

The command autocomplete is built dynamically from the bot's live command tree,
so every current and future command is automatically grantable — no list to maintain.
"""

import discord
from discord import app_commands
from discord.ext import commands
import logging
from datetime import datetime, timezone
from typing import Optional

from utils.permissions import is_owner
from utils.database import add_perm_grant, remove_perm_grant, list_perm_grants

logger = logging.getLogger(__name__)


def _denied() -> discord.Embed:
    return discord.Embed(
        title="❌ Permission Denied",
        description="Only the **Owner** can manage bot access.",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )


def _collect_command_names(tree: app_commands.CommandTree, guild) -> list:
    """
    Walk the command tree and return a flat list of every grantable command name.
    Checks both globally-registered commands and guild-specific commands so that
    every current and future command is always discoverable — no list to maintain.
    """
    names = {"all"}
    # Global commands (registered without a guild — covers most slash commands)
    for cmd in tree.get_commands(guild=None):
        names.add(cmd.name)
    # Guild-specific commands (if any were synced to this guild)
    if guild:
        for cmd in tree.get_commands(guild=guild):
            names.add(cmd.name)
    return sorted(names)


async def _command_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Autocomplete that lists every registered command name from the live tree."""
    tree  = interaction.client.tree
    guild = interaction.guild

    # Pull guild-synced commands (these are what the server actually sees)
    all_names = _collect_command_names(tree, guild)

    # Always surface "all" first
    results: list[app_commands.Choice[str]] = []
    if "all" in all_names and (not current or "all".startswith(current.lower())):
        results.append(app_commands.Choice(name="all commands", value="all"))

    for name in all_names:
        if name == "all":
            continue
        if current.lower() in name.lower():
            results.append(app_commands.Choice(name=name, value=name))
        if len(results) >= 25:   # Discord hard limit
            break

    return results


class PermsCog(commands.Cog, name="Perms"):
    """Bot access permission management — Owner only."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /giveperms ────────────────────────────────────────────────────────────
    @app_commands.command(
        name="giveperms",
        description="Grant a user or role access to one or more bot commands (Owner only)",
    )
    @app_commands.describe(
        command="Command(s) to grant — type to search, or separate multiple with commas (e.g. strike, vouch, all)",
        user="The member to grant access to (leave blank to target a role)",
        role="The role to grant access to (leave blank to target a user)",
    )
    @app_commands.autocomplete(command=_command_autocomplete)
    async def giveperms(
        self,
        interaction: discord.Interaction,
        command: str,
        user: Optional[discord.Member] = None,
        role: Optional[discord.Role] = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if not is_owner(interaction.user):
            await interaction.followup.send(embed=_denied(), ephemeral=True)
            return

        if user is None and role is None:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Missing Target",
                    description="You must specify either a **user** or a **role**.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        if user is not None and role is not None:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Too Many Targets",
                    description="Please specify either a user **or** a role, not both.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        if user is not None and user.bot:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Invalid Target",
                    description="You cannot grant permissions to a bot.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        target_type = "user" if user else "role"
        target      = user or role

        # Support comma-separated list of commands
        cmd_list = [c.strip().lower() for c in command.split(",") if c.strip()]

        granted_new:  list[str] = []
        already_had:  list[str] = []

        for cmd_value in cmd_list:
            is_new = add_perm_grant(
                target_type, target.id, interaction.guild.id,
                cmd_value, interaction.user.id
            )
            if is_new:
                granted_new.append(cmd_value)
                logger.info(
                    "Perm granted: %s %s → %s (guild %s) by %s",
                    target_type, target.id, cmd_value, interaction.guild.id, interaction.user.id,
                )
            else:
                already_had.append(cmd_value)

        if not granted_new and already_had:
            desc = ", ".join(f"`{c}`" for c in already_had)
            embed = discord.Embed(
                title="ℹ️ Already Granted",
                description=f"{target.mention} already has access to {desc}.",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc),
            )
        else:
            embed = discord.Embed(
                title="✅ Permission(s) Granted",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Target",     value=f"{target.mention} ({target_type})",                inline=True)
            embed.add_field(name="Granted By", value=interaction.user.mention,                           inline=True)
            if granted_new:
                embed.add_field(name="✅ Newly Granted", value=", ".join(f"`{c}`" for c in granted_new), inline=False)
            if already_had:
                embed.add_field(name="ℹ️ Already Had",  value=", ".join(f"`{c}`" for c in already_had), inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /removeperms ──────────────────────────────────────────────────────────
    @app_commands.command(
        name="removeperms",
        description="Revoke a user or role's granted command access (Owner only)",
    )
    @app_commands.describe(
        command="Command to revoke access to",
        user="The member to revoke access from (leave blank to target a role)",
        role="The role to revoke access from (leave blank to target a user)",
    )
    @app_commands.autocomplete(command=_command_autocomplete)
    async def removeperms(
        self,
        interaction: discord.Interaction,
        command: str,
        user: Optional[discord.Member] = None,
        role: Optional[discord.Role] = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if not is_owner(interaction.user):
            await interaction.followup.send(embed=_denied(), ephemeral=True)
            return

        if user is None and role is None:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Missing Target",
                    description="You must specify either a **user** or a **role**.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        if user is not None and role is not None:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Too Many Targets",
                    description="Please specify either a user **or** a role, not both.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        target_type = "user" if user else "role"
        target      = user or role
        cmd_value   = command.strip().lower()

        removed = remove_perm_grant(target_type, target.id, interaction.guild.id, cmd_value)

        if not removed:
            embed = discord.Embed(
                title="ℹ️ Not Found",
                description=f"{target.mention} doesn't have a **`{cmd_value}`** grant to remove.",
                color=discord.Color.blue(),
            )
        else:
            embed = discord.Embed(
                title="✅ Permission Removed",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Target",     value=f"{target.mention} ({target_type})", inline=True)
            embed.add_field(name="Command",    value=f"`{cmd_value}`",                    inline=True)
            embed.add_field(name="Removed By", value=interaction.user.mention,            inline=True)
            logger.info(
                "Perm removed: %s %s → %s (guild %s) by %s",
                target_type, target.id, cmd_value, interaction.guild.id, interaction.user.id,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /listperms ────────────────────────────────────────────────────────────
    @app_commands.command(
        name="listperms",
        description="List all granted bot access permissions (Owner only)",
    )
    async def listperms(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not is_owner(interaction.user):
            await interaction.followup.send(embed=_denied(), ephemeral=True)
            return

        rows = list_perm_grants(interaction.guild.id)

        embed = discord.Embed(
            title="🔑 Bot Permission Grants",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )

        if not rows:
            embed.description = "No permissions granted yet.\nUse `/giveperms` to add one."
        else:
            lines = []
            for row in rows:
                if row["target_type"] == "user":
                    target = interaction.guild.get_member(row["target_id"])
                    label  = target.mention if target else f"`user:{row['target_id']}`"
                else:
                    target = interaction.guild.get_role(row["target_id"])
                    label  = target.mention if target else f"`role:{row['target_id']}`"

                cmd  = row["command_name"]
                date = str(row["granted_at"])[:10]
                lines.append(f"{label} → `{cmd}` (since {date})")

            embed.description = "\n".join(lines)
            embed.set_footer(text=f"{len(rows)} grant(s) active")

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PermsCog(bot))
