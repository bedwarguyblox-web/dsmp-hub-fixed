---
name: Listings cog interaction routing
description: Rules for ordering custom_id prefix checks in on_interaction to avoid ValueError crashes
---

In `cogs/listings.py` `on_interaction`, `startswith` branches must go from most-specific to least-specific. Any prefix that is a substring of a longer prefix will swallow the longer one first.

Confirmed conflicts that require specific-before-generic ordering:
- `listing_offer_accept_` / `listing_offer_decline_` / `listing_offer_counter_` BEFORE `listing_offer_`
- `listing_appeal_approve2_cancel` (exact match) BEFORE `listing_appeal_approve2_` BEFORE `listing_appeal_approve_` BEFORE `listing_appeal_deny_` BEFORE `listing_appeal_`
- `listing_buynow_confirm_` / `listing_buynow_abort_` BEFORE `listing_buy_`
- `listing_cancelconfirm_` / `listing_cancelabort_` BEFORE `listing_cancel_`
- `listing_scam_confirm_` / `listing_scam_abort_` BEFORE `listing_scam_vouch_`

**Why:** Python `str.startswith` is not longest-match; the first matching `elif` branch wins. A custom_id like `listing_offer_accept_tok` trivially starts with `listing_offer_`, so the generic handler runs, strips the prefix incorrectly, and passes garbage to the next handler.

**How to apply:** Any new `listing_X_Y_` button group must be placed above the `listing_X_` catch-all in `on_interaction`.
