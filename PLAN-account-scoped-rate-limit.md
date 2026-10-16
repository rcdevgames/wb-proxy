# Plan — a rate limit belongs to the account, not the region

Status: implemented, verified live on 2026-10-16 (running service + real upstream).
Upstreams: `www.workbuddy.ai` (intl), `www.workbuddy.cn` (cn).

## Trigger

A client burst against `gl/deepseek-v4.1-flash` through `https://via-wb.rcdevgames.net/v1`
returned `200` five times and then `429` five times, and the same burst after a pause returned
`200` twelve times. The gateway's own log shows why:

| row | status | outcome | account | upstream_code | duration_ms |
| --- | --- | --- | --- | --- | --- |
| 1–5 | 200 | completed | intl_eef0f9271 / 0991d6893 / 1b479386c / 4b44b6d96 / 692c2e7a3 | – | ~940–1600 |
| 6 | 429 | failed | intl_692c2e7a3 | 14003 | 0.33 |
| 7–10 | 429 | failed | – | 14003 | 0.39–0.45 |

One account answered a genuine upstream `429` (`code 14003`), and `Risk.observe()` recorded
`kind=rate_limited, scope=region` with a 60 s cooldown. The next four requests never reached the
upstream at all: `catalogs_for()` raised the region-wide restriction before any account was
selected, so the four healthy accounts idled while the client got `429` in 0.3 ms.

Two separate defects were behind that, and both are about the *reach* of an account-level fact:

1. `models.py` classified a `429` as `scope='region'`.
2. `server.py` treated any active restriction as fatal to the request. `catalogs_for()` raised
   through `refresh()`, and `generate()` called `risk.check()` **after** picking a single account
   and let it `raise Fault` — the gateway has no per-request failover, so the client paid for a
   restriction that only concerned one account.

The live per-account probe that started this work also showed the `X-Conversation-*` gate from
issue #4892 is **per account**: `neoaimez` answered `400 {"code":11128,"msg":"conversation id
missing"}` to the old headers and `200` with the new header family, while the four other intl
accounts answered `200` to both. So `www.workbuddy.ai` gates the header family for some accounts
only — the reason the conversation-header change (commit `c1ef17b`) is a fix and not just hardening.

## Change

`wb_gateway/models.py`

- `429` → `scope='account'` (was `'region'`). A 5xx still records `scope='region'`, since an
  upstream outage is not per-account; that behaviour is unchanged on purpose.
- `block(region, account, path)` is the non-raising lookup `check()` now uses: it returns the
  active entry with the furthest `retry_at`, or `None`. `check()` keeps its exact old contract
  (raise `Fault` carrying the risk entry), so every existing call site behaves as before.
- the `rate_limited` action text now says the region keeps serving from the other accounts,
  instead of the "no account switching" wording that only made sense for a region-wide block.
- startup migration: an existing `rate_limited` entry with `scope='region'` **and** an
  `account_id` is corrected to `scope='account'`. Without an `account_id` the entry keeps the
  region scope, because then it really does cover every account.

`wb_gateway/server.py`

- `catalogs_for()`: a catalog failure carrying an account-scoped restriction hides that account
  (it is reported in the `errors` list, so `/v1/models` still answers `partial: true`) instead of
  aborting the whole region. Global and region restrictions still raise. When every account is
  hidden that way, the fault raised is the restriction itself (`429` + `Retry-After`) rather than
  a misleading `503 catalog_unavailable`.
- `generate()`: restricted accounts are skipped during selection (`risk.block()`), and the
  `risk.check()` call after the pick is gone. When nothing is left to pick, the client gets the
  restriction that expires last, with its `Retry-After`; the "all matching accounts busy" message
  is now only used when no account was restricted.
- `call()`: the upstream code of an unclassified rejection is carried on the fault, and
  `generate()` writes it to `row['upstream_code']`. A bare `400 11128` used to leave no trace in
  the call log at all.

Deliberate behaviour changes:

- a client-visible `429` now means every account in the requested region is restricted, not one of
  them. A restricted account is invisible to clients until its cooldown expires.
- `/v1/models` reports `partial: true` while an account is restricted (its catalog is skipped).
- the call log gains `upstream_code` for unclassified upstream rejections.

## Tests

`tests/test_rate_limit_scope.py` — 7 tests, `./.venv/bin/python -m unittest discover -s tests -v`
(19 with `test_conversation_headers.py`). The stub upstream fails per account (`X-User-Id`), so the
tests drive the real request path: a restricted account is skipped rather than fatal, the entry is
recorded against the account and does not match its neighbour or the other region, a region
restriction still covers every account, the all-restricted case answers `429` with `Retry-After`
and never reaches the upstream, an unclassified `400` keeps its upstream code in the log, and the
startup migration corrects only the entries that carry an account.

## Live verification

Running service, client key, `gl/deepseek-v4.1-flash`:

- `/v1/models` → `200`, 24 models, `partial: false`.
- 12 chats, 2.5 s apart → **12 × `200`** with content `OK`, 0.7–1.3 s, and no active restriction
  afterwards.

Real upstream, one synthetic account-scoped restriction injected into a **copy** of the data dir
(production untouched): 6 chats → **6 × `200`**, served by `intl_0991d6893`, `intl_1b479386c`,
`intl_692c2e7a3` and `intl_eef0f9271` — the restricted `steezclubka` (`intl_4b44b6d96`) served none
of them. The same run before the change would have answered `429` locally after the first request.

## Pending / not verified

- The 60 s cooldown per account is unchanged; only its reach is. A real upstream `429` under load
  was not reproduced on demand, so the account-scoped entry is verified against the stub and a
  synthetic entry, not against a live upstream `429`.
- Escalation is still `delay * 2^(count-1)` capped at 15 min per account. With five accounts a
  sustained `429` on all of them can now produce five entries instead of one; no aggregate cap is
  enforced.
- `upstream_unavailable` (5xx) stays region-scoped, so a 5xx on one account still hides the region
  for 5 s and up. Left as is: an upstream outage is a plausible cause, and the window is short.
