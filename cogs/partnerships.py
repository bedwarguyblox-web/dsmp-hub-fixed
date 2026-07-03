"""
partnerships.py — /partnership log, /partnership stats, /partnership leaderboard
Also auto-tracks partnerships by watching the configured partnership channel
for Discord invite links — each unique invite in a message = 1 partnership.
"""

import re
import discord
from discord import app_commands
from discord.ext import commands
import logging
from datetime import datetime, timezone
from typing import Optional

from utils.permissions import is_authorized, CONFIG
from utils.database import (
    log_partnership, get_partnership_count,
    get_recent_partnerships, get_partnership_leaderboard,
    get_total_partnerships, log_staff_action,
    add_partnerships_bulk, remove_partnerships_bulk,
    get_guild_config,
)
from utils.views import LeaderboardView, chunk_leaderboard

logger = logging.getLogger(__name__)

# Matches discord.gg/CODE, discord.com/invite/CODE, discordapp.com/invite/CODE
INVITE_RE = re.compile(
    r'discord(?:\.gg|(?:app)?\.com/invite)/([a-zA-Z0-9-]+)',
    re.IGNORECASE
)

# Partnership tier role names in rank order (index 0 = rank 1, etc.)
_PARTNERSHIP_TIERS = [
    "Head Partnership Manager",   # rank 1
    "Sr Partnership Manager",     # rank 2
    "Partnership Manager",        # rank 3
]
_PARTNERSHIP_FALLBACK = "Jr Partnership Manager"  # rank 4 and below


