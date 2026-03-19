from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from chat2api import __version__
from chat2api.config import get_settings
from chat2api.routing.admin import router as admin_router
from chat2api.routing.completions import router as completions_router
from chat2api.routing.models import router as models_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Chat2API",
        description="OpenAI-compatible bridge for Gemini CLI and Codex account pools",
        version=__version__,
    )

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
            "providers": ["gemini", "codex"],
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
