"""
Chat2API - FastAPI Server
OpenAI-compatible API wrapper for Perplexity
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any, Literal
import json
import time
import os
from pathlib import Path

from chat2api.providers.perplexity import PerplexityClient, CookieAuthError

app = FastAPI(
    title="Chat2API",
    description="OpenAI-compatible API wrapper for Perplexity AI",
    version="0.1.0"
)

# Load cookies from environment or file
COOKIES_FILE_PATH = os.getenv('COOKIES_FILE', 'cookies.json')
# Make it absolute if it's relative
if not os.path.isabs(COOKIES_FILE_PATH):
    # Get the project root directory (parent of chat2api package)
    PROJECT_ROOT = Path(__file__).parent.parent
    COOKIES_FILE = PROJECT_ROOT / COOKIES_FILE_PATH
else:
    COOKIES_FILE = Path(COOKIES_FILE_PATH)


# OpenAI-compatible request models
class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "pplx-pro"
    messages: List[Message]
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = 1.0
    n: Optional[int] = 1
    stream: Optional[bool] = False
    max_tokens: Optional[int] = None


# Health check
@app.get("/")
async def root():
    return {
        "name": "Chat2API",
        "version": "0.1.0",
        "status": "running",
        "providers": ["perplexity"]
    }


@app.get("/health")
async def health():
    return {"status": "healthy"}


# Models endpoint
@app.get("/v1/models")
async def list_models():
    """List available models (OpenAI-compatible)"""
    return {
        "object": "list",
        "data": [
            {
                "id": "pplx-pro",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "perplexity",
                "permission": [],
                "root": "pplx-pro",
                "parent": None,
            },
            {
                "id": "pplx-turbo",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "perplexity",
                "permission": [],
                "root": "pplx-turbo",
                "parent": None,
            }
        ]
    }


# Chat completions endpoint
@app.post("/v1/chat/completions")
async def create_chat_completion(request: ChatCompletionRequest):
    """
    Create a chat completion (OpenAI-compatible)
    Supports both streaming and non-streaming responses
    """
    try:
        # Extract the last user message as the query
        user_messages = [msg for msg in request.messages if msg.role == "user"]
        if not user_messages:
            raise HTTPException(status_code=400, detail="No user message found")
        
        query = user_messages[-1].content
        
        # Map model names
        model_map = {
            "pplx-pro": "pplx_pro",
            "pplx-turbo": "turbo",
            "gpt-4": "pplx_pro",  # Fallback mapping
            "gpt-3.5-turbo": "turbo"  # Fallback mapping
        }
        perplexity_model = model_map.get(request.model, "pplx_pro")
        
        # Initialize Perplexity client
        client = PerplexityClient(cookies_file=COOKIES_FILE)
        
        if request.stream:
            # Streaming response
            return StreamingResponse(
                stream_chat_completion(client, query, perplexity_model, request.model),
                media_type="text/event-stream"
            )
        else:
            # Non-streaming response
            return await non_streaming_chat_completion(client, query, perplexity_model, request.model)
    
    except CookieAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def stream_chat_completion(client: PerplexityClient, query: str, perplexity_model: str, model_name: str):
    """Generate streaming chat completion in OpenAI format"""
    try:
        chunks = client.ask(query, model=perplexity_model, stream=True)
        answer_chunks = client.extract_answer(chunks)
        
        # Generate unique ID
        completion_id = f"chatcmpl-{int(time.time())}"
        
        # Send chunks
        for text_chunk in answer_chunks:
            chunk_data = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "content": text_chunk
                        },
                        "finish_reason": None
                    }
                ]
            }
            yield f"data: {json.dumps(chunk_data)}\n\n"
        
        # Send final chunk
        final_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop"
                }
            ]
        }
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"
    
    except CookieAuthError as e:
        # Stream a terminal error in SSE format so clients get a clear action hint.
        err = {
            "error": {
                "message": str(e),
                "type": "authentication_error",
                "code": "cookie_invalid_or_missing"
            }
        }
        yield f"data: {json.dumps(err, ensure_ascii=False)}\\n\\n"
        yield "data: [DONE]\\n\\n"
    finally:
        client.close()


async def non_streaming_chat_completion(client: PerplexityClient, query: str, perplexity_model: str, model_name: str):
    """Generate non-streaming chat completion in OpenAI format"""
    try:
        chunks = client.ask(query, model=perplexity_model, stream=True)
        answer_chunks = client.extract_answer(chunks)
        
        # Collect all text
        full_answer = "".join(answer_chunks)
        
        # Return in OpenAI format
        return {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": full_answer
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
        }
    
    except CookieAuthError:
        raise
    finally:
        client.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
