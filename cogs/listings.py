"""
listings.py — Full marketplace / listings panel system.

Commands:
  /listingsetup          — Post permanent marketplace panel (Admin)
  /setign [ign]          — Save your Minecraft IGN
  /mylistings            — View your active listings
  /listinghistory        — View completed listing history
  /listingadmin …        — Admin config subcommands

Flow:
  Panel → Create Listing → [Type Select] → [Category Select]
       → [Duration Select (bidding)] → Modal → Preview → Confirm → Live Embed

All button interactions are routed via on_interaction with custom_ids
prefixed "listing_".  Persistent buttons use timeout=None.
"""

import asyncio
import json
import logging
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from utils.database import (
    # vouch system (existing — do NOT rebuild)
    add_vouch, add_scam_vouch, remove_scam_vouch, get_vouch_counts,
    # listing helpers (new)
    create_listing, get_listing, update_listing, atomic_claim_listing,
    get_active_listings, get_user_listings, get_listing_history,
    get_expired_active_listings,
    set_user_ign, get_user_ign,
    create_listing_transaction, get_transaction_by_channel, update_transaction,
    add_listing_rating, get_user_avg_rating,
    # guild config
    get_guild_config, set_guild_config,
    log_staff_action,
)
from utils.permissions import is_authorized, CONFIG as BOT_CONFIG

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

CATEGORIES = ["Spawners", "Bases", "Items"]
DURATIONS = {
    "1 minute":  60_000,
    "5 minutes": 5 * 60_000,
    "15 minutes": 15 * 60_000,
    "30 minutes": 30 * 60_000,
    "1 hour":    60 * 60_000,
    "6 hours":   6 * 60 * 60_000,
    "12 hours":  12 * 60 * 60_000,
    "1 day":     24 * 60 * 60_000,
    "2 days":    2 * 24 * 60 * 60_000,
    "3 days":    3 * 24 * 60 * 60_000,
}
PREVIEW_TIMEOUT = 120  # seconds

# ── Config helpers ─────────────────────────────────────────────────────────────

def _lcfg(guild_id: int, key: str) -> Optional[str]:
    return get_guild_config(guild_id, f"listing_{key}")

def _set_lcfg(guild_id: int, key: str, value: str):
    set_guild_config(guild_id, f"listing_{key}", value)

def _admin_role_id(guild_id: int) -> Optional[int]:
    raw = _lcfg(guild_id, "admin_role_id")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    # Fall back to global OWNER_ID
    return None

def _is_listing_admin(member: discord.Member, guild_id: int) -> bool:
    if is_authorized(member, member.guild, "listingadmin"):
        return True
    rid = _admin_role_id(guild_id)
    if rid:
        return any(r.id == rid for r in member.roles)
    return False

# ── ID generation ──────────────────────────────────────────────────────────────

def _gen_listing_id(listing_type: str) -> str:
    prefix = "AUC" if listing_type == "auction" else "BID"
    num = random.randint(1000, 9999)
    return f"{prefix}-{num}"

# ── Price helpers ──────────────────────────────────────────────────────────────

def parse_price(s: str) -> float:
    """
    Parse a monetary value that may use shorthand suffixes (case-insensitive).
    Strips leading $, commas, and spaces before parsing.

    Supported suffixes:
        k  → × 1,000          (e.g. "100k"  → 100,000)
        m  → × 1,000,000      (e.g. "2m"    → 2,000,000)
        b  → × 1,000,000,000  (e.g. "5b"    → 5,000,000,000)
        t  → × 1,000,000,000,000

    Examples:
        "5000"   → 5000.0
        "1,500"  → 1500.0
        "$2k"    → 2000.0
        "1.5m"   → 1500000.0
        "5b"     → 5000000000.0

    Raises ValueError for invalid/non-positive/non-finite input.
    """
    s = s.strip().lstrip("$").replace(",", "").replace(" ", "")
    if not s:
        raise ValueError("Empty price")
    _SUFFIXES = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "t": 1_000_000_000_000}
    lower = s.lower()
    val: float
    for suffix, multiplier in _SUFFIXES.items():
        if lower.endswith(suffix):
            val = float(lower[:-1]) * multiplier
            break
    else:
        val = float(s)
    if not (0 < val < float("inf")):
        raise ValueError(f"Price must be a positive finite number, got: {val}")
    return val


def _normalise_price(s: str) -> str:
    """
    Convert a price string (plain or shorthand) to a canonical integer/float
    string suitable for DB storage and downstream float() calls.
    Returns the original string unchanged if it cannot be parsed (so callers
    that already validate can rely on the return value being numeric).
    """
    val = parse_price(s)
    if val == int(val):
        return str(int(val))
    return f"{val:.2f}"


def _fmt_price(val) -> str:
    """Format a stored price value (str or number) with thousand-separators for display."""
    if val is None or val == "":
        return "N/A"
    try:
        f = float(val)
        if f == int(f):
            return f"{int(f):,}"
        return f"{f:,.2f}"
    except (ValueError, TypeError):
        return str(val)


# ── Embed builders ─────────────────────────────────────────────────────────────

def _stars(avg: float, count: int) -> str:
    if count == 0:
        return "No ratings yet"
    filled = round(avg)
    return "★" * filled + "☆" * (5 - filled) + f" ({avg})"

