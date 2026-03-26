from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from chat2api import __version__
from chat2api.config import get_settings
from chat2api.routing.admin import router as admin_router
from chat2api.routing.completions import router as completions_router
from chat2api.routing.models import router as models_router
from chat2api.usage_logger import init_usage_log


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Chat2API",
        description="OpenAI-compatible LLM pool: Gemini, Codex, Copilot, Groq and more",
        version=__version__,
    )

    # Initialize usage logging
    init_usage_log("logs/usage.jsonl")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(models_router)
    app.include_router(completions_router)
    app.include_router(admin_router)

    @app.get("/")
    async def root() -> dict:
        settings = get_settings()
        return {
            "name": "Chat2API",
            "version": __version__,
            "status": "running",
            "host": settings.server.host,
            "port": settings.server.port,
            "providers": list(settings.providers.keys()),
        }

    @app.get("/health")
    async def health() -> dict:
        return {"status": "healthy"}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host=settings.server.host, port=settings.server.port)
