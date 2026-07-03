"""
setup.py — /setup command for per-server bot configuration.
Only the server owner (or bot owner) can run these commands.
Stores channel IDs and role grants in SQLite so the bot works on any server.
"""

import discord
from discord import app_commands
from discord.ext import commands
import logging
from datetime import datetime, timezone
from typing import Optional

from utils.permissions import is_owner, CONFIG
from utils.database import (
    get_guild_config, set_guild_config, get_all_guild_config,
    add_perm_grant,
)

logger = logging.getLogger(__name__)

# Friendly labels for config keys shown in /setup view
CHANNEL_KEY_LABELS = {
    "vouch_logs_channel":       "Vouch Logs",
    "strike_logs_channel":      "Strike Logs",
    "builder_logs_channel":     "Builder Logs",
    "partnership_logs_channel": "Partnership Logs",
    "staff_logs_channel":       "Staff Logs",
    "scam_vouch_logs_channel":  "Scam Vouch Logs",
}

COMMAND_CHOICES = [
    app_commands.Choice(name="All Commands",   value="all"),
    app_commands.Choice(name="Strikes",        value="strike"),
    app_commands.Choice(name="Vouches",        value="vouch"),
    app_commands.Choice(name="Partnerships",   value="partnership"),
    app_commands.Choice(name="Builder",        value="builder"),
    app_commands.Choice(name="Staff Info",     value="staff"),
    app_commands.Choice(name="Serverify",      value="serverify"),
]


def _is_setup_authorized(interaction: discord.Interaction) -> bool:
    """Allow server owner or bot owner."""
    if is_owner(interaction.user):
        return True
    if interaction.guild and interaction.user.id == interaction.guild.owner_id:
        return True
    return False


def _denied_embed() -> discord.Embed:
    return discord.Embed(
        title="❌ Permission Denied",
        description="Only the **server owner** can run `/setup`.",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )


