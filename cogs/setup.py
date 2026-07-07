"""
setup.py — /setup command for per-server bot configuration.
Only the server owner (or bot owner) can run these commands.
Stores channel IDs and role grants in SQLite so the bot works on any server.
"""

import discord
from discord import app_commands
from discord.ext import commands
import logging
import json
from datetime import datetime, timezone
from typing import Optional

from utils.permissions import is_owner, CONFIG
from utils.database import (
    get_guild_config, set_guild_config, get_all_guild_config,
    add_perm_grant, list_perm_grants,
)

logger = logging.getLogger(__name__)


# ── Helpers for /setup list ────────────────────────────────────────────────────

def _ch(guild: discord.Guild, val) -> str:
    """Resolve a channel id (str or int or None) to a mention or '⚠ not found'."""
    if not val:
        return "*not set*"
    try:
        ch = guild.get_channel(int(val))
        return ch.mention if ch else f"⚠ `{val}` (deleted?)"
    except (ValueError, TypeError):
        return f"`{val}`"


def _role(guild: discord.Guild, val) -> str:
    """Resolve a role id (str or int or None) to a mention or '⚠ not found'."""
    if not val:
        return "*not set*"
    try:
        r = guild.get_role(int(val))
        return r.mention if r else f"⚠ `{val}` (deleted?)"
    except (ValueError, TypeError):
        return f"`{val}`"


