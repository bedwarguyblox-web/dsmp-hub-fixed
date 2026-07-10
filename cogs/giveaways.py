"""
giveaways.py — /giveaway, /quickdrop, /rerollgiveaway, /giveawaycancel, /giveawayset commands.
Button-based entry with live entry count displayed on the button.

'ends' parameter accepts:
  • A duration     — 30s, 5m, 2h, 1d   (draws after that time)
  • A member goal  — "500 members"      (draws when the SERVER reaches 500 members)

PERSISTENT: Giveaways survive bot restarts via database storage.
"""

import asyncio
import random
import logging
import uuid
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from utils.permissions import is_authorized
from utils.database import (
    get_guild_config, set_guild_config,
    create_giveaway, get_active_giveaways, get_giveaway,
    get_giveaway_by_message, add_giveaway_entry, get_giveaway_entries,
    get_giveaway_entry_count, end_giveaway, cancel_giveaway
)

logger = logging.getLogger(__name__)

MEMBER_GOAL_POLL_INTERVAL = 10
MEMBER_GOAL_MAX_SECONDS   = 30 * 24 * 3600


def parse_ends(s: str):
    """
    Returns ('time', seconds) or ('members', count) or None.
    'members' mode = draw when the SERVER has that many total members.
    """
    s = s.strip().lower()
    for suffix in (" members", " member"):
        if s.endswith(suffix):
            num = s[: -len(suffix)].strip()
            if num.isdigit() and int(num) >= 1:
                return ("members", int(num))
            return None
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if len(s) >= 2 and s[-1] in multipliers and s[:-1].isdigit():
        return ("time", int(s[:-1]) * multipliers[s[-1]])
    if s.isdigit():
        return ("time", int(s))
    return None


class PersistentGiveawayView(discord.ui.View):
    """Persistent view that works across bot restarts."""

    def __init__(self, giveaway_id: str, prize: str, is_quickdrop: bool):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id
        self.prize = prize
        self.is_quickdrop = is_quickdrop

        count = get_giveaway_entry_count(giveaway_id)
        self._entry_button = discord.ui.Button(
            label=f"🎉 Enter — {count} {'entry' if count == 1 else 'entries'}",
            style=discord.ButtonStyle.primary,
            custom_id=f"giveaway:enter:{giveaway_id}",
        )
        self._entry_button.callback = self._on_enter
        self.add_item(self._entry_button)

    async def _on_enter(self, interaction: discord.Interaction):
        is_new = add_giveaway_entry(self.giveaway_id, interaction.user.id)
        if not is_new:
            await interaction.response.send_message("✅ You're already entered!", ephemeral=True)
            return

        count = get_giveaway_entry_count(self.giveaway_id)
        self._entry_button.label = f"🎉 Enter — {count} {'entry' if count == 1 else 'entries'}"
        await interaction.response.edit_message(view=self)
        logger.info("Giveaway entry: %s (%d) — total %d", interaction.user, interaction.user.id, count)

    def disable(self):
        self._entry_button.disabled = True
        self._entry_button.style = discord.ButtonStyle.secondary


