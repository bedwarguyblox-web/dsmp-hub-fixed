"""
verify.py — Button-based member verification with self-service role selection.

Flow:
  1. New member joins → bot sends a welcome message in the verify channel
     containing a persistent "🔐 Click to Verify" button (public, visible to all).
  2. Member clicks → bot responds with an EPHEMERAL (only-they-can-see) panel
     showing toggle buttons for every self-assignable role.
  3. They click roles to select/deselect (green = selected, grey = deselected).
  4. They click "✅ Confirm" → roles are permanently assigned, panel updates
     to a "Welcome!" confirmation they can dismiss.

Admin commands (require Admin or above):
  /verify setup channel: role:        — set verify channel + base verified role
  /verify addrole role: [label:]      — add a role to the in-panel picker
  /verify removerole role:            — remove a role from the picker
  /verify view                        — show current settings
  /verify resend                      — re-post the verify panel in the channel
"""

import json
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from utils.database import get_guild_config, set_guild_config
from utils.permissions import is_authorized, CONFIG

logger = logging.getLogger(__name__)

_ROLES_KEY = "verify_roles"   # guild_config key — stores JSON list of {role_id, label}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_selectable(guild_id: int) -> list[dict]:
    """
    Return the list of self-assignable role dicts for this guild.
    Falls back to VERIFY_ROLES in config.json when the DB has nothing set,
    so the picker works out of the box without needing /verify addrole.
    """
    raw = get_guild_config(guild_id, _ROLES_KEY)
    if raw:
        try:
            roles = json.loads(raw)
            if roles:
                return roles
        except (json.JSONDecodeError, TypeError):
            pass
    # Fall back to hardcoded defaults in config.json
    return list(CONFIG.get("VERIFY_ROLES", []))


def _save_selectable(guild_id: int, entries: list[dict]):
    set_guild_config(guild_id, _ROLES_KEY, json.dumps(entries))


# ── Role toggle button ────────────────────────────────────────────────────────

class RoleToggleButton(discord.ui.Button):
    """A single togglable role button inside the ephemeral picker."""

    def __init__(self, role_id: int, label: str, row: int):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            custom_id=f"vrole_{role_id}",
            row=row,
        )
        self.role_id    = role_id
        self.base_label = label
        self.selected   = False

    async def callback(self, interaction: discord.Interaction):
        self.selected = not self.selected
        if self.selected:
            self.style = discord.ButtonStyle.success
            self.label = f"✅  {self.base_label}"
        else:
            self.style = discord.ButtonStyle.secondary
            self.label = self.base_label
        await interaction.response.edit_message(view=self.view)


# ── Ephemeral role-selection view ─────────────────────────────────────────────

