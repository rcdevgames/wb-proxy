# Changelog

## 2026-10-19 — keys can be copied again, without unmasking the list

- `data/api-keys.json` now keeps each key's secret as AES-GCM ciphertext (`secret_enc`, sealed with
  `data/master.key`), next to the existing SHA-256 `digest`. The digest is still what
  `/v1/*` authenticates against; the ciphertext is only for showing the key back to the admin.
- `GET /admin/keys` returns the decrypted `key` field (admin token required, `digest` and
  `secret_enc` never leave the store), so the console can put the full key on the clipboard while
  the card keeps showing `sk-wb-…` only.
- Each key card has a **Copy** button; **Edit → New secret** rotates a key (confirm dialog, old
  secret stops working immediately) and shows the new one in the usual dialog.
- Keys created before this change have no stored secret — the plaintext is gone and cannot be
  recovered. Their Copy button is disabled and points at **New secret**, which is the only way to
  make an old key copyable again.
- Tests: `KeySecrets` covers round-trip, reload, rotation (old secret rejected) and the legacy
  no-secret case — 49 tests pass.

## 2026-10-19 — mobile layout: no more sideways scrolling

- `web/style.css`: the Keys page grid used `1fr` tracks whose automatic minimum size was the widest
  child, so at 390 px the create panel grew to 429 px and the whole page scrolled left/right. Tracks
  are now `minmax(0,1fr)` with `min-width:0` on the create-form children and `width:100%` on its
  inputs, and `.main-shell` clips stray horizontal overflow (`overflow-x:clip`, which keeps the
  sticky sidebar and the inner table scrollers working).
- Verified in an emulated browser at 320/360/375/390/414/430 (portrait) and 667/736/812 (landscape):
  `documentElement.scrollWidth === innerWidth` on Accounts, Pricing, Logs and Keys, and no element
  extends past the viewport. Desktop (1440 px) still renders 260 px + flexible columns with the
  sticky sidebar.

## 2026-10-19 — per-key models, RPM and parallel cap

- `wb_gateway/limits.py` (new): `model_allowed` matches region-qualified model IDs against the key's
  allowlist (exact IDs or `fnmatch` globs; empty list = everything), and `KeyLimits` holds the
  in-memory counters — a sliding 60-second RPM window and per-key in-flight slots.
- `wb_gateway/storage.py`: key records gained `models`, `rpm` and `max_concurrency`; `key_limits()`
  validates them (200 patterns max, 120 chars each, RPM 0–100000, concurrency 0–1000, `0` = no cap),
  `new_key()` stores them, `update_key()` edits them, and records written before this change pick up
  the defaults on load. `Fault` gained `retry_after`.
- `wb_gateway/server.py`: `generate` and `/v1/messages/count_tokens` charge the RPM budget and take a
  parallel slot before reading the body (released in `finally`); a model outside the allowlist is
  `403 model_not_allowed`; `model_list` filters the catalogue by the calling key and flags the result
  `restricted`; `POST /admin/keys/{id}` edits a key; `GET /admin/keys` reports live in-flight counts;
  429 responses now carry `Retry-After`.
- `web/`: the Keys page has Models / Requests per minute / Max parallel on the create form and in a
  new Edit dialog, with a searchable model picker built from the live catalogue; cards show the limits
  and whether the key is idle or running.
- `tests/test_key_limits.py`: 14 tests — glob matching, RPM window, default parallel cap of 2,
  record validation and migration, filtered `/v1/models`, 403 vs 404, 429 + `Retry-After`, the
  parallel cap under a slow upstream, and editing a live key through the admin endpoint.
- Default for every key, including ones created earlier, is **2 parallel requests** and no RPM cap.

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

