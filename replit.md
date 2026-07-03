# Staff Bot Dashboard

A Discord staff management bot with a web dashboard. The bot handles strikes, vouches, builder protection timers, and staff hierarchy — with live stats visible in the dashboard.

## ⚠️ DO NOT RUN THE BOT HERE

The Discord bot (`main.py`) is hosted on an **external dedicated bot host** — do NOT start it from Replit. Running it here alongside the external host will cause:
- Duplicate slash command responses
- Split database state (two separate `database.db` files)
- Race conditions on all write operations (partnerships, strikes, vouches)

**Only the API server and dashboard run on Replit.**

## Run & Operate

- **Discord Bot** — runs on external host only, NOT here
- **API Server** — workflow runs automatically (port 8080, routes under `/api`)
- **Dashboard** — workflow runs automatically (port 23183, preview at `/`)
- `pnpm --filter @workspace/api-server run typecheck` — typecheck API server
- `pnpm --filter @workspace/dashboard run typecheck` — typecheck dashboard
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks from OpenAPI spec

## Invite the Bot

**Bot invite link (already has all required permissions):**

```
https://discord.com/oauth2/authorize?client_id=1463178163331797147&scope=bot+applications.commands&permissions=268651536
```

Permissions granted: View Channels, Send Messages, Embed Links, Read Message History, Mention Everyone, Manage Roles, Manage Channels.

**After inviting:**
1. Right-click the bot in your server → "Grant Administrator" OR ensure the permissions above are correct
2. The bot will auto-sync all slash commands on startup (may take up to 1 hour to appear globally)
3. To force-sync immediately: DM or mention the bot — slash commands appear in your server's command list

## Stack

- **Bot**: Python 3, discord.py 2.x, aiohttp, apscheduler, SQLite (data/database.db)
- **API Server**: Express 5, TypeScript, better-sqlite3 (reads bot's SQLite directly)
- **Dashboard**: React + Vite, Tailwind CSS v4, React Query, Wouter
- **Monorepo**: pnpm workspaces, Node.js 24, TypeScript 5.9

## Where things live

```
main.py                  — Bot entry point, health server on PORT=5000
config.json              — Bot token, server IDs, role IDs
cogs/
  staff.py               — /staffinfo, /promote, /demote
  strikes.py             — /strike, /removestrike, /checkstrikes
  vouches.py             — /vouch, /scamvouch, /checkvouches
  builder.py             — /buildercase, /startbuildertimer, /completebuildertimer
  serverify.py           — /serverify (role permission scanner)
utils/
  database.py            — SQLite schema + all query helpers
  permissions.py         — Role hierarchy + permission templates
  scheduler.py           — Weekly strike reset + builder timer expiry
data/
  database.db            — Live SQLite database (created on first bot run)
artifacts/api-server/    — Express API reading data/database.db via better-sqlite3
artifacts/dashboard/     — React dashboard (5 pages: Overview, Vouches, Strikes, Builder, Activity)
lib/api-spec/openapi.yaml — Single source of truth for API contract
```

## Architecture decisions

- Bot uses SQLite (not Postgres) for simplicity and zero-dependency hosting
- API server reads the same SQLite file directly (readonly) — no separate DB sync needed
- All slash commands are global (not guild-only) — slower first sync, no per-server re-invite needed
- Bot token lives in config.json (not env var) — change to env var before open-sourcing
- Dashboard auto-refreshes every 30s via React Query's refetchInterval

## Product

Staff can use slash commands in Discord to:
- **Strikes**: Add/remove strikes on members, view history, weekly auto-reset
- **Vouches**: Log vouches and scam-vouches for community trust scoring
- **Builder**: Open protection timer cases (48h), auto-ping owner on expiry
- **Staff Info**: View any member's rank and permissions in the hierarchy
- **Serverify**: Audit and fix role permission overwrites across the server

The web dashboard shows all this data in read-only tables, leaderboards, and activity feeds.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

- **DATA RESETS ON RESTART?** — `data/database.db` is gitignored, so any bot host that does a `git pull` on restart will wipe the file. Fix: set the `DATABASE_PATH` environment variable on your bot host to a path on a **persistent volume** that survives restarts:
  ```
  DATABASE_PATH=/data/database.db          # Railway volume at /data
  DATABASE_PATH=/home/container/db.sqlite  # Pterodactyl persistent folder
  ```
  The bot logs `Database path: <path>` on startup — check it to confirm the right file is being used.
- Bot must be **running** before the API server returns real data (it creates the SQLite DB on first start)
- Slash commands take up to **1 hour** to appear globally after first sync — this is a Discord limitation
- `better-sqlite3` requires the `onlyBuiltDependencies` entry in `pnpm-workspace.yaml` to build its native module
- The bot's PORT=5000 health server and the API server's PORT=8080 must not conflict
- Weekly strike reset fires every Monday 08:00 UTC+8 — driven by `utils/scheduler.py` (was incorrectly set to bi-weekly, now fixed to every Monday)

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
- OpenAPI spec → `lib/api-spec/openapi.yaml`; run `pnpm --filter @workspace/api-spec run codegen` after any change
