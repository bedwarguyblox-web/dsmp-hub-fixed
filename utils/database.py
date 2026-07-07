"""
database.py — SQLite database initialization and helper functions.
All tables are created here; other modules import and call these helpers.
"""

import sqlite3
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Database path ─────────────────────────────────────────────────────────────
# Priority: DATABASE_PATH env var → default data/database.db beside project root
#
# ⚠️  IMPORTANT FOR BOT HOSTS THAT RESET ON RESTART (Railway, Heroku, etc.):
# Set the DATABASE_PATH environment variable to a path on a PERSISTENT VOLUME
# that survives restarts and git pulls, e.g.:
#
#   DATABASE_PATH=/data/database.db          (Railway volume mounted at /data)
#   DATABASE_PATH=/home/container/db.sqlite  (Pterodactyl persistent folder)
#
# If you do NOT set this, the bot uses data/database.db inside the project
# folder. Since that file is gitignored, any host that does a fresh git pull
# on restart will wipe your data.
_DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "database.db")
DB_PATH = os.environ.get("DATABASE_PATH", _DEFAULT_DB)

# ── Connection pool / singleton ───────────────────────────────────────────────
_connection: sqlite3.Connection | None = None
_tables_initialized: bool = False


def get_connection() -> sqlite3.Connection:
    """Return a thread-safe SQLite connection with row_factory set (singleton)."""
    global _connection
    if _connection is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _connection = sqlite3.connect(DB_PATH, check_same_thread=False)
        _connection.row_factory = sqlite3.Row
        _connection.execute("PRAGMA journal_mode=WAL")
        _connection.execute("PRAGMA foreign_keys=ON")
    return _connection


def init_db() -> None:
    """Create all tables if they do not already exist."""
    global _tables_initialized
    if _tables_initialized:
        logger.info("Database already initialized, skipping.")
        return

    logger.info("Database path: %s", DB_PATH)
    with get_connection() as conn:
        c = conn.cursor()

        # ── Staff action audit log ──────────────────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS staff_actions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT    NOT NULL,
                actor_id    INTEGER NOT NULL,
                target_id   INTEGER,
                details     TEXT,
                guild_id    INTEGER NOT NULL,
                timestamp   TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # ── Active strike count (one row per user per guild) ────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS strikes (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL,
                guild_id  INTEGER NOT NULL,
                count     INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_id, guild_id)
            )
        """)

        # ── Individual strike history records ───────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS strike_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                guild_id    INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason      TEXT    NOT NULL,
                action      TEXT    NOT NULL DEFAULT 'add',
                timestamp   TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # ── Vouch records ───────────────────────────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS vouches (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_id  INTEGER NOT NULL,
                target_id   INTEGER NOT NULL,
                guild_id    INTEGER NOT NULL,
                proof       TEXT    NOT NULL,
                timestamp   TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(voucher_id, target_id, guild_id)
            )
        """)

        # ── Scam-vouch records ──────────────────────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS scam_vouches (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_id  INTEGER NOT NULL,
                target_id   INTEGER NOT NULL,
                guild_id    INTEGER NOT NULL,
                proof       TEXT    NOT NULL,
                timestamp   TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE(voucher_id, target_id, guild_id)
            )
        """)

        # ── Builder payment records ─────────────────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS builder_payments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id TEXT    NOT NULL UNIQUE,
                staff_id   INTEGER NOT NULL,
                guild_id   INTEGER NOT NULL,
                ign        TEXT    NOT NULL,
                amount     TEXT    NOT NULL,
                timestamp  TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # ── Builder protection timer cases ──────────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS builder_cases (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id     TEXT    NOT NULL UNIQUE,
                builder_id  INTEGER NOT NULL,
                customer_id INTEGER NOT NULL,
                guild_id    INTEGER NOT NULL,
                ign         TEXT    NOT NULL,
                amount      TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'pending_confirmation',
                start_time  TEXT,
                end_time    TEXT,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # ── Builder timer logs (per-case event log) ─────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS builder_timers (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id   TEXT    NOT NULL,
                event     TEXT    NOT NULL,
                actor_id  INTEGER,
                note      TEXT,
                timestamp TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # ── Serverify audit log ─────────────────────────────────────────────
        c.execute("""
            CREATE TABLE IF NOT EXISTS serverify_logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id        INTEGER NOT NULL,
                guild_id        INTEGER NOT NULL,
                roles_scanned   INTEGER NOT NULL DEFAULT 0,
                roles_modified  INTEGER NOT NULL DEFAULT 0,
                perms_added     INTEGER NOT NULL DEFAULT 0,
                perms_removed   INTEGER NOT NULL DEFAULT 0,
                details         TEXT,
                timestamp       TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)

        conn.commit()
    _tables_initialized = True
    logger.info("Database initialised at %s", DB_PATH)


# ── Strike helpers ──────────────────────────────────────────────────────────

def get_strike_count(user_id: int, guild_id: int) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT count FROM strikes WHERE user_id=? AND guild_id=?",
            (user_id, guild_id)
        ).fetchone()
    return row["count"] if row else 0