def _build_listing_embed(data: dict, guild: discord.Guild, is_preview: bool = False) -> discord.Embed:
    """Build the listing embed from a dict of listing fields."""
    category   = data.get("category", "")
    ltype      = data.get("type", "auction")
    item_name  = data.get("item_name", "Unknown")
    seller_id  = data.get("seller_id")
    qty        = data.get("quantity", "1")
    desc       = data.get("description") or ""
    listing_id = data.get("listing_id", "???")
    status     = data.get("status", "active")

    color_map = {
        "active":    discord.Color.gold(),
        "sold":      discord.Color.green(),
        "expired":   discord.Color.greyple(),
        "cancelled": discord.Color.red(),
    }
    color = color_map.get(status, discord.Color.gold())
    if is_preview:
        color = discord.Color.blurple()

    title = f"📦 {item_name}"
    embed = discord.Embed(title=title, color=color)

    seller_mention = f"<@{seller_id}>" if seller_id else "Unknown"
    if not is_preview:
        avg, cnt = get_user_avg_rating(seller_id) if seller_id else (0.0, 0)
        rating_str = _stars(avg, cnt)
        embed.add_field(name="Seller", value=f"{seller_mention}\n{rating_str}", inline=True)
    else:
        embed.add_field(name="Listed by", value=seller_mention, inline=True)

    embed.add_field(name="Category", value=category, inline=True)
    embed.add_field(name="Quantity", value=qty, inline=True)

    if desc:
        embed.add_field(name="Description", value=desc[:500], inline=False)

    if ltype == "auction":
        buy_now = _fmt_price(data.get("buy_now_price"))
        embed.add_field(name="🏷️ AUCTION", value=f"**Buy Now:** ${buy_now}", inline=False)
    else:
        starting = _fmt_price(data.get("starting_bid"))
        current  = data.get("current_bid")
        bidder   = data.get("current_bidder")
        bidder_s = f"<@{bidder}>" if bidder else "None"
        min_inc  = _fmt_price(data.get("min_increment"))
        reserve  = data.get("reserve_price")
        ends_at  = data.get("ends_at")

        lines = [
            f"**Starting Bid:** ${starting}",
            f"**Current Bid:** {'No bids yet' if not bidder else f'${_fmt_price(current)}'}",
            f"**Current Bidder:** {bidder_s}",
            f"**Min Increment:** ${min_inc}",
        ]
        if reserve:
            lines.append("**Reserve:** Hidden")
        buy_now = data.get("buy_now_price")
        if buy_now:
            lines.append(f"**Buy Now:** ${_fmt_price(buy_now)}")
        if ends_at:
            try:
                try:
                    end_dt = datetime.strptime(ends_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    end_dt = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
                ts = int(end_dt.timestamp())
                lines.append(f"**Ends:** <t:{ts}:F> (<t:{ts}:R>)")
            except Exception:
                lines.append(f"**Ends:** {ends_at}")
        embed.add_field(name="🔨 BIDDING", value="\n".join(lines), inline=False)

    status_emoji = {"active": "🟢 ACTIVE", "sold": "✅ SOLD", "expired": "⏰ EXPIRED", "cancelled": "❌ CANCELLED"}
    footer_status = status_emoji.get(status, status.upper())
    if is_preview:
        footer_status = "PREVIEW"
    embed.set_footer(text=f"{listing_id}  •  {footer_status}")
    return embed

# ── Modals ─────────────────────────────────────────────────────────────────────

class AuctionModal(discord.ui.Modal, title="Create Auction Listing"):
    item_name = discord.ui.TextInput(
        label="Item Name",
        placeholder="e.g. Blaze Spawner x3",
        max_length=100,
    )
    description = discord.ui.TextInput(
        label="Description (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="Any extra info about the item…",
        required=False,
        max_length=500,
    )
    quantity = discord.ui.TextInput(
        label="Quantity",
        placeholder="e.g. 3",
        max_length=20,
    )
    buy_now_price = discord.ui.TextInput(
        label="Buy Now Price ($)",
        placeholder="e.g. 5000, 100k, 2.5m, 5b",
        max_length=30,
    )

    def __init__(self, cog, category: str, prefill: dict = None):
        super().__init__()
        self._cog      = cog
        self._category = category
        if prefill:
            self.item_name.default    = prefill.get("item_name", "")
            self.description.default  = prefill.get("description", "")
            self.quantity.default     = prefill.get("quantity", "")
            self.buy_now_price.default = prefill.get("buy_now_price", "")

    async def on_submit(self, interaction: discord.Interaction):
        raw_price = self.buy_now_price.value.strip()
        try:
            norm_price = _normalise_price(raw_price)
        except (ValueError, ZeroDivisionError, OverflowError):
            await interaction.response.send_message(
                "❌ Invalid buy-now price. Enter a positive number like `5000`, `100k`, `2.5m`, or `5b`.",
                ephemeral=True,
            )
            return
        data = {
            "type":          "auction",
            "category":      self._category,
            "item_name":     self.item_name.value.strip(),
            "description":   self.description.value.strip(),
            "quantity":      self.quantity.value.strip(),
            "buy_now_price": norm_price,
        }
        await self._cog._show_preview(interaction, data)


class BiddingModal(discord.ui.Modal, title="Create Bidding Listing"):
    item_name = discord.ui.TextInput(
        label="Item Name",
        placeholder="e.g. Desert Base",
        max_length=100,
    )
    description = discord.ui.TextInput(
        label="Description (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="Any extra info…",
        required=False,
        max_length=500,
    )
    quantity = discord.ui.TextInput(
        label="Quantity",
        placeholder="e.g. 1",
        max_length=20,
    )
    starting_bid = discord.ui.TextInput(
        label="Starting Bid ($)",
        placeholder="e.g. 1000, 50k, 1m",
        max_length=30,
    )
    min_increment = discord.ui.TextInput(
        label="Min Bid Increment ($)",
        placeholder="e.g. 50, 1k, 500k",
        max_length=30,
    )

    def __init__(self, cog, category: str, duration_label: str, duration_ms: int, prefill: dict = None):
        super().__init__()
        self._cog           = cog
        self._category      = category
        self._duration_label = duration_label
        self._duration_ms   = duration_ms
        if prefill:
            self.item_name.default    = prefill.get("item_name", "")
            self.description.default  = prefill.get("description", "")
            self.quantity.default     = prefill.get("quantity", "")
            self.starting_bid.default  = prefill.get("starting_bid", "")
            self.min_increment.default = prefill.get("min_increment", "")

    async def on_submit(self, interaction: discord.Interaction):
        raw_starting = self.starting_bid.value.strip()
        raw_increment = self.min_increment.value.strip()
        try:
            norm_starting = _normalise_price(raw_starting)
        except (ValueError, ZeroDivisionError, OverflowError):
            await interaction.response.send_message(
                "❌ Invalid starting bid. Enter a positive number like `5000`, `100k`, `2.5m`, or `5b`.",
                ephemeral=True,
            )
            return
        try:
            norm_increment = _normalise_price(raw_increment)
        except (ValueError, ZeroDivisionError, OverflowError):
            await interaction.response.send_message(
                "❌ Invalid min increment. Enter a positive number like `500`, `1k`, `0.5m`, etc.",
                ephemeral=True,
            )
            return
        data = {
            "type":           "bidding",
            "category":       self._category,
            "item_name":      self.item_name.value.strip(),
            "description":    self.description.value.strip(),
            "quantity":       self.quantity.value.strip(),
            "starting_bid":   norm_starting,
            "min_increment":  norm_increment,
            "buy_now_price":  None,
            "reserve_price":  None,
            "duration_label": self._duration_label,
            "duration_ms":    self._duration_ms,
        }
        await self._cog._show_preview(interaction, data)


class OfferModal(discord.ui.Modal, title="Make an Offer"):
    offer_amount = discord.ui.TextInput(
        label="Offer Amount ($)",
        placeholder="e.g. 4500, 100k, 2m",
        max_length=30,
    )
    message = discord.ui.TextInput(
        label="Message to Seller (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )

    def __init__(self, cog, listing_id: str):
        super().__init__()
        self._cog        = cog
        self._listing_id = listing_id

    async def on_submit(self, interaction: discord.Interaction):
        await self._cog._handle_offer_submit(interaction, self._listing_id,
                                              self.offer_amount.value.strip(),
                                              self.message.value.strip())


class BidModal(discord.ui.Modal, title="Place a Bid"):
    bid_amount = discord.ui.TextInput(
        label="Your Bid ($)",
        placeholder="e.g. 5000, 100k, 2m — must meet the minimum",
        max_length=30,
    )

    def __init__(self, cog, listing_id: str, min_next: str):
        super().__init__(title=f"Place a Bid (min ${min_next})")
        self._cog        = cog
        self._listing_id = listing_id
        self.bid_amount.placeholder = f"Minimum: ${min_next}"

    async def on_submit(self, interaction: discord.Interaction):
        await self._cog._handle_bid_submit(interaction, self._listing_id,
                                            self.bid_amount.value.strip())


class CounterOfferModal(discord.ui.Modal, title="Counter Offer"):
    counter_amount = discord.ui.TextInput(
        label="Your Counter Offer ($)",
        placeholder="e.g. 4800, 100k, 2m",
        max_length=30,
    )

    def __init__(self, cog, listing_id: str, buyer_id: int):
        super().__init__()
        self._cog        = cog
        self._listing_id = listing_id
        self._buyer_id   = buyer_id

    async def on_submit(self, interaction: discord.Interaction):
        await self._cog._handle_counter_submit(interaction, self._listing_id,
                                                self._buyer_id,
                                                self.counter_amount.value.strip())


class AppealModal(discord.ui.Modal, title="Appeal Scam Vouch"):
    reason = discord.ui.TextInput(
        label="Reason for Appeal",
        style=discord.TextStyle.paragraph,
        placeholder="Explain why this scam vouch is incorrect…",
        max_length=1000,
    )
    evidence = discord.ui.TextInput(
        label="Evidence (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="Image links or description of screenshots…",
        required=False,
        max_length=500,
    )

    def __init__(self, cog, listing_id: str, accused_id: int, reporter_id: int):
        super().__init__()
        self._cog         = cog
        self._listing_id  = listing_id
        self._accused_id  = accused_id
        self._reporter_id = reporter_id

    async def on_submit(self, interaction: discord.Interaction):
        await self._cog._handle_appeal_submit(interaction, self._listing_id,
                                               self._accused_id, self._reporter_id,
                                               self.reason.value.strip(),
                                               self.evidence.value.strip())


class AppealDenyModal(discord.ui.Modal, title="Deny Appeal"):
    reason = discord.ui.TextInput(
        label="Denial Reason",
        style=discord.TextStyle.paragraph,
        placeholder="Explain why the appeal is being denied…",
        max_length=500,
    )

    def __init__(self, cog, channel_id: int, accused_id: int, reporter_id: int):
        super().__init__()
        self._cog         = cog
        self._channel_id  = channel_id
        self._accused_id  = accused_id
        self._reporter_id = reporter_id

    async def on_submit(self, interaction: discord.Interaction):
        await self._cog._handle_appeal_deny_submit(interaction, self._channel_id,
                                                    self._accused_id, self._reporter_id,
                                                    self.reason.value.strip())


# ── Rating view (sent via DM after deal) ──────────────────────────────────────

class RatingView(discord.ui.View):
    def __init__(self, cog, rater_id: int, rated_id: int, listing_id: str):
        super().__init__(timeout=86400)  # 24h
        self._cog       = cog
        self._rater_id  = rater_id
        self._rated_id  = rated_id
        self._listing_id = listing_id

    async def _rate(self, interaction: discord.Interaction, stars: int):
        if interaction.user.id != self._rater_id:
            await interaction.response.send_message("This rating is not for you.", ephemeral=True)
            return
        saved = add_listing_rating(self._rater_id, self._rated_id, self._listing_id, stars)
        label = "★" * stars + "☆" * (5 - stars)
        if saved:
            await interaction.response.edit_message(
                content=f"✅ You rated **{label}** for listing `{self._listing_id}`.",
                view=None,
            )
        else:
            await interaction.response.edit_message(
                content="You already rated this transaction.",
                view=None,
            )
        self.stop()

    @discord.ui.button(label="★", style=discord.ButtonStyle.secondary)
    async def s1(self, i, b): await self._rate(i, 1)
    @discord.ui.button(label="★★", style=discord.ButtonStyle.secondary)
    async def s2(self, i, b): await self._rate(i, 2)
    @discord.ui.button(label="★★★", style=discord.ButtonStyle.primary)
    async def s3(self, i, b): await self._rate(i, 3)
    @discord.ui.button(label="★★★★", style=discord.ButtonStyle.primary)
    async def s4(self, i, b): await self._rate(i, 4)
    @discord.ui.button(label="★★★★★", style=discord.ButtonStyle.success)
    async def s5(self, i, b): await self._rate(i, 5)


# ── Main cog ───────────────────────────────────────────────────────────────────

class ListingsCog(commands.Cog, name="Listings"):
    """Marketplace listings panel — auctions, bidding, transactions, vouches."""

    def __init__(self, bot: commands.Bot):
        self.bot      = bot
        # In-memory store for preview data: temp_id → {user_id, guild_id, data, task}
        self._pending: dict[str, dict] = {}
        # Pending offer DMs: token → {listing_id, buyer_id, offer_amount}
        self._offers:  dict[str, dict] = {}
        # Counter offers: token → {listing_id, seller_id, buyer_id, counter}
        self._counters: dict[str, dict] = {}
        self._expiry_task: Optional[asyncio.Task] = None

    async def cog_load(self):
        self._expiry_task  = asyncio.create_task(self._bid_expiry_loop())
        self._cleanup_task = asyncio.create_task(self._offer_cleanup_loop())
        logger.info("Listings bid-expiry and offer-cleanup tasks started.")

    async def cog_unload(self):
        if self._expiry_task:
            self._expiry_task.cancel()
        if hasattr(self, "_cleanup_task") and self._cleanup_task:
            self._cleanup_task.cancel()

    # ── Bid expiry loop ────────────────────────────────────────────────────────

    async def _offer_cleanup_loop(self):
        """Remove stale entries from _offers and _counters every 15 minutes."""
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(900)  # 15 minutes
            try:
                cutoff_ids: list[str] = []
                # TTL: if the referenced listing no longer exists or is inactive, drop it
                for token, entry in list(self._offers.items()):
                    row = get_listing(entry.get("listing_id", ""))
                    if not row or row["status"] != "active":
                        cutoff_ids.append(token)
                for token in cutoff_ids:
                    self._offers.pop(token, None)

                cutoff_ids = []
                for token, entry in list(self._counters.items()):
                    row = get_listing(entry.get("listing_id", ""))
                    if not row or row["status"] != "active":
                        cutoff_ids.append(token)
                for token in cutoff_ids:
                    self._counters.pop(token, None)
            except Exception as exc:
                logger.warning("Offer cleanup error: %s", exc)

    async def _bid_expiry_loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self._check_expired_listings()
            except Exception as exc:
                logger.exception("Bid expiry loop error: %s", exc)
            await asyncio.sleep(30)

    async def _check_expired_listings(self):
        rows = get_expired_active_listings()
        for row in rows:
            listing_id = row["listing_id"]
            try:
                await self._close_expired_listing(row)
            except Exception as exc:
                logger.exception("Error closing expired listing %s: %s", listing_id, exc)

    async def _close_expired_listing(self, row):
        listing_id = row["listing_id"]
        guild_id   = row["guild_id"]
        seller_id  = row["seller_id"]
        guild      = self.bot.get_guild(guild_id)
        if not guild:
            return

        current_bid    = row["current_bid"]
        current_bidder = row["current_bidder"]
        reserve        = row["reserve_price"]
        watchers_raw   = row["watchers"] or "[]"
        try:
            watchers = json.loads(watchers_raw)
        except Exception:
            watchers = []

        log_ch_id = _lcfg(guild_id, "log_channel_id")

        if not current_bidder:
            # No bids
            update_listing(listing_id, status="expired")
            await self._update_live_embed(guild, row, status="expired")
            seller = guild.get_member(seller_id) or self.bot.get_user(seller_id)
            if seller:
                try:
                    await seller.send(embed=discord.Embed(
                        title="⏰ Listing Expired — No Bids",
                        description=f"Your listing **{row['item_name']}** (`{listing_id}`) expired with no bids.",
                        color=discord.Color.greyple(),
                    ))
                except discord.Forbidden:
                    pass
            for uid in watchers:
                m = guild.get_member(uid) or self.bot.get_user(uid)
                if m:
                    try:
                        await m.send(embed=discord.Embed(
                            title=f"⏰ Listing Expired — {row['item_name']}",
                            description=f"`{listing_id}` expired with no bids.",
                            color=discord.Color.greyple(),
                        ))
                    except discord.Forbidden:
                        pass
            await self._post_log(guild, log_ch_id, "Listing Expired", discord.Color.greyple(), [
                ("Item", row["item_name"], True), ("Seller", f"<@{seller_id}>", True),
                ("Listing ID", listing_id, True), ("Outcome", "No bids", True),
                ("Timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), False),
            ])
        else:
            # Has bids
            reserve_met = True
            if reserve:
                try:
                    if float(current_bid) < float(reserve):
                        reserve_met = False
                except Exception:
                    pass

            if not reserve_met:
                update_listing(listing_id, status="expired")
                await self._update_live_embed(guild, row, status="expired",
                                               extra_note="⚠️ Reserve price not met")
                seller = guild.get_member(seller_id) or self.bot.get_user(seller_id)
                if seller:
                    try:
                        await seller.send(embed=discord.Embed(
                            title="⏰ Listing Expired — Reserve Not Met",
                            description=(
                                f"**{row['item_name']}** (`{listing_id}`) ended.\n"
                                f"Highest bid was **${current_bid}** but the reserve was not met."
                            ),
                            color=discord.Color.orange(),
                        ))
                    except discord.Forbidden:
                        pass
                for uid in watchers:
                    m = guild.get_member(uid) or self.bot.get_user(uid)
                    if m:
                        try:
                            await m.send(embed=discord.Embed(
                                title=f"⏰ Reserve Not Met — {row['item_name']}",
                                description=f"`{listing_id}` ended. Reserve not met.",
                                color=discord.Color.orange(),
                            ))
                        except discord.Forbidden:
                            pass
            else:
                # Winner!
                update_listing(listing_id, status="sold")
                await self._update_live_embed(guild, row, status="sold")
                buyer_id = current_bidder
                ticket_ch = await self._create_transaction_ticket(
                    guild, listing_id, buyer_id, seller_id,
                    final_price=current_bid, listing_data=dict(row)
                )
                buyer = guild.get_member(buyer_id) or self.bot.get_user(buyer_id)
                seller = guild.get_member(seller_id) or self.bot.get_user(seller_id)
                if buyer:
                    try:
                        await buyer.send(embed=discord.Embed(
                            title=f"🏆 You won the auction for {row['item_name']}!",
                            description=f"Winning bid: **${current_bid}**\nTicket: {ticket_ch.mention if ticket_ch else 'created'}",
                            color=discord.Color.gold(),
                        ))
                    except discord.Forbidden:
                        pass
                if seller:
                    try:
                        await seller.send(embed=discord.Embed(
                            title=f"🎉 {row['item_name']} sold!",
                            description=f"Sold to <@{buyer_id}> for **${current_bid}**.",
                            color=discord.Color.green(),
                        ))
                    except discord.Forbidden:
                        pass
                for uid in watchers:
                    m = guild.get_member(uid) or self.bot.get_user(uid)
                    if m:
                        try:
                            await m.send(embed=discord.Embed(
                                title=f"✅ {row['item_name']} — Auction Ended",
                                description=f"`{listing_id}` sold for **${current_bid}**.",
                                color=discord.Color.green(),
                            ))
                        except discord.Forbidden:
                            pass
                await self._post_log(guild, log_ch_id, "Listing Expired → Sold", discord.Color.green(), [
                    ("Item", row["item_name"], True), ("Seller", f"<@{seller_id}>", True),
                    ("Buyer", f"<@{buyer_id}>", True), ("Final Price", f"${_fmt_price(current_bid)}", True),
                    ("Listing ID", listing_id, True),
                    ("Timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), False),
                ])

    # ── Interaction router ─────────────────────────────────────────────────────

    @commands.Cog.listener("on_interaction")
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if not cid.startswith("listing_"):
            return

        # Main panel
        if cid == "listing_panel_create":
            await self._handle_panel_create(interaction)
        elif cid == "listing_panel_browse":
            await self._handle_panel_browse(interaction)
        elif cid == "listing_panel_my":
            await self._handle_panel_my(interaction)

        # Type selection
        elif cid.startswith("listing_type_"):
            parts = cid.split("_", 3)  # listing_type_{type}_{temp_id}
            if len(parts) >= 4:
                ltype   = parts[2]
                temp_id = parts[3]
                await self._handle_type_select(interaction, ltype, temp_id)

        # Category selection
        elif cid.startswith("listing_cat_"):
            # listing_cat_{type}_{category}_{temp_id}
            rest = cid[len("listing_cat_"):]
            parts = rest.split("_", 2)
            if len(parts) == 3:
                ltype, category_enc, temp_id = parts
                category = category_enc.replace("-", " ").title()
                await self._handle_cat_select(interaction, ltype, category, temp_id)

        # Duration selection (bidding only)
        elif cid.startswith("listing_dur_"):
            # listing_dur_{dur_label_enc}_{temp_id}
            rest = cid[len("listing_dur_"):]
            # temp_id is always the last 8 chars (our fixed length)
            temp_id = rest[-8:]
            dur_enc = rest[:-9]  # strip underscore + temp_id
            dur_label = dur_enc.replace("-", " ")
            await self._handle_dur_select(interaction, dur_label, temp_id)

        # Preview actions
        elif cid.startswith("listing_preview_confirm_"):
            temp_id = cid[len("listing_preview_confirm_"):]
            await self._handle_preview_confirm(interaction, temp_id)
        elif cid.startswith("listing_preview_edit_"):
            temp_id = cid[len("listing_preview_edit_"):]
            await self._handle_preview_edit(interaction, temp_id)
        elif cid.startswith("listing_preview_cancel_"):
            temp_id = cid[len("listing_preview_cancel_"):]
            await self._handle_preview_cancel(interaction, temp_id)

        # Buy now — specific prefixes BEFORE generic listing_buy_
        elif cid.startswith("listing_buynow_confirm_"):
            listing_id = cid[len("listing_buynow_confirm_"):]
            await self._handle_buynow_confirm(interaction, listing_id)
        elif cid.startswith("listing_buynow_abort_"):
            await interaction.response.edit_message(
                content="Purchase cancelled.", embed=None, view=None
            )

        # Live listing buttons
        elif cid.startswith("listing_buy_"):
            listing_id = cid[len("listing_buy_"):]
            await self._handle_buy(interaction, listing_id)

        # Offer responses — specific BEFORE generic listing_offer_
        elif cid.startswith("listing_offer_accept_"):
            token = cid[len("listing_offer_accept_"):]
            await self._handle_offer_accept(interaction, token)
        elif cid.startswith("listing_offer_decline_"):
            token = cid[len("listing_offer_decline_"):]
            await self._handle_offer_decline(interaction, token)
        elif cid.startswith("listing_offer_counter_"):
            token = cid[len("listing_offer_counter_"):]
            await self._handle_offer_counter(interaction, token)
        elif cid.startswith("listing_offer_"):
            listing_id = cid[len("listing_offer_"):]
            await self._handle_offer(interaction, listing_id)

        elif cid.startswith("listing_bid_"):
            listing_id = cid[len("listing_bid_"):]
            await self._handle_bid(interaction, listing_id)
        elif cid.startswith("listing_watch_"):
            listing_id = cid[len("listing_watch_"):]
            await self._handle_watch(interaction, listing_id)

        # Cancel — confirm/abort BEFORE generic listing_cancel_
        elif cid.startswith("listing_cancelconfirm_"):
            listing_id = cid[len("listing_cancelconfirm_"):]
            await self._handle_cancel_confirm(interaction, listing_id)
        elif cid.startswith("listing_cancelabort_"):
            await interaction.response.edit_message(
                content="Cancelled — listing not removed.", embed=None, view=None
            )
        elif cid.startswith("listing_cancel_"):
            listing_id = cid[len("listing_cancel_"):]
            await self._handle_cancel(interaction, listing_id)

        # Counter offer responses (buyer DM buttons)
        elif cid.startswith("listing_counter_accept_"):
            token = cid[len("listing_counter_accept_"):]
            await self._handle_counter_accept(interaction, token)
        elif cid.startswith("listing_counter_decline_"):
            token = cid[len("listing_counter_decline_"):]
            await self._handle_counter_decline(interaction, token)

        # Transaction panel
        elif cid.startswith("listing_deal_done_"):
            channel_id = int(cid[len("listing_deal_done_"):])
            await self._handle_deal_done(interaction, channel_id)
        elif cid.startswith("listing_scam_confirm_"):
            channel_id = int(cid[len("listing_scam_confirm_"):])
            await self._handle_scam_confirm(interaction, channel_id)
        elif cid.startswith("listing_scam_abort_"):
            await interaction.response.edit_message(
                content="Scam vouch cancelled.", embed=None, view=None
            )
        elif cid.startswith("listing_scam_vouch_"):
            channel_id = int(cid[len("listing_scam_vouch_"):])
            await self._handle_scam_vouch(interaction, channel_id)

        # Appeal — most specific BEFORE least specific
        elif cid == "listing_appeal_approve2_cancel":
            await interaction.response.edit_message(content="Approval cancelled.", view=None)
        elif cid.startswith("listing_appeal_approve2_"):
            try:
                channel_id = int(cid[len("listing_appeal_approve2_"):])
            except ValueError:
                return
            await self._handle_appeal_approve_confirmed(interaction, channel_id)
        elif cid.startswith("listing_appeal_approve_"):
            channel_id = int(cid[len("listing_appeal_approve_"):])
            await self._handle_appeal_approve(interaction, channel_id)
        elif cid.startswith("listing_appeal_deny_"):
            channel_id = int(cid[len("listing_appeal_deny_"):])
            await self._handle_appeal_deny(interaction, channel_id)
        elif cid.startswith("listing_appeal_"):
            try:
                channel_id = int(cid[len("listing_appeal_"):])
            except ValueError:
                return
            await self._handle_appeal_click(interaction, channel_id)

        # Browse category filter
        elif cid.startswith("listing_browse_cat_"):
            cat = cid[len("listing_browse_cat_"):].replace("-", " ").title()
            await self._handle_browse_cat(interaction, cat if cat != "All" else None)

        # My listings actions
        elif cid.startswith("listing_view_"):
            listing_id = cid[len("listing_view_"):]
            await self._handle_mylistings_view(interaction, listing_id)
        elif cid.startswith("listing_mycancel_"):
            listing_id = cid[len("listing_mycancel_"):]
            await self._handle_cancel(interaction, listing_id)
        elif cid.startswith("listing_relist_"):
            listing_id = cid[len("listing_relist_"):]
            await self._handle_relist(interaction, listing_id)

    # ── Panel: Create Listing ──────────────────────────────────────────────────

    async def _handle_panel_create(self, interaction: discord.Interaction):
        temp_id = self._make_temp_id(interaction.user.id)
        self._pending[temp_id] = {
            "user_id":  interaction.user.id,
            "guild_id": interaction.guild_id,
        }

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Auction — Fixed price + offer",
            emoji="🏷️",
            style=discord.ButtonStyle.primary,
            custom_id=f"listing_type_auction_{temp_id}",
        ))
        view.add_item(discord.ui.Button(
            label="Bidding — Timed bids",
            emoji="🔨",
            style=discord.ButtonStyle.secondary,
            custom_id=f"listing_type_bidding_{temp_id}",
        ))

        await interaction.response.send_message(
            embed=discord.Embed(
                title="📋 What type of listing?",
                description="Choose how you want to sell your item.",
                color=discord.Color.blurple(),
            ),
            view=view,
            ephemeral=True,
        )
        asyncio.create_task(self._expire_preview(temp_id))

    # ── Panel: Browse ──────────────────────────────────────────────────────────

    async def _handle_panel_browse(self, interaction: discord.Interaction):
        view = discord.ui.View(timeout=60)
        for cat in ["All"] + CATEGORIES:
            enc = cat.lower().replace(" ", "-")
            view.add_item(discord.ui.Button(
                label=cat,
                style=discord.ButtonStyle.secondary,
                custom_id=f"listing_browse_cat_{enc}",
            ))
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🔍 Browse Listings",
                description="Select a category to filter.",
                color=discord.Color.blurple(),
            ),
            view=view,
            ephemeral=True,
        )

    async def _handle_browse_cat(self, interaction: discord.Interaction, category: Optional[str]):
        rows = get_active_listings(interaction.guild_id, category)
        if not rows:
            label = category or "all categories"
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="📭 No Active Listings",
                    description=f"There are no active listings in {label}.",
                    color=discord.Color.greyple(),
                ),
                view=None,
            )
            return

        embeds = []
        for row in rows[:10]:
            embeds.append(_build_listing_embed(dict(row), interaction.guild))

        await interaction.response.edit_message(
            content=f"**{len(rows)} active listing(s)**{' in ' + category if category else ''}:",
            embeds=embeds[:10],
            view=None,
        )

    # ── Panel: My Listings ─────────────────────────────────────────────────────

    async def _handle_panel_my(self, interaction: discord.Interaction):
        await self._show_my_listings(interaction)

    async def _show_my_listings(self, interaction: discord.Interaction, followup: bool = False):
        rows = get_user_listings(interaction.user.id, interaction.guild_id)
        active = [r for r in rows if r["status"] == "active"]
        ended  = [r for r in rows if r["status"] in ("expired", "cancelled", "sold")]

        if not rows:
            embed = discord.Embed(
                title="📋 My Listings",
                description="You have no listings yet. Click **Create Listing** on the panel to start.",
                color=discord.Color.blurple(),
            )
            if followup:
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(title="📋 My Listings", color=discord.Color.blurple())
        view = discord.ui.View(timeout=120)
        btn_count = 0

        if active:
            lines = []
            for r in active[:5]:
                lid  = r["listing_id"]
                name = r["item_name"]
                ltype = r["type"]
                if ltype == "bidding":
                    cb = r["current_bid"] or "no bids"
                    ea = r["ends_at"]
                    try:
                        et = datetime.fromisoformat(ea.replace("Z", "+00:00"))
                        ts = int(et.timestamp())
                        extra = f" • ends <t:{ts}:R>"
                    except Exception:
                        extra = ""
                    lines.append(f"🔨 **{name}** `{lid}` — bid: {cb}{extra}")
                else:
                    lines.append(f"🏷️ **{name}** `{lid}` — ${r['buy_now_price']}")
                if btn_count < 5:
                    view.add_item(discord.ui.Button(
                        label=f"View {lid}", style=discord.ButtonStyle.secondary,
                        custom_id=f"listing_view_{lid}", row=0,
                    ))
                    view.add_item(discord.ui.Button(
                        label=f"Cancel {lid}", style=discord.ButtonStyle.danger,
                        custom_id=f"listing_mycancel_{lid}", row=1,
                    ))
                    btn_count += 1
            embed.add_field(name="🟢 Active", value="\n".join(lines), inline=False)

        if ended:
            lines = []
            for r in ended[:5]:
                lid   = r["listing_id"]
                name  = r["item_name"]
                st    = r["status"].upper()
                lines.append(f"**{name}** `{lid}` — {st}")
                if r["status"] in ("expired", "cancelled") and btn_count < 5:
                    view.add_item(discord.ui.Button(
                        label=f"Relist {lid}", style=discord.ButtonStyle.primary,
                        custom_id=f"listing_relist_{lid}", row=2,
                    ))
                    btn_count += 1
            embed.add_field(name="📁 Past Listings", value="\n".join(lines), inline=False)

        if followup:
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── Type → Category select ─────────────────────────────────────────────────

    async def _handle_type_select(self, interaction: discord.Interaction,
                                   ltype: str, temp_id: str):
        if temp_id not in self._pending or self._pending[temp_id]["user_id"] != interaction.user.id:
            await interaction.response.send_message("This menu has expired.", ephemeral=True)
            return
        self._pending[temp_id]["type"] = ltype

        view = discord.ui.View(timeout=None)
        for cat in CATEGORIES:
            enc = cat.lower().replace(" ", "-")
            view.add_item(discord.ui.Button(
                label=cat,
                style=discord.ButtonStyle.secondary,
                custom_id=f"listing_cat_{ltype}_{enc}_{temp_id}",
            ))

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="📂 Select a Category",
                description="What type of item are you listing?",
                color=discord.Color.blurple(),
            ),
            view=view,
        )

    # ── Category → Duration (bidding) or Modal (auction) ──────────────────────

    async def _handle_cat_select(self, interaction: discord.Interaction,
                                  ltype: str, category: str, temp_id: str):
        if temp_id not in self._pending or self._pending[temp_id]["user_id"] != interaction.user.id:
            await interaction.response.send_message("This menu has expired.", ephemeral=True)
            return
        self._pending[temp_id]["category"] = category

        if ltype == "auction":
            prefill = self._pending[temp_id].get("prefill")
            await interaction.response.send_modal(
                AuctionModal(self, category, prefill=prefill)
            )
        else:
            # Show duration select before bidding modal
            view = discord.ui.View(timeout=None)
            for label in DURATIONS:
                enc = label.lower().replace(" ", "-")
                # Encode as listing_dur_{enc}_{temp_id} — temp_id is fixed 8 chars
                view.add_item(discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"listing_dur_{enc}_{temp_id}",
                ))
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="⏱️ Bid Duration",
                    description="How long should this auction run?",
                    color=discord.Color.blurple(),
                ),
                view=view,
            )

    async def _handle_dur_select(self, interaction: discord.Interaction,
                                  dur_label: str, temp_id: str):
        if temp_id not in self._pending or self._pending[temp_id]["user_id"] != interaction.user.id:
            await interaction.response.send_message("This menu has expired.", ephemeral=True)
            return
        dur_ms = DURATIONS.get(dur_label)
        if not dur_ms:
            await interaction.response.send_message("Unknown duration. Please try again.", ephemeral=True)
            return

        category = self._pending[temp_id].get("category", "Items")
        prefill  = self._pending[temp_id].get("prefill")
        await interaction.response.send_modal(
            BiddingModal(self, category, dur_label, dur_ms, prefill=prefill)
        )

    # ── Preview ────────────────────────────────────────────────────────────────

    async def _show_preview(self, interaction: discord.Interaction, data: dict):
        temp_id = self._make_temp_id(interaction.user.id)
        data["seller_id"] = interaction.user.id
        data["guild_id"]  = interaction.guild_id
        data["listing_id"] = _gen_listing_id(data["type"])
        data["status"]    = "preview"

        self._pending[temp_id] = {
            "user_id":  interaction.user.id,
            "guild_id": interaction.guild_id,
            "data":     data,
        }

        embed = _build_listing_embed(data, interaction.guild, is_preview=True)
        embed.set_author(name="⚠️ LISTING PREVIEW — Only you can see this")

        if data["type"] == "bidding":
            dur_label = data.get("duration_label", "?")
            embed.add_field(name="⏱️ Duration", value=dur_label, inline=True)

        embed.set_footer(text=f"Preview expires in {PREVIEW_TIMEOUT}s. Listing ID: {data['listing_id']}")

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="✅ Confirm & Post",
            style=discord.ButtonStyle.success,
            custom_id=f"listing_preview_confirm_{temp_id}",
        ))
        view.add_item(discord.ui.Button(
            label="✏️ Edit",
            style=discord.ButtonStyle.secondary,
            custom_id=f"listing_preview_edit_{temp_id}",
        ))
        view.add_item(discord.ui.Button(
            label="❌ Cancel",
            style=discord.ButtonStyle.danger,
            custom_id=f"listing_preview_cancel_{temp_id}",
        ))

        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        asyncio.create_task(self._expire_preview(temp_id))

    async def _handle_preview_confirm(self, interaction: discord.Interaction, temp_id: str):
        entry = self._pending.pop(temp_id, None)
        if not entry or entry["user_id"] != interaction.user.id:
            await interaction.response.edit_message(
                content="⏰ Preview expired. Please start over.", embed=None, view=None
            )
            return

        data       = entry["data"]
        guild_id   = interaction.guild_id
        listing_id = data["listing_id"]
        ends_at    = None

        if data["type"] == "bidding":
            dur_ms  = data.get("duration_ms", 3600_000)
            ends_at = (datetime.now(timezone.utc) + timedelta(milliseconds=dur_ms)).strftime("%Y-%m-%d %H:%M:%S")

        # Save to DB
        create_listing(
            listing_id  = listing_id,
            guild_id    = guild_id,
            seller_id   = interaction.user.id,
            item_name   = data["item_name"],
            description = data.get("description"),
            quantity    = data["quantity"],
            category    = data["category"],
            listing_type = data["type"],
            buy_now_price = data.get("buy_now_price"),
            starting_bid  = data.get("starting_bid"),
            min_increment = data.get("min_increment"),
            reserve_price = data.get("reserve_price"),
            duration_ms   = data.get("duration_ms"),
            ends_at       = ends_at,
        )

        # Post to listings channel
        listing_ch_id = _lcfg(guild_id, "channel_id")
        if not listing_ch_id:
            await interaction.response.edit_message(
                content="❌ Listings channel not configured. Ask an admin to run `/listingadmin setlistingchannel`.",
                embed=None, view=None,
            )
            return

        listing_ch = interaction.guild.get_channel(int(listing_ch_id))
        if not listing_ch:
            await interaction.response.edit_message(
                content="❌ Listings channel not found.", embed=None, view=None,
            )
            return

        data["status"]  = "active"
        data["ends_at"] = ends_at
        live_embed = _build_listing_embed(data, interaction.guild)
        live_view  = self._build_live_view(listing_id, data["type"])

        msg = await listing_ch.send(embed=live_embed, view=live_view)
        update_listing(listing_id, message_id=msg.id, channel_id=listing_ch.id)

        # Log
        log_ch_id = _lcfg(guild_id, "log_channel_id")
        await self._post_log(interaction.guild, log_ch_id, "📋 Listing Posted", discord.Color.blurple(), [
            ("Item", data["item_name"], True),
            ("Seller", interaction.user.mention, True),
            ("Category", data["category"], True),
            ("Type", data["type"].title(), True),
            ("Listing ID", listing_id, True),
            ("Timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), False),
        ])

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ Listing Posted!",
                description=f"Your listing **{data['item_name']}** (`{listing_id}`) is now live in {listing_ch.mention}.\n\n[Jump to listing](https://discord.com/channels/{guild_id}/{listing_ch.id}/{msg.id})",
                color=discord.Color.green(),
            ),
            view=None,
        )

    async def _handle_preview_edit(self, interaction: discord.Interaction, temp_id: str):
        entry = self._pending.get(temp_id)
        if not entry or entry["user_id"] != interaction.user.id:
            await interaction.response.edit_message(
                content="⏰ Preview expired. Please start over.", embed=None, view=None
            )
            return

        data = entry["data"]
        ltype    = data["type"]
        category = data["category"]

        if ltype == "auction":
            prefill = {
                "item_name":    data.get("item_name", ""),
                "description":  data.get("description", ""),
                "quantity":     data.get("quantity", ""),
                "buy_now_price": data.get("buy_now_price", ""),
            }
            entry["prefill"] = prefill
            await interaction.response.send_modal(AuctionModal(self, category, prefill=prefill))
        else:
            prefill = {
                "item_name":   data.get("item_name", ""),
                "description": data.get("description", ""),
                "quantity":    data.get("quantity", ""),
                "starting_bid": data.get("starting_bid", ""),
                "min_increment": data.get("min_increment", ""),
            }
            entry["prefill"] = prefill
            dur_label = data.get("duration_label", "1 hour")
            dur_ms    = data.get("duration_ms", DURATIONS["1 hour"])
            await interaction.response.send_modal(
                BiddingModal(self, category, dur_label, dur_ms, prefill=prefill)
            )

    async def _handle_preview_cancel(self, interaction: discord.Interaction, temp_id: str):
        self._pending.pop(temp_id, None)
        await interaction.response.edit_message(
            content="❌ Listing cancelled.", embed=None, view=None
        )

    async def _expire_preview(self, temp_id: str):
        await asyncio.sleep(PREVIEW_TIMEOUT)
        if temp_id in self._pending:
            self._pending.pop(temp_id, None)

    # ── Live listing view builder ───────────────────────────────────────────────

    def _build_live_view(self, listing_id: str, ltype: str) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        if ltype == "auction":
            view.add_item(discord.ui.Button(
                label="🛒 Buy Now", style=discord.ButtonStyle.success,
                custom_id=f"listing_buy_{listing_id}",
            ))
            view.add_item(discord.ui.Button(
                label="💬 Make Offer", style=discord.ButtonStyle.primary,
                custom_id=f"listing_offer_{listing_id}",
            ))
        else:
            view.add_item(discord.ui.Button(
                label="🔨 Place Bid", style=discord.ButtonStyle.success,
                custom_id=f"listing_bid_{listing_id}",
            ))
            buy_now_row = discord.ui.Button(
                label="🛒 Buy Now", style=discord.ButtonStyle.primary,
                custom_id=f"listing_buy_{listing_id}",
            )
            view.add_item(buy_now_row)
            view.add_item(discord.ui.Button(
                label="💬 Make Offer", style=discord.ButtonStyle.secondary,
                custom_id=f"listing_offer_{listing_id}",
            ))

        view.add_item(discord.ui.Button(
            label="👁️ Watch", style=discord.ButtonStyle.secondary,
            custom_id=f"listing_watch_{listing_id}", row=1,
        ))
        view.add_item(discord.ui.Button(
            label="🚫 Cancel Listing", style=discord.ButtonStyle.danger,
            custom_id=f"listing_cancel_{listing_id}", row=1,
        ))
        return view

    # ── Buy Now ────────────────────────────────────────────────────────────────

    async def _handle_buy(self, interaction: discord.Interaction, listing_id: str):
        row = get_listing(listing_id)
        if not row or row["status"] != "active":
            await interaction.response.send_message("This listing is no longer active.", ephemeral=True)
            return
        if row["seller_id"] == interaction.user.id:
            await interaction.response.send_message("You cannot buy your own listing.", ephemeral=True)
            return
        buy_now = row["buy_now_price"]
        if not buy_now:
            await interaction.response.send_message("This listing has no Buy Now price.", ephemeral=True)
            return

        view = discord.ui.View(timeout=60)
        view.add_item(discord.ui.Button(
            label="✅ Yes, Buy",
            style=discord.ButtonStyle.success,
            custom_id=f"listing_buynow_confirm_{listing_id}",
        ))
        view.add_item(discord.ui.Button(
            label="❌ No",
            style=discord.ButtonStyle.secondary,
            custom_id=f"listing_buynow_abort_{listing_id}",
        ))
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🛒 Confirm Purchase",
                description=(
                    f"Confirm purchase of **{row['item_name']}** x{row['quantity']} "
                    f"for **${buy_now}**?"
                ),
                color=discord.Color.gold(),
            ),
            view=view,
            ephemeral=True,
        )

    async def _handle_buynow_confirm(self, interaction: discord.Interaction, listing_id: str):
        row = get_listing(listing_id)
        if not row or row["status"] != "active":
            await interaction.response.edit_message(
                content="This listing is no longer available.", embed=None, view=None
            )
            return

        # Atomic claim — prevents double-sell if two buyers click simultaneously
        claimed = atomic_claim_listing(listing_id, "sold")
        if not claimed:
            await interaction.response.edit_message(
                content="This listing was just sold to someone else.", embed=None, view=None
            )
            return
        await self._update_live_embed(interaction.guild, row, status="sold")

        guild = interaction.guild
        buyer_id  = interaction.user.id
        seller_id = row["seller_id"]
        buy_now   = row["buy_now_price"]

        ticket_ch = await self._create_transaction_ticket(
            guild, listing_id, buyer_id, seller_id,
            final_price=buy_now, listing_data=dict(row)
        )

        seller = guild.get_member(seller_id) or self.bot.get_user(seller_id)
        if seller:
            try:
                await seller.send(embed=discord.Embed(
                    title=f"🛒 {row['item_name']} was purchased!",
                    description=f"<@{buyer_id}> bought your listing for **${buy_now}**.",
                    color=discord.Color.green(),
                ))
            except discord.Forbidden:
                pass

        # Notify watchers
        try:
            watchers = json.loads(row["watchers"] or "[]")
        except Exception:
            watchers = []
        for uid in watchers:
            if uid not in (buyer_id, seller_id):
                m = guild.get_member(uid) or self.bot.get_user(uid)
                if m:
                    try:
                        await m.send(embed=discord.Embed(
                            title=f"✅ {row['item_name']} — SOLD",
                            description=f"Listing `{listing_id}` was sold for **${buy_now}**.",
                            color=discord.Color.green(),
                        ))
                    except discord.Forbidden:
                        pass

        log_ch_id = _lcfg(guild.id, "log_channel_id")
        await self._post_log(guild, log_ch_id, "🛒 Buy Now Executed", discord.Color.green(), [
            ("Item", row["item_name"], True),
            ("Buyer", interaction.user.mention, True),
            ("Seller", f"<@{seller_id}>", True),
            ("Price", f"${_fmt_price(buy_now)}", True),
            ("Listing ID", listing_id, True),
            ("Timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), False),
        ])

        jump = f"https://discord.com/channels/{guild.id}/{ticket_ch.id}" if ticket_ch else ""
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ Purchase Confirmed!",
                description=f"A transaction channel has been created.\n{jump}",
                color=discord.Color.green(),
            ),
            view=None,
        )

    # ── Make Offer ─────────────────────────────────────────────────────────────

    async def _handle_offer(self, interaction: discord.Interaction, listing_id: str):
        row = get_listing(listing_id)
        if not row or row["status"] != "active":
            await interaction.response.send_message("This listing is no longer active.", ephemeral=True)
            return
        if row["seller_id"] == interaction.user.id:
            await interaction.response.send_message("You cannot offer on your own listing.", ephemeral=True)
            return
        await interaction.response.send_modal(OfferModal(self, listing_id))

    async def _handle_offer_submit(self, interaction: discord.Interaction,
                                    listing_id: str, amount: str, msg: str):
        row = get_listing(listing_id)
        if not row or row["status"] != "active":
            await interaction.response.send_message("Listing no longer available.", ephemeral=True)
            return

        try:
            amount = _normalise_price(amount)
        except (ValueError, ZeroDivisionError, OverflowError):
            await interaction.response.send_message(
                "❌ Invalid offer amount. Enter a positive number like `5000`, `100k`, `2m`, etc.",
                ephemeral=True,
            )
            return

        token = self._make_temp_id(interaction.user.id)
        self._offers[token] = {
            "listing_id": listing_id,
            "buyer_id":   interaction.user.id,
            "guild_id":   interaction.guild_id,
            "amount":     amount,
        }

        seller_id = row["seller_id"]
        guild     = interaction.guild
        seller    = guild.get_member(seller_id) or self.bot.get_user(seller_id)
        if not seller:
            await interaction.response.send_message("Could not contact seller.", ephemeral=True)
            return

        view = discord.ui.View(timeout=86400)
        view.add_item(discord.ui.Button(label="✅ Accept", style=discord.ButtonStyle.success,
                                         custom_id=f"listing_offer_accept_{token}"))
        view.add_item(discord.ui.Button(label="❌ Decline", style=discord.ButtonStyle.danger,
                                         custom_id=f"listing_offer_decline_{token}"))
        view.add_item(discord.ui.Button(label="↩️ Counter Offer", style=discord.ButtonStyle.primary,
                                         custom_id=f"listing_offer_counter_{token}"))

        offer_embed = discord.Embed(
            title=f"💬 New Offer on {row['item_name']}",
            color=discord.Color.blurple(),
        )
        offer_embed.add_field(name="Buyer", value=interaction.user.mention, inline=True)
        offer_embed.add_field(name="Offer",  value=f"${_fmt_price(amount)}", inline=True)
        offer_embed.add_field(name="Listing", value=listing_id, inline=True)
        if msg:
            offer_embed.add_field(name="Message", value=msg, inline=False)

        try:
            await seller.send(embed=offer_embed, view=view)
        except discord.Forbidden:
            await interaction.response.send_message("Could not DM seller — they may have DMs disabled.", ephemeral=True)
            return

        await interaction.response.send_message(
            embed=discord.Embed(
                title="💬 Offer Sent",
                description=f"Your offer of **${_fmt_price(amount)}** has been sent to the seller.",
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )

    async def _handle_offer_accept(self, interaction: discord.Interaction, token: str):
        entry = self._offers.pop(token, None)
        if not entry:
            await interaction.response.edit_message(content="This offer has expired.", view=None)
            return

        row = get_listing(entry["listing_id"])
        if not row:
            await interaction.response.edit_message(content="Listing not found.", view=None)
            return
        if interaction.user.id != row["seller_id"]:
            await interaction.response.send_message("Only the seller can accept offers.", ephemeral=True)
            return

        guild = self.bot.get_guild(entry["guild_id"])
        if not guild:
            await interaction.response.edit_message(content="Guild not found.", view=None)
            return
        if row["status"] != "active":
            await interaction.response.edit_message(content="Listing no longer available.", view=None)
            return

        # Atomic claim — prevents double-sell
        claimed = atomic_claim_listing(entry["listing_id"], "sold")
        if not claimed:
            await interaction.response.edit_message(content="Listing was just sold to someone else.", view=None)
            return
        await self._update_live_embed(guild, row, status="sold")

        ticket_ch = await self._create_transaction_ticket(
            guild, entry["listing_id"], entry["buyer_id"], interaction.user.id,
            final_price=entry["amount"], listing_data=dict(row)
        )

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ Offer Accepted",
                description=f"Transaction channel created: {ticket_ch.mention if ticket_ch else 'N/A'}",
                color=discord.Color.green(),
            ),
            view=None,
        )
        buyer = guild.get_member(entry["buyer_id"]) or self.bot.get_user(entry["buyer_id"])
        if buyer:
            try:
                await buyer.send(embed=discord.Embed(
                    title="✅ Offer Accepted!",
                    description=f"Your offer on **{row['item_name']}** was accepted. Ticket: {ticket_ch.mention if ticket_ch else 'created'}",
                    color=discord.Color.green(),
                ))
            except discord.Forbidden:
                pass

    async def _handle_offer_decline(self, interaction: discord.Interaction, token: str):
        entry = self._offers.pop(token, None)
        if not entry:
            await interaction.response.edit_message(content="This offer has expired.", view=None)
            return
        guild = self.bot.get_guild(entry["guild_id"])
        buyer = (guild.get_member(entry["buyer_id"]) if guild else None) or self.bot.get_user(entry["buyer_id"])
        if buyer:
            try:
                row = get_listing(entry["listing_id"])
                await buyer.send(embed=discord.Embed(
                    title="❌ Offer Declined",
                    description=f"Your offer on **{row['item_name'] if row else entry['listing_id']}** was declined.",
                    color=discord.Color.red(),
                ))
            except discord.Forbidden:
                pass
        await interaction.response.edit_message(
            embed=discord.Embed(title="❌ Offer Declined", color=discord.Color.red()),
            view=None,
        )

    async def _handle_offer_counter(self, interaction: discord.Interaction, token: str):
        entry = self._offers.get(token)
        if not entry:
            await interaction.response.edit_message(content="This offer has expired.", view=None)
            return
        await interaction.response.send_modal(
            CounterOfferModal(self, entry["listing_id"], entry["buyer_id"])
        )

    async def _handle_counter_submit(self, interaction: discord.Interaction,
                                      listing_id: str, buyer_id: int, counter: str):
        try:
            counter = _normalise_price(counter)
        except (ValueError, ZeroDivisionError, OverflowError):
            await interaction.response.send_message(
                "❌ Invalid counter offer amount. Enter a positive number like `5000`, `100k`, `2m`, etc.",
                ephemeral=True,
            )
            return

        row   = get_listing(listing_id)
        guild = interaction.guild
        buyer = (guild.get_member(buyer_id) if guild else None) or self.bot.get_user(buyer_id)
        if not buyer:
            await interaction.response.send_message("Could not contact buyer.", ephemeral=True)
            return

        ctoken = self._make_temp_id(interaction.user.id)
        self._counters[ctoken] = {
            "listing_id": listing_id,
            "seller_id":  interaction.user.id,
            "buyer_id":   buyer_id,
            "guild_id":   interaction.guild_id,
            "counter":    counter,
        }

        view = discord.ui.View(timeout=86400)
        view.add_item(discord.ui.Button(label="✅ Accept Counter", style=discord.ButtonStyle.success,
                                         custom_id=f"listing_counter_accept_{ctoken}"))
        view.add_item(discord.ui.Button(label="❌ Decline", style=discord.ButtonStyle.danger,
                                         custom_id=f"listing_counter_decline_{ctoken}"))

        try:
            await buyer.send(embed=discord.Embed(
                title=f"↩️ Counter Offer — {row['item_name'] if row else listing_id}",
                description=f"The seller countered with **${_fmt_price(counter)}**. Do you accept?",
                color=discord.Color.orange(),
            ), view=view)
        except discord.Forbidden:
            await interaction.response.send_message("Could not DM buyer.", ephemeral=True)
            return

        await interaction.response.edit_message(
            embed=discord.Embed(title="↩️ Counter Sent", color=discord.Color.orange()),
            view=None,
        )

    async def _handle_counter_accept(self, interaction: discord.Interaction, token: str):
        entry = self._counters.pop(token, None)
        if not entry:
            await interaction.response.edit_message(content="Counter expired.", view=None)
            return
        if interaction.user.id != entry["buyer_id"]:
            await interaction.response.send_message("Only the buyer can accept this.", ephemeral=True)
            return

        row   = get_listing(entry["listing_id"])
        guild = self.bot.get_guild(entry["guild_id"])
        if not row or row["status"] != "active" or not guild:
            await interaction.response.edit_message(content="Listing no longer available.", view=None)
            return

        claimed = atomic_claim_listing(entry["listing_id"], "sold")
        if not claimed:
            await interaction.response.edit_message(content="Listing was just sold to someone else.", view=None)
            return
        await self._update_live_embed(guild, row, status="sold")
        ticket_ch = await self._create_transaction_ticket(
            guild, entry["listing_id"], entry["buyer_id"], entry["seller_id"],
            final_price=entry["counter"], listing_data=dict(row)
        )
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ Counter Accepted!",
                description=f"Ticket: {ticket_ch.mention if ticket_ch else 'created'}",
                color=discord.Color.green(),
            ),
            view=None,
        )

    async def _handle_counter_decline(self, interaction: discord.Interaction, token: str):
        self._counters.pop(token, None)
        await interaction.response.edit_message(
            embed=discord.Embed(title="❌ Counter Declined", color=discord.Color.red()),
            view=None,
        )

    # ── Place Bid ──────────────────────────────────────────────────────────────

    async def _handle_bid(self, interaction: discord.Interaction, listing_id: str):
        row = get_listing(listing_id)
        if not row or row["status"] != "active" or row["type"] != "bidding":
            await interaction.response.send_message("This listing is not available for bidding.", ephemeral=True)
            return
        if row["seller_id"] == interaction.user.id:
            await interaction.response.send_message("You cannot bid on your own listing.", ephemeral=True)
            return

        # Check IGN
        ign = get_user_ign(interaction.user.id)
        if not ign:
            await interaction.response.send_message(
                "⚠️ Set your Minecraft IGN first with `/setign [name]`.",
                ephemeral=True,
            )
            return

        # Fetch balance from Donut SMP API
        balance_api = BOT_CONFIG.get("balanceApiUrl", "https://donutsmp.net/api/v1/balance/")
        current_bid = float(row["current_bid"] or row["starting_bid"] or 0)
        min_inc     = float(row["min_increment"] or 0)
        min_next    = current_bid + min_inc if row["current_bid"] else float(row["starting_bid"] or 0)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{balance_api}{ign}", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        api_data = await resp.json()
                        balance  = float(api_data.get("balance", api_data.get("money", 0)))
                        if balance < min_next:
                            await interaction.response.send_message(
                                f"❌ Insufficient balance. You need at least **${min_next:,.0f}** "
                                f"but your balance is **${balance:,.0f}**.",
                                ephemeral=True,
                            )
                            return
        except Exception:
            pass  # API unavailable — allow bid without balance check

        await interaction.response.send_modal(
            BidModal(self, listing_id, f"{min_next:,.0f}")
        )

    async def _handle_bid_submit(self, interaction: discord.Interaction,
                                  listing_id: str, amount_str: str):
        row = get_listing(listing_id)
        if not row or row["status"] != "active":
            await interaction.response.send_message("Listing no longer available.", ephemeral=True)
            return

        try:
            amount = parse_price(amount_str)
        except (ValueError, ZeroDivisionError, OverflowError):
            await interaction.response.send_message(
                "❌ Invalid bid amount. Enter a positive number like `5000`, `100k`, `2m`, etc.",
                ephemeral=True,
            )
            return

        current_bid = float(row["current_bid"] or row["starting_bid"] or 0)
        min_inc     = float(row["min_increment"] or 0)
        min_next    = current_bid + min_inc if row["current_bid"] else float(row["starting_bid"] or 0)

        if amount < min_next:
            await interaction.response.send_message(
                f"❌ Bid too low. Minimum is **${min_next:,.0f}**.", ephemeral=True
            )
            return

        prev_bidder = row["current_bidder"]
        guild       = interaction.guild

        # Anti-snipe: if bid placed within last 3 min, extend by 3 min
        new_ends_at = row["ends_at"]
        snipe_note  = ""
        if row["ends_at"]:
            try:
                try:
                    end_dt = datetime.strptime(row["ends_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    end_dt = datetime.fromisoformat(row["ends_at"].replace("Z", "+00:00"))
                now_dt = datetime.now(timezone.utc)
                if (end_dt - now_dt).total_seconds() <= 180:
                    end_dt      = end_dt + timedelta(minutes=3)
                    new_ends_at = end_dt.strftime("%Y-%m-%d %H:%M:%S")
                    snipe_note  = "⚡ Extended — anti-snipe triggered"
            except Exception:
                pass

        # Update bid history
        try:
            bid_history = json.loads(row["bid_history"] or "[]")
        except Exception:
            bid_history = []
        bid_history.append({
            "user_id":   interaction.user.id,
            "amount":    amount,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        update_listing(
            listing_id,
            current_bid     = str(amount),
            current_bidder  = interaction.user.id,
            ends_at         = new_ends_at,
            bid_history     = json.dumps(bid_history),
        )

        # Update live embed
        updated = get_listing(listing_id)
        await self._update_live_embed(guild, updated, snipe_note=snipe_note)

        # DM previous bidder
        if prev_bidder and prev_bidder != interaction.user.id:
            prev = guild.get_member(prev_bidder) or self.bot.get_user(prev_bidder)
            if prev:
                try:
                    await prev.send(embed=discord.Embed(
                        title=f"⚠️ Outbid on {row['item_name']}",
                        description=f"You were outbid! New bid: **${amount:,.0f}** by {interaction.user.mention}",
                        color=discord.Color.orange(),
                    ))
                except discord.Forbidden:
                    pass

        # DM seller
        seller = guild.get_member(row["seller_id"]) or self.bot.get_user(row["seller_id"])
        if seller and seller.id != interaction.user.id:
            try:
                await seller.send(embed=discord.Embed(
                    title=f"🔨 New bid on {row['item_name']}",
                    description=f"**${amount:,.0f}** by {interaction.user.mention}\nListing: `{listing_id}`",
                    color=discord.Color.blurple(),
                ))
            except discord.Forbidden:
                pass

        # DM watchers
        try:
            watchers = json.loads(row["watchers"] or "[]")
        except Exception:
            watchers = []
        for uid in watchers:
            if uid not in (interaction.user.id, row["seller_id"]):
                m = guild.get_member(uid) or self.bot.get_user(uid)
                if m:
                    try:
                        await m.send(embed=discord.Embed(
                            title=f"🔨 New bid — {row['item_name']}",
                            description=f"**${amount:,.0f}** by {interaction.user.mention}\n`{listing_id}`",
                            color=discord.Color.blurple(),
                        ))
                    except discord.Forbidden:
                        pass

        log_ch_id = _lcfg(guild.id, "log_channel_id")
        await self._post_log(guild, log_ch_id, "🔨 Bid Placed", discord.Color.blurple(), [
            ("Item", row["item_name"], True),
            ("Bidder", interaction.user.mention, True),
            ("Amount", f"${amount:,.0f}", True),
            ("Previous Bidder", f"<@{prev_bidder}>" if prev_bidder else "None", True),
            ("Listing ID", listing_id, True),
            ("Timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), False),
        ])

        response = f"✅ Bid of **${amount:,.0f}** placed on **{row['item_name']}**!"
        if snipe_note:
            response += f"\n{snipe_note}"
        await interaction.response.send_message(response, ephemeral=True)

    # ── Watch ──────────────────────────────────────────────────────────────────

    async def _handle_watch(self, interaction: discord.Interaction, listing_id: str):
        row = get_listing(listing_id)
        if not row:
            await interaction.response.send_message("Listing not found.", ephemeral=True)
            return
        try:
            watchers = json.loads(row["watchers"] or "[]")
        except Exception:
            watchers = []
        if interaction.user.id in watchers:
            await interaction.response.send_message("You are already watching this listing.", ephemeral=True)
            return
        watchers.append(interaction.user.id)
        update_listing(listing_id, watchers=json.dumps(watchers))
        await interaction.response.send_message(
            "👁️ You'll be notified of updates on this listing.", ephemeral=True
        )

    # ── Cancel Listing ─────────────────────────────────────────────────────────

    async def _handle_cancel(self, interaction: discord.Interaction, listing_id: str):
        row = get_listing(listing_id)
        if not row:
            await interaction.response.send_message("Listing not found.", ephemeral=True)
            return
        if row["seller_id"] != interaction.user.id and \
                not _is_listing_admin(interaction.user, interaction.guild_id):
            await interaction.response.send_message("Only the seller or an admin can cancel this listing.", ephemeral=True)
            return
        if row["status"] != "active":
            await interaction.response.send_message("This listing is not active.", ephemeral=True)
            return

        view = discord.ui.View(timeout=60)
        view.add_item(discord.ui.Button(label="✅ Yes, Cancel It", style=discord.ButtonStyle.danger,
                                         custom_id=f"listing_cancelconfirm_{listing_id}"))
        view.add_item(discord.ui.Button(label="↩️ Keep It", style=discord.ButtonStyle.secondary,
                                         custom_id=f"listing_cancelabort_{listing_id}"))

        if interaction.response.is_done():
            await interaction.followup.send(
                embed=discord.Embed(
                    title="🚫 Cancel Listing?",
                    description=f"Are you sure you want to cancel **{row['item_name']}** (`{listing_id}`)?\nThis will notify all watchers.",
                    color=discord.Color.red(),
                ),
                view=view, ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="🚫 Cancel Listing?",
                    description=f"Are you sure you want to cancel **{row['item_name']}** (`{listing_id}`)?\nThis will notify all watchers.",
                    color=discord.Color.red(),
                ),
                view=view, ephemeral=True,
            )

    async def _handle_cancel_confirm(self, interaction: discord.Interaction, listing_id: str):
        row = get_listing(listing_id)
        if not row or row["status"] != "active":
            await interaction.response.edit_message(content="Listing already inactive.", embed=None, view=None)
            return

        update_listing(listing_id, status="cancelled")
        await self._update_live_embed(interaction.guild, row, status="cancelled")

        # Notify watchers
        try:
            watchers = json.loads(row["watchers"] or "[]")
        except Exception:
            watchers = []
        guild = interaction.guild
        for uid in watchers:
            m = guild.get_member(uid) or self.bot.get_user(uid)
            if m:
                try:
                    await m.send(embed=discord.Embed(
                        title=f"🚫 Listing Cancelled — {row['item_name']}",
                        description=f"`{listing_id}` was cancelled by the seller.",
                        color=discord.Color.red(),
                    ))
                except discord.Forbidden:
                    pass

        log_ch_id = _lcfg(guild.id, "log_channel_id")
        await self._post_log(guild, log_ch_id, "🚫 Listing Cancelled", discord.Color.red(), [
            ("Item", row["item_name"], True),
            ("Seller", f"<@{row['seller_id']}>", True),
            ("Listing ID", listing_id, True),
            ("Cancelled by", interaction.user.mention, True),
            ("Timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), False),
        ])

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ Listing Cancelled",
                description=f"**{row['item_name']}** (`{listing_id}`) has been cancelled.",
                color=discord.Color.green(),
            ),
            view=None,
        )

    # ── Transaction ticket ─────────────────────────────────────────────────────

    async def _create_transaction_ticket(
        self, guild: discord.Guild,
        listing_id: str, buyer_id: int, seller_id: int,
        final_price: str = None, listing_data: dict = None,
    ) -> Optional[discord.TextChannel]:
        listing_data = listing_data or {}
        cat_id_raw  = _lcfg(guild.id, "ticket_category_id")
        category    = guild.get_channel(int(cat_id_raw)) if cat_id_raw else None

        buyer  = guild.get_member(buyer_id)
        seller = guild.get_member(seller_id)

        buyer_name = (buyer.name if buyer else str(buyer_id))[:12].lower()
        ch_name    = f"txn-{listing_id.lower()}-{buyer_name}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me:           discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                manage_channels=True, read_message_history=True,
            ),
        }
        if buyer:
            overwrites[buyer] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
            )
        if seller and seller.id != buyer_id:
            overwrites[seller] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
            )

        try:
            channel = await guild.create_text_channel(
                name=ch_name,
                category=category,
                overwrites=overwrites,
                topic=f"Transaction: {listing_id} | Buyer: {buyer_id} | Seller: {seller_id}",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.error("Could not create transaction channel: %s", exc)
            return None

        # Save to DB
        create_listing_transaction(
            listing_id=listing_id,
            guild_id=guild.id,
            buyer_id=buyer_id,
            seller_id=seller_id,
            ticket_channel_id=channel.id,
            final_price=final_price,
        )

        # Transaction summary embed
        summary = discord.Embed(
            title=f"📋 Transaction Summary — {listing_id}",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        summary.add_field(name="Item",   value=listing_data.get("item_name", listing_id), inline=True)
        summary.add_field(name="Price",  value=f"${_fmt_price(final_price)}" if final_price else "Offer", inline=True)
        summary.add_field(name="Buyer",  value=f"<@{buyer_id}>",  inline=True)
        summary.add_field(name="Seller", value=f"<@{seller_id}>", inline=True)
        summary.add_field(name="Qty",    value=listing_data.get("quantity", "?"), inline=True)
        summary.set_footer(text="Please complete the transaction and click Deal Done when finished.")

        mentions = " ".join(filter(None, [
            buyer.mention if buyer else None,
            seller.mention if seller and seller.id != buyer_id else None,
        ]))
        summary_msg = await channel.send(content=mentions, embed=summary)
        await summary_msg.pin()

        # Transaction panel
        panel_embed = discord.Embed(
            title="🤝 Transaction Panel",
            description=(
                "Use the buttons below to close out this deal.\n"
                "**Both parties must confirm** before vouches are awarded.\n\n"
                "⚠️ Only use **Scam Vouch** if the other party has genuinely scammed you. "
                "False scam vouches can be appealed."
            ),
            color=discord.Color.blurple(),
        )

        panel_view = discord.ui.View(timeout=None)
        panel_view.add_item(discord.ui.Button(
            label="✅ Deal Done",
            style=discord.ButtonStyle.success,
            custom_id=f"listing_deal_done_{channel.id}",
        ))
        panel_view.add_item(discord.ui.Button(
            label="🚨 Scam Vouch",
            style=discord.ButtonStyle.danger,
            custom_id=f"listing_scam_vouch_{channel.id}",
        ))

        panel_msg = await channel.send(embed=panel_embed, view=panel_view)
        await panel_msg.pin()

        return channel

    # ── Deal Done ──────────────────────────────────────────────────────────────

    async def _handle_deal_done(self, interaction: discord.Interaction, channel_id: int):
        txn = get_transaction_by_channel(channel_id)
        if not txn:
            await interaction.response.send_message("Transaction not found.", ephemeral=True)
            return

        user_id   = interaction.user.id
        buyer_id  = txn["buyer_id"]
        seller_id = txn["seller_id"]

        if user_id not in (buyer_id, seller_id):
            await interaction.response.send_message("Only the buyer or seller can confirm the deal.", ephemeral=True)
            return

        try:
            confirmed = json.loads(txn["deal_confirmed_by"] or "[]")
        except Exception:
            confirmed = []

        if user_id in confirmed:
            await interaction.response.send_message("You have already confirmed this deal.", ephemeral=True)
            return

        confirmed.append(user_id)
        update_transaction(channel_id, deal_confirmed_by=json.dumps(confirmed))

        guild = interaction.guild
        row   = get_listing(txn["listing_id"])

        if len(confirmed) == 1:
            # First party confirmed — update panel
            other_id = seller_id if user_id == buyer_id else buyer_id
            embed = discord.Embed(
                title="🤝 Transaction Panel",
                description=(
                    f"✅ Confirmed by <@{user_id}>\n\n"
                    f"Waiting for <@{other_id}> to confirm…"
                ),
                color=discord.Color.gold(),
            )

            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(
                label=f"✅ Confirmed by {interaction.user.display_name}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"listing_deal_done_{channel_id}",
                disabled=True,
            ))
            view.add_item(discord.ui.Button(
                label="✅ Confirm Deal Complete",
                style=discord.ButtonStyle.success,
                custom_id=f"listing_deal_done_{channel_id}",
            ))
            view.add_item(discord.ui.Button(
                label="🚨 Scam Vouch",
                style=discord.ButtonStyle.danger,
                custom_id=f"listing_scam_vouch_{channel_id}",
            ))
            await interaction.response.edit_message(embed=embed, view=view)
            await interaction.channel.send(f"<@{other_id}> — please confirm the deal is complete! ☝️")

        else:
            # Both confirmed — award vouches
            listing_id = txn["listing_id"]
            proof = f"Marketplace deal — {listing_id}"
            add_vouch(buyer_id, seller_id, guild.id, proof)
            add_vouch(seller_id, buyer_id, guild.id, proof)

            buyer_v,  _ = get_vouch_counts(buyer_id,  guild.id)
            seller_v, _ = get_vouch_counts(seller_id, guild.id)

            done_embed = discord.Embed(
                title="✅ Deal Complete",
                description=(
                    "Both parties have confirmed.\n"
                    "Vouches have been awarded to:\n"
                    f"• <@{buyer_id}> (+1 vouch — total: {buyer_v})\n"
                    f"• <@{seller_id}> (+1 vouch — total: {seller_v})\n\n"
                    "This ticket will close in **5 minutes**."
                ),
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.response.edit_message(embed=done_embed, view=None)

            # DM both
            for uid in (buyer_id, seller_id):
                m = guild.get_member(uid) or self.bot.get_user(uid)
                if m:
                    try:
                        await m.send(embed=discord.Embed(
                            title="✅ Deal Confirmed — Vouch Awarded",
                            description=f"Your deal on `{listing_id}` is complete. You received +1 vouch.",
                            color=discord.Color.green(),
                        ))
                    except discord.Forbidden:
                        pass

            update_transaction(channel_id, status="completed")

            log_ch_id = _lcfg(guild.id, "log_channel_id")
            await self._post_log(guild, log_ch_id, "✅ Deal Confirmed", discord.Color.green(), [
                ("Item", row["item_name"] if row else listing_id, True),
                ("Buyer", f"<@{buyer_id}>", True),
                ("Seller", f"<@{seller_id}>", True),
                ("Final Price", f"${_fmt_price(txn['final_price'])}" if txn["final_price"] else "N/A", True),
                ("Both Vouched", "Yes", True),
                ("Timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), False),
            ])

            # Send rating DMs after 1 minute then close after 5 min
            asyncio.create_task(
                self._post_deal_close(guild, interaction.channel, listing_id, buyer_id, seller_id)
            )

    async def _post_deal_close(self, guild, channel, listing_id, buyer_id, seller_id):
        await asyncio.sleep(60)
        for rater_id, rated_id in ((buyer_id, seller_id), (seller_id, buyer_id)):
            m = guild.get_member(rater_id) or self.bot.get_user(rater_id)
            if m:
                try:
                    await m.send(
                        embed=discord.Embed(
                            title="⭐ Rate Your Experience",
                            description=(
                                f"How was your transaction with <@{rated_id}> on listing `{listing_id}`?\n"
                                "Select a rating below:"
                            ),
                            color=discord.Color.gold(),
                        ),
                        view=RatingView(self, rater_id, rated_id, listing_id),
                    )
                except discord.Forbidden:
                    pass

        await asyncio.sleep(240)  # 4 more minutes (total 5 min)
        try:
            await channel.delete(reason=f"Transaction {listing_id} completed")
        except (discord.Forbidden, discord.NotFound):
            pass

    # ── Scam Vouch ─────────────────────────────────────────────────────────────

    async def _handle_scam_vouch(self, interaction: discord.Interaction, channel_id: int):
        txn = get_transaction_by_channel(channel_id)
        if not txn:
            await interaction.response.send_message("Transaction not found.", ephemeral=True)
            return

        if interaction.user.id not in (txn["buyer_id"], txn["seller_id"]):
            await interaction.response.send_message("Only transaction parties can file a scam vouch.", ephemeral=True)
            return

        view = discord.ui.View(timeout=60)
        view.add_item(discord.ui.Button(
            label="🚨 Yes, Submit Scam Vouch",
            style=discord.ButtonStyle.danger,
            custom_id=f"listing_scam_confirm_{channel_id}",
        ))
        view.add_item(discord.ui.Button(
            label="↩️ Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id=f"listing_scam_abort_{channel_id}",
        ))

        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ Are you sure?",
                description=(
                    "This is a serious action. Submitting a **Scam Vouch** will:\n"
                    "• Add a scam mark to the other party's record permanently\n"
                    "• Lock this ticket channel\n"
                    "• Notify admins\n\n"
                    "**False scam vouches can be appealed and may result in action against you.**\n\n"
                    "Only proceed if you have genuinely been scammed."
                ),
                color=discord.Color.red(),
            ),
            view=view,
            ephemeral=True,
        )

    async def _handle_scam_confirm(self, interaction: discord.Interaction, channel_id: int):
        txn = get_transaction_by_channel(channel_id)
        if not txn:
            await interaction.response.edit_message(content="Transaction not found.", embed=None, view=None)
            return

        reporter_id = interaction.user.id
        if reporter_id not in (txn["buyer_id"], txn["seller_id"]):
            await interaction.response.edit_message(content="Not authorized.", embed=None, view=None)
            return

        accused_id = txn["seller_id"] if reporter_id == txn["buyer_id"] else txn["buyer_id"]
        guild      = interaction.guild
        listing_id = txn["listing_id"]
        row        = get_listing(listing_id)

        proof = f"Marketplace scam report — {listing_id}"
        add_scam_vouch(reporter_id, accused_id, guild.id, proof)
        update_transaction(channel_id, scam_reporter_id=reporter_id, scam_accused_id=accused_id, status="scam_vouch")

        # Update transaction panel
        scam_embed = discord.Embed(
            title="🚨 Scam Vouch Submitted",
            description=(
                f"<@{reporter_id}> has filed a scam vouch against <@{accused_id}>.\n\n"
                f"<@{accused_id}>: If this is false, you can appeal using the button below. "
                "A support ticket will be opened."
            ),
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )

        appeal_view = discord.ui.View(timeout=None)
        appeal_view.add_item(discord.ui.Button(
            label="📣 Appeal Scam Vouch",
            style=discord.ButtonStyle.danger,
            custom_id=f"listing_appeal_{channel_id}",
        ))
        await interaction.response.edit_message(embed=scam_embed, view=None)
        await interaction.channel.send(embed=scam_embed, view=appeal_view)

        # Lock ticket channel
        try:
            await interaction.channel.set_permissions(guild.default_role, send_messages=False)
            accused_m = guild.get_member(accused_id)
            if accused_m:
                await interaction.channel.set_permissions(accused_m, send_messages=False, read_messages=True)
            reporter_m = guild.get_member(reporter_id)
            if reporter_m:
                await interaction.channel.set_permissions(reporter_m, send_messages=False, read_messages=True)
        except discord.Forbidden:
            pass

        # DMs
        accused_m  = guild.get_member(accused_id)  or self.bot.get_user(accused_id)
        reporter_m = guild.get_member(reporter_id) or self.bot.get_user(reporter_id)
        if accused_m:
            try:
                await accused_m.send(embed=discord.Embed(
                    title="🚨 Scam Vouch Filed Against You",
                    description=(
                        f"<@{reporter_id}> has filed a scam vouch against you in listing `{listing_id}`.\n"
                        "You can appeal using the button in the transaction channel."
                    ),
                    color=discord.Color.red(),
                ))
            except discord.Forbidden:
                pass
        if reporter_m:
            try:
                await reporter_m.send(embed=discord.Embed(
                    title="🚨 Scam Vouch Submitted",
                    description=f"Your scam vouch against <@{accused_id}> has been submitted and logged.",
                    color=discord.Color.orange(),
                ))
            except discord.Forbidden:
                pass

        log_ch_id = _lcfg(guild.id, "log_channel_id")
        await self._post_log(guild, log_ch_id, "🚨 Scam Vouch Filed", discord.Color.red(), [
            ("Reporter", f"<@{reporter_id}>", True),
            ("Accused",  f"<@{accused_id}>",  True),
            ("Item", row["item_name"] if row else listing_id, True),
            ("Ticket", interaction.channel.mention, True),
            ("Timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), False),
        ])

    # ── Appeal ─────────────────────────────────────────────────────────────────

    async def _handle_appeal_click(self, interaction: discord.Interaction, channel_id: int):
        txn = get_transaction_by_channel(channel_id)
        if not txn:
            await interaction.response.send_message("Transaction not found.", ephemeral=True)
            return

        accused_id  = txn["scam_accused_id"]
        reporter_id = txn["scam_reporter_id"]

        if interaction.user.id != accused_id:
            await interaction.response.send_message(
                "Only the accused party can appeal this scam vouch.", ephemeral=True
            )
            return

        await interaction.response.send_modal(
            AppealModal(self, txn["listing_id"], accused_id, reporter_id)
        )

    async def _handle_appeal_submit(self, interaction: discord.Interaction,
                                     listing_id: str, accused_id: int, reporter_id: int,
                                     reason: str, evidence: str):
        guild = interaction.guild
        row   = get_listing(listing_id)

        # Create appeal channel
        appeal_cat_id = _lcfg(guild.id, "appeal_category_id")
        appeal_cat    = guild.get_channel(int(appeal_cat_id)) if appeal_cat_id else None
        admin_role_id = _admin_role_id(guild.id)
        admin_role    = guild.get_role(admin_role_id) if admin_role_id else None

        accused_m = guild.get_member(accused_id)
        ch_name   = f"appeal-{listing_id.lower()}-{accused_m.name[:12].lower() if accused_m else accused_id}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me:           discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        if accused_m:
            overwrites[accused_m] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        try:
            appeal_ch = await guild.create_text_channel(
                name=ch_name, category=appeal_cat, overwrites=overwrites,
                topic=f"Appeal: {listing_id} | Accused: {accused_id}",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await interaction.response.send_message(f"Failed to create appeal channel: {exc}", ephemeral=True)
            return

        # Store appeal channel mapping
        update_transaction(
            get_transaction_by_channel(interaction.channel.id)["ticket_channel_id"] if interaction.channel else 0,
        )

        appeal_embed = discord.Embed(
            title="📣 Scam Vouch Appeal",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        appeal_embed.add_field(name="Appellant (Accused)", value=f"<@{accused_id}>", inline=True)
        appeal_embed.add_field(name="Reporter",           value=f"<@{reporter_id}>", inline=True)
        appeal_embed.add_field(name="Listing",            value=listing_id, inline=True)
        appeal_embed.add_field(name="Reason",             value=reason[:1000], inline=False)
        if evidence:
            appeal_embed.add_field(name="Evidence", value=evidence[:500], inline=False)

        admin_view = discord.ui.View(timeout=None)
        admin_view.add_item(discord.ui.Button(
            label="✅ Appeal Approved",
            style=discord.ButtonStyle.success,
            custom_id=f"listing_appeal_approve_{appeal_ch.id}",
        ))
        admin_view.add_item(discord.ui.Button(
            label="❌ Appeal Denied",
            style=discord.ButtonStyle.danger,
            custom_id=f"listing_appeal_deny_{appeal_ch.id}",
        ))

        ping = admin_role.mention if admin_role else "@admins"
        await appeal_ch.send(content=ping, embed=appeal_embed, view=admin_view)

        # Store mapping: appeal channel → transaction info
        set_guild_config(guild.id, f"appeal_{appeal_ch.id}",
                         json.dumps({"listing_id": listing_id, "accused_id": accused_id,
                                     "reporter_id": reporter_id}))

        await interaction.response.send_message(
            "✅ Appeal submitted. An admin will review it shortly.", ephemeral=True
        )

        log_ch_id = _lcfg(guild.id, "log_channel_id")
        await self._post_log(guild, log_ch_id, "📣 Scam Vouch Appealed", discord.Color.orange(), [
            ("Accused",  f"<@{accused_id}>",  True),
            ("Reporter", f"<@{reporter_id}>", True),
            ("Listing",  listing_id, True),
            ("Appeal Channel", appeal_ch.mention, True),
            ("Timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), False),
        ])

    async def _handle_appeal_approve(self, interaction: discord.Interaction, channel_id: int):
        if not _is_listing_admin(interaction.user, interaction.guild_id):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return

        raw = get_guild_config(interaction.guild_id, f"appeal_{channel_id}")
        if not raw:
            await interaction.response.send_message("Appeal data not found.", ephemeral=True)
            return

        try:
            info = json.loads(raw)
        except Exception:
            await interaction.response.send_message("Corrupt appeal data.", ephemeral=True)
            return

        # Show ephemeral confirm — routed via listing_appeal_approve2_{channel_id}
        view = discord.ui.View(timeout=120)
        view.add_item(discord.ui.Button(
            label="✅ Yes, Remove Scam Vouch & Close",
            style=discord.ButtonStyle.success,
            custom_id=f"listing_appeal_approve2_{channel_id}",
        ))
        view.add_item(discord.ui.Button(
            label="↩️ Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id=f"listing_appeal_approve2_cancel",
            disabled=False,
        ))
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⚠️ Confirm Appeal Approval",
                description=(
                    f"This will:\n"
                    f"• Remove the scam vouch against <@{info['accused_id']}>\n"
                    f"• Notify both parties\n"
                    f"• Close this appeal channel in 5 minutes"
                ),
                color=discord.Color.orange(),
            ),
            view=view,
            ephemeral=True,
        )

    async def _handle_appeal_approve_confirmed(self, interaction: discord.Interaction, channel_id: int):
        if not _is_listing_admin(interaction.user, interaction.guild_id):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return

        raw = get_guild_config(interaction.guild_id, f"appeal_{channel_id}")
        if not raw:
            await interaction.response.edit_message(content="Appeal data not found.", view=None)
            return

        try:
            info = json.loads(raw)
        except Exception:
            await interaction.response.edit_message(content="Corrupt appeal data.", view=None)
            return

        accused_id  = info["accused_id"]
        reporter_id = info["reporter_id"]
        listing_id  = info["listing_id"]
        guild       = interaction.guild

        remove_scam_vouch(reporter_id, accused_id, guild.id)

        accused_m  = guild.get_member(accused_id)  or self.bot.get_user(accused_id)
        reporter_m = guild.get_member(reporter_id) or self.bot.get_user(reporter_id)

        if accused_m:
            try:
                await accused_m.send(embed=discord.Embed(
                    title="✅ Appeal Approved",
                    description="Your appeal was approved. The scam vouch has been removed.",
                    color=discord.Color.green(),
                ))
            except discord.Forbidden:
                pass
        if reporter_m:
            try:
                await reporter_m.send(embed=discord.Embed(
                    title="ℹ️ Scam Vouch Reviewed",
                    description="The scam vouch you filed was reviewed and removed by staff.",
                    color=discord.Color.orange(),
                ))
            except discord.Forbidden:
                pass

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ Appeal Approved",
                description="Scam vouch removed. Parties notified. Channel closing in 5 minutes.",
                color=discord.Color.green(),
            ),
            view=None,
        )

        log_ch_id = _lcfg(guild.id, "log_channel_id")
        await self._post_log(guild, log_ch_id, "✅ Appeal Approved", discord.Color.green(), [
            ("Accused",       f"<@{accused_id}>",     True),
            ("Admin",         interaction.user.mention, True),
            ("Listing",       listing_id, True),
            ("Vouch Removed", "Yes", True),
            ("Timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), False),
        ])

        asyncio.create_task(self._close_channel_delayed(interaction.channel, 300))

    async def _handle_appeal_deny(self, interaction: discord.Interaction, channel_id: int):
        if not _is_listing_admin(interaction.user, interaction.guild_id):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return

        raw = get_guild_config(interaction.guild_id, f"appeal_{channel_id}")
        if not raw:
            await interaction.response.send_message("Appeal data not found.", ephemeral=True)
            return

        try:
            info = json.loads(raw)
        except Exception:
            await interaction.response.send_message("Corrupt appeal data.", ephemeral=True)
            return

        await interaction.response.send_modal(
            AppealDenyModal(self, channel_id, info["accused_id"], info["reporter_id"])
        )

    async def _handle_appeal_deny_submit(self, interaction: discord.Interaction,
                                          channel_id: int, accused_id: int,
                                          reporter_id: int, reason: str):
        guild = interaction.guild
        raw   = get_guild_config(guild.id, f"appeal_{channel_id}")
        info  = json.loads(raw) if raw else {}
        listing_id = info.get("listing_id", "?")

        accused_m = guild.get_member(accused_id) or self.bot.get_user(accused_id)
        if accused_m:
            try:
                await accused_m.send(embed=discord.Embed(
                    title="❌ Appeal Denied",
                    description=f"Your appeal was denied by staff.\n**Reason:** {reason}\nThe scam vouch remains.",
                    color=discord.Color.red(),
                ))
            except discord.Forbidden:
                pass

        await interaction.response.send_message("✅ Appeal denied, accused has been notified.", ephemeral=True)

        log_ch_id = _lcfg(guild.id, "log_channel_id")
        await self._post_log(guild, log_ch_id, "❌ Appeal Denied", discord.Color.red(), [
            ("Accused",  f"<@{accused_id}>",  True),
            ("Admin",    interaction.user.mention, True),
            ("Listing",  listing_id, True),
            ("Reason",   reason, False),
            ("Timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), False),
        ])

        asyncio.create_task(self._close_channel_delayed(interaction.channel, 300))

    # ── Relist ─────────────────────────────────────────────────────────────────

    async def _handle_relist(self, interaction: discord.Interaction, listing_id: str):
        row = get_listing(listing_id)
        if not row or row["seller_id"] != interaction.user.id:
            await interaction.response.send_message("Listing not found or not yours.", ephemeral=True)
            return
        if row["status"] == "active":
            await interaction.response.send_message("This listing is still active.", ephemeral=True)
            return

        data = {
            "type":          row["type"],
            "category":      row["category"],
            "item_name":     row["item_name"],
            "description":   row["description"] or "",
            "quantity":      row["quantity"],
            "buy_now_price": row["buy_now_price"],
            "starting_bid":  row["starting_bid"],
            "min_increment": row["min_increment"],
            "reserve_price": row["reserve_price"],
            "duration_ms":   row["duration_ms"],
            "duration_label": "previous",
        }
        await self._show_preview(interaction, data)

    # ── My listings view ───────────────────────────────────────────────────────

    async def _handle_mylistings_view(self, interaction: discord.Interaction, listing_id: str):
        row = get_listing(listing_id)
        if not row:
            await interaction.response.send_message("Listing not found.", ephemeral=True)
            return
        embed = _build_listing_embed(dict(row), interaction.guild)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _update_live_embed(self, guild: discord.Guild, row,
                                  status: str = None, snipe_note: str = "",
                                  extra_note: str = ""):
        """Fetch the live listing message and update its embed and buttons."""
        listing_id = row["listing_id"] if hasattr(row, "__getitem__") else row.get("listing_id")
        channel_id = row["channel_id"] if hasattr(row, "__getitem__") else row.get("channel_id")
        message_id = row["message_id"] if hasattr(row, "__getitem__") else row.get("message_id")

        if not channel_id or not message_id:
            return

        try:
            channel = guild.get_channel(channel_id) or await guild.fetch_channel(channel_id)
            msg     = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        updated = get_listing(listing_id)
        if not updated:
            return

        data = dict(updated)
        if status:
            data["status"] = status

        embed = _build_listing_embed(data, guild)
        if snipe_note:
            embed.add_field(name="⚡ Anti-Snipe", value=snipe_note, inline=False)
        if extra_note:
            embed.add_field(name="⚠️ Note", value=extra_note, inline=False)

        if data.get("status") == "active":
            view = self._build_live_view(listing_id, data["type"])
        else:
            # Disable all buttons
            view = discord.ui.View(timeout=None)
            status_label = {"sold": "✅ SOLD", "expired": "⏰ EXPIRED", "cancelled": "❌ CANCELLED"}.get(
                data["status"], data["status"].upper()
            )
            view.add_item(discord.ui.Button(
                label=status_label, style=discord.ButtonStyle.secondary, disabled=True,
                custom_id=f"listing_status_{listing_id}",
            ))

        try:
            await msg.edit(embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Could not update listing embed for %s: %s", listing_id, exc)

    async def _post_log(self, guild: discord.Guild, log_ch_id: Optional[str],
                         title: str, color: discord.Color, fields: list):
        if not log_ch_id:
            return
        try:
            ch = guild.get_channel(int(log_ch_id))
            if not ch:
                return
            embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
            for name, value, inline in fields:
                embed.add_field(name=name, value=str(value)[:1024], inline=inline)
            await ch.send(embed=embed)
        except Exception as exc:
            logger.warning("Log post failed: %s", exc)

    async def _close_channel_delayed(self, channel, delay_seconds: int):
        await asyncio.sleep(delay_seconds)
        try:
            await channel.delete(reason="Appeal/transaction closed")
        except (discord.Forbidden, discord.NotFound):
            pass

    @staticmethod
    def _make_temp_id(user_id: int) -> str:
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
        return suffix

    # ── Commands ───────────────────────────────────────────────────────────────

    @app_commands.command(name="listingsetup", description="Post the permanent marketplace panel (Admin only)")
    async def listingsetup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not _is_listing_admin(interaction.user, interaction.guild_id):
            await interaction.followup.send("❌ You need admin permission to run setup.", ephemeral=True)
            return

        panel_embed = discord.Embed(
            title="🏪 Server Marketplace",
            description=(
                "Buy, sell, bid, and trade items with other players safely through the bot.\n\n"
                "Click below to get started."
            ),
            color=discord.Color.gold(),
        )
        panel_embed.set_footer(text="Safe • Moderated • Vouch-tracked")

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="📦 Create Listing",
            style=discord.ButtonStyle.success,
            custom_id="listing_panel_create",
        ))
        view.add_item(discord.ui.Button(
            label="🔍 Browse Listings",
            style=discord.ButtonStyle.primary,
            custom_id="listing_panel_browse",
        ))
        view.add_item(discord.ui.Button(
            label="📋 My Listings",
            style=discord.ButtonStyle.secondary,
            custom_id="listing_panel_my",
        ))

        # Check if a panel already exists; edit it instead of reposting
        existing_id = _lcfg(interaction.guild_id, "panel_message_id")
        existing_ch_id = _lcfg(interaction.guild_id, "channel_id")
        if existing_id and existing_ch_id:
            try:
                ch  = interaction.guild.get_channel(int(existing_ch_id))
                msg = await ch.fetch_message(int(existing_id))
                await msg.edit(embed=panel_embed, view=view)
                await interaction.followup.send("✅ Panel refreshed (edited existing).", ephemeral=True)
                return
            except Exception:
                pass  # Panel gone — repost

        msg = await interaction.channel.send(embed=panel_embed, view=view)
        _set_lcfg(interaction.guild_id, "panel_message_id", str(msg.id))
        _set_lcfg(interaction.guild_id, "channel_id", str(interaction.channel_id))
        await interaction.followup.send("✅ Marketplace panel posted!", ephemeral=True)

    @app_commands.command(name="setign", description="Save your Minecraft IGN for bidding")
    @app_commands.describe(ign="Your in-game name (case-sensitive)")
    async def setign(self, interaction: discord.Interaction, ign: str):
        await interaction.response.defer(ephemeral=True)
        set_user_ign(interaction.user.id, ign.strip())
        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ IGN Registered",
                description=f"IGN **{ign.strip()}** registered to your account.",
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )

    @app_commands.command(name="mylistings", description="View all your active listings")
    async def mylistings(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self._show_my_listings(interaction, followup=True)

    @app_commands.command(name="listinghistory", description="View completed listing history")
    @app_commands.describe(user="User to view history for (admin only)")
    async def listinghistory(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if user and not _is_listing_admin(interaction.user, interaction.guild_id):
            await interaction.followup.send("Only admins can view other users' history.", ephemeral=True)
            return

        target_id = user.id if user else interaction.user.id
        is_admin  = _is_listing_admin(interaction.user, interaction.guild_id)

        rows = get_listing_history(
            interaction.guild_id,
            user_id=None if (is_admin and not user) else target_id,
        )

        if not rows:
            await interaction.followup.send(
                embed=discord.Embed(title="📁 No History", description="No completed listings found.",
                                     color=discord.Color.greyple()),
                ephemeral=True,
            )
            return

        embed = discord.Embed(title="📁 Listing History", color=discord.Color.blurple())
        lines = []
        for r in rows[:20]:
            st   = r["status"].upper()
            date = str(r["created_at"])[:10]
            lines.append(f"**{r['item_name']}** `{r['listing_id']}` — {st} on {date}")
        embed.description = "\n".join(lines)
        if len(rows) > 20:
            embed.set_footer(text=f"Showing 20 of {len(rows)} records")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /listingadmin group ────────────────────────────────────────────────────

    ladmin = app_commands.Group(
        name="listingadmin",
        description="Admin commands for the listings system",
    )

    @ladmin.command(name="setlistingchannel", description="Set the channel where listings are posted")
    @app_commands.describe(channel="The listings channel")
    async def la_setlistingchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        if not _is_listing_admin(interaction.user, interaction.guild_id):
            await interaction.followup.send("Admins only.", ephemeral=True)
            return
        _set_lcfg(interaction.guild_id, "channel_id", str(channel.id))
        await interaction.followup.send(f"✅ Listings channel set to {channel.mention}.", ephemeral=True)

    @ladmin.command(name="setticketcategory", description="Set the category for transaction ticket channels")
    @app_commands.describe(category_id="Discord category ID")
    async def la_setticketcategory(self, interaction: discord.Interaction, category_id: str):
        await interaction.response.defer(ephemeral=True)
        if not _is_listing_admin(interaction.user, interaction.guild_id):
            await interaction.followup.send("Admins only.", ephemeral=True)
            return
        _set_lcfg(interaction.guild_id, "ticket_category_id", category_id.strip())
        await interaction.followup.send(f"✅ Ticket category set to `{category_id.strip()}`.", ephemeral=True)

    @ladmin.command(name="setlogchannel", description="Set the channel for listing event logs")
    @app_commands.describe(channel="The log channel")
    async def la_setlogchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        if not _is_listing_admin(interaction.user, interaction.guild_id):
            await interaction.followup.send("Admins only.", ephemeral=True)
            return
        _set_lcfg(interaction.guild_id, "log_channel_id", str(channel.id))
        await interaction.followup.send(f"✅ Log channel set to {channel.mention}.", ephemeral=True)

    @ladmin.command(name="setappealcategory", description="Set the category for appeal channels")
    @app_commands.describe(category_id="Discord category ID")
    async def la_setappealcategory(self, interaction: discord.Interaction, category_id: str):
        await interaction.response.defer(ephemeral=True)
        if not _is_listing_admin(interaction.user, interaction.guild_id):
            await interaction.followup.send("Admins only.", ephemeral=True)
            return
        _set_lcfg(interaction.guild_id, "appeal_category_id", category_id.strip())
        await interaction.followup.send(f"✅ Appeal category set to `{category_id.strip()}`.", ephemeral=True)

    @ladmin.command(name="setadminrole", description="Set the admin role for the listings system")
    @app_commands.describe(role="The admin role")
    async def la_setadminrole(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        if not is_authorized(interaction.user, interaction.guild, "listingadmin"):
            await interaction.followup.send("Only the bot owner can set the admin role.", ephemeral=True)
            return
        _set_lcfg(interaction.guild_id, "admin_role_id", str(role.id))
        await interaction.followup.send(f"✅ Admin role set to {role.mention}.", ephemeral=True)

    @ladmin.command(name="forceclose", description="Forcefully cancel an active listing")
    @app_commands.describe(listing_id="The listing ID to cancel (e.g. AUC-4821)")
    async def la_forceclose(self, interaction: discord.Interaction, listing_id: str):
        await interaction.response.defer(ephemeral=True)
        if not _is_listing_admin(interaction.user, interaction.guild_id):
            await interaction.followup.send("Admins only.", ephemeral=True)
            return
        row = get_listing(listing_id.upper())
        if not row:
            await interaction.followup.send("Listing not found.", ephemeral=True)
            return
        if row["status"] != "active":
            await interaction.followup.send("Listing is not active.", ephemeral=True)
            return

        update_listing(listing_id.upper(), status="cancelled")
        await self._update_live_embed(interaction.guild, row, status="cancelled")
        await interaction.followup.send(f"✅ Listing `{listing_id.upper()}` force-cancelled.", ephemeral=True)

    @ladmin.command(name="refreshpanel", description="Re-post the marketplace panel if buried or deleted")
    async def la_refreshpanel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not _is_listing_admin(interaction.user, interaction.guild_id):
            await interaction.followup.send("Admins only.", ephemeral=True)
            return
        # Call listingsetup logic inline
        await self.listingsetup.callback(self, interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(ListingsCog(bot))
