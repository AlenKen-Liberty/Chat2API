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

## Usage

Start the background service (by default, it will listen on `127.0.0.1:8000`):

```bash
uvicorn chat2api.main:app --host 127.0.0.1 --port 8000
```

You can now call the API using any OpenAI-compatible client, securely bridging out via your local CLI proxies.