def add_strike(user_id: int, guild_id: int, moderator_id: int, reason: str) -> int:
    """Add one strike and return the new total count."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO strikes (user_id, guild_id, count)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, guild_id) DO UPDATE SET count = count + 1
        """, (user_id, guild_id))
        conn.execute("""
            INSERT INTO strike_history (user_id, guild_id, moderator_id, reason, action)
            VALUES (?, ?, ?, ?, 'add')
        """, (user_id, guild_id, moderator_id, reason))
        conn.commit()
        row = conn.execute(
            "SELECT count FROM strikes WHERE user_id=? AND guild_id=?",
            (user_id, guild_id)
        ).fetchone()
    return row["count"] if row else 1


def remove_strike(user_id: int, guild_id: int, moderator_id: int) -> int:
    """Remove one strike (min 0) and return the new total count."""
    with get_connection() as conn:
        # Get current count and update atomically in same transaction
        row = conn.execute(
            "SELECT count FROM strikes WHERE user_id=? AND guild_id=?",
            (user_id, guild_id)
        ).fetchone()
        current = row["count"] if row else 0

        if current <= 0:
            return 0

        conn.execute(
            "UPDATE strikes SET count = MAX(0, count - 1) WHERE user_id=? AND guild_id=?",
            (user_id, guild_id)
        )
        conn.execute("""
            INSERT INTO strike_history (user_id, guild_id, moderator_id, reason, action)
            VALUES (?, ?, ?, 'Strike removed by moderator', 'remove')
        """, (user_id, guild_id, moderator_id))
        conn.commit()
    return max(0, current - 1)


def get_strike_history(user_id: int, guild_id: int, limit: int = 10):
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM strike_history
            WHERE user_id=? AND guild_id=? AND action='add'
            ORDER BY timestamp DESC
            LIMIT ?
        """, (user_id, guild_id, limit)).fetchall()
    return rows


def reset_all_strikes(guild_id: int) -> int:
    """Reset every user's strike count to 0 for a guild. Returns number of rows reset."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE strikes SET count=0 WHERE guild_id=? AND count>0", (guild_id,)
        )
        conn.commit()
    return cur.rowcount


# ── Vouch helpers ───────────────────────────────────────────────────────────

def add_vouch(voucher_id: int, target_id: int, guild_id: int, proof: str) -> bool:
    """Returns True if inserted, False if duplicate."""
    try:
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO vouches (voucher_id, target_id, guild_id, proof)
                VALUES (?, ?, ?, ?)
            """, (voucher_id, target_id, guild_id, proof))
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def add_scam_vouch(voucher_id: int, target_id: int, guild_id: int, proof: str) -> bool:
    try:
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO scam_vouches (voucher_id, target_id, guild_id, proof)
                VALUES (?, ?, ?, ?)
            """, (voucher_id, target_id, guild_id, proof))
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def get_vouch_counts(target_id: int, guild_id: int):
    """Returns (vouch_count, scam_vouch_count)."""
    with get_connection() as conn:
        v  = conn.execute(
            "SELECT COUNT(*) as c FROM vouches WHERE target_id=? AND guild_id=?",
            (target_id, guild_id)
        ).fetchone()["c"]
        sv = conn.execute(
            "SELECT COUNT(*) as c FROM scam_vouches WHERE target_id=? AND guild_id=?",
            (target_id, guild_id)
        ).fetchone()["c"]
    return v, sv


def get_recent_vouches(target_id: int, guild_id: int, limit: int = 5):
    with get_connection() as conn:
        return conn.execute("""
            SELECT * FROM vouches WHERE target_id=? AND guild_id=?
            ORDER BY timestamp DESC LIMIT ?
        """, (target_id, guild_id, limit)).fetchall()


def get_recent_scam_vouches(target_id: int, guild_id: int, limit: int = 5):
    with get_connection() as conn:
        return conn.execute("""
            SELECT * FROM scam_vouches WHERE target_id=? AND guild_id=?
            ORDER BY timestamp DESC LIMIT ?
        """, (target_id, guild_id, limit)).fetchall()


def remove_vouch(voucher_id: int, target_id: int, guild_id: int) -> bool:
    """Delete the vouch from voucher→target. Returns True if a row was deleted."""
    with get_connection() as conn:
        cur = conn.execute("""
            DELETE FROM vouches
            WHERE voucher_id=? AND target_id=? AND guild_id=?
        """, (voucher_id, target_id, guild_id))
        conn.commit()
    return cur.rowcount > 0


def remove_scam_vouch(voucher_id: int, target_id: int, guild_id: int) -> bool:
    """Delete the scam-vouch from voucher→target. Returns True if a row was deleted."""
    with get_connection() as conn:
        cur = conn.execute("""
            DELETE FROM scam_vouches
            WHERE voucher_id=? AND target_id=? AND guild_id=?
        """, (voucher_id, target_id, guild_id))
        conn.commit()
    return cur.rowcount > 0


def get_vouch_leaderboard(guild_id: int, limit: int = 10):
    with get_connection() as conn:
        return conn.execute("""
            SELECT target_id, COUNT(*) as total
            FROM vouches WHERE guild_id=?
            GROUP BY target_id ORDER BY total DESC LIMIT ?
        """, (guild_id, limit)).fetchall()


