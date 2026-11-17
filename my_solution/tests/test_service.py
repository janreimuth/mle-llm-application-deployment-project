"""Asynchronous, bounded, replaceable provider calls and error mapping."""

import asyncio

from conftest import FakeProvider, make_settings

from deployment_project.models import GenerateRequest
from deployment_project.service import GenerationService


def test_concurrency_never_exceeds_the_configured_bound():
    provider = FakeProvider(delay=0.05)
    service = GenerationService(provider, make_settings(max_concurrent_requests=2))

    async def burst():
        # Distinct prompts so the cache cannot absorb any of the calls.
        await asyncio.gather(
            *(service.generate(GenerateRequest(prompt=f"p{i}")) for i in range(6))
        )

    asyncio.run(burst())
    assert len(provider.requests) == 6
    assert provider.max_in_flight == 2


def test_calls_run_concurrently_up_to_the_bound():
    provider = FakeProvider(delay=0.1)
    service = GenerationService(provider, make_settings(max_concurrent_requests=4))

    async def burst() -> float:
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.gather(
            *(service.generate(GenerateRequest(prompt=f"p{i}")) for i in range(4))
        )
        return loop.time() - started

    elapsed = asyncio.run(burst())
    assert provider.max_in_flight == 4
    assert elapsed < 0.3  # four 0.1 s calls in parallel, not 0.4 s in series


def test_slow_provider_returns_504(make_client):
    client, _ = make_client(
        provider=FakeProvider(delay=1.0), request_timeout_seconds=0.05
    )
    response = client.post("/generate", json={"prompt": "slow"})
    assert response.status_code == 504
    assert response.json() == {"detail": "Generation request timed out"}


def test_provider_timeout_error_returns_504(make_client):
    client, _ = make_client(provider=FakeProvider(error=TimeoutError("upstream")))
    assert client.post("/generate", json={"prompt": "x"}).status_code == 504


def test_queue_time_counts_toward_the_deadline():
    provider = FakeProvider(delay=0.2)
    service = GenerationService(
        provider,
        make_settings(max_concurrent_requests=1, request_timeout_seconds=0.3),
    )

    async def two_calls():
        return await asyncio.gather(
            service.generate(GenerateRequest(prompt="first")),
            service.generate(GenerateRequest(prompt="second")),
            return_exceptions=True,
        )

    first, second = asyncio.run(two_calls())
    assert not isinstance(first, BaseException)
    # The second call waits 0.2 s for the slot, then needs 0.2 s more: over 0.3 s.
    assert isinstance(second, TimeoutError)


def test_provider_failure_returns_502_without_leaking_details(make_client):
    client, _ = make_client(provider=FakeProvider(error=RuntimeError("secret-key-xyz")))
    response = client.post("/generate", json={"prompt": "x"})
    assert response.status_code == 502
    assert response.json() == {"detail": "Generation provider unavailable"}
    assert "secret" not in response.text


def test_blank_provider_answer_returns_502_and_is_not_cached(make_client):
    provider = FakeProvider(text="   ")
    client, _ = make_client(provider=provider)
    assert client.post("/generate", json={"prompt": "x"}).status_code == 502
    assert client.post("/generate", json={"prompt": "x"}).status_code == 502
    assert len(provider.requests) == 2
