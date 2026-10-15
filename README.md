# WB Gateway

Python multi-account model gateway with support for CN (domestic) and GL (international) accounts, a Web UI, balance and check-in tracking, model multipliers, call logs, and region-scoped API keys. It does not depend on the original desktop client or a Node.js backend.

Unofficial project — use it only with accounts and endpoints you are authorized to use.

<img width="2538" height="1400" alt="02feabeb-ff93-44d6-8376-3c346d1cc2c9" src="https://github.com/user-attachments/assets/52ecf49a-479d-4a4d-a514-0d025919098a" />

One-click run, visual web management interface



## Run

Verified on Windows + Python 3.12:

```powershell
py -3.12 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe app.py
```

The default port is `8787`. Open the console in the GUI window, copy the admin key, then sign in. `start_gateway.pyw` works too.

Headless mode:

```powershell
./.venv/Scripts/python.exe app.py --headless --host 127.0.0.1 --port 8787
```

In headless mode the admin key can be set with the `WB_ADMIN_KEY` environment variable; if it is not set, a key is generated and saved to the private `data/keys.json`. Other systems must use headless mode — this build has not been verified on them.

## Endpoints

- Console: `http://127.0.0.1:8787/ui/`
- OpenAI-style base URL: `http://127.0.0.1:8787/v1`
- Messages-style base URL: `http://127.0.0.1:8787`

Add an account in the console, complete the official web authorization, then create a call key. Clients use the call key, not the admin key.

Fetch the real models with `GET /v1/models`; the `cn/` or `gl/` prefix is preserved. Reasoning effort, output limits, and tools are set by the client and follow the model's actual capabilities. Chat Completions, stateless Responses, Messages, and SSE are supported.

## Notes

- Listens on localhost only by default; remote deployments must configure HTTPS and access control themselves.
- Tools are executed by the client; unsupported capabilities and malformed parameters are rejected explicitly. Complex workflows of every agent are not guaranteed.

No license has been specified yet; choose one and add `LICENSE` before publishing.