def get_scam_vouch_leaderboard(guild_id: int, limit: int = 10):
    with get_connection() as conn:
        return conn.execute("""
            SELECT target_id, COUNT(*) as total
            FROM scam_vouches WHERE guild_id=?
            GROUP BY target_id ORDER BY total DESC LIMIT ?
        """, (guild_id, limit)).fetchall()


# ── Builder payment helpers ─────────────────────────────────────────────────

def add_builder_payment(payment_id: str, staff_id: int, guild_id: int, ign: str, amount: str):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO builder_payments (payment_id, staff_id, guild_id, ign, amount)
            VALUES (?, ?, ?, ?, ?)
        """, (payment_id, staff_id, guild_id, ign, amount))
        conn.commit()


def get_builder_payments(staff_id: int, guild_id: int):
    with get_connection() as conn:
        return conn.execute("""
            SELECT * FROM builder_payments
            WHERE staff_id=? AND guild_id=?
            ORDER BY timestamp DESC
        """, (staff_id, guild_id)).fetchall()


# ── Builder case helpers ────────────────────────────────────────────────────

def create_builder_case(case_id: str, builder_id: int, customer_id: int,
                        guild_id: int, ign: str, amount: str):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO builder_cases (case_id, builder_id, customer_id, guild_id, ign, amount)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (case_id, builder_id, customer_id, guild_id, ign, amount))
        conn.execute("""
            INSERT INTO builder_timers (case_id, event, actor_id, note)
            VALUES (?, 'created', ?, 'Case created, awaiting customer confirmation')
        """, (case_id, builder_id))
        conn.commit()


def get_builder_case(case_id: str):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM builder_cases WHERE case_id=?", (case_id,)
        ).fetchone()


def get_all_builder_cases(guild_id: int):
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM builder_cases WHERE guild_id=? ORDER BY created_at DESC",
            (guild_id,)
        ).fetchall()


def update_builder_case_status(case_id: str, status: str,
                                start_time: str = None, end_time: str = None):
    with get_connection() as conn:
        if start_time and end_time:
            conn.execute("""
                UPDATE builder_cases
                SET status=?, start_time=?, end_time=?
                WHERE case_id=?
            """, (status, start_time, end_time, case_id))
        else:
            conn.execute(
                "UPDATE builder_cases SET status=? WHERE case_id=?",
                (status, case_id)
            )
        conn.commit()


def log_builder_timer_event(case_id: str, event: str, actor_id: int, note: str = None):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO builder_timers (case_id, event, actor_id, note)
            VALUES (?, ?, ?, ?)
        """, (case_id, event, actor_id, note))
        conn.commit()


def get_builder_case_logs(case_id: str):
    with get_connection() as conn:
        return conn.execute("""
            SELECT * FROM builder_timers WHERE case_id=?
            ORDER BY timestamp ASC
        """, (case_id,)).fetchall()


def get_pending_builder_cases():
    """Return all active (timer running) cases — used by scheduler on restart."""
    with get_connection() as conn:
        return conn.execute("""
            SELECT * FROM builder_cases
            WHERE status='active' AND end_time IS NOT NULL
        """).fetchall()


# ── Staff action log helper ─────────────────────────────────────────────────

def log_staff_action(action_type: str, actor_id: int, guild_id: int,
                     target_id: int = None, details: str = None):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO staff_actions (action_type, actor_id, target_id, details, guild_id)
            VALUES (?, ?, ?, ?, ?)
        """, (action_type, actor_id, target_id, details, guild_id))
        conn.commit()


# ── Bot permission grants ────────────────────────────────────────────────────

def _ensure_bot_perms_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_perms (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            target_type  TEXT    NOT NULL,
            target_id    INTEGER NOT NULL,
            guild_id     INTEGER NOT NULL,
            command_name TEXT    NOT NULL,
            granted_by   INTEGER NOT NULL,
            granted_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(target_type, target_id, guild_id, command_name)
        )
    """)


def has_perm_grant(target_type: str, target_id: int, guild_id: int, command_name: str) -> bool:
    """Return True if target_type/target_id has a grant for command_name (or 'all') in guild."""
    with get_connection() as conn:
        _ensure_bot_perms_table(conn)
        row = conn.execute("""
            SELECT 1 FROM bot_perms
            WHERE target_type=? AND target_id=? AND guild_id=?
              AND (command_name=? OR command_name='all')
        """, (target_type, target_id, guild_id, command_name)).fetchone()
    return row is not None


def add_perm_grant(target_type: str, target_id: int, guild_id: int,
                   command_name: str, granted_by: int) -> bool:
    """Insert a grant. Returns True if new, False if already existed."""
    try:
        with get_connection() as conn:
            _ensure_bot_perms_table(conn)
            conn.execute("""
                INSERT INTO bot_perms (target_type, target_id, guild_id, command_name, granted_by)
                VALUES (?, ?, ?, ?, ?)
            """, (target_type, target_id, guild_id, command_name, granted_by))
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def remove_perm_grant(target_type: str, target_id: int, guild_id: int,
                      command_name: str) -> bool:
    """Delete a specific grant. Returns True if something was deleted."""
    with get_connection() as conn:
        _ensure_bot_perms_table(conn)
        cur = conn.execute("""
            DELETE FROM bot_perms
            WHERE target_type=? AND target_id=? AND guild_id=? AND command_name=?
        """, (target_type, target_id, guild_id, command_name))
        conn.commit()
    return cur.rowcount > 0


