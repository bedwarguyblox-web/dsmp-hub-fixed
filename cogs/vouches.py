"""
vouches.py — /vouch, /scamvouch, /checkvouches slash commands
             + !vouch, !scamvouch, !checkvouch prefix commands.

Prefix commands send a private panel (button → modal) so the user can
submit without exposing details in chat.  Slash commands remain for staff
who prefer them.
Anyone can vouch; duplicates are blocked per (voucher, target, guild) triplet.
"""

import discord
from discord import app_commands
from discord.ext import commands
import logging
from datetime import datetime, timezone

from utils.permissions import is_authorized, CONFIG
from utils.database import (
    add_vouch, add_scam_vouch,
    remove_vouch, remove_scam_vouch,
    get_vouch_counts, get_recent_vouches, get_recent_scam_vouches,
    get_vouch_leaderboard, get_scam_vouch_leaderboard,
    log_staff_action,
)
from utils.views import LeaderboardView, chunk_leaderboard

logger = logging.getLogger(__name__)

VOUCH_COOLDOWN = app_commands.checks.cooldown(1, 30.0, key=lambda i: (i.guild_id, i.user.id))


# ── Shared helpers ────────────────────────────────────────────────────────────

def _resolve_member(guild: discord.Guild, raw: str):
    """Try to resolve a member from a mention, ID, or display name string."""
    raw = raw.strip()
    # Mention <@ID> or <@!ID>
    if raw.startswith("<@") and raw.endswith(">"):
        try:
            uid = int(raw.strip("<@!>"))
            return guild.get_member(uid)
        except ValueError:
            pass
    # Raw integer ID
    try:
        return guild.get_member(int(raw))
    except ValueError:
        pass
    # Display name / username (case-insensitive)
    lower = raw.lower()
    return discord.utils.find(
        lambda m: m.display_name.lower() == lower or m.name.lower() == lower,
        guild.members,
    )


async def _send_to_logs(guild: discord.Guild, channel_id, embed: discord.Embed):
    if not channel_id:
        return
    ch = guild.get_channel(int(channel_id))
    if ch:
        try:
            await ch.send(embed=embed)
        except discord.Forbidden:
            logger.warning("Cannot send to logs channel %s", channel_id)


# ── Modals ────────────────────────────────────────────────────────────────────

