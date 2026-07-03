"""
activitycheck.py — /activitycheck command.

Sends a 24-hour staff activity check panel to the channel, pinging the staff role.
Staff click "✅ Mark Attendance" to check in.
Staff with the LOA role are automatically exempt.
After 24 hours, any staff member who did not respond receives a strike.
If a staff member reaches 3 strikes, all their staff roles are removed (auto-demotion).
"""

import asyncio
import discord
from discord import app_commands
from discord.ext import commands
import logging
from datetime import datetime, timezone, timedelta

from utils.permissions import is_authorized, CONFIG
from utils.database import (
    log_staff_action,
    add_strike,
    create_activity_check,
    update_activity_check_message,
    update_activity_check_status,
    get_activity_check,
    get_activity_check_by_message,
    get_all_active_activity_checks,
    record_activity_response,
    get_activity_responses,
)

logger = logging.getLogger(__name__)


async def _auto_demote_staff(bot, member: discord.Member, guild: discord.Guild,
                              strike_count: int, reason: str):
    """Remove all staff roles from a member and notify them. Called on 3+ strikes."""
    staff_roles_cfg = CONFIG.get("STAFF_ROLES", {})
    roles_to_remove = []
    for role_name, role_id in staff_roles_cfg.items():
        role = guild.get_role(int(role_id))
        if role and role in member.roles:
            roles_to_remove.append(role)

    if not roles_to_remove:
        return

    try:
        await member.remove_roles(
            *roles_to_remove,
            reason=f"Auto-demoted: {reason} ({strike_count} strikes)"
        )
        log_staff_action(
            "auto_demote", bot.user.id, guild.id,
            target_id=member.id,
            details=(
                f"Removed {len(roles_to_remove)} staff role(s) | "
                f"Strikes: {strike_count} | Reason: {reason}"
            )
        )
        logger.info(
            "Auto-demoted %s (%d) — %d staff role(s) removed",
            member, member.id, len(roles_to_remove)
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        logger.error("Failed to auto-demote %s: %s", member.id, exc)
        return

    removed_names = ", ".join(r.name for r in roles_to_remove)

    # DM the member
    try:
        await member.send(embed=discord.Embed(
            title="📋 Staff Demotion Notice",
            description=(
                f"You have been **automatically demoted** from **{guild.name}**.\n\n"
                f"**Reason:** {reason}\n"
                f"**Strike total:** {strike_count}\n"
                f"**Roles removed:** {removed_names}\n\n"
                f"Please contact staff management if you believe this is an error."
            ),
            color=discord.Color.dark_red(),
            timestamp=datetime.now(timezone.utc),
        ))
    except discord.Forbidden:
        pass

    # Post to staff logs channel
    logs_ch_id = CONFIG.get("STAFF_LOGS_CHANNEL_ID")
    if logs_ch_id:
        ch = guild.get_channel(int(logs_ch_id))
        if ch:
            embed = discord.Embed(
                title="🔴 Auto-Demotion",
                color=discord.Color.dark_red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="Staff Member", value=member.mention,    inline=True)
            embed.add_field(name="Strikes",      value=str(strike_count), inline=True)
            embed.add_field(name="Reason",       value=reason,            inline=True)
            embed.add_field(name="Roles Removed", value=removed_names,   inline=False)
            embed.set_footer(text=f"User ID: {member.id}")
            try:
                await ch.send(embed=embed)
            except discord.Forbidden:
                pass


class AttendanceView(discord.ui.View):
    """Persistent button view for the activity check panel.
    timeout=None makes it survive bot restarts when registered with bot.add_view()."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅ Mark Attendance",
        style=discord.ButtonStyle.success,
        custom_id="activity_check:attend",
    )
    async def attend(self, interaction: discord.Interaction, button: discord.ui.Button):
        check = get_activity_check_by_message(interaction.message.id)
        if not check:
            await interaction.response.send_message(
                "⚠️ This activity check could not be found.", ephemeral=True
            )
            return

        if check["status"] != "active":
            await interaction.response.send_message(
                "This activity check has already ended.", ephemeral=True
            )
            return

        is_new = record_activity_response(check["id"], interaction.user.id)
        if is_new:
            await interaction.response.send_message(
                "✅ Your attendance has been recorded!", ephemeral=True
            )
            logger.info(
                "Activity check #%d: %s (%d) marked attendance",
                check["id"], interaction.user, interaction.user.id
            )
        else:
            await interaction.response.send_message(
                "ℹ️ You've already marked your attendance for this check.",
                ephemeral=True
            )


class ActivityCheckCog(commands.Cog, name="ActivityCheck"):
    """24-hour staff activity check with auto-strike and auto-demotion."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._tasks = {}  # check_id -> asyncio.Task

    async def cog_load(self):
        """Register the persistent view and restore any active checks on startup."""
        self.bot.add_view(AttendanceView())

        active = get_all_active_activity_checks()
        now = datetime.now(timezone.utc)
        for check in active:
            deadline = datetime.fromisoformat(check["deadline"])
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            remaining = (deadline - now).total_seconds()
            if remaining > 0:
                task = asyncio.create_task(
                    self._run_timer(check["id"], check["guild_id"], remaining)
                )
                self._tasks[check["id"]] = task
                logger.info(
                    "Restored activity check #%d — %.0fs remaining",
                    check["id"], remaining
                )
            else:
                # Already expired while bot was offline — process immediately
                asyncio.create_task(
                    self._process_check(check["id"], check["guild_id"])
                )

    # ── /activitycheck ────────────────────────────────────────────────────────
    @app_commands.command(
        name="activitycheck",
        description="Send a 24-hour activity check panel — staff must mark attendance or receive a strike"
    )
    async def activitycheck(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not is_authorized(interaction.user, interaction.guild, "activitycheck"):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You must be **Admin** or above to run an activity check.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        guild    = interaction.guild
        deadline = datetime.now(timezone.utc) + timedelta(hours=24)
        ts       = int(deadline.timestamp())

        staff_role_id = CONFIG.get("STAFF_ROLE_ID")
        staff_role    = guild.get_role(int(staff_role_id)) if staff_role_id else None
        ping          = staff_role.mention if staff_role else "**@Staff**"

        check_id = create_activity_check(
            guild_id   = guild.id,
            channel_id = interaction.channel_id,
            actor_id   = interaction.user.id,
            deadline   = deadline.isoformat(),
        )

        embed = discord.Embed(
            title="📋 Staff Activity Check",
            description=(
                f"{ping} — please mark your attendance below.\n\n"
                f"⏰ **Deadline:** <t:{ts}:F> (<t:{ts}:R>)\n\n"
                f"• Staff on **LOA** are automatically exempt.\n"
                f"• Staff who do not respond will receive a **⚡ strike**.\n"
                f"• Reaching **3 strikes** triggers automatic demotion."
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(
            text=f"Check #{check_id} • Started by {interaction.user.display_name}"
        )

        view = AttendanceView()
        msg  = await interaction.followup.send(
            content=ping, embed=embed, view=view,
            allowed_mentions=discord.AllowedMentions(roles=True),
        )

        update_activity_check_message(check_id, msg.id)

        task = asyncio.create_task(
            self._run_timer(check_id, guild.id, 86400)
        )
        self._tasks[check_id] = task

        log_staff_action(
            "activity_check_started", interaction.user.id, guild.id,
            details=f"Check #{check_id} | Deadline: {deadline.isoformat()}"
        )

    # ── Timer & processing ─────────────────────────────────────────────────────
    async def _run_timer(self, check_id: int, guild_id: int, delay: float):
        await asyncio.sleep(delay)
        await self._process_check(check_id, guild_id)

    async def _process_check(self, check_id: int, guild_id: int):
        """After 24h: identify non-responders, strike them, demote if needed."""
        check = get_activity_check(check_id)
        if not check or check["status"] != "active":
            return

        update_activity_check_status(check_id, "completed")

        guild = self.bot.get_guild(guild_id)
        if not guild:
            logger.warning("Activity check #%d: guild %d not found", check_id, guild_id)
            return

        # LOA role (exempt from activity checks)
        loa_role_id = CONFIG.get("LOA_ROLE_ID")
        loa_role    = guild.get_role(int(loa_role_id)) if loa_role_id else None

        # Who already responded
        responses    = get_activity_responses(check_id)
        responded    = {r["staff_id"] for r in responses}

        # All staff role IDs from config
        staff_role_ids = {
            int(rid) for rid in CONFIG.get("STAFF_ROLES", {}).values()
        }

        struck   = []   # list of (member, new_strike_count)
        exempted = []   # list of member

        for member in guild.members:
            if member.bot:
                continue
            member_role_ids = {r.id for r in member.roles}
            # Only target members who hold at least one staff role
            if not (member_role_ids & staff_role_ids):
                continue
            # LOA exempt
            if loa_role and loa_role in member.roles:
                exempted.append(member)
                continue
            # Already checked in
            if member.id in responded:
                continue

            # Strike the non-responder
            new_count = add_strike(
                member.id, guild_id,
                moderator_id=self.bot.user.id,
                reason="Did not mark attendance in 24h activity check",
            )
            log_staff_action(
                "activity_check_strike", self.bot.user.id, guild_id,
                target_id=member.id,
                details=f"Check #{check_id} | New strike total: {new_count}"
            )
            struck.append((member, new_count))

            # DM the struck member
            try:
                await member.send(embed=discord.Embed(
                    title="⚡ Strike — Missed Activity Check",
                    description=(
                        f"You missed the **24-hour activity check** in **{guild.name}**.\n\n"
                        f"**New strike total:** {new_count}\n\n"
                        f"If you were on LOA and forgot to set the role, "
                        f"please contact staff management."
                    ),
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                ))
            except discord.Forbidden:
                pass

            # Auto-demote if they've hit 3+ strikes
            if new_count >= 3:
                await _auto_demote_staff(
                    self.bot, member, guild, new_count,
                    "Reached 3 strikes (activity check)"
                )

        # Post results to the original channel
        channel = guild.get_channel(check["channel_id"])
        if channel:
            await self._post_results(channel, check_id, struck, exempted, len(responded))

    async def _post_results(self, channel, check_id: int,
                             struck, exempted, responded_count: int):
        embed = discord.Embed(
            title="📋 Activity Check — Results",
            color=discord.Color.green() if not struck else discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="✅ Responded",   value=str(responded_count), inline=True)
        embed.add_field(name="🛡️ LOA Exempt", value=str(len(exempted)),   inline=True)
        embed.add_field(name="⚡ Struck",      value=str(len(struck)),     inline=True)

        if struck:
            lines = []
            for member, count in struck[:20]:
                line = f"{member.mention} — {count} strike(s)"
                if count >= 3:
                    line += "  🔴 **Auto-demoted**"
                lines.append(line)
            if len(struck) > 20:
                lines.append(f"… and {len(struck) - 20} more")
            embed.add_field(name="Struck Staff", value="\n".join(lines), inline=False)

        embed.set_footer(text=f"Check #{check_id}")
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning("Cannot post activity check results to channel %d", channel.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(ActivityCheckCog(bot))