def list_perm_grants(guild_id: int):
    """Return all grants for a guild, newest first."""
    with get_connection() as conn:
        _ensure_bot_perms_table(conn)
        return conn.execute("""
            SELECT * FROM bot_perms WHERE guild_id=? ORDER BY granted_at DESC
        """, (guild_id,)).fetchall()


# ── Partnership tracker ──────────────────────────────────────────────────────

def _ensure_partnerships_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS partnerships (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id     INTEGER NOT NULL,
            guild_id     INTEGER NOT NULL,
            partner_name TEXT    NOT NULL,
            notes        TEXT,
            invite_code  TEXT,
            timestamp    TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # Migrate existing tables that predate invite_code column
    try:
        conn.execute("ALTER TABLE partnerships ADD COLUMN invite_code TEXT")
    except Exception:
        pass  # column already exists


def log_partnership(staff_id: int, guild_id: int, partner_name: str,
                    notes: str = None, invite_code: str = None):
    with get_connection() as conn:
        _ensure_partnerships_table(conn)
        conn.execute("""
            INSERT INTO partnerships (staff_id, guild_id, partner_name, notes, invite_code)
            VALUES (?, ?, ?, ?, ?)
        """, (staff_id, guild_id, partner_name, notes, invite_code))
        conn.commit()


def get_partnership_count(staff_id: int, guild_id: int) -> int:
    with get_connection() as conn:
        _ensure_partnerships_table(conn)
        row = conn.execute("""
            SELECT COUNT(*) as c FROM partnerships WHERE staff_id=? AND guild_id=?
        """, (staff_id, guild_id)).fetchone()
    return row["c"] if row else 0


def get_recent_partnerships(staff_id: int, guild_id: int, limit: int = 5):
    with get_connection() as conn:
        _ensure_partnerships_table(conn)
        return conn.execute("""
            SELECT * FROM partnerships WHERE staff_id=? AND guild_id=?
            ORDER BY timestamp DESC LIMIT ?
        """, (staff_id, guild_id, limit)).fetchall()


def get_partnership_leaderboard(guild_id: int, limit: int = 10):
    with get_connection() as conn:
        _ensure_partnerships_table(conn)
        return conn.execute("""
            SELECT staff_id, COUNT(*) as total
            FROM partnerships WHERE guild_id=?
            GROUP BY staff_id ORDER BY total DESC LIMIT ?
        """, (guild_id, limit)).fetchall()


def get_total_partnerships(guild_id: int) -> int:
    with get_connection() as conn:
        _ensure_partnerships_table(conn)
        row = conn.execute(
            "SELECT COUNT(*) as c FROM partnerships WHERE guild_id=?", (guild_id,)
        ).fetchone()
    return row["c"] if row else 0


def add_partnerships_bulk(staff_id: int, guild_id: int, amount: int, added_by_id: int) -> int:
    """Insert `amount` manual partnership rows for staff_id. Returns new total."""
    with get_connection() as conn:
        _ensure_partnerships_table(conn)
        for _ in range(amount):
            conn.execute("""
                INSERT INTO partnerships (staff_id, guild_id, partner_name, notes)
                VALUES (?, ?, 'Manual entry', ?)
            """, (staff_id, guild_id, f"Manually added by {added_by_id}"))
        conn.commit()
        row = conn.execute(
            "SELECT COUNT(*) as c FROM partnerships WHERE staff_id=? AND guild_id=?",
            (staff_id, guild_id)
        ).fetchone()
    return row["c"] if row else 0


def remove_partnerships_bulk(staff_id: int, guild_id: int, amount: int) -> tuple[int, int]:
    """Delete up to `amount` most recent partnership rows. Returns (removed, new_total)."""
    with get_connection() as conn:
        _ensure_partnerships_table(conn)
        ids = conn.execute("""
            SELECT id FROM partnerships WHERE staff_id=? AND guild_id=?
            ORDER BY timestamp DESC LIMIT ?
        """, (staff_id, guild_id, amount)).fetchall()
        if ids:
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"DELETE FROM partnerships WHERE id IN ({placeholders})",
                [row["id"] for row in ids]
            )
        conn.commit()
        row = conn.execute(
            "SELECT COUNT(*) as c FROM partnerships WHERE staff_id=? AND guild_id=?",
            (staff_id, guild_id)
        ).fetchone()
    removed = len(ids)
    return removed, (row["c"] if row else 0)


# ── Ticket system ─────────────────────────────────────────────────────────────

