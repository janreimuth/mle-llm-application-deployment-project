"""Shared fakes and fixtures. No test calls a real provider."""

import asyncio
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from deployment_project.app import create_app
from deployment_project.config import Settings
from deployment_project.models import GenerateRequest, ProviderResult, TokenUsage
from deployment_project.service import GenerationService


class FakeProvider:
    """Records calls and in-flight concurrency; can be slowed down or broken."""

    def __init__(
        self,
        text: str = "Fake answer",
        delay: float = 0.0,
        error: Exception | None = None,
        usage: TokenUsage | None = None,
    ) -> None:
        self.text = text
        self.delay = delay
        self.error = error
        self.usage = usage or TokenUsage(input_tokens=10, output_tokens=20)
        self.requests: list[GenerateRequest] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def generate(self, request: GenerateRequest) -> ProviderResult:
        self.requests.append(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return ProviderResult(text=self.text, model="fake-model", usage=self.usage)
        finally:
            self.in_flight -= 1


def make_settings(**overrides) -> Settings:
    """Settings independent of the developer's environment and .env file."""
    values = {
        "provider": "dry",
        "model": "test-model",
        "groq_api_key": "",
        "cache_ttl_seconds": 300,
        "max_concurrent_requests": 4,
        "request_timeout_seconds": 5.0,
        "input_usd_per_million": 0.0,
        "output_usd_per_million": 0.0,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def make_client() -> Callable[..., tuple[TestClient, FakeProvider]]:
    """Build a client around a fake provider: make_client(provider=..., **settings)."""

    def build(provider: FakeProvider | None = None, **overrides):
        provider = provider or FakeProvider()
        service = GenerationService(provider, make_settings(**overrides))
        return TestClient(create_app(service=service)), provider

    return build