def _cat(guild: discord.Guild, val) -> str:
    """Resolve a category id to its name, or '⚠ not found'."""
    if not val:
        return "*not set*"
    try:
        ch = guild.get_channel(int(val))
        return f"📁 {ch.name}" if ch else f"⚠ `{val}` (deleted?)"
    except (ValueError, TypeError):
        return f"`{val}`"

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

    # ── /setup list ───────────────────────────────────────────────────────────
    @setup_group.command(
        name="list",
        description="Show every configured setting for this server — channels, roles, permissions, and more",
    )
    async def setup_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not _is_setup_authorized(interaction):
            await interaction.followup.send(embed=_denied_embed(), ephemeral=True)
            return

        guild    = interaction.guild
        guild_id = guild.id
        cfg      = get_all_guild_config(guild_id)    # full DB row dict for this guild
        now      = datetime.now(timezone.utc)

        embeds: list[discord.Embed] = []

        # ── 1. Log Channels ──────────────────────────────────────────────────
        e1 = discord.Embed(title="📢 Log Channels", color=discord.Color.blurple(), timestamp=now)

        config_fallback = {
            "vouch_logs_channel":       "VOUCH_LOGS_CHANNEL_ID",
            "scam_vouch_logs_channel":  "SCAM_VOUCH_LOGS_CHANNEL_ID",
            "strike_logs_channel":      "STRIKE_LOGS_CHANNEL_ID",
            "builder_logs_channel":     "BUILDER_LOGS_CHANNEL_ID",
            "partnership_logs_channel": "PARTNERSHIP_LOGS_CHANNEL_ID",
            "staff_logs_channel":       "STAFF_LOGS_CHANNEL_ID",
        }
        log_rows = []
        for db_key, label in CHANNEL_KEY_LABELS.items():
            val = cfg.get(db_key)
            if val:
                log_rows.append(f"**{label}:** {_ch(guild, val)}")
            else:
                fb_id = CONFIG.get(config_fallback.get(db_key, ""), None)
                if fb_id:
                    log_rows.append(f"**{label}:** {_ch(guild, fb_id)} *(config.json)*")
                else:
                    log_rows.append(f"**{label}:** *not set*")

        e1.description = "\n".join(log_rows) or "*none configured*"
        e1.set_footer(text="Set with /setup logs")
        embeds.append(e1)

        # ── 2. Global Roles & Channels (config.json) ─────────────────────────
        e2 = discord.Embed(title="⚙️ Global Roles & Channels", color=discord.Color.og_blurple(), timestamp=now)
        e2.description = (
            "_These values come from_ `config.json` _and apply to every server._"
        )

        global_roles = [
            ("Staff Role",               CONFIG.get("STAFF_ROLE_ID")),
            ("LOA Role",                 CONFIG.get("LOA_ROLE_ID")),
            ("Partnership Ping Role",    CONFIG.get("PARTNERSHIP_PING_ROLE_ID")),
            ("Partnership Member Role",  CONFIG.get("PARTNERSHIP_MEMBER_ROLE_ID")),
        ]
        global_channels = [
            ("Partnership Channel",       CONFIG.get("PARTNERSHIP_CHANNEL_ID")),
            ("Partnership Ticket Channel",CONFIG.get("PARTNERSHIP_TICKET_CHANNEL_ID")),
            ("Owner Review Channel",      CONFIG.get("OWNER_REVIEW_CHANNEL_ID")),
            ("Ticket Staff Channel",      CONFIG.get("TICKET_STAFF_CHANNEL_ID")),
        ]
        global_misc = [
            ("Ticket Staff Role",         CONFIG.get("TICKET_STAFF_ROLE_ID")),
            ("Balance API URL",           CONFIG.get("balanceApiUrl") or "*not set*"),
        ]

        role_lines = "\n".join(f"**{lbl}:** {_role(guild, val)}" for lbl, val in global_roles)
        ch_lines   = "\n".join(f"**{lbl}:** {_ch(guild, val)}" for lbl, val in global_channels)
        misc_lines = []
        for lbl, val in global_misc:
            if lbl == "Balance API URL":
                misc_lines.append(f"**{lbl}:** `{val}`")
            else:
                misc_lines.append(f"**{lbl}:** {_role(guild, val)}")

        e2.add_field(name="Roles", value=role_lines or "*none*", inline=False)
        e2.add_field(name="Channels", value=ch_lines or "*none*", inline=False)
        e2.add_field(name="Other", value="\n".join(misc_lines) or "*none*", inline=False)
        embeds.append(e2)

        # ── 3. Staff Roles Hierarchy ──────────────────────────────────────────
        e3 = discord.Embed(title="👥 Staff Role Hierarchy", color=discord.Color.dark_gold(), timestamp=now)
        e3.description = "_All ranks defined in_ `config.json → STAFF_ROLES`_._"
        staff_roles: dict = CONFIG.get("STAFF_ROLES", {})
        if staff_roles:
            lines = []
            for rank_name, role_id in staff_roles.items():
                r = guild.get_role(int(role_id)) if role_id else None
                mention = r.mention if r else f"⚠ `{role_id}` (not found)"
                lines.append(f"`{rank_name:<26}` {mention}")
            # Discord field value limit is 1024 chars; split if needed
            chunk, chunks = [], []
            for line in lines:
                chunk.append(line)
                if len("\n".join(chunk)) > 900:
                    chunks.append("\n".join(chunk[:-1]))
                    chunk = [line]
            if chunk:
                chunks.append("\n".join(chunk))
            for i, text in enumerate(chunks, 1):
                e3.add_field(name=f"Ranks {i}" if len(chunks) > 1 else "Ranks", value=text, inline=False)
        else:
            e3.description = "*No STAFF_ROLES defined in config.json.*"
        embeds.append(e3)

        # ── 4. Marketplace (Listings) ─────────────────────────────────────────
        e4 = discord.Embed(title="🏪 Marketplace (Listings)", color=discord.Color.green(), timestamp=now)

        listing_cfg = {
            "Listings Channel":    _ch(guild,   cfg.get("listing_channel_id")),
            "Log Channel":         _ch(guild,   cfg.get("listing_log_channel_id")  or CONFIG.get("logChannelId")),
            "Admin Role":          _role(guild, cfg.get("listing_admin_role_id")   or CONFIG.get("adminRoleId")),
            "Ticket Category":     _cat(guild,  cfg.get("listing_ticket_category_id") or CONFIG.get("ticketCategoryId")),
            "Appeal Category":     _cat(guild,  cfg.get("listing_appeal_category_id") or CONFIG.get("appealCategoryId")),
            "Balance API":         f'`{CONFIG.get("balanceApiUrl") or "not set"}`',
            "Preview Timeout":     f'{CONFIG.get("previewTimeoutSeconds", 120)}s',
            "Ticket Close Delay":  f'{CONFIG.get("ticketCloseDelayMs", 300000) // 1000}s',
        }

        panel_msg_id = cfg.get("listing_panel_message_id") or CONFIG.get("listingPanelMessageId")
        ch_id        = cfg.get("listing_channel_id") or CONFIG.get("listingChannelId")
        if panel_msg_id and ch_id:
            listing_cfg["Panel Message"] = f"[Jump](https://discord.com/channels/{guild_id}/{ch_id}/{panel_msg_id})"
        else:
            listing_cfg["Panel Message"] = "*not posted yet*"

        e4.description = "\n".join(f"**{k}:** {v}" for k, v in listing_cfg.items())
        e4.set_footer(text="Set with /listingsetup and /listingadmin")
        embeds.append(e4)

        # ── 5. Verification ───────────────────────────────────────────────────
        e5 = discord.Embed(title="✅ Verification", color=discord.Color.teal(), timestamp=now)
        verify_ch   = cfg.get("verify_channel_id")
        verify_role = cfg.get("verify_role_id")
        lines5 = [
            f"**Verify Channel:** {_ch(guild, verify_ch)}",
            f"**Auto-assign Role:** {_role(guild, verify_role)}",
        ]

        raw_verify_roles = cfg.get("verify_roles")
        if raw_verify_roles:
            try:
                vr_list = json.loads(raw_verify_roles)
                vr_lines = []
                for entry in vr_list:
                    r = guild.get_role(int(entry["role_id"]))
                    mention = r.mention if r else f"⚠ `{entry['role_id']}`"
                    vr_lines.append(f"{mention} — {entry.get('label', '?')}")
                lines5.append("**Self-assign Roles:**\n" + "\n".join(f"  • {l}" for l in vr_lines))
            except Exception:
                lines5.append("**Self-assign Roles:** *(parse error)*")
        else:
            # Fall back to config.json VERIFY_ROLES
            cfg_vr = CONFIG.get("VERIFY_ROLES", [])
            if cfg_vr:
                vr_lines = []
                for entry in cfg_vr:
                    r = guild.get_role(int(entry["role_id"]))
                    mention = r.mention if r else f"⚠ `{entry['role_id']}`"
                    vr_lines.append(f"{mention} — {entry.get('label', '?')}")
                lines5.append("**Self-assign Roles** *(config.json)*:\n" + "\n".join(f"  • {l}" for l in vr_lines))
            else:
                lines5.append("**Self-assign Roles:** *not configured*")

        e5.description = "\n".join(lines5)
        e5.set_footer(text="Set with /verify setup")
        embeds.append(e5)

        # ── 6. Partnerships & Giveaways ───────────────────────────────────────
        e6 = discord.Embed(title="🤝 Partnerships & 🎉 Giveaways", color=discord.Color.purple(), timestamp=now)

        partner_lines = [
            f"**Partnership Channel:** {_ch(guild, CONFIG.get('PARTNERSHIP_CHANNEL_ID'))} *(config.json)*",
            f"**Partnership Ticket Ch:** {_ch(guild, CONFIG.get('PARTNERSHIP_TICKET_CHANNEL_ID'))} *(config.json)*",
            f"**Ping Role:** {_role(guild, CONFIG.get('PARTNERSHIP_PING_ROLE_ID'))} *(config.json)*",
            f"**Member Role:** {_role(guild, CONFIG.get('PARTNERSHIP_MEMBER_ROLE_ID'))} *(config.json)*",
        ]
        pr_ch  = cfg.get("partnershipreq_channel_id")
        pr_msg = cfg.get("partnershipreq_message_id")
        if pr_ch:
            partner_lines.append(f"**Req Embed Channel:** {_ch(guild, pr_ch)}")
            if pr_msg:
                partner_lines.append(f"**Req Embed Message:** [Jump](https://discord.com/channels/{guild_id}/{pr_ch}/{pr_msg})")
        else:
            partner_lines.append("**Req Embed Channel:** *not set* — run `/partnershipreq setup`")

        e6.add_field(name="🤝 Partnerships", value="\n".join(partner_lines), inline=False)

        gw_role = cfg.get("giveaway_ping_role_id")
        e6.add_field(
            name="🎉 Giveaways",
            value=f"**Ping Role:** {_role(guild, gw_role)}",
            inline=False,
        )
        embeds.append(e6)

        # ── 7. Role Permissions ───────────────────────────────────────────────
        e7 = discord.Embed(title="🔑 Role Permissions", color=discord.Color.gold(), timestamp=now)
        grants = list_perm_grants(guild_id)
        role_grants   = [g for g in grants if g["target_type"] == "role"]
        user_grants   = [g for g in grants if g["target_type"] == "user"]

        if role_grants:
            by_cmd: dict[str, list[str]] = {}
            for g in role_grants:
                r = guild.get_role(g["target_id"])
                mention = r.mention if r else f"⚠ `{g['target_id']}`"
                by_cmd.setdefault(g["command_name"], []).append(mention)
            role_perm_lines = []
            for cmd, mentions in sorted(by_cmd.items()):
                role_perm_lines.append(f"**`{cmd}`** — {', '.join(mentions)}")
            e7.add_field(name="Roles", value="\n".join(role_perm_lines), inline=False)
        else:
            e7.add_field(name="Roles", value="*No role grants — use `/setup roles` to add.*", inline=False)

        if user_grants:
            user_lines = []
            for g in user_grants:
                user_lines.append(f"<@{g['target_id']}> → `{g['command_name']}`")
            e7.add_field(name="Individual Users", value="\n".join(user_lines), inline=False)

        e7.set_footer(text="Set with /setup roles")
        embeds.append(e7)

        # ── Send all embeds ───────────────────────────────────────────────────
        # Discord allows max 10 embeds per message; we have 7 so we're safe.
        header = discord.Embed(
            title=f"⚙️ Full Server Configuration — {guild.name}",
            description=(
                "Everything configured for this server, organised by system.\n"
                f"Guild ID: `{guild_id}` • {len(embeds)} sections below."
            ),
            color=discord.Color.blurple(),
            timestamp=now,
        )
        header.set_thumbnail(url=guild.icon.url if guild.icon else discord.Embed.Empty)
        await interaction.followup.send(embeds=[header, *embeds], ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))
