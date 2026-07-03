"""
staff.py — /staff addroles and /staff removeroles commands.
Only Staff Manager and above may use these commands.
Respects Discord role hierarchy at all times.
"""

import discord
from discord import app_commands
from discord.ext import commands
import logging
from datetime import datetime, timezone
import json, os

from utils.permissions import is_at_least, is_authorized, can_manage_specific_role, CONFIG
from utils.database import log_staff_action

logger = logging.getLogger(__name__)


class StaffCog(commands.Cog, name="Staff"):
    """Staff role management commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /staff group ────────────────────────────────────────────────────────
    staff_group = app_commands.Group(name="staff", description="Staff management commands")

    # ── Helper: resolve comma-separated user mentions/IDs to Member objects ──
    def _resolve_members(self, guild: discord.Guild, users_str: str) -> tuple[list[discord.Member], list[str]]:
        """
        Parse a comma-separated string of user mentions or IDs.
        Returns (found_members, not_found_tokens).
        """
        found, missing = [], []
        for token in [t.strip() for t in users_str.split(",") if t.strip()]:
            member = None
            # Try mention format <@ID> or <@!ID>
            if token.startswith("<@") and token.endswith(">"):
                raw = token[2:-1].lstrip("!")
                try:
                    member = guild.get_member(int(raw))
                except ValueError:
                    pass
            # Try raw ID
            if member is None:
                try:
                    member = guild.get_member(int(token))
                except ValueError:
                    pass
            if member:
                found.append(member)
            else:
                missing.append(token)
        return found, missing

    # ── Helper: resolve comma-separated role names to Role objects ──────────
    def _resolve_roles(self, guild: discord.Guild, roles_str: str) -> tuple[list[discord.Role], list[str]]:
        """
        Parse a comma-separated string of role names / mentions / IDs.
        Returns (found_roles, not_found_names).
        """
        found, missing = [], []
        for token in [t.strip() for t in roles_str.split(",") if t.strip()]:
            role = None
            # Try mention format <@&ID>
            if token.startswith("<@&") and token.endswith(">"):
                try:
                    role = guild.get_role(int(token[3:-1]))
                except ValueError:
                    pass
            # Try raw ID
            if role is None:
                try:
                    role = guild.get_role(int(token))
                except ValueError:
                    pass
            # Try name (case-insensitive)
            if role is None:
                role = discord.utils.find(
                    lambda r, t=token: r.name.lower() == t.lower(),
                    guild.roles
                )
            if role:
                found.append(role)
            else:
                missing.append(token)
        return found, missing

    # ── /staff addroles ─────────────────────────────────────────────────────
    @staff_group.command(name="addroles", description="Add multiple roles to a user at once")
    @app_commands.describe(
        user="The member to receive the roles",
        roles="Comma-separated list of role names, mentions, or IDs"
    )
    async def addroles(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        roles: str
    ):
        await interaction.response.defer(ephemeral=True)

        if not is_authorized(interaction.user, interaction.guild, "addroles"):
            embed = discord.Embed(
                title="❌ Permission Denied",
                description="You must be **Admin** or above to use this command.",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        guild = interaction.guild
        found_roles, missing = self._resolve_roles(guild, roles)

        if not found_roles:
            embed = discord.Embed(
                title="❌ No Roles Found",
                description=f"Could not find any roles matching: `{roles}`",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        added, skipped, failed = [], [], []

        for role in found_roles:
            # Hierarchy check: actor's top role must be above the role being assigned
            if not can_manage_specific_role(interaction.user, role):
                skipped.append(f"{role.name} (above your rank)")
                continue
            # Don't add a role the user already has
            if role in user.roles:
                skipped.append(f"{role.name} (already assigned)")
                continue
            try:
                await user.add_roles(role, reason=f"Added by {interaction.user} via /staff addroles")
                added.append(role.name)
                log_staff_action(
                    "add_role", interaction.user.id, guild.id,
                    target_id=user.id, details=f"Role: {role.name}"
                )
            except discord.Forbidden:
                failed.append(f"{role.name} (bot lacks permission)")
            except discord.HTTPException as e:
                failed.append(f"{role.name} (HTTP error: {e.status})")

        # ── Build response embed ────────────────────────────────────────────
        embed = discord.Embed(
            title="📋 Role Assignment Results",
            color=discord.Color.green() if added else discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Target", value=user.mention, inline=True)
        embed.add_field(name="Performed By", value=interaction.user.mention, inline=True)
        if added:
            embed.add_field(name=f"✅ Added ({len(added)})", value="\n".join(added), inline=False)
        if skipped:
            embed.add_field(name=f"⚠️ Skipped ({len(skipped)})", value="\n".join(skipped), inline=False)
        if failed:
            embed.add_field(name=f"❌ Failed ({len(failed)})", value="\n".join(failed), inline=False)
        if missing:
            embed.add_field(name="🔍 Not Found", value="\n".join(missing), inline=False)
        embed.set_footer(text="Staff Role Management")

        await interaction.followup.send(embed=embed, ephemeral=False)

        # ── Log to staff logs channel ───────────────────────────────────────
        await self._send_to_logs(guild, embed)

    # ── /staff removeroles ──────────────────────────────────────────────────
    @staff_group.command(name="removeroles", description="Remove multiple roles from a user at once")
    @app_commands.describe(
        user="The member to lose the roles",
        roles="Comma-separated list of role names, mentions, or IDs"
    )
    async def removeroles(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        roles: str
    ):
        await interaction.response.defer(ephemeral=True)

        if not is_authorized(interaction.user, interaction.guild, "removeroles"):
            embed = discord.Embed(
                title="❌ Permission Denied",
                description="You must be **Admin** or above to use this command.",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        guild = interaction.guild
        found_roles, missing = self._resolve_roles(guild, roles)

        if not found_roles:
            embed = discord.Embed(
                title="❌ No Roles Found",
                description=f"Could not find any roles matching: `{roles}`",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        removed, skipped, failed = [], [], []

        for role in found_roles:
            if not can_manage_specific_role(interaction.user, role):
                skipped.append(f"{role.name} (above your rank)")
                continue
            if role not in user.roles:
                skipped.append(f"{role.name} (not assigned)")
                continue
            try:
                await user.remove_roles(role, reason=f"Removed by {interaction.user} via /staff removeroles")
                removed.append(role.name)
                log_staff_action(
                    "remove_role", interaction.user.id, guild.id,
                    target_id=user.id, details=f"Role: {role.name}"
                )
            except discord.Forbidden:
                failed.append(f"{role.name} (bot lacks permission)")
            except discord.HTTPException as e:
                failed.append(f"{role.name} (HTTP error: {e.status})")

        embed = discord.Embed(
            title="📋 Role Removal Results",
            color=discord.Color.green() if removed else discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Target", value=user.mention, inline=True)
        embed.add_field(name="Performed By", value=interaction.user.mention, inline=True)
        if removed:
            embed.add_field(name=f"✅ Removed ({len(removed)})", value="\n".join(removed), inline=False)
        if skipped:
            embed.add_field(name=f"⚠️ Skipped ({len(skipped)})", value="\n".join(skipped), inline=False)
        if failed:
            embed.add_field(name=f"❌ Failed ({len(failed)})", value="\n".join(failed), inline=False)
        if missing:
            embed.add_field(name="🔍 Not Found", value="\n".join(missing), inline=False)
        embed.set_footer(text="Staff Role Management")

        await interaction.followup.send(embed=embed, ephemeral=False)
        await self._send_to_logs(guild, embed)

    # ── /staff bulkroles ────────────────────────────────────────────────────
    @staff_group.command(name="bulkroles", description="Add and/or remove roles from multiple users at once")
    @app_commands.describe(
        users="Comma-separated user mentions or IDs",
        add_roles="Comma-separated roles to ADD (leave blank to skip)",
        remove_roles="Comma-separated roles to REMOVE (leave blank to skip)",
    )
    async def bulkroles(
        self,
        interaction: discord.Interaction,
        users: str,
        add_roles: str = "",
        remove_roles: str = "",
    ):
        await interaction.response.defer(ephemeral=True)

        if not is_authorized(interaction.user, interaction.guild, "bulkroles"):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You must be **Admin** or above to use this command.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        if not add_roles.strip() and not remove_roles.strip():
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Nothing to do",
                    description="Provide at least one role in `add_roles` or `remove_roles`.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        guild = interaction.guild
        members, missing_users = self._resolve_members(guild, users)
        roles_to_add, missing_add = self._resolve_roles(guild, add_roles) if add_roles.strip() else ([], [])
        roles_to_remove, missing_remove = self._resolve_roles(guild, remove_roles) if remove_roles.strip() else ([], [])

        if not members:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ No Users Found",
                    description=f"Could not find any members matching: `{users}`",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        results: list[str] = []

        for member in members:
            added, skipped_add, removed, skipped_remove, failed = [], [], [], [], []

            for role in roles_to_add:
                if not can_manage_specific_role(interaction.user, role):
                    skipped_add.append(f"{role.name} (above your rank)")
                    continue
                if role in member.roles:
                    skipped_add.append(f"{role.name} (already has)")
                    continue
                try:
                    await member.add_roles(role, reason=f"Bulk add by {interaction.user} via /staff bulkroles")
                    added.append(role.name)
                    log_staff_action("add_role", interaction.user.id, guild.id, target_id=member.id, details=f"Role: {role.name} (bulk)")
                except (discord.Forbidden, discord.HTTPException) as e:
                    failed.append(f"+{role.name} (error)")

            for role in roles_to_remove:
                if not can_manage_specific_role(interaction.user, role):
                    skipped_remove.append(f"{role.name} (above your rank)")
                    continue
                if role not in member.roles:
                    skipped_remove.append(f"{role.name} (doesn't have)")
                    continue
                try:
                    await member.remove_roles(role, reason=f"Bulk remove by {interaction.user} via /staff bulkroles")
                    removed.append(role.name)
                    log_staff_action("remove_role", interaction.user.id, guild.id, target_id=member.id, details=f"Role: {role.name} (bulk)")
                except (discord.Forbidden, discord.HTTPException) as e:
                    failed.append(f"-{role.name} (error)")

            parts = []
            if added:
                parts.append(f"✅ Added: {', '.join(added)}")
            if removed:
                parts.append(f"🗑️ Removed: {', '.join(removed)}")
            if skipped_add + skipped_remove:
                parts.append(f"⚠️ Skipped: {', '.join(skipped_add + skipped_remove)}")
            if failed:
                parts.append(f"❌ Failed: {', '.join(failed)}")
            results.append(f"**{member.display_name}** — " + " | ".join(parts) if parts else f"**{member.display_name}** — no changes")

        embed = discord.Embed(
            title="📋 Bulk Role Results",
            description="\n".join(results),
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Performed By", value=interaction.user.mention, inline=True)
        embed.add_field(name="Users Processed", value=str(len(members)), inline=True)
        if missing_users:
            embed.add_field(name="🔍 Users Not Found", value=", ".join(missing_users), inline=False)
        if missing_add:
            embed.add_field(name="🔍 Add-Roles Not Found", value=", ".join(missing_add), inline=False)
        if missing_remove:
            embed.add_field(name="🔍 Remove-Roles Not Found", value=", ".join(missing_remove), inline=False)
        embed.set_footer(text="Staff Bulk Role Management")

        await interaction.followup.send(embed=embed, ephemeral=False)
        await self._send_to_logs(guild, embed)

    # ── /staffinfo ──────────────────────────────────────────────────────────
    @app_commands.command(name="staffinfo", description="View staff information for a user")
    @app_commands.describe(user="The member to look up")
    async def staffinfo(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer()

        from utils.permissions import get_staff_rank
        from utils.database import get_strike_count, get_vouch_counts

        guild = interaction.guild
        rank = get_staff_rank(user) or "No staff rank"
        strike_count = get_strike_count(user.id, guild.id)
        vouches, scam_vouches = get_vouch_counts(user.id, guild.id)

        # Collect all non-default roles
        user_roles = [r.mention for r in reversed(user.roles) if r != guild.default_role]

        embed = discord.Embed(
            title=f"👤 Staff Info — {user.display_name}",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="User",        value=user.mention,                              inline=True)
        embed.add_field(name="Staff Rank",  value=rank,                                      inline=True)
        embed.add_field(name="Joined Server", value=f"<t:{int(user.joined_at.timestamp())}:D>" if user.joined_at else "Unknown", inline=True)
        embed.add_field(name="⚡ Strikes",  value=str(strike_count),                          inline=True)
        embed.add_field(name="✅ Vouches",  value=str(vouches),                               inline=True)
        embed.add_field(name="🚨 Scam Vouches", value=str(scam_vouches),                      inline=True)
        embed.add_field(
            name=f"Roles ({len(user_roles)})",
            value=", ".join(user_roles[:20]) if user_roles else "None",
            inline=False
        )
        embed.set_footer(text=f"ID: {user.id}")
        await interaction.followup.send(embed=embed)

    # ── Internal: send embed to staff logs channel ──────────────────────────
    async def _send_to_logs(self, guild: discord.Guild, embed: discord.Embed):
        channel_id = CONFIG.get("STAFF_LOGS_CHANNEL_ID")
        if not channel_id:
            return
        ch = guild.get_channel(channel_id)
        if ch:
            try:
                await ch.send(embed=embed)
            except discord.Forbidden:
                logger.warning("Cannot send to staff logs channel %s", channel_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(StaffCog(bot))