class SetupCog(commands.Cog, name="Setup"):
    """Per-server configuration — server owner only."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    setup_group = app_commands.Group(
        name="setup",
        description="Configure the bot for this server (server owner only)"
    )

    # ── /setup logs ───────────────────────────────────────────────────────────
    @setup_group.command(
        name="logs",
        description="Set the log channels for each event type"
    )
    @app_commands.describe(
        vouch_logs="Channel where vouch events are posted",
        scam_vouch_logs="Channel where scam-vouch events are posted",
        strike_logs="Channel where strike events are posted",
        builder_logs="Channel where builder timer events are posted",
        partnership_logs="Channel where partnership events are posted",
        staff_logs="Channel where general staff actions are posted",
    )
    async def setup_logs(
        self,
        interaction: discord.Interaction,
        vouch_logs:       Optional[discord.TextChannel] = None,
        scam_vouch_logs:  Optional[discord.TextChannel] = None,
        strike_logs:      Optional[discord.TextChannel] = None,
        builder_logs:     Optional[discord.TextChannel] = None,
        partnership_logs: Optional[discord.TextChannel] = None,
        staff_logs:       Optional[discord.TextChannel] = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if not _is_setup_authorized(interaction):
            await interaction.followup.send(embed=_denied_embed(), ephemeral=True)
            return

        guild_id = interaction.guild.id
        updates = []

        mapping = {
            "vouch_logs_channel":       vouch_logs,
            "scam_vouch_logs_channel":  scam_vouch_logs,
            "strike_logs_channel":      strike_logs,
            "builder_logs_channel":     builder_logs,
            "partnership_logs_channel": partnership_logs,
            "staff_logs_channel":       staff_logs,
        }

        for key, channel in mapping.items():
            if channel is not None:
                set_guild_config(guild_id, key, str(channel.id))
                label = CHANNEL_KEY_LABELS.get(key, key)
                updates.append(f"✅ **{label}** → {channel.mention}")

        if not updates:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Nothing Changed",
                    description="You didn't pick any channels. Provide at least one channel to save.",
                    color=discord.Color.yellow(),
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="✅ Log Channels Updated",
            description="\n".join(updates),
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Run /setup view to see the full config")
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info("Guild %s log channels updated by %s", guild_id, interaction.user.id)

    # ── /setup roles ──────────────────────────────────────────────────────────
    @setup_group.command(
        name="roles",
        description="Allow one or more roles to use a specific command group"
    )
    @app_commands.describe(
        command="Which command group to grant access to",
        role1="First role to grant access",
        role2="Second role (optional)",
        role3="Third role (optional)",
        role4="Fourth role (optional)",
        role5="Fifth role (optional)",
    )
    @app_commands.choices(command=COMMAND_CHOICES)
    async def setup_roles(
        self,
        interaction: discord.Interaction,
        command: app_commands.Choice[str],
        role1: discord.Role,
        role2: Optional[discord.Role] = None,
        role3: Optional[discord.Role] = None,
        role4: Optional[discord.Role] = None,
        role5: Optional[discord.Role] = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if not _is_setup_authorized(interaction):
            await interaction.followup.send(embed=_denied_embed(), ephemeral=True)
            return

        guild_id = interaction.guild.id
        roles = [r for r in [role1, role2, role3, role4, role5] if r is not None]
        lines = []

        for role in roles:
            is_new = add_perm_grant("role", role.id, guild_id, command.value, interaction.user.id)
            status = "✅ Granted" if is_new else "ℹ️ Already had access"
            lines.append(f"{status} — {role.mention} → `{command.name}`")
            logger.info(
                "Setup: %s granted %s → %s in guild %s by %s",
                "new" if is_new else "existing",
                role.id, command.value, guild_id, interaction.user.id
            )

        embed = discord.Embed(
            title="🔑 Role Permissions Updated",
            description="\n".join(lines),
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="Use /setup view to see all granted roles")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /setup view ───────────────────────────────────────────────────────────
    @setup_group.command(
        name="view",
        description="View the current bot configuration for this server"
    )
    async def setup_view(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not _is_setup_authorized(interaction):
            await interaction.followup.send(embed=_denied_embed(), ephemeral=True)
            return

        guild_id = interaction.guild.id
        cfg = get_all_guild_config(guild_id)

        embed = discord.Embed(
            title="⚙️ Server Configuration",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )

        # ── Log channels ─────────────────────────────────────────────────────
        channel_lines = []
        for key, label in CHANNEL_KEY_LABELS.items():
            # Check guild_config DB first, fall back to config.json
            val = cfg.get(key)
            if val:
                ch = interaction.guild.get_channel(int(val))
                channel_lines.append(f"**{label}:** {ch.mention if ch else f'`{val}` (not found)'}")
            else:
                # Try falling back to config.json key
                config_key_map = {
                    "vouch_logs_channel":       "VOUCH_LOGS_CHANNEL_ID",
                    "scam_vouch_logs_channel":  "SCAM_VOUCH_LOGS_CHANNEL_ID",
                    "strike_logs_channel":      "STRIKE_LOGS_CHANNEL_ID",
                    "builder_logs_channel":     "BUILDER_LOGS_CHANNEL_ID",
                    "partnership_logs_channel": "PARTNERSHIP_LOGS_CHANNEL_ID",
                    "staff_logs_channel":       "STAFF_LOGS_CHANNEL_ID",
                }
                fallback_id = CONFIG.get(config_key_map.get(key, ""))
                if fallback_id:
                    ch = interaction.guild.get_channel(fallback_id)
                    channel_lines.append(f"**{label}:** {ch.mention if ch else f'`{fallback_id}`'} *(from config.json)*")
                else:
                    channel_lines.append(f"**{label}:** *not set*")

        embed.add_field(
            name="📢 Log Channels",
            value="\n".join(channel_lines) or "*none configured*",
            inline=False,
        )

        # ── Role grants ───────────────────────────────────────────────────────
        from utils.database import list_perm_grants
        grants = list_perm_grants(guild_id)
        role_grants = [g for g in grants if g["target_type"] == "role"]

        if role_grants:
            role_lines = []
            for g in role_grants:
                role = interaction.guild.get_role(g["target_id"])
                role_label = role.mention if role else f"`role:{g['target_id']}`"
                role_lines.append(f"{role_label} → `{g['command_name']}`")
            embed.add_field(
                name="🔑 Role Permissions",
                value="\n".join(role_lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="🔑 Role Permissions",
                value="*No roles granted yet — use `/setup roles` to add some.*",
                inline=False,
            )

        embed.set_footer(text=f"Guild ID: {guild_id} • Use /setup logs or /setup roles to change")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))
