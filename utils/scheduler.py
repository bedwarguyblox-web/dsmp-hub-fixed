"""
scheduler.py — Background task scheduler.

Tasks:
  1. Weekly strike reset — every Monday at 08:00 UTC+8 (00:00 UTC).
     Derives next run time from the current wall clock so it survives restarts.
  2. Builder-timer expiry — checks active 48-hour cases every minute and
     fires the owner-review embed when the deadline passes.

Both tasks survive bot restarts because they derive their next run time from
the current wall clock and re-attach to running cases stored in SQLite.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import tasks

from utils.permissions import CONFIG  # Use cached config

logger = logging.getLogger(__name__)

# UTC+8 offset
TZ_UTC8 = timezone(timedelta(hours=8))


def _next_weekly_monday_reset() -> float:
    """
    Return the number of seconds until the next Monday 08:00 UTC+8.
    """
    now_utc8 = datetime.now(TZ_UTC8)

    # Monday = weekday 0
    days_to_monday = (0 - now_utc8.weekday()) % 7  # 0 if already Monday
    candidate = now_utc8.replace(hour=8, minute=0, second=0, microsecond=0) \
                + timedelta(days=days_to_monday)

    # If today is Monday but already past 08:00, jump to next Monday
    if days_to_monday == 0 and now_utc8.replace(hour=8, minute=0, second=0, microsecond=0) <= now_utc8:
        candidate += timedelta(days=7)

    return (candidate - now_utc8).total_seconds()


class BotScheduler:
    """
    Attach this to a running bot instance.  Call `start(bot)` once the bot is ready.
    """

    def __init__(self):
        self.bot = None
        self._strike_reset_task = None
        self._timer_check_loop  = None

    def start(self, bot: discord.Client):
        self.bot = bot
        self._strike_reset_task = asyncio.create_task(self._strike_reset_loop())
        self._start_timer_check_loop()
        logger.info("Scheduler started.")

    # ── Strike reset ────────────────────────────────────────────────────────

    async def _strike_reset_loop(self):
        """Sleep until the next Monday 08:00 UTC+8, reset strikes, repeat."""
        while True:
            wait_secs = _next_weekly_monday_reset()
            logger.info(
                "Weekly strike reset scheduled in %.0f seconds (%.2f hours)",
                wait_secs, wait_secs / 3600
            )
            await asyncio.sleep(wait_secs)
            await self._do_strike_reset()

    async def _do_strike_reset(self):
        from utils.database import reset_all_strikes

        reset_count = 0
        for guild in self.bot.guilds:
            count = reset_all_strikes(guild.id)
            reset_count += count
            channel_id = CONFIG.get("STRIKE_LOGS_CHANNEL_ID")
            if channel_id:
                ch = guild.get_channel(channel_id)
                if ch:
                    embed = discord.Embed(
                        title="⚙️ Weekly Strike Reset",
                        description=(
                            f"All strikes have been automatically reset.\n"
                            f"**{count}** user record(s) cleared."
                        ),
                        color=discord.Color.blue(),
                        timestamp=datetime.now(timezone.utc),
                    )
                    embed.set_footer(text="Automated weekly reset — every Monday 08:00 UTC+8")
                    try:
                        await ch.send(embed=embed)
                    except discord.Forbidden:
                        logger.warning("Cannot send to strike logs channel %s", channel_id)

        logger.info("Weekly strike reset complete. %d records cleared.", reset_count)

    # ── Builder timer expiry ────────────────────────────────────────────────

    def _start_timer_check_loop(self):
        @tasks.loop(seconds=60)
        async def _check():
            await self._check_expired_timers()

        @_check.before_loop
        async def _before():
            await self.bot.wait_until_ready()

        self._timer_check_loop = _check
        _check.start()
        logger.info("Builder timer check loop started (60s interval).")

    async def _check_expired_timers(self):
        """Fire owner-review embeds for any 48-hour case that has just expired."""
        from utils.database import (
            get_pending_builder_cases,
            update_builder_case_status,
            log_builder_timer_event,
        )

        cases = get_pending_builder_cases()
        now = datetime.now(timezone.utc)

        for case in cases:
            end_time = datetime.fromisoformat(case["end_time"]).replace(tzinfo=timezone.utc)
            if now >= end_time:
                await self._fire_owner_review(case)

    async def _fire_owner_review(self, case):
        """Send the owner-review embed with Approve / Hold / Investigate buttons."""
        from utils.database import update_builder_case_status, log_builder_timer_event
        from cogs.builder import OwnerReviewView

        guild = self.bot.get_guild(case["guild_id"])
        if not guild:
            return

        channel_id = CONFIG.get("OWNER_REVIEW_CHANNEL_ID")
        ch = guild.get_channel(channel_id) if channel_id else None
        if not ch:
            logger.warning("Owner review channel %s not found", channel_id)
            return

        update_builder_case_status(case["case_id"], "awaiting_review")
        log_builder_timer_event(case["case_id"], "timer_expired", None, "48h timer expired")

        builder  = guild.get_member(case["builder_id"])
        customer = guild.get_member(case["customer_id"])

        embed = discord.Embed(
            title="🕐 Builder Timer Expired — Owner Review Required",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Case ID",    value=case["case_id"], inline=True)
        embed.add_field(name="Builder",    value=builder.mention  if builder  else str(case["builder_id"]),  inline=True)
        embed.add_field(name="Customer",   value=customer.mention if customer else str(case["customer_id"]), inline=True)
        embed.add_field(name="IGN",        value=case["ign"],     inline=True)
        embed.add_field(name="Amount",     value=case["amount"],  inline=True)
        embed.add_field(name="Start Time", value=case["start_time"], inline=True)
        embed.add_field(name="End Time",   value=case["end_time"],   inline=True)
        embed.set_footer(text="Only Owner and Head Admin may action this")

        owner = guild.get_member(CONFIG.get("OWNER_ID", 0))
        ping  = owner.mention if owner else "@Owner"

        view = OwnerReviewView(case["case_id"], CONFIG)
        try:
            await ch.send(
                content=f"{ping} — A builder timer has expired and requires your review.",
                embed=embed,
                view=view,
            )
        except discord.Forbidden:
            logger.warning("Cannot send to owner review channel %s", channel_id)

    def stop(self):
        if self._strike_reset_task:
            self._strike_reset_task.cancel()
        if self._timer_check_loop and self._timer_check_loop.is_running():
            self._timer_check_loop.cancel()
        logger.info("Scheduler stopped.")
