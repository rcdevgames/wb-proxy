# Plan — conversation headers on the upstream chat call

Status: implemented and verified live on 2026-10-16 (running service, intl region).
Upstreams: `www.workbuddy.ai` (intl), `www.workbuddy.cn` (cn).

## Trigger

9router issue #4892: since 2026-10-15 the intl gateway (`www.codebuddy.ai`) rejects every chat call
that does not carry the `X-Conversation-*` header family the official IDE sends —
`HTTP 400 {"code":11128,"msg":"conversation id missing"}`. The fix there makes
`CodeBuddyIntlExecutor` mirror the official client: an RFC-4122-v5-shaped conversation id derived
stably from the session key (`session_id` / `prompt_cache_key` / `conversation_id`, then the opening
user message, then random), a fresh request id per request and a fresh message id per message.

## What the upstream actually enforces (live A/B)

Account `steezclubka`, model `deepseek-v4.1-flash`, body
`{"messages":[{"role":"system",…},{"role":"user","content":"Say OK."}]}`, stream, `max_tokens` 16.
Only the header family differs between the three runs:

| Request headers | Result |
| --- | --- |
| what the gateway sent before (`conv_<hex32>`; `X-Conversation-Request-ID` = `X-Conversation-Message-ID` = `req_<hex32>`) | `200`, SSE streams |
| official-client shape (dashed UUIDs, fresh ids) | `200`, SSE streams |
| no `X-Conversation-*` at all | `200`, SSE streams |

`www.workbuddy.ai` therefore does **not** gate the header family. Its 11128 is a body rule —
`{"code":11128,"msg":"first message is not system prompt", displayTips:"…not sent from an official
client"}` — and it fires only when the first message is not a system prompt, which `prepare()` in
`wb_gateway/protocol.py` already covers for intl. Same error code, different message from #4892.

Operator decision: port the header pattern anyway, as hardening and for conversation continuity,
because the gateway used to send a random conversation id on every request.

## Change

`wb_gateway/protocol.py`

- `conversation_key(chat,incoming)`: client `X-Conversation-ID` → `session_id` → `prompt_cache_key` →
  `conversation_id` → text of the first user message → random `session_<hex>`.
- `message_key(message)`: text of a string content or of typed blocks; non-text parts are JSON-dumped
  with sorted keys, so the derived key does not depend on object key order.
- `conversation_headers(chat,incoming)`: `X-Conversation-ID` = `uuid5(CONVERSATION_NAMESPACE,key)`,
  stable for a session and therefore across retries and account failovers;
  `X-Conversation-Request-ID` and `X-Conversation-Message-ID` = fresh `uuid4` per request.

`wb_gateway/server.py`

- the chat call builds the official-client header block from `conversation_headers()` instead of
  `uid('conv_')`; the static headers (`X-Model-ID`, `X-Agent-Intent`, `X-Agent-Purpose`,
  `x-codebuddy-request`, `X-Private-Data`) are unchanged.

Deliberate behaviour changes:

- the three header values are UUID-shaped now, and a client-supplied `X-Conversation-ID` seeds the
  UUID instead of being forwarded verbatim. The upstream echoes `X-Conversation-Request-ID` as the
  completion `id` and never the conversation id, so the conversation id stays unobservable to
  clients.
- the completion `id` in the response body is the fresh UUID echoed by the upstream instead of
  `req_<hex>`. The gateway's own `X-Request-ID` response header and its log rows are unchanged.
- cn gets the same header values (one code path, upstream not gated). Its body handling is untouched:
  the injected default system prompt stays intl-only.

## Tests

`tests/test_conversation_headers.py` — 12 tests, `./.venv/bin/python -m unittest discover -s tests -v`.
The gateway runs against a stub upstream that records the outbound headers, so the assertions cover
the real request path rather than a helper in isolation: header presence and UUID shape, conversation
id stable across a retry and across an account failover, `session_id` precedence, stability derived
from the opening user message, typed-block equivalence with the string form, cn getting the headers
without the intl body rule, plus the session-key unit cases.

## Live verification

Short-lived client key created through `/admin/keys`, three `/v1/chat/completions` calls (no hints /
`prompt_cache_key` / `session_id`) to `gl/deepseek-v4.1-flash`: all `200` with content `OK` and
completion ids `11279b7f-…`, `58e8f696-…`, `aab5425d-…` — UUID4, i.e. the upstream received the new
request id. Unknown body fields (`session_id`, `prompt_cache_key`) pass through and are accepted. Key
revoked afterwards; the store holds only the original `waw` key.

## Pending / not verified

- cn has no account in the store, so the cn path is covered by the stub-upstream test only.
- Conversation-id stability is not observable against the live upstream (it echoes only the request
  id); it is asserted against the stub.
- `Risk.observe()` classifies 11128 only when the message contains "unapproved channel". The real
  messages are "first message is not system prompt" (workbuddy.ai) and "conversation id missing"
  (codebuddy.ai), so they fall through to a plain 400 `upstream_11128` with no risk entry. Left as is,
  because 11128 is a per-request diagnostic that must not block an account or a region.