def _ensure_tickets_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id     TEXT    NOT NULL UNIQUE,
            user_id       INTEGER NOT NULL,
            guild_id      INTEGER NOT NULL,
            category      TEXT    NOT NULL,
            status        TEXT    NOT NULL DEFAULT 'open',
            channel_id    INTEGER,
            opened_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            closed_at     TEXT
        )
    """)
    # Migrate old installs: add channel_id if missing
    try:
        conn.execute("ALTER TABLE tickets ADD COLUMN channel_id INTEGER")
    except Exception:
        pass


def open_ticket(ticket_id: str, user_id: int, guild_id: int, category: str) -> bool:
    """Create a new ticket. Returns False if user already has an open ticket."""
    try:
        with get_connection() as conn:
            _ensure_tickets_table(conn)
            conn.execute("""
                INSERT INTO tickets (ticket_id, user_id, guild_id, category)
                VALUES (?, ?, ?, ?)
            """, (ticket_id, user_id, guild_id, category))
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def get_open_ticket_for_user(user_id: int, guild_id: int):
    """Return the most recent open (non-closed) ticket row for a user, or None."""
    with get_connection() as conn:
        _ensure_tickets_table(conn)
        return conn.execute("""
            SELECT * FROM tickets
            WHERE user_id=? AND guild_id=? AND status != 'closed'
            ORDER BY opened_at DESC LIMIT 1
        """, (user_id, guild_id)).fetchone()


def get_ticket(ticket_id: str):
    with get_connection() as conn:
        _ensure_tickets_table(conn)
        return conn.execute(
            "SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)
        ).fetchone()


def update_ticket(ticket_id: str, **fields):
    """Update arbitrary columns on a ticket row."""
    if not fields:
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [ticket_id]
    with get_connection() as conn:
        _ensure_tickets_table(conn)
        conn.execute(
            f"UPDATE tickets SET {set_clause} WHERE ticket_id=?", values
        )
        conn.commit()


def close_ticket(ticket_id: str):
    with get_connection() as conn:
        _ensure_tickets_table(conn)
        conn.execute("""
            UPDATE tickets SET status='closed', closed_at=datetime('now')
            WHERE ticket_id=?
        """, (ticket_id,))
        conn.commit()


def get_ticket_by_channel(channel_id: int):
    """Return the open ticket for a given Discord channel, or None."""
    with get_connection() as conn:
        _ensure_tickets_table(conn)
        return conn.execute(
            "SELECT * FROM tickets WHERE channel_id=? AND status != 'closed' LIMIT 1",
            (channel_id,)
        ).fetchone()


def get_all_open_tickets(guild_id: int):
    with get_connection() as conn:
        _ensure_tickets_table(conn)
        return conn.execute("""
            SELECT * FROM tickets
            WHERE guild_id=? AND status != 'closed'
            ORDER BY opened_at DESC
        """, (guild_id,)).fetchall()


# ── Guild config (per-server settings) ───────────────────────────────────────

def _ensure_guild_config_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER NOT NULL,
            key      TEXT    NOT NULL,
            value    TEXT    NOT NULL,
            PRIMARY KEY (guild_id, key)
        )
    """)


def get_guild_config(guild_id: int, key: str) -> str | None:
    """Return config value for a guild key, or None if not set."""
    with get_connection() as conn:
        _ensure_guild_config_table(conn)
        row = conn.execute(
            "SELECT value FROM guild_config WHERE guild_id=? AND key=?",
            (guild_id, key)
        ).fetchone()
    return row["value"] if row else None


def set_guild_config(guild_id: int, key: str, value: str):
    """Insert or update a guild config key."""
    with get_connection() as conn:
        _ensure_guild_config_table(conn)
        conn.execute("""
            INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?)
            ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value
        """, (guild_id, key, value))
        conn.commit()


def get_all_guild_config(guild_id: int) -> dict:
    """Return all config key-value pairs for a guild."""
    with get_connection() as conn:
        _ensure_guild_config_table(conn)
        rows = conn.execute(
            "SELECT key, value FROM guild_config WHERE guild_id=?", (guild_id,)
        ).fetchall()
    return {row["key"]: row["value"] for row in rows}


# ── Partner blacklist ─────────────────────────────────────────────────────────

def _ensure_blacklist_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS partner_blacklist (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id    INTEGER NOT NULL UNIQUE,
            server_name  TEXT    NOT NULL,
            reason       TEXT    NOT NULL,
            added_by     INTEGER NOT NULL,
            guild_id     INTEGER NOT NULL,
            timestamp    TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)


def blacklist_add(server_id: int, server_name: str, reason: str,
                  added_by: int, guild_id: int) -> bool:
    """Add a server to the blacklist. Returns True if new, False if already listed."""
    try:
        with get_connection() as conn:
            _ensure_blacklist_table(conn)
            conn.execute("""
                INSERT INTO partner_blacklist
                    (server_id, server_name, reason, added_by, guild_id)
                VALUES (?, ?, ?, ?, ?)
            """, (server_id, server_name, reason, added_by, guild_id))
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def blacklist_remove(server_id: int) -> bool:
    """Remove a server from the blacklist. Returns True if a row was deleted."""
    with get_connection() as conn:
        _ensure_blacklist_table(conn)
        cur = conn.execute(
            "DELETE FROM partner_blacklist WHERE server_id=?", (server_id,)
        )
        conn.commit()
    return cur.rowcount > 0


def blacklist_check(server_id: int):
    """Return the blacklist row for server_id, or None if not blacklisted."""
    with get_connection() as conn:
        _ensure_blacklist_table(conn)
        return conn.execute(
            "SELECT * FROM partner_blacklist WHERE server_id=?", (server_id,)
        ).fetchone()


