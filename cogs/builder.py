"""
builder.py — Builder payment confirmation, protection timer system, and case management.

Commands:
  /builder_payment_confirm  — Log a confirmed payment to Builder Logs
  /builder_history          — View all payment records for a user
  /builder_start_timer      — Start a 48h protection timer (builder only)
  /builder_timer_status     — Check status of an active timer
  /builder_cases            — List all cases for this guild
  /builder_case             — Detailed view of a single case
  /builder_cancel_timer     — Cancel a case before confirmation

Interactive Views:
  CustomerConfirmView       — Confirm/Reject buttons sent to the customer
  OwnerReviewView           — Approve/Hold/Investigate buttons sent to owner after 48h
"""

import discord
from discord import app_commands
from discord.ext import commands
import logging
import uuid
from datetime import datetime, timezone, timedelta

from utils.permissions import is_at_least, CONFIG
from utils.database import (
    add_builder_payment, get_builder_payments,
    create_builder_case, get_builder_case, get_all_builder_cases,
    update_builder_case_status, log_builder_timer_event,
    get_builder_case_logs, log_staff_action
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Interactive Views
# ═══════════════════════════════════════════════════════════════════

class CustomerConfirmView(discord.ui.View):
    """
    Buttons sent to the customer when a builder starts a timer.
    Only the specified customer may interact.
    Times out after 24h — case remains 'pending_confirmation'.
    """

    def __init__(self, case_id: str, builder: discord.Member, customer_id: int, cfg: dict):
        super().__init__(timeout=86400)   # 24 hours
        self.case_id     = case_id
        self.builder     = builder
        self.customer_id = customer_id
        self.cfg         = cfg

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.customer_id:
            await interaction.response.send_message(
                "❌ Only the specified customer can interact with these buttons.",
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="✅ Confirm Timer", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        now      = datetime.now(timezone.utc)
        end_time = now + timedelta(hours=48)

        update_builder_case_status(
            self.case_id, "active",
            start_time=now.isoformat(),
            end_time=end_time.isoformat()
        )
        log_builder_timer_event(self.case_id, "confirmed", interaction.user.id,
                                "Customer confirmed timer. 48h started.")

        embed = discord.Embed(
            title="✅ Timer Confirmed & Started",
            description=(
                f"The 48-hour protection timer for case `{self.case_id}` has begun.\n\n"
                f"**Starts:** <t:{int(now.timestamp())}:F>\n"
                f"**Expires:** <t:{int(end_time.timestamp())}:F> "
                f"(<t:{int(end_time.timestamp())}:R>)"
            ),
            color=discord.Color.green(),
            timestamp=now,
        )
        await interaction.followup.send(embed=embed)

        # Notify builder
        try:
            notify = discord.Embed(
                title="✅ Customer Confirmed Your Timer",
                description=(
                    f"Case `{self.case_id}` is now active.\n"
                    f"**Expires:** <t:{int(end_time.timestamp())}:F> "
                    f"(<t:{int(end_time.timestamp())}:R>)"
                ),
                color=discord.Color.green(),
            )
            await self.builder.send(embed=notify)
        except discord.Forbidden:
            pass

        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="❌ Reject Timer", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        update_builder_case_status(self.case_id, "rejected")
        log_builder_timer_event(self.case_id, "rejected", interaction.user.id,
                                "Customer rejected the timer request.")

        embed = discord.Embed(
            title="❌ Timer Rejected",
            description=f"Case `{self.case_id}` has been rejected by the customer.",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.followup.send(embed=embed)

        # Notify builder
        try:
            notify = discord.Embed(
                title="❌ Customer Rejected Your Timer",
                description=f"Case `{self.case_id}` was rejected by the customer.",
                color=discord.Color.red(),
            )
            await self.builder.send(embed=notify)
        except discord.Forbidden:
            pass

        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)


class OwnerReviewView(discord.ui.View):
    """
    Buttons sent to the Owner Review Channel after a 48h timer expires.
    Only Owner (OWNER_ID) and members holding the Head Admin role may interact.
    Uses case_id in custom_id for persistence across restarts.
    """

    def __init__(self, case_id: str, cfg: dict):
        super().__init__(timeout=None)   # persistent — no auto-disable
        self.case_id = case_id
        self.cfg = cfg

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        from utils.permissions import is_at_least, is_owner
        if is_owner(interaction.user) or is_at_least(interaction.user, "Head Admin"):
            return True
        await interaction.response.send_message(
            "❌ Only the Owner and Head Admin can action this review.", ephemeral=True
        )
        return False

    @discord.ui.button(
        label="✅ Approve Payment",
        style=discord.ButtonStyle.success,
        custom_id="owner_review:approve"
    )
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        # Get case_id from the message's view (set when created)
        case_id = self._get_case_id_from_message(interaction)
        if not case_id:
            await interaction.followup.send("❌ Could not find case information.", ephemeral=True)
            return
        update_builder_case_status(case_id, "approved")
        log_builder_timer_event(case_id, "approved", interaction.user.id,
                                f"Payment approved by {interaction.user}")
        await self._notify_parties(interaction, case_id, "approved", "✅ Payment Approved",
                                   discord.Color.green())
        self._disable_all()
        await interaction.message.edit(view=self)

    @discord.ui.button(
        label="❌ Hold Payment",
        style=discord.ButtonStyle.danger,
        custom_id="owner_review:hold"
    )
    async def hold(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        case_id = self._get_case_id_from_message(interaction)
        if not case_id:
            await interaction.followup.send("❌ Could not find case information.", ephemeral=True)
            return
        update_builder_case_status(case_id, "on_hold")
        log_builder_timer_event(case_id, "on_hold", interaction.user.id,
                                f"Payment held by {interaction.user}")
        await self._notify_parties(interaction, case_id, "on_hold", "⏸️ Payment On Hold",
                                   discord.Color.orange())
        self._disable_all()
        await interaction.message.edit(view=self)

    @discord.ui.button(
        label="⚠️ Investigation Required",
        style=discord.ButtonStyle.secondary,
        custom_id="owner_review:investigate"
    )
    async def investigate(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        case_id = self._get_case_id_from_message(interaction)
        if not case_id:
            await interaction.followup.send("❌ Could not find case information.", ephemeral=True)
            return
        update_builder_case_status(case_id, "under_investigation")
        log_builder_timer_event(case_id, "under_investigation", interaction.user.id,
                                f"Investigation opened by {interaction.user}")

        # Ping owner
        owner = interaction.guild.get_member(self.cfg.get("OWNER_ID", 0))
        embed = discord.Embed(
            title="⚠️ Investigation Opened",
            description=(
                f"Case `{case_id}` has been flagged for investigation.\n"
                f"**Actioned by:** {interaction.user.mention}"
            ),
            color=discord.Color.yellow(),
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.followup.send(
            content=owner.mention if owner else "",
            embed=embed
        )
        await self._notify_parties(interaction, case_id, "under_investigation",
                                   "⚠️ Investigation Required", discord.Color.yellow())
        self._disable_all()
        await interaction.message.edit(view=self)

    def _get_case_id_from_message(self, interaction: discord.Interaction) -> str:
        """Extract case_id from the embed description."""
        if interaction.message and interaction.message.embeds:
            for embed in interaction.message.embeds:
                if embed.description and "Case:" in embed.description:
                    # Extract case ID from "Case: `CASE-XXXX`" format
                    import re
                    match = re.search(r'Case:\s*`?([^`\n]+)`?', embed.description)
                    if match:
                        return match.group(1).strip()
        return self.case_id

    async def _notify_parties(self, interaction: discord.Interaction, case_id: str,
                              status: str, title: str, color: discord.Color):
        case = get_builder_case(case_id)
        if not case:
            return
        guild = interaction.guild
        builder = guild.get_member(case["builder_id"])
        customer = guild.get_member(case["customer_id"])

        embed = discord.Embed(
            title=title,
            description=(
                f"**Case:** `{case_id}`\n"
                f"**Status:** {status}\n"
                f"**IGN:** {case['ign']}\n"
                f"**Amount:** {case['amount']}\n"
                f"**Actioned by:** {interaction.user.mention}"
            ),
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        for member in [builder, customer]:
            if member:
                try:
                    await member.send(embed=embed)
                except discord.Forbidden:
                    pass

    def _disable_all(self):
        for item in self.children:
            item.disabled = True


# ═══════════════════════════════════════════════════════════════════
# Cog
# ═══════════════════════════════════════════════════════════════════

class BuilderCog(commands.Cog, name="Builder"):
    """Builder payment and protection timer commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        """Register persistent views on startup."""
        # Register OwnerReviewView template for persistent buttons
        # Pass proper config from CONFIG
        self.bot.add_view(OwnerReviewView("", CONFIG))
        logger.info("Registered OwnerReviewView as persistent view")

    # ── /builder_payment_confirm ────────────────────────────────────────────
    @app_commands.command(
        name="builder_payment_confirm",
        description="Confirm and log a builder payment"
    )
    @app_commands.describe(
        ign="In-game name of the recipient",
        amount="Amount paid"
    )
    async def builder_payment_confirm(
        self,
        interaction: discord.Interaction,
        ign: str,
        amount: str
    ):
        await interaction.response.defer()

        # Any staff member can confirm payments
        staff_role_id = CONFIG.get("STAFF_ROLE_ID")
        member_role_ids = {r.id for r in interaction.user.roles}
        if staff_role_id and staff_role_id not in member_role_ids:
            embed = discord.Embed(
                title="❌ Permission Denied",
                description="You must be a staff member to confirm payments.",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        guild      = interaction.guild
        payment_id = f"PAY-{uuid.uuid4().hex[:8].upper()}"
        now        = datetime.now(timezone.utc)

        add_builder_payment(payment_id, interaction.user.id, guild.id, ign, amount)
        log_staff_action("builder_payment", interaction.user.id, guild.id,
                         details=f"ID: {payment_id} | IGN: {ign} | Amount: {amount}")

        embed = discord.Embed(
            title="💰 Builder Payment Confirmed",
            color=discord.Color.green(),
            timestamp=now,
        )
        embed.add_field(name="Payment ID",   value=payment_id,              inline=True)
        embed.add_field(name="IGN",          value=ign,                     inline=True)
        embed.add_field(name="Amount",       value=amount,                  inline=True)
        embed.add_field(name="Staff Member", value=interaction.user.mention, inline=True)
        embed.add_field(name="Date",         value=now.strftime("%Y-%m-%d"), inline=True)
        embed.add_field(name="Time (UTC)",   value=now.strftime("%H:%M:%S"), inline=True)
        embed.set_footer(text=f"Staff ID: {interaction.user.id}")
        await interaction.followup.send(embed=embed)

        # Send to builder logs channel
        ch_id = CONFIG.get("BUILDER_LOGS_CHANNEL_ID")
        if ch_id:
            ch = guild.get_channel(ch_id)
            if ch:
                try:
                    await ch.send(embed=embed)
                except discord.Forbidden:
                    pass

    # ── /builder_history ────────────────────────────────────────────────────
    @app_commands.command(
        name="builder_history",
        description="View all builder payment records for a user"
    )
    @app_commands.describe(user="The staff member whose records to view")
    async def builder_history(
        self,
        interaction: discord.Interaction,
        user: discord.Member
    ):
        await interaction.response.defer()

        guild    = interaction.guild
        payments = get_builder_payments(user.id, guild.id)

        embed = discord.Embed(
            title=f"📋 Builder Payment History — {user.display_name}",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=user.display_avatar.url)

        if not payments:
            embed.description = "No payment records found for this user."
        else:
            lines = []
            for p in payments[:20]:    # cap at 20 to fit embed limit
                lines.append(
                    f"`{p['payment_id']}` | **IGN:** {p['ign']} | "
                    f"**Amount:** {p['amount']} | {p['timestamp'][:10]}"
                )
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"Total records: {len(payments)} (showing up to 20)")

        await interaction.followup.send(embed=embed)

    # ── /builder_start_timer ────────────────────────────────────────────────
    @app_commands.command(
        name="builder_start_timer",
        description="Start a 48-hour builder protection timer"
    )
    @app_commands.describe(
        customer="The customer for this transaction",
        ign="In-game name",
        amount="Transaction amount"
    )
    async def builder_start_timer(
        self,
        interaction: discord.Interaction,
        customer: discord.Member,
        ign: str,
        amount: str
    ):
        await interaction.response.defer()

        # Builder role only
        builder_role_id  = CONFIG.get("STAFF_ROLES", {}).get("Builder")
        member_role_ids  = {r.id for r in interaction.user.roles}
        if not builder_role_id or builder_role_id not in member_role_ids:
            embed = discord.Embed(
                title="❌ Permission Denied",
                description="Only members with the **Builder** role can start timers.",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if customer.id == interaction.user.id:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Invalid", description="You cannot set yourself as the customer.", color=discord.Color.red()),
                ephemeral=True
            )
            return

        guild   = interaction.guild
        case_id = f"CASE-{uuid.uuid4().hex[:8].upper()}"

        create_builder_case(case_id, interaction.user.id, customer.id, guild.id, ign, amount)

        # Send confirmation embed with buttons to the customer
        embed = discord.Embed(
            title="🔒 Builder Protection Timer Request",
            description=(
                f"{customer.mention}, a builder has started a protection timer for your transaction.\n"
                f"Please confirm or reject below."
            ),
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Case ID",  value=case_id,                    inline=True)
        embed.add_field(name="Builder",  value=interaction.user.mention,   inline=True)
        embed.add_field(name="Customer", value=customer.mention,           inline=True)
        embed.add_field(name="IGN",      value=ign,                        inline=True)
        embed.add_field(name="Amount",   value=amount,                     inline=True)
        embed.add_field(name="Duration", value="48 hours after confirmation", inline=True)
        embed.set_footer(text="Only the customer named above can use these buttons")

        view = CustomerConfirmView(case_id, interaction.user, customer.id, CONFIG)
        await interaction.followup.send(content=customer.mention, embed=embed, view=view)

        log_builder_timer_event(case_id, "timer_requested", interaction.user.id,
                                f"Timer request sent to {customer}")

    # ── /builder_timer_status ────────────────────────────────────────────────
    @app_commands.command(
        name="builder_timer_status",
        description="Check the status of a builder timer case"
    )
    @app_commands.describe(case_id="The case ID to check (e.g. CASE-AB12CD34)")
    async def builder_timer_status(
        self,
        interaction: discord.Interaction,
        case_id: str
    ):
        await interaction.response.defer()

        case = get_builder_case(case_id.upper())
        if not case:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Not Found", description=f"Case `{case_id}` does not exist.", color=discord.Color.red()),
                ephemeral=True
            )
            return

        guild    = interaction.guild
        builder  = guild.get_member(case["builder_id"])
        customer = guild.get_member(case["customer_id"])
        now      = datetime.now(timezone.utc)

        # Remaining time
        remaining_str = "N/A"
        if case["end_time"] and case["status"] == "active":
            end = datetime.fromisoformat(case["end_time"]).replace(tzinfo=timezone.utc)
            remaining = end - now
            if remaining.total_seconds() > 0:
                h, rem = divmod(int(remaining.total_seconds()), 3600)
                m, s   = divmod(rem, 60)
                remaining_str = f"{h}h {m}m {s}s"
            else:
                remaining_str = "Expired"

        status_colors = {
            "pending_confirmation": discord.Color.yellow(),
            "active":               discord.Color.green(),
            "approved":             discord.Color.green(),
            "on_hold":              discord.Color.orange(),
            "rejected":             discord.Color.red(),
            "under_investigation":  discord.Color.red(),
            "awaiting_review":      discord.Color.blue(),
        }
        color = status_colors.get(case["status"], discord.Color.greyple())

        embed = discord.Embed(
            title=f"🔍 Case Status — {case_id.upper()}",
            color=color,
            timestamp=now,
        )
        embed.add_field(name="Case ID",    value=case["case_id"],                                                              inline=True)
        embed.add_field(name="Status",     value=case["status"].replace("_", " ").title(),                                    inline=True)
        embed.add_field(name="\u200b",     value="\u200b",                                                                    inline=True)
        embed.add_field(name="Builder",    value=builder.mention  if builder  else str(case["builder_id"]),                   inline=True)
        embed.add_field(name="Customer",   value=customer.mention if customer else str(case["customer_id"]),                  inline=True)
        embed.add_field(name="\u200b",     value="\u200b",                                                                    inline=True)
        embed.add_field(name="IGN",        value=case["ign"],                                                                 inline=True)
        embed.add_field(name="Amount",     value=case["amount"],                                                              inline=True)
        embed.add_field(name="\u200b",     value="\u200b",                                                                    inline=True)
        embed.add_field(name="Start Time", value=case["start_time"]  or "Not started",                                        inline=True)
        embed.add_field(name="End Time",   value=case["end_time"]    or "N/A",                                                inline=True)
        embed.add_field(name="Remaining",  value=remaining_str,                                                               inline=True)
        embed.set_footer(text=f"Created: {case['created_at'][:19]}")
        await interaction.followup.send(embed=embed)

    # ── /builder_cases ────────────────────────────────────────────────────────
    @app_commands.command(name="builder_cases", description="List all builder cases in this server")
    async def builder_cases(self, interaction: discord.Interaction):
        await interaction.response.defer()

        guild = interaction.guild
        cases = get_all_builder_cases(guild.id)

        embed = discord.Embed(
            title="📋 All Builder Cases",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        if not cases:
            embed.description = "No builder cases found."
        else:
            lines = []
            for c in cases[:20]:
                builder = guild.get_member(c["builder_id"])
                bname   = builder.display_name if builder else f"ID:{c['builder_id']}"
                lines.append(
                    f"`{c['case_id']}` | **{bname}** | "
                    f"{c['status'].replace('_', ' ').title()} | {c['created_at'][:10]}"
                )
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"Total: {len(cases)} (showing up to 20)")

        await interaction.followup.send(embed=embed)

    # ── /builder_case ─────────────────────────────────────────────────────────
    @app_commands.command(name="builder_case", description="View full details of a single builder case")
    @app_commands.describe(case_id="The case ID (e.g. CASE-AB12CD34)")
    async def builder_case(self, interaction: discord.Interaction, case_id: str):
        await interaction.response.defer()

        case = get_builder_case(case_id.upper())
        if not case:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Not Found", description=f"Case `{case_id}` not found.", color=discord.Color.red()),
                ephemeral=True
            )
            return

        guild    = interaction.guild
        builder  = guild.get_member(case["builder_id"])
        customer = guild.get_member(case["customer_id"])
        logs     = get_builder_case_logs(case_id.upper())

        embed = discord.Embed(
            title=f"📁 Case Detail — {case_id.upper()}",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Status",   value=case["status"].replace("_", " ").title(), inline=True)
        embed.add_field(name="Builder",  value=builder.mention  if builder  else str(case["builder_id"]),  inline=True)
        embed.add_field(name="Customer", value=customer.mention if customer else str(case["customer_id"]), inline=True)
        embed.add_field(name="IGN",      value=case["ign"],   inline=True)
        embed.add_field(name="Amount",   value=case["amount"], inline=True)
        embed.add_field(name="Created",  value=case["created_at"][:19], inline=True)

        if case["start_time"]:
            embed.add_field(name="Start", value=case["start_time"][:19], inline=True)
        if case["end_time"]:
            embed.add_field(name="End",   value=case["end_time"][:19],   inline=True)

        if logs:
            log_lines = []
            for lg in logs[-10:]:   # last 10 events
                actor = guild.get_member(lg["actor_id"]) if lg["actor_id"] else None
                aname = actor.display_name if actor else "System"
                log_lines.append(
                    f"`{lg['timestamp'][:19]}` **{lg['event']}** by {aname}"
                    + (f"\n  ↳ {lg['note']}" if lg["note"] else "")
                )
            embed.add_field(name="Event Log (last 10)", value="\n".join(log_lines), inline=False)

        await interaction.followup.send(embed=embed)

    # ── /builder_cancel_timer ────────────────────────────────────────────────
    @app_commands.command(
        name="builder_cancel_timer",
        description="Cancel a builder timer before the customer confirms"
    )
    @app_commands.describe(case_id="The case ID to cancel")
    async def builder_cancel_timer(
        self,
        interaction: discord.Interaction,
        case_id: str
    ):
        await interaction.response.defer()

        case = get_builder_case(case_id.upper())
        if not case:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Not Found", description=f"Case `{case_id}` not found.", color=discord.Color.red()),
                ephemeral=True
            )
            return

        # Only the builder who created the case may cancel it pre-confirmation
        if case["builder_id"] != interaction.user.id:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Permission Denied", description="Only the builder who created this case can cancel it.", color=discord.Color.red()),
                ephemeral=True
            )
            return

        if case["status"] != "pending_confirmation":
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Cannot Cancel",
                    description=f"Case `{case_id}` has status **{case['status']}** and cannot be cancelled at this stage.",
                    color=discord.Color.orange()
                ),
                ephemeral=True
            )
            return

        update_builder_case_status(case_id.upper(), "cancelled")
        log_builder_timer_event(case_id.upper(), "cancelled", interaction.user.id,
                                "Cancelled by builder before customer confirmation.")
        log_staff_action("builder_cancel", interaction.user.id, interaction.guild.id,
                         details=f"Case {case_id} cancelled")

        embed = discord.Embed(
            title="🗑️ Timer Cancelled",
            description=f"Case `{case_id.upper()}` has been cancelled.",
            color=discord.Color.greyple(),
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(BuilderCog(bot))
