---
name: Listings atomic state transitions
description: Race-safety pattern for the Discord bot's listings/marketplace system (buy/offer/counter/WTB flows).
---

- `atomic_claim_listing(listing_id, new_status)` does an atomic `active → new_status` UPDATE (checked via rowcount) to prevent double-sell races; always use it, not a read-then-write check, before creating a transaction ticket.
- **Why:** two users can accept/buy/fulfil concurrently; only a single atomic UPDATE call correctly picks one winner.
- **How to apply:** any new "accept" path (e.g. WTB seller-offer accept, counter-offer accept) must call `atomic_claim_listing` before `_create_transaction_ticket`, and treat a `False` return as "someone else already claimed this."
- For DB-level dedupe (e.g. "one active WTB request per user+item"), a pre-check query is not race-safe by itself — back it with a partial UNIQUE index (SQLite supports `CREATE UNIQUE INDEX ... WHERE <condition>`) and have the insert function catch `IntegrityError` and return `False`/`True` so the caller can react atomically.
- When persisting a pending-offer/counter token that leads to a DM, insert it into the in-memory dict only *after* the DM send succeeds — otherwise a `discord.Forbidden` leaves an orphaned, non-actionable token that lingers until the periodic cleanup loop reaps it.
