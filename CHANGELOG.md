# Changelog

## 2026-10-16 — a rate limit belongs to the account, not the region

- `wb_gateway/models.py`: `429` now records `scope='account'` (was `'region'`); `block()` is the
  non-raising lookup `check()` uses, with `check()` keeping its exact old contract; the
  `rate_limited` action text no longer claims no account switching happens; startup migration
  corrects an existing region-scoped `rate_limited` entry that carries an `account_id`.
- `wb_gateway/server.py`: an account-scoped restriction hides that one account in `catalogs_for()`
  instead of aborting the region, `generate()` skips restricted accounts during selection instead
  of failing after the pick, the all-restricted case answers `429` with `Retry-After`, and an
  unclassified upstream rejection keeps its code in `row['upstream_code']`.
- `tests/test_rate_limit_scope.py`: 7 tests against a per-account stub upstream, all passing
  (19 together with the conversation-header tests).
- `PLAN-account-scoped-rate-limit.md`: the log evidence for the region-wide blackout, the change,
  and the live verification.

Found while diagnosing: the `X-Conversation-*` gate of issue #4892 is **per account**. `neoaimez`
answered `400 11128 "conversation id missing"` to the old headers and `200` to the new header
family, while four other intl accounts answered `200` to both — so the previous entry's conclusion
that `www.workbuddy.ai` does not gate the header family was drawn from a single account and is
corrected here.

## 2026-10-16 — conversation headers mirror the official client

- `wb_gateway/protocol.py`: `conversation_key()`, `message_key()`, `conversation_headers()` — one
  stable RFC-4122 v5 conversation id per session (client `X-Conversation-ID` → `session_id` →
  `prompt_cache_key` → `conversation_id` → opening user message → random) and fresh UUID4 request and
  message ids per request.
- `wb_gateway/server.py`: chat calls use those headers instead of a random `conv_<hex>` id per
  request.
- `tests/test_conversation_headers.py`: 12 tests against a stub upstream, all passing.
- `PLAN-conversation-headers.md`: the A/B evidence, the behaviour changes, and what is still
  unverified.

