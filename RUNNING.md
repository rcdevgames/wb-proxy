# Running locally with PM2

The gateway is a headless Python (aiohttp) service on Linux; the tkinter launcher is Windows-only and not used here.

## Process

| Item | Value |
| --- | --- |
| PM2 name | `wb-gateway` |
| Port | **8787** (bound to `127.0.0.1` only) |
| Interpreter | `./.venv/bin/python` (required — PM2's default `node` interpreter cannot run `app.py`) |
| Args | `--headless --host 127.0.0.1 --port 8787 --data-dir <repo>/data` |
| Data dir | `<repo>/data` (gitignored runtime state) |
| Config | `ecosystem.config.cjs` |
| Public host | `https://via-wb.rcdevgames.net` (Caddy inside FrankenPHP) |

## Endpoints

Local:

- Web UI: `http://127.0.0.1:8787/ui/`
- API base: `http://127.0.0.1:8787/v1`
- Health: `http://127.0.0.1:8787/health`

Public: same paths under `https://via-wb.rcdevgames.net`.

The admin key for the console is generated on first start and stored in `data/keys.json` (mode 600). It is a local secret — do not paste it into commits, docs or chat.

## Public exposure

The host block lives in `/etc/frankenphp/Caddyfile` and proxies to `127.0.0.1:8787`, with the SSE branch (`Accept: text/event-stream` → `flush_interval -1`, `X-Accel-Buffering: no`) so streamed completions are not buffered. The hostname is also listed in the `:80, :443` catch-all allowlist, and TLS is issued normally by Let's Encrypt (HTTP-01).

Environment required for the public host to work (`ecosystem.config.cjs`):

- `WB_ALLOWED_ORIGINS=https://via-wb.rcdevgames.net` — browser POSTs from the UI carry an `Origin` header; without this the app answers `403 origin_not_allowed`.
- `WB_TRUSTED_PROXY_IPS=127.0.0.1` — trust Caddy's `X-Forwarded-For` for client IPs in logs.

Access control is the application's own: `/admin/*` requires the admin key (or a session cookie from `/admin/session`), and `/v1/*` requires an API key created under Admin → Keys. With no API keys in the store, every `/v1` request is `401`.

### Reloading Caddy

`sudo systemctl reload frankenphp` currently fails on this host with `status=226/NAMESPACE` (systemd cannot set up the unit's mount namespace for the control process) while the service keeps running the old config. Load the new config through the admin API instead:

```bash
sudo frankenphp validate --config /etc/frankenphp/Caddyfile
curl -sS -X POST 'http://127.0.0.1:2019/load' \
  -H 'Content-Type: text/caddyfile' --data-binary @/etc/frankenphp/Caddyfile -w '%{http_code}\n'
```

Keep a timestamped copy in `/etc/frankenphp/` before editing; the service's `ExecStartPre` validates the file on every start.

## Commands

```bash
cd ~/.workspace/workbuddy/workbuddy_to_api
./.venv/bin/pip install -r requirements.txt   # first time only
pm2 start ecosystem.config.cjs
pm2 restart wb-gateway
pm2 logs wb-gateway --lines 50
pm2 delete wb-gateway
```

Changing `args`, `interpreter` or `env` requires `pm2 delete wb-gateway` followed by `pm2 start ecosystem.config.cjs` — a plain `restart` does not pick those up.

## Adding accounts from exported tokens

The console exports one `name|token` pair per line (Keycloak offline token). Two ways in:

**1. Console UI (no restart):** Account page → *Add account* → *Import accounts from .txt*. Choose a `.txt` file or paste the lines, then *Import*. The endpoint is `POST /admin/accounts/import` with `{"text": "<lines>"}`; it answers `201` with `{imported, updated, failed, total, account_ids, results[]}` where each result carries `line`, `name`, `status`, `account_id`, `region`, `nickname` and, on failure, `error.code` / `error.message`. After a successful import the UI refreshes the dashboard and runs a balance sync for the imported accounts.

**2. Helper script (service stopped):**

```bash
cd ~/.workspace/workbuddy/workbuddy_to_api
pm2 stop wb-gateway
./.venv/bin/python ~/.workspace/workbuddy/add_accounts.py <file-with-name|token-lines>
pm2 start wb-gateway
```

Stop the service first: a running instance holds the decrypted store in memory and would overwrite the file on its next save. The UI import runs inside the process and has no such constraint.

Behaviour shared by both paths:

- The region is read from the token's own `iss` claim (`*.workbuddy.cn` / `*.codebuddy.cn` → CN, `*.workbuddy.ai` / `*.codebuddy.ai` → GL), so no region has to be picked by hand.
- Two token kinds are accepted. An offline token (`typ: Offline`) is exchanged for an access token and stays refreshable. A console session token (`typ: Bearer`, what the web console hands out) is not a refresh token — `POST /v2/plugin/auth/token/refresh` answers `12153 invalid_grant` for it — so it is stored as an access-only account that serves requests until its own `exp` (usually a year) and is reported with `refreshable: false`. Re-import a fresh session token before that date to extend it.
- The token is verified against upstream (`/v2/plugin/accounts`) before it is stored, so the account is usable immediately.
- Re-importing is safe: accounts are matched on `uid` + `enterpriseId` and updated in place instead of duplicated. A `login_required` pause on the account is cleared by the import.
- A pause that names an account which is no longer in the store blocks nothing and cannot be acted on, so it is resolved automatically (`resolution: account_removed`) at startup instead of sitting in the console as a manual action.
- The line's `name` is kept as the account label and shown as the row subtitle in the console (the upstream nickname remains the account title).
- Token values are never returned: the UI shows only per-line status and, on failure, an error code/message with the token redacted. Limits: 2 MB of text and 50 lines per import.

## Per-key limits

Each API key carries a region and three limits, editable in Admin → Keys (create form, and **Edit** on a key card):

- **Region** — `cn`, `intl` or `all`. It decides which accounts the key may reach; `all` means every account. Changing it on an existing key takes effect immediately, no need to recreate the key.
- **Models** — a comma-separated allowlist of region-qualified IDs (`gl/deepseek-v4.1-flash`) or globs (`cn/*`). Empty means every model. The model picker lists only models inside the key's region (`cn/*` for CN, `gl/*` for GL, both for `all`), so a CN key never shows GL models. `GET /v1/models` returns only what the key may call, and a request outside the list is `403 model_not_allowed` (an ID that does not exist at all stays `404 model_not_found`).
- **Requests / minute** — a sliding 60-second window. Over budget: `429 key_rate_limited` with `Retry-After`.
- **Max parallel** — in-flight requests for that key. Over budget: `429 key_concurrency_limit` with `Retry-After`. Default is **2**; `0` disables the cap.

The limits live on the key record in `data/api-keys.json`, so they survive a restart and older key records pick up the defaults on load. The counters are memory-only by design: a restart forgets requests that were already in flight. The Keys page shows the live in-flight count per key.

Limits are charged before the request body is read, so an over-budget key cannot consume bandwidth or an account slot.

## Failover on rate limits

A request that lands on a rate-limited (or transiently failing) account is replayed on the next
account that serves the same model, before anything is written to the client — the caller does not
wait out the cooldown. Whether a failure is replayed follows the restriction's scope: account-scoped
ones (rate limit, permission denied, quota, login) move to another account, global or region-wide
ones do not. `WB_MAX_FAILOVER` caps the attempts (default 3, `0` disables); client-caused
rejections are never replayed. Once streaming has started there is nothing to replay, so a failure
at that point is reported on the open stream. The attempt list is kept on the call row and shown as
**Failover** in the log detail.

## Copying a key

Each key card shows only `sk-wb-…` (the prefix) so a screenshot of the console cannot leak a working
key. **Copy** puts the full key on the clipboard without unmasking the card. The plaintext lives in
`data/api-keys.json` as AES-GCM ciphertext sealed with `data/master.key` and is returned only by the
admin-authenticated `GET /admin/keys`; `/v1/*` still authenticates against the SHA-256 digest.

Keys created before secrets were stored have no recoverable plaintext. Their Copy button is disabled
— use **Edit → New secret** to rotate, which issues a new key and invalidates the old one.

## Notes

- `pm2 save` has been run, so the process is part of the saved PM2 dump.
- Account flows (authorization, balance, check-in, multipliers) are exercised against the live accounts imported on 2026-10-14; `pm2 restart wb-gateway` is the normal way to apply code changes.
