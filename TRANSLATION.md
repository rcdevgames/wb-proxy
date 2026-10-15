# Translation notes (CN → EN)

All Chinese text in this repository was translated to English. The upstream project is
`yxxawa/workbuddy_to_api`; this branch is a local translation pass over commit 077af16.

## Scope

Translated:

- `README.md` — full English rewrite.
- `web/index.html` — all visible text, `aria-label`, `title` and `placeholder` attributes; `lang="zh-CN"` → `lang="en"`.
- `web/app.js` — all user-facing strings (toasts, confirmations, badges, table labels, detail panes, risk panel).
- `web/style.css` — mobile table pseudo-element labels (`content:'原价 '` → `'Base '`, `content:'积分 '` → `'Credits '`).
- `wb_gateway/launcher_ui.py` — the `TEXT` dictionary used by the tkinter launcher window.
- `wb_gateway/models.py` — risk/restriction action messages returned through the API.
- `app.py` — data-directory lock error message.
- `启动网关.pyw` → `start_gateway.pyw` (the only reference was in the README, updated there).

## Deliberate behaviour decisions

- **Date/number locales switched to `en-US`** (`toLocaleString('zh-CN')` etc. → `'en-US'`) so dates and balances match the English UI. Formatting semantics are unchanged.
- **Upstream-matching keywords are preserved.** The verification regex in `wb_gateway/models.py` keeps the Chinese keywords (`验证码|人机验证|风控`) so Chinese upstream error bodies still classify as `device_verification`; English alternatives (`verification.?code|human.?verification`) were added on top. No matching behaviour was removed.
- **Promotion labels prefer English.** `badge.get('label', labelZh, labelEn)` → `badge.get('label', labelEn, labelZh)`, so the English UI shows the English promotion label when upstream provides one. Display-only.
- **API contract untouched.** Field names, error codes, paths, model IDs, and the `cn/`/`gl/` prefixes are unchanged; only human-readable text was translated.
- **Line endings preserved** (CRLF), so the diff contains translation changes only.

## Verification

- `node --check web/app.js` → OK; `python -m py_compile app.py start_gateway.pyw wb_gateway/*.py` → OK.
- Repo-wide scan: the only remaining CJK characters are the intentional upstream-matching keywords in `wb_gateway/models.py`.
- Headless smoke test (`python app.py --headless --port 8789`): `/health` 200, `/ui/`, `/ui/app.js`, `/ui/style.css` 200, and the served assets contain no CJK.
- Admin API smoke test with `WB_ADMIN_KEY`: `POST /admin/session` → authenticated; `/admin/dashboard`, `/admin/keys`, `/admin/model-pricing` return English payloads; error paths (`401`, unknown bulk action, empty Key name) return English messages.
- `wb_gateway.models.Risk.observe()` exercised directly: rate-limit, quota, and 11128 messages render in English, and both Chinese and English verification-code bodies still map to `device_verification`.

## Pending / not verified

- The tkinter launcher window was not opened (headless environment); only its `TEXT` strings were translated and the module was syntax-checked.
- Account-level flows that require a real WorkBuddy login (authorization, balance, check-in, pricing rows) were not exercised — no upstream account was available.
- The repository still has no `LICENSE`.
