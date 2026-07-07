---
name: Listings ends_at timestamp format
description: Why ends_at must be stored in SQLite-native format, not ISO 8601 with timezone
---

SQLite's `datetime('now')` returns the format `"YYYY-MM-DD HH:MM:SS"` (space-separated, no timezone suffix).
`datetime.isoformat()` returns `"YYYY-MM-DDTHH:MM:SS+00:00"` (T separator, timezone suffix).

Text comparison between these two formats is unreliable for same-day timestamps — the `T` vs space difference causes incorrect ordering.

**Fix applied:** Store `ends_at` using `.strftime("%Y-%m-%d %H:%M:%S")` (UTC). Parse back with `datetime.strptime(val, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)`, with a `.fromisoformat()` fallback for legacy rows.

**Why:** `get_expired_active_listings()` uses `WHERE ends_at <= datetime('now')` — this only works correctly when both sides use the same text format.

**How to apply:** Any new code writing datetimes to the `listings` table must use `.strftime("%Y-%m-%d %H:%M:%S")` and parse with `strptime`. Never use `.isoformat()` for DB-bound datetimes.
