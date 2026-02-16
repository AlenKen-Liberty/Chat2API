# Chat2API

OpenAI-compatible API wrapper for Perplexity AI and other chat interfaces.

## Features

- ✅ **OpenAI-Compatible API**: Drop-in replacement for OpenAI API
- ✅ **Perplexity Pro Support**: Direct integration with Perplexity AI
- ✅ **Streaming Responses**: Real-time streaming chat completions
- ✅ **Cookie-Based Auth**: No API keys needed, uses browser cookies
- ✅ **Fast & Lightweight**: Direct HTTP calls, no browser overhead

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Setup Cookies

You need to obtain your Perplexity session cookies:

1. Open Perplexity.ai in your browser and log in
2. Open Developer Tools (F12) → Application/Storage → Cookies
3. Copy the cookies and save them as `cookies.json` in the project root:

```json
[
  {
    "name": "cookie_name",
    "value": "cookie_value",
    "domain": ".perplexity.ai"
  }
]
```

**Note**: See [SECURITY.md](SECURITY.md) for important security information about cookies.

### 3. Configure Environment (Optional)

```bash
cp .env.example .env
# Edit .env if needed (default settings work for most cases)
```

### 3. Start the Server

```bash
python3 -m chat2api.server
# or
uvicorn chat2api.server:app --host 0.0.0.0 --port 8000
```

### 4. Test the API

```bash
# List models
curl http://localhost:8000/v1/models

# Chat completion (non-streaming)
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "pplx-pro",
    "messages": [{"role": "user", "content": "What is AI?"}],
    "stream": false
  }'

# Chat completion (streaming)
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "pplx-pro",
    "messages": [{"role": "user", "content": "What is AI?"}],
    "stream": true
  }'
```

## API Endpoints

### `GET /v1/models`
List available models

### `POST /v1/chat/completions`
Create a chat completion (OpenAI-compatible)

**Request Body:**
```json
{
  "model": "pplx-pro",
  "messages": [
    {"role": "user", "content": "Your question here"}
  ],
  "stream": false
}
```

**Supported Models:**
- `pplx-pro` - Perplexity Pro model
- `pplx-turbo` - Perplexity Turbo model
- `gpt-4` - Maps to pplx-pro (for compatibility)
- `gpt-3.5-turbo` - Maps to pplx-turbo (for compatibility)

## Usage with OpenAI Client

```python
from openai import OpenAI

# Point to your local Chat2API server
client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="dummy"  # Not used, but required by OpenAI client
)

# Use like normal OpenAI API
response = client.chat.completions.create(
    model="pplx-pro",
    messages=[
        {"role": "user", "content": "What is the capital of France?"}
    ]
)

print(response.choices[0].message.content)
```

## Project Structure

```
Chat2API/
├── chat2api/
│   ├── __init__.py
│   ├── server.py              # FastAPI server
│   └── providers/
│       ├── __init__.py
│       └── perplexity.py      # Perplexity client
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
├── SECURITY.md
├── CONTRIBUTING.md
├── LICENSE
├── test_api.py               # API test suite
└── test_openai_client.py     # OpenAI client example
```

## Configuration

Edit `.env` file (or copy from `.env.example`):

```bash
COOKIES_FILE=cookies.json
HOST=0.0.0.0
PORT=8000
LOG_LEVEL=info
```

## Integration with OpenClaw

Configure OpenClaw to use Chat2API as a model provider:

```bash
openclaw model set-primary http://localhost:8000/v1 pplx-pro
```

## Development

### Running in Development Mode

```bash
uvicorn chat2api.server:app --reload --host 0.0.0.0 --port 8000
```

### Testing

```bash
# Test with curl
curl http://localhost:8000/health

# Test models endpoint
curl http://localhost:8000/v1/models

# Test chat completion
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "pplx-pro", "messages": [{"role": "user", "content": "Hello"}]}'
```

## License

MIT

## Credits

Built with:
- FastAPI - Modern web framework
- httpx - HTTP client
- Patchright - Stealth browser automation (POC only)
