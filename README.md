# Chat2API

Chat2API is a local proxy tool that wraps Gemini CLI and Codex OAuth credential pools into a standard OpenAI-compatible API (`/v1/chat/completions`). It is designed for seamless integration with OpenAI clients, keeping account pools, quota management, and credentials completely transparent from the caller.

## Features
- **OpenAI-Compatible Endpoints**: Use `/v1/chat/completions` directly with your favorite OpenAI API client tools and libraries. Supports both streaming and non-streaming.
- **Provider Aggregation & Routing**: Intelligently routes requests to the lowest-busy or highest-quota account currently available.
- **Quota & Fallback Management**: Gracefully cascades back across equivalent LLMs. If Gemini Pro quotas exhaust, it cascades to other available proxies (like Codex flagship models) without returning rate-limit failures to the user.
- **Anti-Detection Security**: Employs advanced TLS Client Hello impersonation, session-stickiness logic, and strict single-concurrency per account.

## Setup

First, ensure you have your backend authenticated locally:
- **Gemini**: authenticate with `gemini` CLI to emit tokens to `~/.gemini/`
- **Codex**: authenticate with `codex --full-setup` to emit tokens to `~/.codex/`

**Configuration:**
1. Clone the repository.
2. Install Python dependencies: `pip install -r requirements.txt`.
3. Copy `.env.example` into `.env` and configure your credentials:
   ```env
   # Your Google Cloud Project OAuth Client ID/Secret (Required for token refresh)
   GEMINI_CLIENT_ID="your_client_id"
   GEMINI_CLIENT_SECRET="your_client_secret"
   ```

## Testing

To run the unit and integration tests, ensure you have `pytest` installed and run:

```bash
pytest tests/ -v
```

## Usage

Start the background service (by default, it will listen on `127.0.0.1:7860`):

```bash
uvicorn chat2api.main:app --host 127.0.0.1 --port 7860
```

To keep it running across reboots with `systemd`, install the bundled unit and enable it:

```bash
sudo cp chat2api.service /etc/systemd/system/chat2api.service
sudo systemctl daemon-reload
sudo systemctl enable --now chat2api.service
```

You can now call the API using any OpenAI-compatible client, securely bridging out via your local CLI proxies.

Semantic model aliases are also available, while keeping direct model selection intact:

- `gemini-thinking` / `gemini-pro` -> `gemini-3.1-pro-preview`
- `gemini-balanced` / `gemini-flash` -> `gemini-3-flash-preview`
- `gemini-fast` / `gemini-lite` -> `gemini-3.1-flash-lite-preview`
- `codex-thinking` -> `gpt-5.4`
- `codex-balanced` -> `gpt-5.3-codex`
- `codex-fast` -> `gpt-5.1-codex-mini`

The original concrete model IDs still work, so experienced callers can continue selecting the exact model they want.

Basic request examples:

```bash
curl http://127.0.0.1:7860/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gemini-2.5-pro",
    "messages": [{"role": "user", "content": "Say hello in one sentence."}],
    "stream": false
  }'
```

```bash
curl http://127.0.0.1:7860/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "codex-thinking",
    "messages": [{"role": "user", "content": "List three refactor ideas for this repo."}],
    "stream": true
  }'
```

Codex-specific compatibility notes:

- Unsupported knobs such as `temperature`, `top_p`, and `max_tokens` are silently ignored for Codex-backed requests instead of being forwarded upstream.
- Multi-turn chat still uses the normal OpenAI-style `messages` array. Chat2API now flattens Codex conversation history into an internal transcript before calling the Codex backend, so callers do not need to manually merge prior turns into one prompt.

Gemini-specific compatibility notes:

- `temperature`, `top_p`, and `max_tokens` are forwarded to Gemini as native generation settings.
- Multi-turn chat keeps the normal `messages` array shape. `assistant` history is preserved and forwarded as Gemini `assistant` turns.
- `n` and `stop` are currently ignored on the Gemini path, because Chat2API only returns a single streamed candidate today.

## Admin quota pages

The admin router now exposes browser-friendly quota pages:

- `/admin/quota-urls` lists one local quota URL per configured model and per account, and marks which Gemini/Codex account is currently active.
- `/admin/quota?provider=gemini&account=you@example.com&model=gemini-2.5-pro` shows live quota for that account/model.
- `/admin/quota?provider=codex&account=you@example.com&model=gpt-5.4` shows live Codex quota. Codex quota is shared across all configured Codex models, so each model link resolves to the same account-level usage windows.
- The dashboard can switch the active Gemini or Codex account in-place, so the next routed requests use that credential set immediately.
- Gemini cards collapse models into shared pools when Gemini reports the same live quota bucket for multiple models, while Codex cards summarize the shared weekly window for the active account.

Add `?format=json` if you want the raw JSON response instead of the HTML browser view.
