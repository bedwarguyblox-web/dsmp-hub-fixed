"""
serverify.py — /serverify command.

Scans all roles currently in the server that sit above the @Staff role,
compares their permissions against the centralised ROLE_PERMISSION_TEMPLATES,
and syncs them (add missing, remove incorrect).
Does NOT create new roles — only modifies existing ones.
"""

import discord
from discord import app_commands
from discord.ext import commands
import logging
from datetime import datetime, timezone

from utils.permissions import (
    is_authorized, ROLE_PERMISSION_TEMPLATES, CONFIG
)
from utils.database import log_staff_action

logger = logging.getLogger(__name__)

# Roles whose names indicate they are NOT staff roles and should be skipped
NON_STAFF_KEYWORDS = [
    "member", "giveaway", "notification", "notify", "notif",
    "ping", "colour", "color", "booster", "muted", "bot",
    "everyone", "@everyone", "patreon", "subscriber",
]


def _is_non_staff_role(role: discord.Role) -> bool:
    """
    Return True if the role name contains any non-staff keyword,
    or if the role has no permissions that indicate staff activity.
    """
    name_lower = role.name.lower()
    for kw in NON_STAFF_KEYWORDS:
        if kw in name_lower:
            return True
    return False


class ServerifyCog(commands.Cog, name="Serverify"):
    """Serverify — sync staff role permissions to templates."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="serverify",
        description="Sync all staff role permissions to the defined templates"
    )
    async def serverify(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        if not is_authorized(interaction.user, interaction.guild, "serverify"):
            embed = discord.Embed(
                title="❌ Permission Denied",
                description="You must be **Admin** or above to use `/serverify`.",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        guild        = interaction.guild
        staff_role_id = CONFIG.get("STAFF_ROLE_ID")
        staff_role    = guild.get_role(staff_role_id) if staff_role_id else None

        # Collect staff role IDs from config
        cfg_staff_ids: set[int] = {
            rid for rid in CONFIG.get("STAFF_ROLES", {}).values()
            if isinstance(rid, int)
        }

        roles_scanned   = 0
        roles_modified  = 0
        total_perms_added   = 0
        total_perms_removed = 0
        report_lines: list[str] = []

        for role in guild.roles:
            # Skip @everyone, the bot's own managed roles, and the Staff role itself
            if role.is_default():
                continue
            if role.managed:
                continue
            if staff_role and role <= staff_role:
                # Only process roles above the @Staff threshold
                continue
            # Skip obvious non-staff roles
            if _is_non_staff_role(role):
                continue
            # Only process roles that are in the configured STAFF_ROLES mapping
            if role.id not in cfg_staff_ids:
                continue

            # Find the template name for this role ID
            template_name = None
            for name, rid in CONFIG.get("STAFF_ROLES", {}).items():
                if rid == role.id and name in ROLE_PERMISSION_TEMPLATES:
                    template_name = name
                    break

            if template_name is None:
                continue

            roles_scanned += 1
            template: discord.Permissions = ROLE_PERMISSION_TEMPLATES[template_name]

            # Compute diff
            current_value  = role.permissions.value
            template_value = template.value
            needs_add    = template_value & ~current_value   # bits in template not in current
            needs_remove = current_value & ~template_value   # bits in current not in template

            if needs_add == 0 and needs_remove == 0:
                report_lines.append(f"✅ **{role.name}** — no changes needed")
                continue

            # Build new permission value
            new_perms = discord.Permissions(value=(current_value | needs_add) & ~needs_remove)

            # Count individual permission bits
            added_count   = bin(needs_add).count("1")
            removed_count = bin(needs_remove).count("1")

            try:
                await role.edit(
                    permissions=new_perms,
                    reason=f"Serverify by {interaction.user} — syncing to template '{template_name}'"
                )
                roles_modified      += 1
                total_perms_added   += added_count
                total_perms_removed += removed_count

                added_names   = self._perm_names(needs_add)
                removed_names = self._perm_names(needs_remove)

                line = f"🔄 **{role.name}**"
                if added_names:
                    line += f"\n  ➕ Added: {', '.join(added_names)}"
                if removed_names:
                    line += f"\n  ➖ Removed: {', '.join(removed_names)}"
                report_lines.append(line)

                logger.info(
                    "Serverify: modified role '%s' (id=%s) — +%d -%d perms",
                    role.name, role.id, added_count, removed_count
                )

            except discord.Forbidden:
                report_lines.append(f"❌ **{role.name}** — bot lacks permission to edit")
            except discord.HTTPException as e:
                report_lines.append(f"❌ **{role.name}** — HTTP error {e.status}")

        # Log to DB
        log_staff_action(
            "serverify", interaction.user.id, guild.id,
            details=(
                f"Scanned: {roles_scanned} | Modified: {roles_modified} | "
                f"+{total_perms_added} perms / -{total_perms_removed} perms"
            )
        )

        # ── Build report embed ──────────────────────────────────────────────
        embed = discord.Embed(
            title="⚙️ Serverify Report",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Roles Scanned",      value=str(roles_scanned),       inline=True)
        embed.add_field(name="Roles Modified",     value=str(roles_modified),      inline=True)
        embed.add_field(name="Permissions Added",  value=str(total_perms_added),   inline=True)
        embed.add_field(name="Permissions Removed",value=str(total_perms_removed), inline=True)
        embed.add_field(name="Performed By",       value=interaction.user.mention, inline=True)

        # Chunk report lines to avoid embed field limit (1024 chars per field)
        if report_lines:
            chunk, chunks = [], []
            for line in report_lines:
                chunk.append(line)
                if len("\n".join(chunk)) > 950:
                    chunks.append("\n".join(chunk[:-1]))
                    chunk = [line]
            if chunk:
                chunks.append("\n".join(chunk))
            for i, c in enumerate(chunks):
                embed.add_field(
                    name=f"Details (part {i+1})" if len(chunks) > 1 else "Details",
                    value=c,
                    inline=False
                )
        else:
            embed.add_field(name="Details", value="No matching staff roles found above @Staff.", inline=False)

        embed.set_footer(text="Only existing roles were modified — no new roles were created")
        await interaction.followup.send(embed=embed)

        # ── Log to staff logs channel ───────────────────────────────────────
        ch_id = CONFIG.get("STAFF_LOGS_CHANNEL_ID")
        if ch_id:
            ch = guild.get_channel(ch_id)
            if ch:
                try:
                    await ch.send(embed=embed)
                except discord.Forbidden:
                    pass

    @staticmethod
    def _perm_names(bitfield: int) -> list[str]:
        """Return human-readable names for set permission bits."""
        names = []
        for name, value in discord.Permissions.VALID_FLAGS.items():
            if bitfield & value:
                names.append(name.replace("_", " ").title())
        return names


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerifyCog(bot))