class RoleSelectionView(discord.ui.View):
    """Shown ephemerally to the verifying member. Not persistent."""

    def __init__(self, selectable: list[dict], base_role_id: int | None):
        super().__init__(timeout=300)   # 5-minute window to pick roles
        self.base_role_id = base_role_id

        # Add toggle buttons for up to 20 roles (rows 0-3, 5 per row)
        for i, entry in enumerate(selectable[:20]):
            self.add_item(
                RoleToggleButton(
                    role_id=entry["role_id"],
                    label=entry["label"],
                    row=i // 5,
                )
            )

        # Confirm button always on row 4
        confirm = discord.ui.Button(
            label="✅  Confirm",
            style=discord.ButtonStyle.primary,
            custom_id="verify_confirm",
            row=4,
        )
        confirm.callback = self._on_confirm
        self.add_item(confirm)

    async def _on_confirm(self, interaction: discord.Interaction):
        guild = interaction.guild

        # Collect selected + base roles
        to_add: list[discord.Role] = []

        if self.base_role_id:
            base = guild.get_role(self.base_role_id)
            if base and base not in interaction.user.roles:
                to_add.append(base)

        for item in self.children:
            if isinstance(item, RoleToggleButton) and item.selected:
                role = guild.get_role(item.role_id)
                if role and role not in interaction.user.roles:
                    to_add.append(role)

        if not to_add:
            # Already has everything — just confirm
            embed = discord.Embed(
                title="✅ You're already set!",
                description="You already have all the roles you selected. You're good to go!",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)
            return

        try:
            await interaction.user.add_roles(*to_add, reason="Verification — self-selected roles")
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to assign those roles. Please contact a moderator.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            logger.warning("HTTPException assigning verify roles to %s: %s", interaction.user.id, exc)
            await interaction.response.send_message(
                "⚠️ Something went wrong assigning your roles. Please try again or contact a moderator.",
                ephemeral=True,
            )
            return

        role_names = ", ".join(f"**{r.name}**" for r in to_add)
        embed = discord.Embed(
            title="🎉 Welcome to the server!",
            description=(
                f"You're now verified, {interaction.user.mention}!\n\n"
                f"**Roles granted:** {role_names}\n\n"
                f"You can dismiss this message — enjoy your stay!"
            ),
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(embed=embed, view=self)
        logger.info(
            "Verified %s (%d) in guild %s — roles: %s",
            interaction.user, interaction.user.id,
            guild.name, [r.id for r in to_add],
        )

    async def on_timeout(self):
        # Silently expire — ephemeral message will just stop responding
        pass


# ── Persistent "Click to Verify" button ───────────────────────────────────────

class VerifyPromptView(discord.ui.View):
    """
    Single-button view attached to the public welcome message.
    Registered persistently so it survives bot restarts.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🔐  Click to Verify",
        style=discord.ButtonStyle.success,
        custom_id="verify:start",
    )
    async def start_verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        gid          = interaction.guild_id
        role_id_str  = get_guild_config(gid, "verify_role_id")
        base_role_id = int(role_id_str) if role_id_str else None

        selectable = _load_selectable(gid)

        # Already verified? Only short-circuit when there is no role picker to show.
        # If selectable roles are configured, always show the picker so the user
        # can pick additional roles even after their initial verification.
        if base_role_id and not selectable:
            base_role = interaction.guild.get_role(base_role_id)
            if base_role and base_role in interaction.user.roles:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="✅ Already Verified",
                        description="You're already verified and have access to the server!",
                        color=discord.Color.green(),
                    ),
                    ephemeral=True,
                )
                return

        if not selectable:
            # No role picker configured — just grant the base verified role
            if base_role_id:
                base_role = interaction.guild.get_role(base_role_id)
                if base_role:
                    try:
                        await interaction.user.add_roles(base_role, reason="Verification")
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        logger.warning("Could not assign verified role to %s: %s", interaction.user.id, exc)
                        await interaction.response.send_message(
                            "❌ I couldn't assign your role. Please contact a moderator.",
                            ephemeral=True,
                        )
                        return
                    await interaction.response.send_message(
                        embed=discord.Embed(
                            title="🎉 You're verified!",
                            description=(
                                f"Welcome to the server, {interaction.user.mention}!\n"
                                f"You now have the **{base_role.name}** role."
                            ),
                            color=discord.Color.green(),
                            timestamp=datetime.now(timezone.utc),
                        ),
                        ephemeral=True,
                    )
                    logger.info("Verified (no picker) %s (%d) in guild %s", interaction.user, interaction.user.id, gid)
                    return
            await interaction.response.send_message(
                "⚠️ Verification isn't fully configured yet. Please contact a moderator.",
                ephemeral=True,
            )
            return

        # Build and show the role-selection panel (ephemeral)
        lines = [f"• **{e['label']}**" for e in selectable[:20]]
        embed = discord.Embed(
            title="🎭 Choose Your Roles",
            description=(
                "Select the roles you want below, then click **✅ Confirm**.\n"
                "You can pick as many as you like.\n\n"
                + "\n".join(lines)
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="Only you can see this panel • Expires in 5 minutes")

        view = RoleSelectionView(selectable, base_role_id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ── Cog ───────────────────────────────────────────────────────────────────────

class VerifyCog(commands.Cog, name="Verify"):
    """Button-based member verification with ephemeral role selection."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(VerifyPromptView())
        logger.info("VerifyPromptView registered (persistent).")

    # ── /verify group ─────────────────────────────────────────────────────────
    verify_group = app_commands.Group(
        name="verify",
        description="Verification system — setup and management",
    )

    # ── /verify setup ─────────────────────────────────────────────────────────
    @verify_group.command(
        name="setup",
        description="Set the verify channel and the base role granted on verification",
    )
    @app_commands.describe(
        channel="Channel where the verify prompt lives",
        role="Base role automatically granted to everyone who verifies",
    )
    async def verify_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        role: discord.Role,
    ):
        await interaction.response.defer(ephemeral=True)

        if not is_authorized(interaction.user, interaction.guild, "verify"):
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Permission Denied", description="You must be **Admin** or above.", color=discord.Color.red()),
                ephemeral=True,
            )
            return

        gid = interaction.guild.id
        set_guild_config(gid, "verify_channel_id", str(channel.id))
        set_guild_config(gid, "verify_role_id",    str(role.id))

        embed = discord.Embed(
            title="✅ Verification Configured",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Verify Channel", value=channel.mention, inline=True)
        embed.add_field(name="Base Role",      value=role.mention,    inline=True)
        embed.add_field(
            name="Next steps",
            value=(
                f"• Use `/verify addrole` to add self-assignable roles to the picker\n"
                f"• Use `/verify resend` to post the verify panel in {channel.mention}\n"
                f"• Members who join will see the panel automatically"
            ),
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info("Verify set up in guild %s: channel=%s role=%s", gid, channel.id, role.id)

    # ── /verify addrole ───────────────────────────────────────────────────────
    @verify_group.command(
        name="addrole",
        description="Add a role to the verification role-picker buttons",
    )
    @app_commands.describe(
        role="The role to add as a selectable option",
        label="Button label (defaults to the role name)",
    )
    async def verify_addrole(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        label: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if not is_authorized(interaction.user, interaction.guild, "verify"):
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Permission Denied", description="You must be **Admin** or above.", color=discord.Color.red()),
                ephemeral=True,
            )
            return

        gid     = interaction.guild.id
        entries = _load_selectable(gid)
        btn_label = (label or role.name)[:80]

        if any(e["role_id"] == role.id for e in entries):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Already Added",
                    description=f"{role.mention} is already in the verification picker.",
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return

        if len(entries) >= 20:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Limit Reached",
                    description="The picker supports up to **20 roles**. Remove one before adding another.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        entries.append({"role_id": role.id, "label": btn_label})
        _save_selectable(gid, entries)

        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Role Added",
                description=f"{role.mention} will now appear as **\"{btn_label}\"** in the verify picker.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            ),
            ephemeral=True,
        )
        logger.info("Verify picker: added role %s ('%s') to guild %s", role.id, btn_label, gid)

    # ── /verify removerole ────────────────────────────────────────────────────
    @verify_group.command(
        name="removerole",
        description="Remove a role from the verification role-picker",
    )
    @app_commands.describe(role="The role to remove from the picker")
    async def verify_removerole(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
    ):
        await interaction.response.defer(ephemeral=True)

        if not is_authorized(interaction.user, interaction.guild, "verify"):
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Permission Denied", description="You must be **Admin** or above.", color=discord.Color.red()),
                ephemeral=True,
            )
            return

        gid      = interaction.guild.id
        entries  = _load_selectable(gid)
        filtered = [e for e in entries if e["role_id"] != role.id]

        if len(filtered) == len(entries):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Not Found",
                    description=f"{role.mention} wasn't in the verification picker.",
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return

        _save_selectable(gid, filtered)
        await interaction.followup.send(
            embed=discord.Embed(
                title="🗑️ Role Removed",
                description=f"{role.mention} has been removed from the verify picker.",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            ),
            ephemeral=True,
        )
        logger.info("Verify picker: removed role %s from guild %s", role.id, gid)

    # ── /verify view ──────────────────────────────────────────────────────────
    @verify_group.command(
        name="view",
        description="Show the current verification settings and role picker",
    )
    async def verify_view(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        gid     = interaction.guild.id
        ch_id   = get_guild_config(gid, "verify_channel_id")
        role_id = get_guild_config(gid, "verify_role_id")

        ch   = interaction.guild.get_channel(int(ch_id))   if ch_id   else None
        role = interaction.guild.get_role(int(role_id))     if role_id else None

        embed = discord.Embed(
            title="⚙️ Verification Settings",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Verify Channel",
            value=ch.mention if ch else ("Not set" if not ch_id else f"Unknown (ID `{ch_id}`)"),
            inline=True,
        )
        embed.add_field(
            name="Base Verified Role",
            value=role.mention if role else ("Not set" if not role_id else f"Unknown (ID `{role_id}`)"),
            inline=True,
        )

        entries = _load_selectable(gid)
        if entries:
            lines = [f"{i+1}. **{e['label']}** — <@&{e['role_id']}>" for i, e in enumerate(entries)]
            embed.add_field(
                name=f"Self-Assignable Roles in Picker ({len(entries)}/20)",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="Self-Assignable Roles in Picker",
                value="None configured. Use `/verify addrole` to add roles.",
                inline=False,
            )

        embed.set_footer(text="Use /verify addrole and /verify removerole to manage the picker")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /verify resend ────────────────────────────────────────────────────────
    @verify_group.command(
        name="resend",
        description="Re-post the verify panel in the configured channel",
    )
    async def verify_resend(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not is_authorized(interaction.user, interaction.guild, "verify"):
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Permission Denied", description="You must be **Admin** or above.", color=discord.Color.red()),
                ephemeral=True,
            )
            return

        gid   = interaction.guild.id
        ch_id = get_guild_config(gid, "verify_channel_id")
        if not ch_id:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Not Configured",
                    description="Run `/verify setup` first to set a verify channel.",
                    color=discord.Color.yellow(),
                ),
                ephemeral=True,
            )
            return

        ch = interaction.guild.get_channel(int(ch_id))
        if not ch:
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Channel Not Found", description=f"Channel ID `{ch_id}` not found.", color=discord.Color.red()),
                ephemeral=True,
            )
            return

        await _send_verify_panel(ch)
        await interaction.followup.send(
            embed=discord.Embed(
                description=f"✅ Verify panel sent to {ch.mention}.",
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )

    # ── on_member_join ────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return

        gid   = member.guild.id
        ch_id = get_guild_config(gid, "verify_channel_id")
        if not ch_id:
            return

        channel = member.guild.get_channel(int(ch_id))
        if not channel:
            return

        embed = discord.Embed(
            title=f"👋 Welcome, {member.display_name}!",
            description=(
                f"Hey {member.mention}! Welcome to **{member.guild.name}**. 🎉\n\n"
                f"Click the button below to verify and pick your roles.\n"
                f"It only takes a second!"
            ),
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"User ID: {member.id}")

        try:
            await channel.send(
                content=member.mention,
                embed=embed,
                view=VerifyPromptView(),
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            logger.info("Verify prompt sent to %s (%d) in guild %s", member, member.id, gid)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Could not send verify prompt in guild %s: %s", gid, exc)


# ── Standalone helper (used by /verify resend) ────────────────────────────────

async def _send_verify_panel(channel: discord.TextChannel):
    """Post a standalone verify panel (not tied to a specific user joining)."""
    embed = discord.Embed(
        title="🔐 Verify to Access the Server",
        description=(
            "Click the button below to verify your account and choose your roles.\n\n"
            "The process is quick — you'll be done in seconds!"
        ),
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    await channel.send(embed=embed, view=VerifyPromptView())


async def setup(bot: commands.Bot):
    await bot.add_cog(VerifyCog(bot))
