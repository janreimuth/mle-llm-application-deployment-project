"""FastAPI entry point for the containerized LLM service."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from groq import APITimeoutError
from httpx import TimeoutException

from deployment_project.config import Settings, settings
from deployment_project.models import GenerateRequest, GenerateResponse
from deployment_project.provider import build_provider
from deployment_project.service import GenerationService

logger = logging.getLogger(__name__)


def get_service(request: Request) -> GenerationService:
    """Return the service attached to this application instance."""
    return request.app.state.service


ServiceDep = Annotated[GenerationService, Depends(get_service)]


def create_app(
    service: GenerationService | None = None,
    config: Settings = settings,
) -> FastAPI:
    """Build the API around an injected service, or the configured one.

    Tests pass a service wrapping a fake provider. In production no service is
    passed, so the lifespan builds it from configuration at startup; a missing
    API key still stops the container before it accepts traffic.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "service", None) is None:
            app.state.service = GenerationService(build_provider(config), config)
        yield

    app = FastAPI(title="Containerized LLM Service", version="1.0.0", lifespan=lifespan)
    app.state.service = service

    @app.get("/")
    def root() -> dict[str, str]:
        """Confirm that the service is reachable and point to the real routes."""
        return {
            "message": "Containerized LLM service is running. "
            "Use GET /health, GET /ready, or POST /generate."
        }

    @app.get("/health")
    def health() -> dict[str, str]:
        """Confirm that the process and HTTP router are alive."""
        return {"status": "healthy"}

    @app.get("/ready")
    def ready(service: ServiceDep) -> dict[str, str]:
        """Expose configuration readiness without calling the provider."""
        return {
            "status": "ready",
            "provider": service.settings.provider,
            "model": service.settings.model,
        }

    @app.post("/generate", response_model=GenerateResponse)
    async def generate(
        request: GenerateRequest, service: ServiceDep
    ) -> GenerateResponse:
        """Generate text with bounded concurrency and exact-match caching."""
        try:
            return await service.generate(request)
        except (TimeoutError, APITimeoutError, TimeoutException) as exc:
            raise HTTPException(
                status_code=504, detail="Generation request timed out"
            ) from exc
        except Exception as exc:
            logger.exception("Generation provider failed")
            raise HTTPException(
                status_code=502,
                detail="Generation provider unavailable",
            ) from exc

    return app


app = create_app()