class GiveawaysCog(commands.Cog, name="Giveaways"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._finished:       dict[int, list[int]] = {}   # msg_id → entrant user IDs (for reroll)
        self._active:         dict[int, asyncio.Task] = {} # msg_id → running task
        self._staff_cancelled: set[int] = set()            # msg_ids explicitly cancelled via /giveawaycancel

    async def cog_load(self):
        """Register persistent views and re-schedule all active giveaway timers on startup."""
        active = get_active_giveaways()
        for gw in active:
            # Re-register button views so interactions still work
            view = PersistentGiveawayView(
                gw["giveaway_id"], gw["prize"], bool(gw["is_quickdrop"])
            )
            self.bot.add_view(view)
            logger.info(
                "Registered persistent view for giveaway %s (msg %d)",
                gw["giveaway_id"], gw["message_id"]
            )

            # Re-create the end timer so the draw fires after restart
            task = asyncio.create_task(self._resume_giveaway(dict(gw)))
            self._active[gw["message_id"]] = task

        if active:
            logger.info("Resumed %d active giveaway timer(s) from database", len(active))

    # ── Config helpers ────────────────────────────────────────────────────────
    def _ping_role(self, guild: discord.Guild) -> discord.Role | None:
        raw = get_guild_config(guild.id, "giveaway_ping_role_id")
        if raw:
            return guild.get_role(int(raw))
        return None

    # ── Shared cancellation helper ────────────────────────────────────────────
    async def _cancel_giveaway_embed(
        self,
        msg: discord.Message,
        giveaway_id: str,
        prize: str,
        is_quickdrop: bool,
        kind_tag: str,
        host_name: str,
    ):
        """Edit the embed to cancelled state and mark the DB. Safe to call from any task."""
        view = PersistentGiveawayView(giveaway_id, prize, is_quickdrop)
        view.disable()
        cancelled_embed = discord.Embed(
            title=f"🚫 {kind_tag} CANCELLED — {prize}",
            description=(
                f"**Prize:** {prize}\n"
                f"**Entries at cancellation:** {get_giveaway_entry_count(giveaway_id)}\n\n"
                "This giveaway was cancelled by a staff member. No winner was drawn."
            ),
            color=discord.Color.dark_red(),
            timestamp=datetime.now(timezone.utc),
        )
        cancelled_embed.set_footer(text=f"Hosted by {host_name} • Cancelled")
        try:
            await msg.edit(embed=cancelled_embed, view=view)
        except discord.NotFound:
            pass
        end_giveaway(giveaway_id, 'cancelled')

    # ── Shared draw logic ─────────────────────────────────────────────────────
    async def _finish_giveaway(
        self,
        msg: discord.Message,
        giveaway_id: str,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
        host,            # discord.Member | discord.User | None
        prize: str,
        num_winners: int,
        is_quickdrop: bool,
        kind_tag: str,
    ):
        """
        Pick winners, edit the giveaway embed to show results, post the
        winner announcement, DM each winner, and mark the giveaway ended in DB.
        Wrapped in try/except so any failure is logged rather than silently lost.
        """
        try:
            # Atomic guard: transition active → ended right now.
            # Only one concurrent caller can win; the other sees rowcount=0 and aborts.
            # This also catches cancellations that raced with the sleep completing.
            if not end_giveaway(giveaway_id, 'ended'):
                logger.info(
                    "Giveaway %s already ended/cancelled — skipping draw", giveaway_id
                )
                return

            entries     = get_giveaway_entries(giveaway_id)
            entrant_ids = [e["user_id"] for e in entries]
            self._finished[msg.id] = entrant_ids  # keep for /rerollgiveaway

            entrants = [m for uid in entrant_ids if (m := guild.get_member(uid)) and not m.bot]

            actual_winners = min(num_winners, len(entrants))
            if entrants:
                winners        = random.sample(entrants, actual_winners)
                winner_mentions = ", ".join(w.mention for w in winners)
                win_text        = f"🏆 **Winner{'s' if actual_winners > 1 else ''}:** {winner_mentions}"
            else:
                winners         = []
                winner_mentions = ""
                win_text        = "😢 Nobody entered — no winners this time!"

            host_name = host.display_name if host else "Unknown"

            # Edit original embed to show winner(s)
            view = PersistentGiveawayView(giveaway_id, prize, is_quickdrop)
            view.disable()
            ended_embed = discord.Embed(
                title=f"{kind_tag} ENDED — {prize}",
                description=(
                    f"**Prize:** {prize}\n"
                    f"**Total Entries:** {len(entrant_ids)}\n\n"
                    f"{win_text}"
                ),
                color=discord.Color.dark_grey(),
                timestamp=datetime.now(timezone.utc),
            )
            ended_embed.set_footer(text=f"Hosted by {host_name} • Ended")

            try:
                await msg.edit(embed=ended_embed, view=view)
            except discord.NotFound:
                logger.warning("Giveaway %s: message deleted before update", giveaway_id)

            host_ref = host.mention if host else "the host"

            if winners:
                # Public announcement
                await channel.send(
                    content=winner_mentions,
                    embed=discord.Embed(
                        title=f"🎉 Congratulations {winner_mentions}!",
                        description=(
                            f"You won **{prize}**!\n\n"
                            f"Contact {host_ref} to claim your prize."
                        ),
                        color=discord.Color.gold(),
                        timestamp=datetime.now(timezone.utc),
                    ),
                    allowed_mentions=discord.AllowedMentions(users=True),
                )

                # DM each winner
                for winner in winners:
                    try:
                        await winner.send(embed=discord.Embed(
                            title="🎉 You won a giveaway!",
                            description=(
                                f"You won **{prize}** in the **{guild.name}** "
                                f"{'quickdrop' if is_quickdrop else 'giveaway'}!\n\n"
                                f"Contact {host_ref} to claim your prize."
                            ),
                            color=discord.Color.gold(),
                            timestamp=datetime.now(timezone.utc),
                        ))
                    except discord.Forbidden:
                        logger.info(
                            "Giveaway %s: could not DM winner %s — DMs closed",
                            giveaway_id, winner,
                        )
            else:
                await channel.send(
                    embed=discord.Embed(
                        title="😔 No one entered this giveaway.",
                        description=(
                            f"Nobody entered the {'quickdrop' if is_quickdrop else 'giveaway'} "
                            f"for **{prize}**."
                        ),
                        color=discord.Color.dark_grey(),
                        timestamp=datetime.now(timezone.utc),
                    )
                )

            logger.info(
                "Giveaway %s ended — prize: %s, winners: %s",
                giveaway_id, prize,
                [w.id for w in winners] if winners else "none",
            )

        except Exception:
            logger.exception("Error finishing giveaway %s", giveaway_id)

    # ── Resume active giveaways after restart ─────────────────────────────────
    async def _resume_giveaway(self, gw: dict):
        """
        Re-attach a single active giveaway after a bot restart.
        Computes how much time remains, waits that long, then draws.
        If the end time already passed, draws immediately.
        """
        giveaway_id  = gw["giveaway_id"]
        message_id   = gw["message_id"]
        channel_id   = gw["channel_id"]
        guild_id     = gw["guild_id"]
        host_id      = gw["host_id"]
        prize        = gw["prize"]
        end_mode     = gw["end_mode"]
        end_value    = gw["end_value"]   # seconds (for "time") or member count (for "members")
        num_winners  = gw["num_winners"]
        is_quickdrop = bool(gw["is_quickdrop"])
        kind_tag     = "⚡ QUICKDROP" if is_quickdrop else "🎉 GIVEAWAY"

        try:
            await self.bot.wait_until_ready()

            guild = self.bot.get_guild(guild_id)
            if not guild:
                logger.warning("Giveaway %s: guild %d not found on resume", giveaway_id, guild_id)
                return

            channel = guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden):
                    logger.warning(
                        "Giveaway %s: channel %d not found — marking ended",
                        giveaway_id, channel_id,
                    )
                    end_giveaway(giveaway_id, 'ended')
                    return

            try:
                msg = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden):
                logger.warning(
                    "Giveaway %s: message %d not found — marking ended",
                    giveaway_id, message_id,
                )
                end_giveaway(giveaway_id, 'ended')
                return

            # Host may have left the server; fall back to a plain User object
            host = guild.get_member(host_id)
            if host is None:
                try:
                    host = await self.bot.fetch_user(host_id)
                except (discord.NotFound, discord.HTTPException):
                    host = None

            host_name = host.display_name if host else "Unknown"

            if end_mode == "time":
                # Compute remaining seconds from stored created_at + duration
                created_at_str = gw.get("created_at", "")
                try:
                    created_at = datetime.strptime(
                        created_at_str, "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    # Fallback: draw immediately if we can't parse the timestamp
                    created_at = datetime.now(timezone.utc)
                    end_value  = 0

                end_at    = created_at.timestamp() + end_value
                remaining = end_at - datetime.now(timezone.utc).timestamp()

                if remaining <= 0:
                    logger.info(
                        "Giveaway %s expired during downtime (%.0fs ago) — drawing now",
                        giveaway_id, -remaining,
                    )
                    await self._finish_giveaway(
                        msg, giveaway_id, guild, channel, host,
                        prize, num_winners, is_quickdrop, kind_tag,
                    )
                    return

                logger.info(
                    "Giveaway %s resumed — %.0fs remaining", giveaway_id, remaining
                )
                try:
                    await asyncio.sleep(remaining)
                except asyncio.CancelledError:
                    if message_id in self._staff_cancelled:
                        self._staff_cancelled.discard(message_id)
                        await self._cancel_giveaway_embed(
                            msg, giveaway_id, prize, is_quickdrop, kind_tag, host_name
                        )
                    return

            else:
                # Member-goal mode: resume polling
                elapsed = 0
                cancelled = False
                while elapsed < MEMBER_GOAL_MAX_SECONDS:
                    if (guild.member_count or 0) >= end_value:
                        break
                    try:
                        await asyncio.sleep(MEMBER_GOAL_POLL_INTERVAL)
                    except asyncio.CancelledError:
                        cancelled = True
                        break
                    elapsed += MEMBER_GOAL_POLL_INTERVAL
                if cancelled:
                    if message_id in self._staff_cancelled:
                        self._staff_cancelled.discard(message_id)
                        await self._cancel_giveaway_embed(
                            msg, giveaway_id, prize, is_quickdrop, kind_tag, host_name
                        )
                    return

            await self._finish_giveaway(
                msg, giveaway_id, guild, channel, host,
                prize, num_winners, is_quickdrop, kind_tag,
            )

        except asyncio.CancelledError:
            # Task was cancelled during setup (wait_until_ready / channel or
            # message fetch) before reaching the inner sleep/poll handlers.
            # Only act if this was an explicit staff cancel — system-level
            # cancellations (shutdown/cog reload) must not mutate DB or embed
            # so the giveaway is resumed correctly on the next restart.
            if message_id in self._staff_cancelled:
                self._staff_cancelled.discard(message_id)
                try:
                    g = self.bot.get_guild(guild_id)
                    if g:
                        ch = g.get_channel(channel_id)
                        if ch is None:
                            try:
                                ch = await g.fetch_channel(channel_id)
                            except Exception:
                                ch = None
                        if ch:
                            m = await ch.fetch_message(message_id)
                            h = g.get_member(host_id)
                            await self._cancel_giveaway_embed(
                                m, giveaway_id, prize, is_quickdrop, kind_tag,
                                h.display_name if h else "Unknown",
                            )
                except Exception:
                    logger.debug(
                        "Giveaway %s: could not update embed on setup-phase cancel",
                        giveaway_id,
                    )
        except Exception:
            logger.exception("Unhandled error resuming giveaway %s", giveaway_id)
        finally:
            self._active.pop(message_id, None)
            self._staff_cancelled.discard(message_id)  # clean up any stale entry

    # ── Background wait-and-draw task ─────────────────────────────────────────
    async def _wait_and_draw(
        self,
        msg: discord.Message,
        giveaway_id: str,
        guild: discord.Guild,
        channel: discord.TextChannel,
        host: discord.Member,
        prize: str,
        end_mode: str,
        end_value: int,
        num_winners: int,
        is_quickdrop: bool,
        kind_tag: str,
    ):
        try:
            if end_mode == "time":
                await asyncio.sleep(end_value)
            else:
                elapsed = 0
                while elapsed < MEMBER_GOAL_MAX_SECONDS:
                    if (guild.member_count or 0) >= end_value:
                        break
                    await asyncio.sleep(MEMBER_GOAL_POLL_INTERVAL)
                    elapsed += MEMBER_GOAL_POLL_INTERVAL

        except asyncio.CancelledError:
            # ── Cancelled by /giveawaycancel (or system shutdown/reload) ─────
            # Only update embed + DB when a staff member explicitly requested
            # the cancel; system-level cancellations leave state untouched so
            # the giveaway is picked up correctly on the next restart.
            if msg.id in self._staff_cancelled:
                await self._cancel_giveaway_embed(
                    msg, giveaway_id, prize, is_quickdrop, kind_tag,
                    host.display_name if host else "Unknown",
                )
            return

        finally:
            self._active.pop(msg.id, None)
            self._staff_cancelled.discard(msg.id)  # clean up any stale entry

        # ── Normal end: delegate to shared draw logic ─────────────────────────
        await self._finish_giveaway(
            msg, giveaway_id, guild, channel, host,
            prize, num_winners, is_quickdrop, kind_tag,
        )

    # ── Core runner ───────────────────────────────────────────────────────────
    async def _run(
        self,
        interaction: discord.Interaction,
        prize: str,
        ends_str: str,
        num_winners: int,
        is_quickdrop: bool,
    ):
        await interaction.response.defer(ephemeral=True)

        cmd_name = "quickdrop" if is_quickdrop else "giveaway"
        if not is_authorized(interaction.user, interaction.guild, cmd_name):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description=f"You must be **Admin** or above (or granted `{cmd_name}` access) to use this command.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        parsed = parse_ends(ends_str)
        if parsed is None:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Invalid 'ends' Value",
                    description=(
                        "Use a **duration** (`30s`, `5m`, `2h`, `1d`) "
                        "or a **server member goal** (`500 members`).\n"
                        "Minimum duration: 5 seconds — maximum: 7 days."
                    ),
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        end_mode, end_value = parsed
        if end_mode == "time" and (end_value < 5 or end_value > 604800):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Invalid Duration",
                    description="Minimum **5 seconds**, maximum **7 days**.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        kind_tag = "⚡ QUICKDROP" if is_quickdrop else "🎉 GIVEAWAY"
        color = discord.Color.orange() if is_quickdrop else discord.Color.blue()
        guild = interaction.guild

        # Generate persistent giveaway ID
        giveaway_id = f"GW-{uuid.uuid4().hex[:12].upper()}"

        if end_mode == "time":
            end_ts = int(datetime.now(timezone.utc).timestamp()) + end_value
            end_line = f"**Ends:** <t:{end_ts}:R> (<t:{end_ts}:t>)"
            ftr_sfx = "Ends at"
            embed_ts = datetime.fromtimestamp(end_ts, tz=timezone.utc)
        else:
            current = guild.member_count or 0
            end_line = (
                f"**Member Goal:** {end_value:,} server members\n"
                f"**Current Members:** {current:,}"
            )
            ftr_sfx = "Draws at member goal"
            embed_ts = datetime.now(timezone.utc)

        embed = discord.Embed(
            title=f"{kind_tag} — {prize}",
            description=(
                f"Click **🎉 Enter** below to join!\n\n"
                f"**Prize:** {prize}\n"
                f"**Winners:** {num_winners}\n"
                f"{end_line}"
            ),
            color=color,
            timestamp=embed_ts,
        )
        embed.set_footer(text=f"Hosted by {interaction.user.display_name} • {ftr_sfx}")

        ping_role = self._ping_role(guild)
        view = PersistentGiveawayView(giveaway_id, prize, is_quickdrop)

        if ping_role:
            msg = await interaction.channel.send(
                content=ping_role.mention,
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        else:
            msg = await interaction.channel.send(embed=embed, view=view)

        # Store in database for persistence
        create_giveaway(
            giveaway_id, msg.id, interaction.channel.id, guild.id,
            interaction.user.id, prize, end_mode, end_value, num_winners, is_quickdrop
        )

        await interaction.followup.send(
            embed=discord.Embed(
                description=(
                    f"✅ {'Quickdrop' if is_quickdrop else 'Giveaway'} started in {interaction.channel.mention}!\n"
                    f"**Message ID:** `{msg.id}` *(use this with `/giveawaycancel` if needed)*\n"
                    f"**Giveaway ID:** `{giveaway_id}`"
                ),
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )

        logger.info(
            "%s started by %s in %s — prize: %s, mode: %s=%s, winners: %d",
            kind_tag, interaction.user, guild.name, prize, end_mode, end_value, num_winners,
        )

        task = asyncio.create_task(
            self._wait_and_draw(
                msg, giveaway_id, guild, interaction.channel,
                interaction.user, prize,
                end_mode, end_value, num_winners, is_quickdrop, kind_tag,
            )
        )
        self._active[msg.id] = task

    # ── /giveaway ─────────────────────────────────────────────────────────────
    @app_commands.command(name="giveaway", description="Start a giveaway in this channel")
    @app_commands.describe(
        prize="What you're giving away",
        ends="Duration (30s, 5m, 2h, 1d) OR server member goal (500 members)",
        winners="Number of winners (1–10, default 1)",
    )
    async def giveaway(
        self,
        interaction: discord.Interaction,
        prize: str,
        ends: str,
        winners: app_commands.Range[int, 1, 10] = 1,
    ):
        await self._run(interaction, prize, ends, winners, is_quickdrop=False)

    # ── /quickdrop ────────────────────────────────────────────────────────────
    @app_commands.command(name="quickdrop", description="Start a flash quickdrop in this channel")
    @app_commands.describe(
        prize="What you're dropping",
        ends="Duration (30s, 5m, 2h) OR server member goal (100 members)",
        winners="Number of winners (1–10, default 1)",
    )
    async def quickdrop(
        self,
        interaction: discord.Interaction,
        prize: str,
        ends: str,
        winners: app_commands.Range[int, 1, 10] = 1,
    ):
        await self._run(interaction, prize, ends, winners, is_quickdrop=True)

    # ── /giveawaycancel ───────────────────────────────────────────────────────
    @app_commands.command(name="giveawaycancel", description="Cancel an active giveaway without drawing a winner")
    @app_commands.describe(message_id="Message ID of the active giveaway to cancel")
    async def giveawaycancel(
        self,
        interaction: discord.Interaction,
        message_id: str,
    ):
        if not is_authorized(interaction.user, interaction.guild, "giveaway"):
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You must be **Admin** or above (or granted `giveaway` access) to cancel a giveaway.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        try:
            mid = int(message_id)
        except ValueError:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ Invalid Message ID",
                    description="Please provide a valid message ID.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        # Check in-memory active tasks first
        task = self._active.get(mid)
        if task and not task.done():
            # Atomically mark cancelled in DB first.  If it returns False the
            # giveaway already ended (drew winners) — tell the user and bail.
            if not cancel_giveaway(mid):
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="❌ Already Ended",
                        description=(
                            f"Giveaway `{mid}` already ended or was cancelled "
                            "before the cancel could be applied."
                        ),
                        color=discord.Color.orange(),
                        timestamp=datetime.now(timezone.utc),
                    ),
                    ephemeral=True,
                )
                return
            # Record staff intent so the CancelledError handler knows this
            # is a user-requested cancel, not a system shutdown/reload.
            self._staff_cancelled.add(mid)
            task.cancel()
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="🚫 Giveaway Cancelled",
                    description=f"The giveaway (`{mid}`) has been cancelled. The embed will be updated shortly.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            logger.info("Giveaway %s cancelled by %s in %s", mid, interaction.user, interaction.guild.name)
            return

        # Check database for active giveaway
        gw = get_giveaway_by_message(mid)
        if gw and gw["status"] == "active":
            # Mark as cancelled in DB
            cancel_giveaway(mid)
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="🚫 Giveaway Cancelled",
                    description=f"The giveaway (`{mid}`) has been marked as cancelled in the database. No winner will be drawn.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            logger.info("Giveaway %s cancelled (DB only) by %s in %s", mid, interaction.user, interaction.guild.name)
            return

        await interaction.response.send_message(
            embed=discord.Embed(
                title="❌ No Active Giveaway",
                description=(
                    f"No active giveaway found with message ID `{mid}`.\n"
                    "It may have already ended or been cancelled."
                ),
                color=discord.Color.orange(),
            ),
            ephemeral=True,
        )

    # ── /rerollgiveaway ───────────────────────────────────────────────────────
    @app_commands.command(
        name="rerollgiveaway",
        description="Reroll new winner(s) for an ended giveaway or quickdrop",
    )
    @app_commands.describe(
        message_id="ID of the ended giveaway message to reroll",
        winners="How many new winners to pick (default 1)",
    )
    async def rerollgiveaway(
        self,
        interaction: discord.Interaction,
        message_id: str,
        winners: app_commands.Range[int, 1, 10] = 1,
    ):
        await interaction.response.defer()

        if not is_authorized(interaction.user, interaction.guild, "giveaway"):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You must be **Admin** or above (or granted `giveaway` access) to reroll.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        try:
            mid = int(message_id)
        except ValueError:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Invalid Message ID",
                    description="Please provide a valid message ID.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        # Check in-memory finished first
        entrant_ids = self._finished.get(mid)

        # If not in memory, try database
        if not entrant_ids:
            gw = get_giveaway_by_message(mid)
            if gw:
                entries = get_giveaway_entries(gw["giveaway_id"])
                entrant_ids = [e["user_id"] for e in entries]

        if not entrant_ids:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Not Found",
                    description=(
                        f"No entry data found for message `{mid}`.\n"
                        "Reroll only works for giveaways that ended in this bot session or are stored in the database."
                    ),
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return

        entrants = [m for uid in entrant_ids if (m := interaction.guild.get_member(uid)) and not m.bot]

        if not entrants:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="😢 No Entrants",
                    description="No valid entrants found to reroll from.",
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return

        actual_winners = min(winners, len(entrants))
        picked = random.sample(entrants, actual_winners)
        winner_mentions = ", ".join(w.mention for w in picked)

        try:
            orig = await interaction.channel.fetch_message(mid)
            jump = orig.jump_url
        except (discord.NotFound, discord.Forbidden):
            jump = None

        ref_text = f"*(Rerolled from [this message]({jump}))*" if jump else ""

        await interaction.followup.send(
            content=winner_mentions,
            embed=discord.Embed(
                title=f"🔁 Reroll — New Winner{'s' if actual_winners > 1 else ''}!",
                description=(
                    f"🏆 {winner_mentions}\n\n"
                    f"Congratulations! Please contact {interaction.user.mention} to claim your prize.\n"
                    f"{ref_text}"
                ),
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc),
            ),
            allowed_mentions=discord.AllowedMentions(users=True),
        )

        logger.info(
            "Reroll by %s in %s: msg=%s, winners=%s",
            interaction.user, interaction.guild.name, mid, [w.id for w in picked],
        )

    # ── /giveawayset ──────────────────────────────────────────────────────────
    giveawayset = app_commands.Group(
        name="giveawayset",
        description="Configure giveaway settings (Admin only)",
    )

    @giveawayset.command(name="ping", description="Set the role to ping when a giveaway starts")
    @app_commands.describe(role="Role to mention — leave empty to clear the ping")
    async def giveawayset_ping(
        self,
        interaction: discord.Interaction,
        role: discord.Role | None = None,
    ):
        if not is_authorized(interaction.user, interaction.guild, "giveaway"):
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You must be **Admin** or above to configure giveaway settings.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        if role:
            set_guild_config(interaction.guild.id, "giveaway_ping_role_id", str(role.id))
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="✅ Giveaway Ping Set",
                    description=(
                        f"{role.mention} will be pinged whenever a giveaway or quickdrop starts.\n\n"
                        "**Tip:** Make sure this role has **Allow anyone to @mention this role** enabled."
                    ),
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
        else:
            set_guild_config(interaction.guild.id, "giveaway_ping_role_id", "")
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="✅ Giveaway Ping Cleared",
                    description="No role will be pinged when giveaways start.",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )

    @giveawayset.command(name="status", description="Show current giveaway configuration")
    async def giveawayset_status(self, interaction: discord.Interaction):
        if not is_authorized(interaction.user, interaction.guild, "giveaway"):
            await interaction.response.send_message(
                embed=discord.Embed(title="❌ Permission Denied", color=discord.Color.red()),
                ephemeral=True,
            )
            return

        ping_role = self._ping_role(interaction.guild)
        active_count = sum(1 for t in self._active.values() if not t.done())
        embed = discord.Embed(
            title="⚙️ Giveaway Settings",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Ping Role",
            value=ping_role.mention if ping_role else "*Not set — use `/giveawayset ping`*",
            inline=False,
        )
        embed.add_field(name="Active Giveaways (in-memory)", value=str(active_count), inline=True)

        db_active = len(get_active_giveaways())
        embed.add_field(name="Active Giveaways (DB)", value=str(db_active), inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GiveawaysCog(bot))
