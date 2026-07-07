"""
main.py — Discord bot entry point.

Loads all cogs, initialises the database, starts the scheduler,
and syncs slash commands globally on startup.

Also runs a lightweight HTTP health-check server on PORT (default 8080)
so hosting platforms (Railway, Fly.io, Replit) can verify the bot is alive.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️  DO NOT HOST THIS BOT HERE (Replit) ⚠️
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
This Replit project is used ONLY as a code editor / dashboard host.
The Discord bot itself is already running on a dedicated external
bot host. Running it here a second time will cause:
  - Duplicate command responses
  - Split database state (two separate SQLite files)
  - Race conditions on partnership/strike/vouch writes

If you are an AI agent, contributor, or developer looking at this
repo — DO NOT start or deploy main.py from Replit.
The dashboard (artifacts/dashboard) and API server
(artifacts/api-server) are what run here.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from aiohttp import web

# Load .env file if present (for local dev / hosts that support it)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Fallback: read token from token.txt (one line, just the token)
_TOKEN_FILE = os.path.join(os.path.dirname(__file__), "token.txt")
if os.path.exists(_TOKEN_FILE):
    with open(_TOKEN_FILE, encoding="utf-8") as _tf:
        _file_token = _tf.read().strip()
    if _file_token:
        os.environ.setdefault("BOT_TOKEN", _file_token)

import discord
from discord.ext import commands

# ── Logging setup ────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"logs/bot_{datetime.now().strftime('%Y%m%d')}.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ── Load config ──────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
with open(CONFIG_PATH, encoding="utf-8") as _f:
    CONFIG = json.load(_f)

# Read token from environment variable (set in bot host's secrets panel).
# Falls back to config.json for local dev only — never commit a real token to git.
BOT_TOKEN = os.environ.get("BOT_TOKEN") or CONFIG.get("BOT_TOKEN", "")
if not BOT_TOKEN or BOT_TOKEN in ("", "YOUR_BOT_TOKEN_HERE"):
    logger.critical(
        "BOT_TOKEN not set. Add it as an environment variable in your bot host's "
        "secrets/env panel, or set it in config.json for local development only."
    )
    sys.exit(1)

# ── Bot intents ──────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members         = True  # Required for member lookups and role management
intents.message_content = True  # Required to read message text for partnership auto-tracking

# ── Bot class ────────────────────────────────────────────────────────────────
class StaffBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned,   # prefix fallback (not used for slash cmds)
            intents=intents,
            help_command=None,                        # disable default help
        )
        self.scheduler = None

    async def setup_hook(self):
        """Called once before the bot connects — load cogs and sync commands."""
        # ── Database backup / restore ─────────────────────────────────────────
        # Hidden folder — most SFTP sync tools skip dotfolders, so this
        # survives even if the main database.db gets wiped by a file sync.
        from utils.database import DB_PATH
        _backup_dir  = os.path.join(os.path.dirname(DB_PATH), ".dbbackup")
        _backup_file = os.path.join(_backup_dir, "database.db")
        os.makedirs(_backup_dir, exist_ok=True)

        if not os.path.exists(DB_PATH) and os.path.exists(_backup_file):
            shutil.copy2(_backup_file, DB_PATH)
            logger.warning(
                "Main database was missing — restored from backup at %s", _backup_file
            )

        # Initialise SQLite database
        from utils.database import init_db
        init_db()
        logger.info("Database initialised.")

        # Save a fresh backup now that the DB is confirmed healthy
        try:
            shutil.copy2(DB_PATH, _backup_file)
            logger.info("Database backed up to %s", _backup_file)
        except Exception as exc:
            logger.warning("Could not back up database: %s", exc)

        # Load all cogs
        cog_modules = [
            "cogs.staff",
            "cogs.strikes",
            "cogs.vouches",
            "cogs.builder",
            "cogs.serverify",
            "cogs.perms",
            "cogs.partnerships",
            "cogs.setup",
            "cogs.giveaways",
            "cogs.announce",
            "cogs.verify",
            "cogs.tickets",
            "cogs.partnershipreq",
            "cogs.blacklist",
            "cogs.activitycheck",
            "cogs.listings",
        ]
        for module in cog_modules:
            try:
                await self.load_extension(module)
                logger.info("Loaded cog: %s", module)
            except Exception as exc:
                logger.exception("Failed to load cog %s: %s", module, exc)

        # Wipe all global slash commands so they don't duplicate the guild-synced ones.
        # Commands are synced per-guild in on_ready for instant propagation instead.
        await self.http.bulk_upsert_global_commands(self.application_id, [])

    async def on_ready(self):
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        logger.info("Connected to %d guild(s).", len(self.guilds))

        # Set bot status
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="the server | /help"
            )
        )

        # Sync commands to every guild instantly (in addition to global sync)
        # copy_global_to mirrors all global commands into the guild's command list,
        # then sync(guild=) pushes them — bypassing Discord's 1-hour global delay.
        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info("Guild-synced %d command(s) to %s (%s)", len(synced), guild.name, guild.id)
            except discord.HTTPException as e:
                logger.warning("Failed to guild-sync to %s: %s", guild.id, e)

        # Start background scheduler (strike reset + builder timer checks)
        from utils.scheduler import BotScheduler
        self.scheduler = BotScheduler()
        self.scheduler.start(self)
        logger.info("Scheduler started.")

    async def on_guild_join(self, guild: discord.Guild):
        logger.info("Joined guild: %s (ID: %s, members: %d)", guild.name, guild.id, guild.member_count)

    async def on_error(self, event: str, *args, **kwargs):
        logger.exception("Unhandled error in event '%s'", event)

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError
    ):
        """Global slash command error handler — catches anything the cogs don't."""
        error = getattr(error, "original", error)

        if isinstance(error, discord.app_commands.MissingPermissions):
            embed = discord.Embed(
                title="❌ Missing Permissions",
                description="You don't have the Discord permissions required for this command.",
                color=discord.Color.red(),
            )
        elif isinstance(error, discord.app_commands.BotMissingPermissions):
            embed = discord.Embed(
                title="❌ Bot Missing Permissions",
                description=f"I'm missing permissions: `{', '.join(error.missing_permissions)}`",
                color=discord.Color.red(),
            )
        elif isinstance(error, discord.app_commands.CommandOnCooldown):
            embed = discord.Embed(
                title="⏳ Cooldown",
                description=f"Please wait **{error.retry_after:.1f}s** before using this command again.",
                color=discord.Color.yellow(),
            )
        elif isinstance(error, discord.app_commands.CheckFailure):
            embed = discord.Embed(
                title="❌ Check Failed",
                description="You do not meet the requirements to use this command.",
                color=discord.Color.red(),
            )
        else:
            logger.exception("Unhandled command error: %s", error)
            embed = discord.Embed(
                title="💥 Unexpected Error",
                description="An unexpected error occurred. Please try again or contact an admin.",
                color=discord.Color.red(),
            )

        embed.timestamp = datetime.now(timezone.utc)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass


