# Changelog

## 2026-10-24 — a failed meter call no longer pauses the account

- The balance sync was answering a 401 on `/billing/meter/get-user-resource-summary` with an
  account-scoped `login_required` pause, which also blocked `/v2/chat/completions`. A metering
  endpoint can fail for its own reasons — an edge/WAF page instead of JSON, a throttled background
  job — and none of that is evidence that the account cannot serve chat. Two CN accounts were
  parked behind "Login required again" by their own balance probe.
- Only chat-serving paths (`/v2/chat/completions`, `/v3/config`, `/auth/token/refresh`) can now take
  an account out of the pool. A rejection from any other path is recorded with
  `diagnostic_only: true`, `manual: false`, `retry_at: now`: visible in the state file, never in
  `block()`, and chat traffic decides the account's fate on its own. `ip_restricted` and
  `request_illegal` keep their full effect from any path: both are verdicts about the account's
  standing, not about the endpoint being reachable.
- A diagnostic row never merges with a chat-created pause for the same account, so a later probe
  cannot silently lift a pause that chat traffic established.
- An unparsable body (an HTML edge page) is no longer reported as `upstream_code: invalid_upstream`;
  the status alone classifies it and the row records `upstream_body: non-json`.
- A 401 on an access-only account (no `refreshToken`) said "resolve the account restriction through
  official channels", advice that does not exist for a console session token. It now says the token
  was revoked or expired and to import a fresh one.
- `risk-state.json` had grown to 635 rows of dead cooldowns. Rows resolved more than a week ago, and
  cooldowns that expired more than a week ago, are dropped at load (capped at 2000 rows); unresolved
  manual pauses are never dropped. Rows parked on an auxiliary path by the old build are demoted at
  load, which releases the CN accounts on the next restart.
- Tests: `test_a_failed_balance_probe_does_not_pause_the_account`,
  `test_the_account_is_still_usable_for_chat_after_the_probe`, `test_a_chat_401_still_pauses_the_account`,
  `test_a_diagnostic_row_never_extends_a_chat_pause`,
  `test_a_revoked_console_token_says_to_import_a_fresh_one`,
  `test_an_unparsable_body_is_not_reported_as_an_upstream_code`,
  `test_a_metering_pause_is_lifted_at_startup`, `test_dead_cooldowns_are_dropped_and_live_pauses_kept`.

## 2026-10-22 — 11140 is risk control, not a dead account

- WorkBuddy answers 403 `11140` (`request illegal`) when its risk control rejects a request. It is
  not a revoked permission: the same request succeeds on another account, and the same account can
  serve again minutes later. The gateway mapped every 403 to `permission_denied` with a permanent
  manual pause, so each wave of 11140s took accounts out of the pool until someone clicked Resume —
  and the shrinking pool produced more 11140s.
- `11140` now maps to `request_illegal`: account-scoped, 60 s cooldown doubling on repeats (capped
  at 15 min), `manual: false`, so the account returns on its own.
- The rejection stays account-scoped: live traffic served 200s on healthy accounts in the same
  seconds another account got 11140, so widening it to the region would turn a partial rejection
  into an outage.
- Rows an older build parked as `permission_denied` with `upstream_code: 11140` are reclassified and
  resolved on load, so a restart restores the accounts that were stuck behind the old pause.
- Tests: `test_request_illegal_cools_down_instead_of_waiting_for_a_human`,
  `test_a_rejected_account_does_not_take_the_region_with_it`,
  `test_a_permanently_paused_11140_row_is_reclassified_and_cleared`.

## 2026-10-20 — a Key can change region, and its model picker follows the region

- `POST /admin/keys/{id}` accepts `scope` (`cn`, `intl`, `all`), so a Key no longer has to be
  recreated to move it to another region. Anything the body does not name stays as it was.
- The Edit Key dialog gained a Region select; previously only creation could set it.
- The model picker lists only models the Key's region can serve: `cn` shows `cn/*`, `intl` shows
  `gl/*`, `all` shows both under CN and GL headings. Saved models that fall outside the chosen
  region are called out above the list and dropped on Apply.
- The picker's search box was bound to `click`, so typing never filtered anything; it now listens
  for `input` and matches case-insensitively.

## 2026-10-19 — fail over to another account before the first byte

- `generate()` no longer returns a 429 when the account it picked is rate limited. It walks the
  candidate list (least-busy / least-recently-picked first), skips accounts that are in a cooldown,
  and replays the request on the next account that serves the same model — inside the same request,
  so the caller never waits out the cooldown.
- Only failures another account can plausibly absorb are replayed. The decision now follows the
  restriction's own scope: an account-scoped one (`rate_limited`, `permission_denied`, `quota_exhausted`,
  `login_required`) is replayed on another account, while a global or region-wide one
  (`device_verification`, `ip_restricted`, `upstream_unavailable`) is returned immediately because
  it would follow every candidate. Client-caused rejections (unknown model, bad parameter, 11128)
  are never replayed either.
- Bounded by `WB_MAX_FAILOVER` (default 10, 0 disables), counted as consecutive failures: one
  success ends the streak and the next request starts from zero. Each attempt is logged in the call
  row as `failover: [{account_id, status, code, at_ms}]`, and the log detail dialog shows a
  **Failover** line.
- Once the response has started streaming there is nothing to replay: a mid-stream failure is still
  reported in-band as an error event on the open stream, never retried.
- Tests: `test_a_rate_limited_account_is_replaced_before_the_first_byte`,
  `test_a_stream_that_already_started_is_never_replayed`,
  `test_a_client_error_is_not_replayed_on_another_account`,
  `test_an_account_scoped_permission_denial_moves_to_another_account`,
  `test_a_global_restriction_is_not_replayed`,
  `test_failover_stops_at_the_configured_cap` — 54 tests pass.

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

