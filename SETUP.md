# Discord Bot — Setup Instructions

## Requirements

- Python 3.11+
- pip

---

## 1. Install Dependencies

```bash
pip install -r requirements.txt
```

---

## 2. Configure the Bot

Open `config.json` and fill in every value:

```json
{
  "BOT_TOKEN":                  "your-bot-token-here",
  "OWNER_ID":                   123456789012345678,
  "STAFF_ROLE_ID":              123456789012345678,
  "STAFF_LOGS_CHANNEL_ID":      123456789012345678,
  "STRIKE_LOGS_CHANNEL_ID":     123456789012345678,
  "VOUCH_LOGS_CHANNEL_ID":      123456789012345678,
  "SCAM_VOUCH_LOGS_CHANNEL_ID": 123456789012345678,
  "BUILDER_LOGS_CHANNEL_ID":    123456789012345678,
  "OWNER_REVIEW_CHANNEL_ID":    123456789012345678,
  "STAFF_ROLES": {
    "Jr Helper":                 123456789012345678,
    "Helper":                    123456789012345678,
    ...
  }
}
```

**How to get IDs:** Enable Developer Mode in Discord (Settings → Advanced → Developer Mode),
then right-click any user, role, or channel and select **"Copy ID"**.

---

## 3. Create the Discord Application

1. Go to <https://discord.com/developers/applications>
2. Click **New Application** → give it a name
3. Go to **Bot** → **Add Bot**
4. Copy the **Token** and paste it into `config.json` → `BOT_TOKEN`
5. Enable the following **Privileged Gateway Intents**:
   - **Server Members Intent** ✅ (required for member lookups & role management)
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot` + `applications.commands`
   - Bot Permissions: `Administrator` (recommended) **or** at minimum:
     - Manage Roles, Manage Channels, Kick Members, Ban Members,
       Manage Nicknames, Mute Members, View Audit Log,
       Send Messages, Embed Links, Read Message History
7. Copy the generated URL, open it in your browser, and invite the bot to your server

---

## 4. Bot Role Position

The bot's role **must be above every staff role** it will manage in the server's
role list (Server Settings → Roles → drag the bot's role to the top).

---

## 5. Run the Bot

```bash
python main.py
```

The bot will:
- Create `data/database.db` automatically on first run
- Sync slash commands globally (may take up to **1 hour** to appear everywhere)
- For **instant** command appearance during testing, edit `setup_hook` in `main.py` and replace:
  ```python
  synced = await self.tree.sync()
  ```
  with:
  ```python
  GUILD_ID = YOUR_GUILD_ID_HERE
  synced = await self.tree.sync(guild=discord.Object(id=GUILD_ID))
  ```

Logs are written to both stdout and `logs/bot_YYYYMMDD.log`.

---

## File Structure

```
main.py               ← Bot entry point
config.json           ← All tokens, IDs, and settings
requirements.txt      ← Python dependencies

cogs/
  staff.py            ← /staff addroles, /staff removeroles, /staffinfo
  strikes.py          ← /strike, /removestrike, /checkstrikes
  vouches.py          ← /vouch, /scamvouch, /checkvouches, leaderboards
  builder.py          ← Payment confirm, protection timers, case management
  serverify.py        ← /serverify — auto-sync staff role permissions

utils/
  database.py         ← SQLite schema + all DB helpers
  permissions.py      ← Role hierarchy, permission templates, guards
  scheduler.py        ← Weekly strike reset + 48h timer expiry

data/
  database.db         ← SQLite database (auto-created)

logs/
  bot_YYYYMMDD.log    ← Daily log files (auto-created)
```

---

## Commands Reference

| Command | Who can use | Description |
|---|---|---|
| `/staff addroles` | Staff Manager+ | Add multiple roles to a user |
| `/staff removeroles` | Staff Manager+ | Remove multiple roles from a user |
| `/staffinfo` | Everyone | View a user's staff information |
| `/strike` | Moderator+ | Add one strike to a user |
| `/removestrike` | Sr Moderator+ | Remove one strike from a user |
| `/checkstrikes` | Everyone | View strike history for a user |
| `/vouch` | Everyone | Vouch for a user (one per target) |
| `/scamvouch` | Everyone | Submit a scam report (one per target) |
| `/checkvouches` | Everyone | View vouch record for a user |
| `/leaderboard_vouches` | Everyone | Top-10 most vouched users |
| `/leaderboard_scamvouches` | Everyone | Top-10 most scam-vouched users |
| `/serverify` | Staff Manager+ | Sync staff role permissions to templates |
| `/builder_payment_confirm` | Staff | Confirm and log a builder payment |
| `/builder_history` | Everyone | View payment history for a user |
| `/builder_start_timer` | Builder role | Start a 48h protection timer |
| `/builder_timer_status` | Everyone | Check timer status |
| `/builder_cases` | Everyone | List all builder cases |
| `/builder_case` | Everyone | View detailed case info |
| `/builder_cancel_timer` | Builder (creator) | Cancel pre-confirmation timer |

---

## Automatic Tasks

| Task | Schedule | Description |
|---|---|---|
| Strike Reset | Every Monday 08:00 UTC+8 | Resets ALL user strikes to 0, posts to strike logs |
| Builder Timer | Every 60 seconds | Checks for expired 48h cases, fires owner review embed |

Both survive bot restarts — they re-derive their schedule from the current clock on startup.

---

## Troubleshooting

**Slash commands not showing up** — wait up to 1 hour for global sync, or use guild-specific sync (see step 5).

**"Missing Permissions" errors** — ensure the bot's role is above the roles it manages, and that the bot has the permissions listed in step 3.

**Database errors on startup** — ensure the `data/` directory exists (created automatically) and the bot has write access.

**Strike reset not firing** — check the `logs/` directory. The scheduler logs the next reset time on startup. Ensure the bot stays running over the reset window.