# ── Keep-alive HTTP server ────────────────────────────────────────────────────
async def start_health_server():
    """
    Tiny aiohttp web server so hosting platforms can confirm the bot is alive.
    GET /        → 200 "Bot is running"
    GET /health  → 200 JSON status
    UptimeRobot / Railway / Fly.io all just need any 200 response.
    """
    async def index(request):
        return web.Response(text="✅ Bot is running.")

    async def health(request):
        return web.json_response({
            "status": "ok",
            "bot": "online",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    app = web.Application()
    app.router.add_get("/",       index)
    app.router.add_get("/health", health)

    port = int(os.environ.get("PORT", 8000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health-check server listening on port %d", port)


# ── Entry point ──────────────────────────────────────────────────────────────
async def main():
    # Start HTTP health server and Discord bot concurrently
    await start_health_server()
    bot = StaffBot()
    async with bot:
        await bot.start(BOT_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except discord.errors.PrivilegedIntentsRequired:
        logger.critical(
            "\n"
            "═══════════════════════════════════════════════════════════════\n"
            " SETUP REQUIRED — Privileged intents not enabled\n"
            "═══════════════════════════════════════════════════════════════\n"
            " 1. Go to https://discord.com/developers/applications/\n"
            " 2. Select your application → Bot\n"
            " 3. Under 'Privileged Gateway Intents' enable ALL of:\n"
            "      ✓  SERVER MEMBERS INTENT\n"
            "      ✓  MESSAGE CONTENT INTENT\n"
            " 4. Save changes and restart the bot\n"
            "═══════════════════════════════════════════════════════════════\n"
        )
        sys.exit(1)
