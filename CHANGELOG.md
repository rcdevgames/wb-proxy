# Changelog

## 2026-10-19 — import accepts a console session token

- `wb_gateway/server.py`: a token whose JWT `typ` is `Bearer` (what the web console exports) is no
  longer sent to `/v2/plugin/auth/token/refresh`, which answers `12153 invalid_grant` for it and used
  to fail the whole line. It is stored as an access-only account with `expiresAt` from its own `exp`
  and `auth_type: access_token` in the import result; `typ: Offline` still goes through the refresh
  exchange. `refresh()` now says "holds an access token only" instead of "Session expired" for those.
- `wb_gateway/models.py`: a `login_required` (or any account-scoped) pause that names an account no
  longer in the store is resolved at startup with `resolution: account_removed` — that is why the
  console kept showing "GL · Login required again / manual action still required" for ten accounts
  that had already been dropped, after the cooldown had expired.
- `tests/test_token_import.py`: 5 tests (access token stored without a refresh call, offline token
  still refreshed, expired access token rejected, pause for a removed account resolved, pause for a
  stored account kept) — 31 tests together, all passing.
- `RUNNING.md`: documents both token kinds and the automatic resolution of orphaned pauses.

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