def blacklist_list(guild_id: int):
    """Return all blacklisted entries for a guild, newest first."""
    with get_connection() as conn:
        _ensure_blacklist_table(conn)
        return conn.execute("""
            SELECT * FROM partner_blacklist WHERE guild_id=?
            ORDER BY timestamp DESC
        """, (guild_id,)).fetchall()


# ── Activity check system ─────────────────────────────────────────────────────

def _ensure_activity_check_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activity_checks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER NOT NULL,
            channel_id  INTEGER NOT NULL,
            message_id  INTEGER,
            actor_id    INTEGER NOT NULL,
            deadline    TEXT    NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'active',
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activity_responses (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            check_id     INTEGER NOT NULL,
            staff_id     INTEGER NOT NULL,
            responded_at TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(check_id, staff_id)
        )
    """)


def create_activity_check(guild_id: int, channel_id: int,
                           actor_id: int, deadline: str) -> int:
    """Create a new activity check and return its ID."""
    with get_connection() as conn:
        _ensure_activity_check_tables(conn)
        cur = conn.execute("""
            INSERT INTO activity_checks (guild_id, channel_id, actor_id, deadline)
            VALUES (?, ?, ?, ?)
        """, (guild_id, channel_id, actor_id, deadline))
        conn.commit()
    return cur.lastrowid


def update_activity_check_message(check_id: int, message_id: int):
    """Save the Discord message ID for a check (set after the message is sent)."""
    with get_connection() as conn:
        _ensure_activity_check_tables(conn)
        conn.execute(
            "UPDATE activity_checks SET message_id=? WHERE id=?",
            (message_id, check_id)
        )
        conn.commit()


def update_activity_check_status(check_id: int, status: str):
    with get_connection() as conn:
        _ensure_activity_check_tables(conn)
        conn.execute(
            "UPDATE activity_checks SET status=? WHERE id=?",
            (status, check_id)
        )
        conn.commit()


def get_activity_check(check_id: int):
    with get_connection() as conn:
        _ensure_activity_check_tables(conn)
        return conn.execute(
            "SELECT * FROM activity_checks WHERE id=?", (check_id,)
        ).fetchone()


def get_activity_check_by_message(message_id: int):
    with get_connection() as conn:
        _ensure_activity_check_tables(conn)
        return conn.execute(
            "SELECT * FROM activity_checks WHERE message_id=?", (message_id,)
        ).fetchone()


def get_all_active_activity_checks():
    with get_connection() as conn:
        _ensure_activity_check_tables(conn)
        return conn.execute(
            "SELECT * FROM activity_checks WHERE status='active'"
        ).fetchall()


def record_activity_response(check_id: int, staff_id: int) -> bool:
    """Record a staff member's attendance. Returns True if new, False if duplicate."""
    try:
        with get_connection() as conn:
            _ensure_activity_check_tables(conn)
            conn.execute("""
                INSERT INTO activity_responses (check_id, staff_id)
                VALUES (?, ?)
            """, (check_id, staff_id))
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def get_activity_responses(check_id: int):
    with get_connection() as conn:
        _ensure_activity_check_tables(conn)
        return conn.execute(
            "SELECT * FROM activity_responses WHERE check_id=?", (check_id,)
        ).fetchall()


# ── Giveaway persistence ──────────────────────────────────────────────────────

def _ensure_giveaways_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS giveaways (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            giveaway_id  TEXT    NOT NULL UNIQUE,
            message_id   INTEGER NOT NULL,
            channel_id   INTEGER NOT NULL,
            guild_id     INTEGER NOT NULL,
            host_id      INTEGER NOT NULL,
            prize        TEXT    NOT NULL,
            end_mode     TEXT    NOT NULL,
            end_value    INTEGER NOT NULL,
            num_winners  INTEGER NOT NULL DEFAULT 1,
            is_quickdrop INTEGER NOT NULL DEFAULT 0,
            status       TEXT    NOT NULL DEFAULT 'active',
            created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS giveaway_entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            giveaway_id TEXT    NOT NULL,
            user_id     INTEGER NOT NULL,
            UNIQUE(giveaway_id, user_id)
        )
    """)


def create_giveaway(giveaway_id: str, message_id: int, channel_id: int,
                    guild_id: int, host_id: int, prize: str, end_mode: str,
                    end_value: int, num_winners: int, is_quickdrop: bool):
    with get_connection() as conn:
        _ensure_giveaways_table(conn)
        conn.execute("""
            INSERT INTO giveaways
                (giveaway_id, message_id, channel_id, guild_id, host_id, prize,
                 end_mode, end_value, num_winners, is_quickdrop)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (giveaway_id, message_id, channel_id, guild_id, host_id, prize,
              end_mode, end_value, num_winners, 1 if is_quickdrop else 0))
        conn.commit()


def get_active_giveaways():
    with get_connection() as conn:
        _ensure_giveaways_table(conn)
        return conn.execute(
            "SELECT * FROM giveaways WHERE status='active'"
        ).fetchall()


def get_giveaway(giveaway_id: str):
    with get_connection() as conn:
        _ensure_giveaways_table(conn)
        return conn.execute(
            "SELECT * FROM giveaways WHERE giveaway_id=?", (giveaway_id,)
        ).fetchone()


