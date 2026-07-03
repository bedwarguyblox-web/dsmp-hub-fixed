"""
partnershipreq.py — Dynamic partnership requirements embed.

The requirements tiers are percentage-based relative to the server's current
member count, so they scale automatically as the server grows or shrinks.

Tier thresholds (% of server member count):
  0–30%   → You @everyone       / We nothing
  30–60%  → You @everyone       / We @Partnership Ping
  60–80%  → You @Member         / We @here
  80–100% → You @Member         / We @here + @Partnership Ping
  100–120%→ Both same ping
  120–300%→ You @here + @PP     / We @Member
  300%+   → Your requirements (must be fair)

Setup: /partnershipreq setup channel:#channel
Auto-updates the embed on every member join/leave (debounced 5s).
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.database import get_guild_config, set_guild_config, log_staff_action
from utils.permissions import is_authorized, CONFIG

logger = logging.getLogger(__name__)

# ── Tier definitions (lo_pct, hi_pct, label, you_ping, we_ping) ──────────────
# Percentages are relative to the server's current member count.
# hi_pct=None means "300%+" (open-ended top tier).
TIERS = [
    (0,   30,  "You: **@everyone**\nWe: nothing"),
    (30,  60,  "You: **@everyone**\nWe: **{pp}**"),
    (60,  80,  "You: **{member}**\nWe: **@here**"),
    (80,  100, "You: **{member}**\nWe: **@here** + **{pp}**"),
    (100, 130, "Both: **same ping**"),
    (130, 300, "You: **@here** + **{pp}**\nWe: **{member}**"),
    (300, None, "Your requirements *(must be fair)*"),
]


def _resolve_mentions(guild: discord.Guild) -> tuple[str, str]:
    """Return (partnership_ping_mention, member_role_mention)."""
    pp_id  = CONFIG.get("PARTNERSHIP_PING_ROLE_ID")
    mem_id = CONFIG.get("PARTNERSHIP_MEMBER_ROLE_ID")

    pp_role  = guild.get_role(pp_id)  if pp_id  else None
    mem_role = guild.get_role(mem_id) if mem_id else None

    pp_str  = pp_role.mention  if pp_role  else "@Partnership Ping"
    mem_str = mem_role.mention if mem_role else "@Member"
    return pp_str, mem_str


def _tier_member_range(lo_pct: int, hi_pct: Optional[int], total: int) -> str:
    """Convert percentage thresholds to a human-readable member-count range."""
    lo = max(0, round(total * lo_pct / 100))
    if hi_pct is None:
        return f"{lo}+ members"
    hi = round(total * hi_pct / 100) - 1
    return f"{lo}–{hi} members"


def _current_tier_index(total: int) -> int:
    """Return the index (0-based) of the tier the server is currently in."""
    for i, (lo, hi, _) in enumerate(TIERS):
        if hi is None:
            return i
        lo_count = round(total * lo / 100)
        hi_count = round(total * hi / 100)
        if lo_count <= total < hi_count:
            return i
    return len(TIERS) - 1


def _build_embed(guild: discord.Guild) -> discord.Embed:
    """Build the full requirements embed for the guild's current member count."""
    # Exclude bots from the count
    total  = sum(1 for m in guild.members if not m.bot)
    pp_str, mem_str = _resolve_mentions(guild)

    ticket_ch_id = CONFIG.get("PARTNERSHIP_TICKET_CHANNEL_ID")
    ticket_mention = f"<#{ticket_ch_id}>" if ticket_ch_id else "#tickets"

    current_idx = _current_tier_index(total)

    embed = discord.Embed(
        title="🤝 Partnership Requirements",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(
        text=f"Auto-updates on member changes • Current server size: {total} members"
    )

    for i, (lo_pct, hi_pct, template) in enumerate(TIERS):
        label = template.format(pp=pp_str, member=mem_str)
        range_str = _tier_member_range(lo_pct, hi_pct, total)

        is_current = (i == current_idx)
        field_name = f"{'▶ ' if is_current else ''}**{range_str}**{'  ← You are here' if is_current else ''}"

        embed.add_field(name=field_name, value=label, inline=False)

    embed.add_field(
        name="📋 Notes",
        value=(
            f"• Open {ticket_mention} to partner\n"
            "• If you leave within **1 week** of posting your ad, the ad will be deleted\n"
            "• **Partner cooldown:** 3 days"
        ),
        inline=False,
    )

    return embed


class PartnershipReqCog(commands.Cog, name="PartnershipReq"):
    """Dynamic partnership requirements embed — auto-updates on join/leave."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # guild_id → pending asyncio Task (debounce)
        self._debounce: dict[int, asyncio.Task] = {}

    # ── /partnershipreq group ─────────────────────────────────────────────────
    req_group = app_commands.Group(
        name="partnershipreq",
        description="Partnership requirements embed management",
    )

    @req_group.command(
        name="setup",
        description="Post the partnership requirements embed in a channel (Admin only)",
    )
    @app_commands.describe(channel="Channel to post the requirements in")
    async def req_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        await interaction.response.defer(ephemeral=True)

        if not is_authorized(interaction.user, interaction.guild, "partnershipreq"):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You must be **Admin** or above.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        guild = interaction.guild
        embed = _build_embed(guild)

        try:
            msg = await channel.send(embed=embed)
        except discord.Forbidden:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Missing Permission",
                    description=f"I can't send messages in {channel.mention}.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        # Persist channel + message IDs so we can edit later
        set_guild_config(guild.id, "partnershipreq_channel_id", str(channel.id))
        set_guild_config(guild.id, "partnershipreq_message_id", str(msg.id))

        log_staff_action("partnershipreq_setup", interaction.user.id, guild.id,
                         details=f"Channel: {channel.id} | Message: {msg.id}")

        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Requirements Posted",
                description=(
                    f"Partnership requirements embed posted in {channel.mention}.\n"
                    "It will auto-update whenever members join or leave."
                ),
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )

    @req_group.command(
        name="preview",
        description="Preview what the requirements embed looks like at any member count (Admin only)",
    )
    @app_commands.describe(members="Hypothetical member count to preview (e.g. 250)")
    async def req_preview(
        self,
        interaction: discord.Interaction,
        members: app_commands.Range[int, 1, 100000],
    ):
        await interaction.response.defer(ephemeral=True)

        if not is_authorized(interaction.user, interaction.guild, "partnershipreq"):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You must be **Admin** or above.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        guild  = interaction.guild
        pp_str, mem_str = _resolve_mentions(guild)
        ticket_ch_id    = CONFIG.get("PARTNERSHIP_TICKET_CHANNEL_ID")
        ticket_mention  = f"<#{ticket_ch_id}>" if ticket_ch_id else "#tickets"
        current_idx     = _current_tier_index(members)

        embed = discord.Embed(
            title=f"🔍 Requirements Preview — {members:,} members",
            description="*This is a preview only — your live embed is unchanged.*",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Previewing at {members:,} members • Current server: {sum(1 for m in guild.members if not m.bot):,} members")

        for i, (lo_pct, hi_pct, template) in enumerate(TIERS):
            label     = template.format(pp=pp_str, member=mem_str)
            range_str = _tier_member_range(lo_pct, hi_pct, members)
            is_current = (i == current_idx)
            field_name = f"{'▶ ' if is_current else ''}**{range_str}**{'  ← at this size' if is_current else ''}"
            embed.add_field(name=field_name, value=label, inline=False)

        embed.add_field(
            name="📋 Notes",
            value=(
                f"• Open {ticket_mention} to partner\n"
                "• If you leave within **1 week** of posting your ad, the ad will be deleted\n"
                "• **Partner cooldown:** 3 days"
            ),
            inline=False,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @req_group.command(
        name="refresh",
        description="Manually refresh the partnership requirements embed (Admin only)",
    )
    async def req_refresh(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not is_authorized(interaction.user, interaction.guild, "partnershipreq"):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You must be **Admin** or above.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        updated = await self._update_embed(interaction.guild)

        if updated:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="✅ Refreshed",
                    description="The partnership requirements embed has been updated.",
                    color=discord.Color.green(),
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Not Set Up",
                    description="No requirements embed found. Run `/partnershipreq setup` first.",
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )

    # ── Member join/leave — debounced auto-update ─────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        self._schedule_update(member.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot:
            return
        self._schedule_update(member.guild)

    def _schedule_update(self, guild: discord.Guild):
        """Cancel any pending update and schedule a new one after 5 s debounce."""
        existing = self._debounce.get(guild.id)
        if existing and not existing.done():
            existing.cancel()
        self._debounce[guild.id] = asyncio.create_task(
            self._delayed_update(guild)
        )

    async def _delayed_update(self, guild: discord.Guild):
        """Wait 5 seconds then update the embed (avoids spam on bulk joins)."""
        await asyncio.sleep(5)
        await self._update_embed(guild)

    async def _update_embed(self, guild: discord.Guild) -> bool:
        """
        Edit the stored requirements message with a freshly built embed.
        Returns True if the edit succeeded, False if not configured or message missing.
        """
        ch_id_str  = get_guild_config(guild.id, "partnershipreq_channel_id")
        msg_id_str = get_guild_config(guild.id, "partnershipreq_message_id")

        if not ch_id_str or not msg_id_str:
            return False

        channel = guild.get_channel(int(ch_id_str))
        if not channel:
            return False

        try:
            msg = await channel.fetch_message(int(msg_id_str))
            await msg.edit(embed=_build_embed(guild))
            logger.info(
                "Partnership req embed updated for guild %s (%d members)",
                guild.id, guild.member_count,
            )
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logger.warning("Could not update partnership req embed in guild %s: %s", guild.id, e)
            return False


async def setup(bot: commands.Bot):
    await bot.add_cog(PartnershipReqCog(bot))
