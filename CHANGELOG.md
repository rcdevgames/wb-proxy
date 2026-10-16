# Changelog

## 2026-10-16 — conversation headers mirror the official client

- `wb_gateway/protocol.py`: `conversation_key()`, `message_key()`, `conversation_headers()` — one
  stable RFC-4122 v5 conversation id per session (client `X-Conversation-ID` → `session_id` →
  `prompt_cache_key` → `conversation_id` → opening user message → random) and fresh UUID4 request and
  message ids per request.
- `wb_gateway/server.py`: chat calls use those headers instead of a random `conv_<hex>` id per
  request.
- `tests/test_conversation_headers.py`: 12 tests against a stub upstream, all passing.
- `PLAN-conversation-headers.md`: the A/B evidence (the intl upstream does not gate the header family;
  its 11128 is the leading-system-prompt body rule), the behaviour changes, and what is still
  unverified.

Not yet pushed to `origin`.
