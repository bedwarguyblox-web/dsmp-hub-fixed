---
name: Listings atomic state transitions
description: How to prevent double-sell race conditions when closing a listing
---

Multiple Discord interaction handlers (buy-now confirm, offer accept, counter accept) can fire concurrently for the same listing. A read-then-write pattern allows two buyers to both see `status='active'` and both create transaction tickets.

**Fix:** `atomic_claim_listing(listing_id, new_status)` in `utils/database.py` issues:
```sql
UPDATE listings SET status=? WHERE listing_id=? AND status='active'
```
and returns `True` only if `rowcount > 0`. Callers reject the interaction immediately on `False`.

**Why:** SQLite serializes writes per connection, so this conditional update is effectively atomic for our single-file DB.

**How to apply:** Any code path that transitions a listing out of `active` must use `atomic_claim_listing` instead of `update_listing(... status=...)`. Currently covers: `_handle_buynow_confirm`, `_handle_offer_accept`, `_handle_counter_accept`. The bid-expiry loop uses a direct `update_listing` call but runs on a single asyncio task so no concurrent risk there.