def get_giveaway_by_message(message_id: int):
    with get_connection() as conn:
        _ensure_giveaways_table(conn)
        return conn.execute(
            "SELECT * FROM giveaways WHERE message_id=?", (message_id,)
        ).fetchone()


def add_giveaway_entry(giveaway_id: str, user_id: int) -> bool:
    """Add entry. Returns True if new, False if duplicate."""
    try:
        with get_connection() as conn:
            _ensure_giveaways_table(conn)
            conn.execute("""
                INSERT INTO giveaway_entries (giveaway_id, user_id)
                VALUES (?, ?)
            """, (giveaway_id, user_id))
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def get_giveaway_entries(giveaway_id: str):
    with get_connection() as conn:
        _ensure_giveaways_table(conn)
        return conn.execute(
            "SELECT user_id FROM giveaway_entries WHERE giveaway_id=?",
            (giveaway_id,)
        ).fetchall()


def get_giveaway_entry_count(giveaway_id: str) -> int:
    with get_connection() as conn:
        _ensure_giveaways_table(conn)
        row = conn.execute(
            "SELECT COUNT(*) as c FROM giveaway_entries WHERE giveaway_id=?",
            (giveaway_id,)
        ).fetchone()
    return row["c"] if row else 0


def end_giveaway(giveaway_id: str, status: str = 'ended'):
    with get_connection() as conn:
        _ensure_giveaways_table(conn)
        conn.execute(
            "UPDATE giveaways SET status=? WHERE giveaway_id=?",
            (status, giveaway_id)
        )
        conn.commit()


def cancel_giveaway(message_id: int) -> bool:
    """Cancel giveaway by message_id. Returns True if found and cancelled."""
    with get_connection() as conn:
        _ensure_giveaways_table(conn)
        cur = conn.execute(
            "UPDATE giveaways SET status='cancelled' WHERE message_id=? AND status='active'",
            (message_id,)
        )
        conn.commit()
    return cur.rowcount > 0


# ── Listings system ───────────────────────────────────────────────────────────

def _ensure_listings_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            listing_id      TEXT    NOT NULL UNIQUE,
            guild_id        INTEGER NOT NULL,
            seller_id       INTEGER NOT NULL,
            item_name       TEXT    NOT NULL,
            description     TEXT,
            quantity        TEXT    NOT NULL,
            category        TEXT    NOT NULL,
            type            TEXT    NOT NULL,
            buy_now_price   TEXT,
            starting_bid    TEXT,
            current_bid     TEXT,
            current_bidder  INTEGER,
            min_increment   TEXT,
            reserve_price   TEXT,
            duration_ms     INTEGER,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            ends_at         TEXT,
            status          TEXT    NOT NULL DEFAULT 'active',
            message_id      INTEGER,
            channel_id      INTEGER,
            bid_history     TEXT    NOT NULL DEFAULT '[]',
            watchers        TEXT    NOT NULL DEFAULT '[]'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listing_users (
            discord_id  INTEGER PRIMARY KEY,
            ign         TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listing_transactions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id      TEXT    NOT NULL,
            guild_id        INTEGER NOT NULL,
            buyer_id        INTEGER NOT NULL,
            seller_id       INTEGER NOT NULL,
            ticket_channel_id INTEGER NOT NULL UNIQUE,
            final_price     TEXT,
            status          TEXT    NOT NULL DEFAULT 'open',
            deal_confirmed_by TEXT  NOT NULL DEFAULT '[]',
            scam_reporter_id  INTEGER,
            scam_accused_id   INTEGER,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS listing_ratings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            rater_id    INTEGER NOT NULL,
            rated_id    INTEGER NOT NULL,
            listing_id  TEXT    NOT NULL,
            stars       INTEGER NOT NULL,
            timestamp   TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(rater_id, listing_id)
        )
    """)


def create_listing(listing_id: str, guild_id: int, seller_id: int, item_name: str,
                   description: str, quantity: str, category: str, listing_type: str,
                   buy_now_price: str = None, starting_bid: str = None,
                   min_increment: str = None, reserve_price: str = None,
                   duration_ms: int = None, ends_at: str = None):
    with get_connection() as conn:
        _ensure_listings_tables(conn)
        conn.execute("""
            INSERT INTO listings
                (listing_id, guild_id, seller_id, item_name, description, quantity,
                 category, type, buy_now_price, starting_bid, min_increment,
                 reserve_price, duration_ms, ends_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (listing_id, guild_id, seller_id, item_name, description, quantity,
              category, listing_type, buy_now_price, starting_bid, min_increment,
              reserve_price, duration_ms, ends_at))
        conn.commit()


def get_listing(listing_id: str):
    with get_connection() as conn:
        _ensure_listings_tables(conn)
        return conn.execute(
            "SELECT * FROM listings WHERE listing_id=?", (listing_id,)
        ).fetchone()


def update_listing(listing_id: str, **fields):
    if not fields:
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [listing_id]
    with get_connection() as conn:
        _ensure_listings_tables(conn)
        conn.execute(f"UPDATE listings SET {set_clause} WHERE listing_id=?", values)
        conn.commit()