class VouchModal(discord.ui.Modal, title="Submit Vouch"):
    target_input = discord.ui.TextInput(
        label="User (mention, ID, or display name)",
        placeholder="e.g. @Username or 123456789012345678",
        max_length=100,
    )
    proof_input = discord.ui.TextInput(
        label="Proof",
        style=discord.TextStyle.paragraph,
        placeholder="Link or description of proof",
        max_length=500,
    )

    def __init__(self, panel_msg: discord.Message):
        super().__init__()
        self._panel_msg = panel_msg   # the temporary panel — we'll delete it after

    async def on_submit(self, interaction: discord.Interaction):
        guild  = interaction.guild
        raw    = self.target_input.value.strip()
        proof  = self.proof_input.value.strip()
        member = _resolve_member(guild, raw)

        if not member:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ Member Not Found",
                    description=f"Could not find `{raw}` in this server. Try using their ID.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        if member.id == interaction.user.id:
            await interaction.response.send_message(
                embed=discord.Embed(title="❌ Invalid", description="You cannot vouch for yourself.", color=discord.Color.red()),
                ephemeral=True,
            )
            return

        if member.bot:
            await interaction.response.send_message(
                embed=discord.Embed(title="❌ Invalid", description="You cannot vouch for a bot.", color=discord.Color.red()),
                ephemeral=True,
            )
            return

        success = add_vouch(interaction.user.id, member.id, guild.id, proof)
        if not success:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⚠️ Duplicate Vouch",
                    description=f"You have already vouched for {member.mention}.",
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return

        total_v, _ = get_vouch_counts(member.id, guild.id)
        embed = discord.Embed(
            title="✅ Vouch Submitted",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Vouched For",   value=member.mention,           inline=True)
        embed.add_field(name="Vouched By",    value=interaction.user.mention, inline=True)
        embed.add_field(name="Total Vouches", value=str(total_v),             inline=True)
        embed.add_field(name="Proof",         value=proof,                    inline=False)
        embed.set_footer(text=f"User ID: {member.id}")

        await interaction.response.send_message(embed=embed)
        log_staff_action("vouch", interaction.user.id, guild.id, target_id=member.id, details=proof)
        await _send_to_logs(guild, CONFIG.get("VOUCH_LOGS_CHANNEL_ID"), embed)

        # Clean up the panel message
        try:
            await self._panel_msg.delete()
        except (discord.NotFound, discord.Forbidden):
            pass


class ScamVouchModal(discord.ui.Modal, title="Submit Scam Report"):
    target_input = discord.ui.TextInput(
        label="User (mention, ID, or display name)",
        placeholder="e.g. @Username or 123456789012345678",
        max_length=100,
    )
    proof_input = discord.ui.TextInput(
        label="Proof",
        style=discord.TextStyle.paragraph,
        placeholder="Link or description of proof",
        max_length=500,
    )

    def __init__(self, panel_msg: discord.Message):
        super().__init__()
        self._panel_msg = panel_msg

    async def on_submit(self, interaction: discord.Interaction):
        guild  = interaction.guild
        raw    = self.target_input.value.strip()
        proof  = self.proof_input.value.strip()
        member = _resolve_member(guild, raw)

        if not member:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ Member Not Found",
                    description=f"Could not find `{raw}` in this server. Try using their ID.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        if member.id == interaction.user.id:
            await interaction.response.send_message(
                embed=discord.Embed(title="❌ Invalid", description="You cannot scam-report yourself.", color=discord.Color.red()),
                ephemeral=True,
            )
            return

        if member.bot:
            await interaction.response.send_message(
                embed=discord.Embed(title="❌ Invalid", description="You cannot scam-report a bot.", color=discord.Color.red()),
                ephemeral=True,
            )
            return

        success = add_scam_vouch(interaction.user.id, member.id, guild.id, proof)
        if not success:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⚠️ Duplicate Report",
                    description=f"You have already submitted a scam report for {member.mention}.",
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return

        _, total_sv = get_vouch_counts(member.id, guild.id)
        embed = discord.Embed(
            title="🚨 Scam Vouch Submitted",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Reported User",      value=member.mention,           inline=True)
        embed.add_field(name="Reported By",        value=interaction.user.mention, inline=True)
        embed.add_field(name="Total Scam Vouches", value=str(total_sv),            inline=True)
        embed.add_field(name="Proof",              value=proof,                    inline=False)
        embed.set_footer(text=f"User ID: {member.id}")

        await interaction.response.send_message(embed=embed)
        log_staff_action("scam_vouch", interaction.user.id, guild.id, target_id=member.id, details=proof)
        await _send_to_logs(guild, CONFIG.get("SCAM_VOUCH_LOGS_CHANNEL_ID"), embed)

        try:
            await self._panel_msg.delete()
        except (discord.NotFound, discord.Forbidden):
            pass


class CheckVouchModal(discord.ui.Modal, title="Check Vouch Record"):
    target_input = discord.ui.TextInput(
        label="User (mention, ID, or display name)",
        placeholder="e.g. @Username or 123456789012345678",
        max_length=100,
    )

    def __init__(self, panel_msg: discord.Message):
        super().__init__()
        self._panel_msg = panel_msg

    async def on_submit(self, interaction: discord.Interaction):
        guild  = interaction.guild
        raw    = self.target_input.value.strip()
        member = _resolve_member(guild, raw)

        if not member:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ Member Not Found",
                    description=f"Could not find `{raw}` in this server.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        total_v, total_sv = get_vouch_counts(member.id, guild.id)
        recent_v  = get_recent_vouches(member.id, guild.id, 5)
        recent_sv = get_recent_scam_vouches(member.id, guild.id, 5)

        if total_v + total_sv == 0:
            ratio_str = "N/A"
        elif total_sv == 0:
            ratio_str = "✅ 100% positive"
        else:
            pct = total_v / (total_v + total_sv) * 100
            ratio_str = f"{pct:.1f}% positive"

        color = (discord.Color.green() if total_sv == 0
                 else discord.Color.yellow() if total_v >= total_sv * 2
                 else discord.Color.red())

        embed = discord.Embed(
            title=f"📊 Vouch Record — {member.display_name}",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="User",             value=member.mention, inline=True)
        embed.add_field(name="✅ Total Vouches", value=str(total_v),   inline=True)
        embed.add_field(name="🚨 Scam Vouches",  value=str(total_sv),  inline=True)
        embed.add_field(name="📈 Vouch Ratio",   value=ratio_str,      inline=False)

        if recent_v:
            lines = []
            for v in recent_v:
                voucher = guild.get_member(v["voucher_id"])
                vname   = voucher.display_name if voucher else f"ID:{v['voucher_id']}"
                lines.append(f"• By **{vname}** on {v['timestamp'][:10]}\n  Proof: {v['proof'][:80]}")
            embed.add_field(name=f"Recent Vouches (last {len(recent_v)})", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Recent Vouches", value="None yet.", inline=False)

        if recent_sv:
            lines = []
            for sv in recent_sv:
                reporter = guild.get_member(sv["voucher_id"])
                rname    = reporter.display_name if reporter else f"ID:{sv['voucher_id']}"
                lines.append(f"• By **{rname}** on {sv['timestamp'][:10]}\n  Proof: {sv['proof'][:80]}")
            embed.add_field(name=f"Recent Scam Vouches (last {len(recent_sv)})", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Recent Scam Vouches", value="None reported.", inline=False)

        embed.set_footer(text=f"User ID: {member.id}")
        await interaction.response.send_message(embed=embed)

        try:
            await self._panel_msg.delete()
        except (discord.NotFound, discord.Forbidden):
            pass


# ── Panel views (open a modal on button click) ────────────────────────────────

class VouchPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Submit Vouch", style=discord.ButtonStyle.success, emoji="✅")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VouchModal(panel_msg=interaction.message))


class ScamVouchPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Submit Scam Report", style=discord.ButtonStyle.danger, emoji="🚨")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ScamVouchModal(panel_msg=interaction.message))


class CheckVouchPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Check Vouch Record", style=discord.ButtonStyle.primary, emoji="📊")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CheckVouchModal(panel_msg=interaction.message))


# ── Cog ───────────────────────────────────────────────────────────────────────

class VouchesCog(commands.Cog, name="Vouches"):
    """Vouch and scam-vouch commands (slash + prefix)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Prefix command listener ───────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        content = message.content.strip()
        lower   = content.lower()

        if lower.startswith("!vouch") and not lower.startswith("!vouchremove"):
            await self._prefix_panel(
                message,
                title="✅ Submit a Vouch",
                description=(
                    "Click **Submit Vouch** below to open the vouch form.\n"
                    "You will be asked for the user and your proof."
                ),
                color=discord.Color.green(),
                view=VouchPanelView(),
                check_cmd="vouch",
            )

        elif lower.startswith("!scamvouch") and not lower.startswith("!scamvouchremove"):
            await self._prefix_panel(
                message,
                title="🚨 Submit a Scam Report",
                description=(
                    "Click **Submit Scam Report** below to open the report form.\n"
                    "You will be asked for the user and your proof."
                ),
                color=discord.Color.red(),
                view=ScamVouchPanelView(),
                check_cmd="scamvouch",
            )

        elif lower.startswith("!checkvouch"):
            await self._prefix_panel(
                message,
                title="📊 Check a Vouch Record",
                description=(
                    "Click **Check Vouch Record** below to look up a member's vouch history."
                ),
                color=discord.Color.blurple(),
                view=CheckVouchPanelView(),
                check_cmd="checkvouches",
            )

    async def _prefix_panel(
        self,
        message: discord.Message,
        title: str,
        description: str,
        color: discord.Color,
        view: discord.ui.View,
        check_cmd: str,
    ):
        """Delete the trigger message, check permission, then send the panel."""
        # Delete the trigger message silently
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

        if not is_authorized(message.author, message.guild, check_cmd):
            try:
                await message.author.send(
                    embed=discord.Embed(
                        title="❌ Permission Denied",
                        description="You must be **Admin** or above (or granted access) to use this command.",
                        color=discord.Color.red(),
                    )
                )
            except discord.Forbidden:
                pass
            return

        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Requested by {message.author.display_name} • Panel expires in 2 minutes")

        try:
            panel = await message.channel.send(
                content=message.author.mention,
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.Forbidden:
            return

        # Auto-delete the panel after 2 minutes if unused
        import asyncio
        async def _cleanup():
            await asyncio.sleep(120)
            try:
                await panel.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
        asyncio.create_task(_cleanup())

    # ── /vouch ───────────────────────────────────────────────────────────────
    @app_commands.command(name="vouch", description="Vouch for a user with proof")
    @app_commands.describe(
        user="The member you are vouching for",
        proof="Link or description of your proof"
    )
    @VOUCH_COOLDOWN
    async def vouch(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        proof: str,
    ):
        await interaction.response.defer()

        if not is_authorized(interaction.user, interaction.guild, "vouch"):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You must be **Admin** or above to submit vouches.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        if user.id == interaction.user.id:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Invalid", description="You cannot vouch for yourself.", color=discord.Color.red()),
                ephemeral=True,
            )
            return

        if user.bot:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Invalid", description="You cannot vouch for a bot.", color=discord.Color.red()),
                ephemeral=True,
            )
            return

        guild   = interaction.guild
        success = add_vouch(interaction.user.id, user.id, guild.id, proof)

        if not success:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Duplicate Vouch",
                    description=f"You have already vouched for {user.mention}.",
                    color=discord.Color.orange(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        total_v, _ = get_vouch_counts(user.id, guild.id)
        embed = discord.Embed(
            title="✅ Vouch Submitted",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Vouched For",   value=user.mention,             inline=True)
        embed.add_field(name="Vouched By",    value=interaction.user.mention, inline=True)
        embed.add_field(name="Total Vouches", value=str(total_v),             inline=True)
        embed.add_field(name="Proof",         value=proof,                    inline=False)
        embed.set_footer(text=f"User ID: {user.id}")
        await interaction.followup.send(embed=embed)
        log_staff_action("vouch", interaction.user.id, guild.id, target_id=user.id, details=proof)
        await _send_to_logs(guild, CONFIG.get("VOUCH_LOGS_CHANNEL_ID"), embed)

    # ── /scamvouch ────────────────────────────────────────────────────────────
    @app_commands.command(name="scamvouch", description="Submit a scam report against a user with proof")
    @app_commands.describe(
        user="The member you are reporting",
        proof="Link or description of your proof"
    )
    @VOUCH_COOLDOWN
    async def scamvouch(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        proof: str,
    ):
        await interaction.response.defer()

        if not is_authorized(interaction.user, interaction.guild, "scamvouch"):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Permission Denied",
                    description="You must be **Admin** or above to submit scam vouches.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        if user.id == interaction.user.id:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Invalid", description="You cannot scam-vouch yourself.", color=discord.Color.red()),
                ephemeral=True,
            )
            return

        if user.bot:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Invalid", description="You cannot scam-vouch a bot.", color=discord.Color.red()),
                ephemeral=True,
            )
            return

        guild   = interaction.guild
        success = add_scam_vouch(interaction.user.id, user.id, guild.id, proof)

        if not success:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Duplicate Scam Vouch",
                    description=f"You have already submitted a scam report for {user.mention}.",
                    color=discord.Color.orange(),
                    timestamp=datetime.now(timezone.utc),
                ),
                ephemeral=True,
            )
            return

        _, total_sv = get_vouch_counts(user.id, guild.id)
        embed = discord.Embed(
            title="🚨 Scam Vouch Submitted",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Reported User",      value=user.mention,             inline=True)
        embed.add_field(name="Reported By",        value=interaction.user.mention, inline=True)
        embed.add_field(name="Total Scam Vouches", value=str(total_sv),            inline=True)
        embed.add_field(name="Proof",              value=proof,                    inline=False)
        embed.set_footer(text=f"User ID: {user.id}")
        await interaction.followup.send(embed=embed)
        log_staff_action("scam_vouch", interaction.user.id, guild.id, target_id=user.id, details=proof)
        await _send_to_logs(guild, CONFIG.get("SCAM_VOUCH_LOGS_CHANNEL_ID"), embed)

    # ── /vouchremove ──────────────────────────────────────────────────────────
    @app_commands.command(name="vouchremove", description="Remove a specific vouch from a voucher to a target user")
    @app_commands.describe(
        user="The target whose vouch record you are editing",
        voucher="The member who originally submitted the vouch"
    )
    async def vouchremove(self, interaction: discord.Interaction, user: discord.Member, voucher: discord.Member):
        await interaction.response.defer(ephemeral=True)

        if not is_authorized(interaction.user, interaction.guild, "vouch"):
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Permission Denied", description="You must be **Admin** or above.", color=discord.Color.red()),
                ephemeral=True,
            )
            return

        guild   = interaction.guild
        removed = remove_vouch(voucher.id, user.id, guild.id)

        if not removed:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Not Found",
                    description=f"No vouch from {voucher.mention} → {user.mention} found.",
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return

        total_v, _ = get_vouch_counts(user.id, guild.id)
        embed = discord.Embed(title="🗑️ Vouch Removed", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Target",            value=user.mention,             inline=True)
        embed.add_field(name="Voucher Removed",   value=voucher.mention,          inline=True)
        embed.add_field(name="Remaining Vouches", value=str(total_v),             inline=True)
        embed.add_field(name="Removed By",        value=interaction.user.mention, inline=False)
        embed.set_footer(text=f"Target ID: {user.id}")
        await interaction.followup.send(embed=embed, ephemeral=True)
        log_staff_action("vouchremove", interaction.user.id, guild.id, target_id=user.id, details=f"Removed vouch from {voucher.id}")
        await _send_to_logs(guild, CONFIG.get("VOUCH_LOGS_CHANNEL_ID"), embed)

    # ── /scamvouchremove ──────────────────────────────────────────────────────
    @app_commands.command(name="scamvouchremove", description="Remove a specific scam vouch from a reporter to a target user")
    @app_commands.describe(
        user="The target whose scam vouch record you are editing",
        voucher="The member who originally submitted the scam vouch"
    )
    async def scamvouchremove(self, interaction: discord.Interaction, user: discord.Member, voucher: discord.Member):
        await interaction.response.defer(ephemeral=True)

        if not is_authorized(interaction.user, interaction.guild, "scamvouch"):
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Permission Denied", description="You must be **Admin** or above.", color=discord.Color.red()),
                ephemeral=True,
            )
            return

        guild   = interaction.guild
        removed = remove_scam_vouch(voucher.id, user.id, guild.id)

        if not removed:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Not Found",
                    description=f"No scam vouch from {voucher.mention} → {user.mention} found.",
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return

        _, total_sv = get_vouch_counts(user.id, guild.id)
        embed = discord.Embed(title="🗑️ Scam Vouch Removed", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Target",                 value=user.mention,             inline=True)
        embed.add_field(name="Reporter Removed",       value=voucher.mention,          inline=True)
        embed.add_field(name="Remaining Scam Vouches", value=str(total_sv),            inline=True)
        embed.add_field(name="Removed By",             value=interaction.user.mention, inline=False)
        embed.set_footer(text=f"Target ID: {user.id}")
        await interaction.followup.send(embed=embed, ephemeral=True)
        log_staff_action("scamvouchremove", interaction.user.id, guild.id, target_id=user.id, details=f"Removed scam vouch from {voucher.id}")
        await _send_to_logs(guild, CONFIG.get("SCAM_VOUCH_LOGS_CHANNEL_ID"), embed)

    # ── /checkvouches ─────────────────────────────────────────────────────────
    @app_commands.command(name="checkvouches", description="Check vouch record for a user")
    @app_commands.describe(user="The member to check")
    async def checkvouches(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer()

        if not is_authorized(interaction.user, interaction.guild, "checkvouches"):
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Permission Denied", description="You must be **Admin** or above.", color=discord.Color.red(), timestamp=datetime.now(timezone.utc)),
                ephemeral=True,
            )
            return

        guild     = interaction.guild
        total_v, total_sv = get_vouch_counts(user.id, guild.id)
        recent_v  = get_recent_vouches(user.id, guild.id, 5)
        recent_sv = get_recent_scam_vouches(user.id, guild.id, 5)

        if total_v + total_sv == 0:
            ratio_str = "N/A"
        elif total_sv == 0:
            ratio_str = "✅ 100% positive"
        else:
            pct = total_v / (total_v + total_sv) * 100
            ratio_str = f"{pct:.1f}% positive"

        color = (discord.Color.green() if total_sv == 0
                 else discord.Color.yellow() if total_v >= total_sv * 2
                 else discord.Color.red())

        embed = discord.Embed(title=f"📊 Vouch Record — {user.display_name}", color=color, timestamp=datetime.now(timezone.utc))
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="User",             value=user.mention, inline=True)
        embed.add_field(name="✅ Total Vouches", value=str(total_v),  inline=True)
        embed.add_field(name="🚨 Scam Vouches",  value=str(total_sv), inline=True)
        embed.add_field(name="📈 Vouch Ratio",   value=ratio_str,     inline=False)

        if recent_v:
            lines = [f"• By **{guild.get_member(v['voucher_id']).display_name if guild.get_member(v['voucher_id']) else 'Unknown'}** on {v['timestamp'][:10]}\n  Proof: {v['proof'][:80]}" for v in recent_v]
            embed.add_field(name=f"Recent Vouches (last {len(recent_v)})", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Recent Vouches", value="None yet.", inline=False)

        if recent_sv:
            lines = [f"• By **{guild.get_member(sv['voucher_id']).display_name if guild.get_member(sv['voucher_id']) else 'Unknown'}** on {sv['timestamp'][:10]}\n  Proof: {sv['proof'][:80]}" for sv in recent_sv]
            embed.add_field(name=f"Recent Scam Vouches (last {len(recent_sv)})", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Recent Scam Vouches", value="None reported.", inline=False)

        embed.set_footer(text=f"User ID: {user.id}")
        await interaction.followup.send(embed=embed)

    # ── /leaderboard_vouches ──────────────────────────────────────────────────
    @app_commands.command(name="leaderboard_vouches", description="Most-vouched users — paginated top 10 per page")
    async def leaderboard_vouches(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild = interaction.guild
        rows  = get_vouch_leaderboard(guild.id, 100)
        if not rows:
            await interaction.followup.send(embed=discord.Embed(title="🏆 Vouch Leaderboard", description="No vouches recorded yet.", color=discord.Color.gold(), timestamp=datetime.now(timezone.utc)))
            return
        resolved = [{"name": (guild.get_member(r["target_id"]) or type("_", (), {"display_name": f"ID:{r['target_id']}"})()).display_name, "total": f"{r['total']} vouch(es)"} for r in rows]
        pages = chunk_leaderboard(resolved, name_key="name", total_key="total")
        view  = LeaderboardView(pages=pages, title="🏆 Vouch Leaderboard", color=discord.Color.gold(), footer=f"Guild: {guild.name} • {len(rows)} total")
        await interaction.followup.send(embed=view.make_embed(), view=view)

    # ── /leaderboard_scamvouches ──────────────────────────────────────────────
    @app_commands.command(name="leaderboard_scamvouches", description="Most scam-vouched users — paginated top 10 per page")
    async def leaderboard_scamvouches(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild = interaction.guild
        rows  = get_scam_vouch_leaderboard(guild.id, 100)
        if not rows:
            await interaction.followup.send(embed=discord.Embed(title="🚨 Scam Vouch Leaderboard", description="No scam vouches recorded yet.", color=discord.Color.red(), timestamp=datetime.now(timezone.utc)))
            return
        resolved = [{"name": (guild.get_member(r["target_id"]) or type("_", (), {"display_name": f"ID:{r['target_id']}"})()).display_name, "total": f"{r['total']} report(s)"} for r in rows]
        pages = chunk_leaderboard(resolved, name_key="name", total_key="total")
        view  = LeaderboardView(pages=pages, title="🚨 Scam Vouch Leaderboard", color=discord.Color.red(), footer=f"Guild: {guild.name} • {len(rows)} total", medals=["⚠️"] * 50)
        await interaction.followup.send(embed=view.make_embed(), view=view)

    # ── Cooldown error handler ────────────────────────────────────────────────
    @vouch.error
    @scamvouch.error
    async def on_cooldown(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                embed=discord.Embed(title="⏳ Cooldown", description=f"Please wait **{error.retry_after:.1f}s** before submitting again.", color=discord.Color.yellow()),
                ephemeral=True,
            )
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(VouchesCog(bot))