class PartnershipsCog(commands.Cog, name="Partnerships"):
    """Partnership tracking commands + auto-detection from channel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    partnership_group = app_commands.Group(
        name="partnership",
        description="Partnership tracking commands"
    )

    # ── Auto-track: watch partnership channel for invite links ───────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Skip bots and DMs
        if message.author.bot or not message.guild:
            return

        channel_id = CONFIG.get("PARTNERSHIP_CHANNEL_ID")
        if not channel_id or message.channel.id != channel_id:
            return

        # Find all unique invite codes in the message
        codes = list(dict.fromkeys(INVITE_RE.findall(message.content)))
        if not codes:
            return

        guild    = message.guild
        staff_id = message.author.id
        logged   = 0

        for code in codes:
            partner_name = f"discord.gg/{code}"
            log_partnership(
                staff_id, guild.id,
                partner_name=partner_name,
                notes=f"Auto-detected in #{message.channel.name}",
                invite_code=code,
            )
            log_staff_action(
                "partnership_auto", staff_id, guild.id,
                details=f"Invite: {code} | Channel: {message.channel.id}"
            )
            logged += 1

        if logged:
            try:
                await message.add_reaction("🤝")
            except (discord.Forbidden, discord.HTTPException):
                pass

            total = get_partnership_count(staff_id, guild.id)
            logger.info(
                "Auto-logged %d partnership(s) for %s (%s) — total now %d",
                logged, message.author, staff_id, total
            )

            await self._send_to_logs(guild, discord.Embed(
                title="🤝 Partnership Auto-Logged",
                description=(
                    f"**Staff:** {message.author.mention}\n"
                    f"**Invite(s):** {', '.join(f'discord.gg/{c}' for c in codes)}\n"
                    f"**Their Total:** {total}"
                ),
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            ))
            await self._update_partnership_roles(guild)

    # ── /partnership log ─────────────────────────────────────────────────────
    @partnership_group.command(
        name="log",
        description="Manually log a completed partnership"
    )
    @app_commands.describe(
        partner_name="Name of the server or person you partnered with",
        notes="Optional notes (invite link, deal details, etc.)"
    )
    async def partnership_log(
        self,
        interaction: discord.Interaction,
        partner_name: str,
        notes: Optional[str] = None,
    ):
        await interaction.response.defer()

        if not is_authorized(interaction.user, interaction.guild, "partnership"):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You must be **Admin** or above (or have been granted partnership access) to log partnerships.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        guild = interaction.guild
        log_partnership(interaction.user.id, guild.id, partner_name, notes)
        total = get_partnership_count(interaction.user.id, guild.id)

        log_staff_action(
            "partnership_log", interaction.user.id, guild.id,
            details=f"Partner: {partner_name} | Notes: {notes or 'none'}"
        )

        embed = discord.Embed(
            title="🤝 Partnership Logged",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="Logged By",  value=interaction.user.mention, inline=True)
        embed.add_field(name="Partner",    value=partner_name,             inline=True)
        embed.add_field(name="Your Total", value=str(total),               inline=True)
        if notes:
            embed.add_field(name="Notes", value=notes, inline=False)
        embed.set_footer(text=f"Staff ID: {interaction.user.id}")

        await interaction.followup.send(embed=embed)
        await self._send_to_logs(guild, embed)
        await self._update_partnership_roles(guild)

    # ── /partnership stats ───────────────────────────────────────────────────
    @partnership_group.command(
        name="stats",
        description="View partnership stats for yourself or another staff member"
    )
    @app_commands.describe(user="The staff member to check (defaults to yourself)")
    async def partnership_stats(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
    ):
        await interaction.response.defer()

        if not is_authorized(interaction.user, interaction.guild, "partnership"):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You must be **Admin** or above to view partnership stats.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        target = user or interaction.user
        guild  = interaction.guild

        total  = get_partnership_count(target.id, guild.id)
        recent = get_recent_partnerships(target.id, guild.id, 5)

        embed = discord.Embed(
            title=f"🤝 Partnership Stats — {target.display_name}",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Staff Member",       value=target.mention, inline=True)
        embed.add_field(name="Total Partnerships", value=str(total),     inline=True)

        if recent:
            lines = []
            for p in recent:
                ts    = str(p["timestamp"])[:10]
                notes = f" — {p['notes'][:50]}" if p["notes"] else ""
                # Show invite code badge if auto-tracked
                badge = " 🔗" if p["invite_code"] else ""
                lines.append(f"• **{p['partner_name']}**{badge} on {ts}{notes}")
            embed.add_field(
                name=f"Recent (last {len(recent)})",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(name="Recent", value="No partnerships logged yet.", inline=False)

        embed.set_footer(text=f"User ID: {target.id} • 🔗 = auto-tracked from channel")
        await interaction.followup.send(embed=embed)

    # ── /partnership leaderboard ─────────────────────────────────────────────
    @partnership_group.command(
        name="leaderboard",
        description="Top staff members by number of partnerships completed"
    )
    async def partnership_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not is_authorized(interaction.user, interaction.guild, "partnership"):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You must be **Admin** or above to view the partnership leaderboard.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        guild       = interaction.guild
        rows        = get_partnership_leaderboard(guild.id, 100)
        server_total = get_total_partnerships(guild.id)

        if not rows:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="🏆 Partnership Leaderboard",
                    description="No partnerships have been logged yet.\nPost an invite link in the partnership channel to get started.",
                    color=discord.Color.gold(),
                    timestamp=datetime.now(timezone.utc),
                )
            )
            return

        resolved = []
        for row in rows:
            member = guild.get_member(row["staff_id"])
            name   = member.display_name if member else f"ID:{row['staff_id']}"
            resolved.append({"name": name, "total": f"{row['total']} partnership(s)"})

        pages = chunk_leaderboard(resolved, name_key="name", total_key="total")
        view  = LeaderboardView(
            pages=pages,
            title="🏆 Partnership Leaderboard",
            color=discord.Color.gold(),
            footer=f"Server total: {server_total} | {len(rows)} staff ranked",
        )
        await interaction.followup.send(embed=view.make_embed(), view=view)

    # ── /partnershipadd ───────────────────────────────────────────────────────
    @app_commands.command(
        name="partnershipadd",
        description="Manually add a number of partnerships to a staff member's count"
    )
    @app_commands.describe(
        user="The staff member to credit",
        amount="How many partnerships to add"
    )
    async def partnershipadd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 500],
    ):
        await interaction.response.defer()

        if not is_authorized(interaction.user, interaction.guild, "partnership"):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You must be **Admin** or above to manually adjust partnerships.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        new_total = add_partnerships_bulk(user.id, interaction.guild.id, amount, interaction.user.id)

        log_staff_action(
            "partnership_add_manual", interaction.user.id, interaction.guild.id,
            target_id=user.id,
            details=f"Added {amount} partnership(s) manually | New total: {new_total}"
        )

        embed = discord.Embed(
            title="🤝 Partnerships Added",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Staff Member", value=user.mention,    inline=True)
        embed.add_field(name="Added By",     value=interaction.user.mention, inline=True)
        embed.add_field(name="Added",        value=f"+{amount}",    inline=True)
        embed.add_field(name="New Total",    value=str(new_total),  inline=True)
        embed.set_footer(text=f"User ID: {user.id}")

        await interaction.followup.send(embed=embed)
        await self._send_to_logs(interaction.guild, embed)
        await self._update_partnership_roles(interaction.guild)

    # ── /partnershipremove ────────────────────────────────────────────────────
    @app_commands.command(
        name="partnershipremove",
        description="Manually remove a number of partnerships from a staff member's count"
    )
    @app_commands.describe(
        user="The staff member to deduct from",
        amount="How many partnerships to remove"
    )
    async def partnershipremove(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 500],
    ):
        await interaction.response.defer()

        if not is_authorized(interaction.user, interaction.guild, "partnership"):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You must be **Admin** or above to manually adjust partnerships.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        removed, new_total = remove_partnerships_bulk(user.id, interaction.guild.id, amount)

        if removed == 0:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Nothing to Remove",
                    description=f"{user.mention} has no partnership entries to remove.",
                    color=discord.Color.yellow(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        log_staff_action(
            "partnership_remove_manual", interaction.user.id, interaction.guild.id,
            target_id=user.id,
            details=f"Removed {removed} partnership(s) manually | New total: {new_total}"
        )

        embed = discord.Embed(
            title="🤝 Partnerships Removed",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Staff Member", value=user.mention,    inline=True)
        embed.add_field(name="Removed By",   value=interaction.user.mention, inline=True)
        embed.add_field(name="Removed",      value=f"-{removed}",   inline=True)
        embed.add_field(name="New Total",    value=str(new_total),  inline=True)
        if removed < amount:
            embed.set_footer(text=f"⚠️ Only {removed} entry/entries existed — all removed. User ID: {user.id}")
        else:
            embed.set_footer(text=f"User ID: {user.id}")

        await interaction.followup.send(embed=embed)
        await self._send_to_logs(interaction.guild, embed)
        await self._update_partnership_roles(interaction.guild)

    # ── Partnership auto-role ─────────────────────────────────────────────────
    async def _update_partnership_roles(self, guild: discord.Guild):
        """Re-rank every member on the leaderboard and assign the correct tier role."""
        try:
            await self._do_update_partnership_roles(guild)
        except Exception as exc:
            logger.error("_update_partnership_roles failed for guild %s: %s", guild.id, exc, exc_info=True)

    async def _do_update_partnership_roles(self, guild: discord.Guild):
        staff_roles_cfg = CONFIG.get("STAFF_ROLES", {})

        # Collect all four tier role objects (skip any that aren't configured)
        all_tier_names = _PARTNERSHIP_TIERS + [_PARTNERSHIP_FALLBACK]
        tier_roles = {}
        for name in all_tier_names:
            rid = staff_roles_cfg.get(name)
            if rid:
                role = guild.get_role(int(rid))
                if role:
                    tier_roles[name] = role

        if not tier_roles:
            logger.warning("No partnership tier roles found in guild %s — skipping auto-role", guild.id)
            return

        all_tier_role_objs = list(tier_roles.values())

        # Full leaderboard — everyone with at least 1 partnership
        rows = get_partnership_leaderboard(guild.id, 500)

        # Build: staff_id → the role name they should hold
        assignments = {}
        for i, row in enumerate(rows):
            rank = i + 1
            if rank <= len(_PARTNERSHIP_TIERS):
                assignments[row["staff_id"]] = _PARTNERSHIP_TIERS[rank - 1]
            else:
                assignments[row["staff_id"]] = _PARTNERSHIP_FALLBACK

        # Apply changes to each ranked member
        for staff_id, correct_name in assignments.items():
            member = guild.get_member(staff_id)
            if not member:
                continue
            correct_role = tier_roles.get(correct_name)
            if not correct_role:
                continue

            roles_to_remove = [
                r for r in all_tier_role_objs
                if r != correct_role and r in member.roles
            ]
            needs_add = correct_role not in member.roles

            try:
                if roles_to_remove:
                    await member.remove_roles(
                        *roles_to_remove,
                        reason="Partnership leaderboard auto-role update"
                    )
                if needs_add:
                    await member.add_roles(
                        correct_role,
                        reason="Partnership leaderboard auto-role update"
                    )
                    logger.info(
                        "Auto-role: gave %s (%s) the '%s' role",
                        member, staff_id, correct_name
                    )
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning(
                    "Could not update partnership roles for %s: %s", staff_id, exc
                )

    # ── Internal log helper ───────────────────────────────────────────────────
    async def _send_to_logs(self, guild: discord.Guild, embed: discord.Embed):
        # Check guild_config DB first, then fall back to config.json
        channel_id_str = get_guild_config(guild.id, "partnership_logs_channel")
        if channel_id_str:
            channel_id = int(channel_id_str)
        else:
            channel_id = CONFIG.get("PARTNERSHIP_LOGS_CHANNEL_ID")

        if not channel_id:
            return
        ch = guild.get_channel(int(channel_id))
        if ch:
            try:
                await ch.send(embed=embed)
            except discord.Forbidden:
                logger.warning("Cannot send to partnership logs channel %s", channel_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(PartnershipsCog(bot))