def atomic_claim_listing(listing_id: str, new_status: str) -> bool:
    """Atomically transition a listing from 'active' to new_status.
    Returns True if the update succeeded (listing was active), False otherwise.
    This prevents double-sell race conditions."""
    with get_connection() as conn:
        _ensure_listings_tables(conn)
        cur = conn.execute(
            "UPDATE listings SET status=? WHERE listing_id=? AND status='active'",
            (new_status, listing_id)
        )
        conn.commit()
    return cur.rowcount > 0


def get_active_listings(guild_id: int, category: str = None):
    with get_connection() as conn:
        _ensure_listings_tables(conn)
        if category:
            return conn.execute("""
                SELECT * FROM listings
                WHERE guild_id=? AND status='active' AND category=?
                ORDER BY created_at DESC
            """, (guild_id, category)).fetchall()
        return conn.execute("""
            SELECT * FROM listings
            WHERE guild_id=? AND status='active'
            ORDER BY created_at DESC
        """, (guild_id,)).fetchall()


def get_user_listings(seller_id: int, guild_id: int, status: str = None):
    with get_connection() as conn:
        _ensure_listings_tables(conn)
        if status:
            return conn.execute("""
                SELECT * FROM listings
                WHERE seller_id=? AND guild_id=? AND status=?
                ORDER BY created_at DESC
            """, (seller_id, guild_id, status)).fetchall()
        return conn.execute("""
            SELECT * FROM listings
            WHERE seller_id=? AND guild_id=?
            ORDER BY created_at DESC
        """, (seller_id, guild_id)).fetchall()


def get_listing_history(guild_id: int, user_id: int = None, limit: int = 20, offset: int = 0):
    with get_connection() as conn:
        _ensure_listings_tables(conn)
        if user_id:
            return conn.execute("""
                SELECT * FROM listings
                WHERE guild_id=? AND seller_id=? AND status IN ('sold','expired','cancelled')
                ORDER BY created_at DESC LIMIT ? OFFSET ?
            """, (guild_id, user_id, limit, offset)).fetchall()
        return conn.execute("""
            SELECT * FROM listings
            WHERE guild_id=? AND status IN ('sold','expired','cancelled')
            ORDER BY created_at DESC LIMIT ? OFFSET ?
        """, (guild_id, limit, offset)).fetchall()


def get_expired_active_listings():
    """Return bidding listings whose ends_at has passed and are still active."""
    with get_connection() as conn:
        _ensure_listings_tables(conn)
        return conn.execute("""
            SELECT * FROM listings
            WHERE status='active' AND type='bidding' AND ends_at IS NOT NULL
              AND ends_at <= datetime('now')
        """).fetchall()


def set_user_ign(discord_id: int, ign: str):
    with get_connection() as conn:
        _ensure_listings_tables(conn)
        conn.execute("""
            INSERT INTO listing_users (discord_id, ign, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(discord_id) DO UPDATE SET ign=excluded.ign, updated_at=excluded.updated_at
        """, (discord_id, ign))
        conn.commit()


def get_user_ign(discord_id: int) -> str | None:
    with get_connection() as conn:
        _ensure_listings_tables(conn)
        row = conn.execute(
            "SELECT ign FROM listing_users WHERE discord_id=?", (discord_id,)
        ).fetchone()
    return row["ign"] if row else None


def create_listing_transaction(listing_id: str, guild_id: int, buyer_id: int,
                                seller_id: int, ticket_channel_id: int,
                                final_price: str = None) -> bool:
    try:
        with get_connection() as conn:
            _ensure_listings_tables(conn)
            conn.execute("""
                INSERT INTO listing_transactions
                    (listing_id, guild_id, buyer_id, seller_id, ticket_channel_id, final_price)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (listing_id, guild_id, buyer_id, seller_id, ticket_channel_id, final_price))
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def get_transaction_by_channel(channel_id: int):
    with get_connection() as conn:
        _ensure_listings_tables(conn)
        return conn.execute(
            "SELECT * FROM listing_transactions WHERE ticket_channel_id=?", (channel_id,)
        ).fetchone()


def update_transaction(ticket_channel_id: int, **fields):
    if not fields:
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [ticket_channel_id]
    with get_connection() as conn:
        _ensure_listings_tables(conn)
        conn.execute(
            f"UPDATE listing_transactions SET {set_clause} WHERE ticket_channel_id=?", values
        )
        conn.commit()


def add_listing_rating(rater_id: int, rated_id: int, listing_id: str, stars: int) -> bool:
    """Returns True if new rating saved, False if already rated this listing."""
    try:
        with get_connection() as conn:
            _ensure_listings_tables(conn)
            conn.execute("""
                INSERT INTO listing_ratings (rater_id, rated_id, listing_id, stars)
                VALUES (?, ?, ?, ?)
            """, (rater_id, rated_id, listing_id, stars))
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def get_user_avg_rating(user_id: int) -> tuple[float, int]:
    """Returns (avg_stars, count). avg_stars is 0.0 if no ratings."""
    with get_connection() as conn:
        _ensure_listings_tables(conn)
        row = conn.execute("""
            SELECT AVG(stars) as avg, COUNT(*) as cnt
            FROM listing_ratings WHERE rated_id=?
        """, (user_id,)).fetchone()
    if row and row["cnt"] > 0:
        return round(row["avg"], 1), row["cnt"]
    return 0.0, 0
