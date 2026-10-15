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
- The token is exchanged for an access token and the profile is read from upstream, so the account is usable immediately and refreshable afterwards.
- Re-importing is safe: accounts are matched on `uid` + `enterpriseId` and updated in place instead of duplicated.
- The line's `name` is kept as the account label and shown as the row subtitle in the console (the upstream nickname remains the account title).
- Token values are never returned: the UI shows only per-line status and, on failure, an error code/message with the token redacted. Limits: 2 MB of text and 50 lines per import.

## Notes

- `pm2 save` has been run, so the process is part of the saved PM2 dump.
- Account flows (authorization, balance, check-in, multipliers) are exercised against the live accounts imported on 2026-10-14; `pm2 restart wb-gateway` is the normal way to apply code changes.
