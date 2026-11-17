"""Typed input and output contracts of the public API."""

import pytest
from conftest import FakeProvider

from deployment_project.models import GenerateResponse, TokenUsage


def test_health_returns_200(make_client):
    client, _ = make_client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_ready_reports_injected_configuration(make_client):
    client, provider = make_client()
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "provider": "dry",
        "model": "test-model",
    }
    assert provider.requests == []  # readiness never calls the provider


def test_generate_returns_full_response_contract(make_client):
    client, _ = make_client()
    response = client.post("/generate", json={"prompt": "Hello"})
    assert response.status_code == 200
    body = GenerateResponse.model_validate(response.json())  # schema check
    assert set(response.json()) == set(GenerateResponse.model_fields)
    assert body.text == "Fake answer"
    assert body.model == "fake-model"
    assert body.usage.input_tokens == 10 and body.usage.output_tokens == 20
    assert body.cache_hit is False
    assert body.latency_ms >= 0


def test_defaults_and_whitespace_stripping_reach_the_provider(make_client):
    client, provider = make_client()
    client.post("/generate", json={"prompt": "  padded prompt  "})
    sent = provider.requests[0]
    assert sent.prompt == "padded prompt"
    assert sent.max_tokens == 1024
    assert sent.temperature == 0.2


def test_explicit_parameters_reach_the_provider(make_client):
    client, provider = make_client()
    client.post("/generate", json={"prompt": "x", "max_tokens": 64, "temperature": 1.5})
    assert provider.requests[0].max_tokens == 64
    assert provider.requests[0].temperature == 1.5


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"prompt": ""},
        {"prompt": "   "},
        {"prompt": "x" * 8001},
        {"prompt": 123},
        {"prompt": "x", "max_tokens": 0},
        {"prompt": "x", "max_tokens": 2049},
        {"prompt": "x", "temperature": -0.1},
        {"prompt": "x", "temperature": 2.1},
    ],
    ids=[
        "missing-prompt",
        "empty",
        "whitespace-only",
        "too-long",
        "wrong-type",
        "max-tokens-zero",
        "max-tokens-too-high",
        "temperature-negative",
        "temperature-too-high",
    ],
)
def test_invalid_requests_are_rejected_before_the_provider(make_client, payload):
    client, provider = make_client()
    response = client.post("/generate", json=payload)
    assert response.status_code == 422
    assert provider.requests == []


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": "x" * 8000, "max_tokens": 1, "temperature": 0},
        {"prompt": "x", "max_tokens": 2048, "temperature": 2},
    ],
    ids=["lower-bounds", "upper-bounds"],
)
def test_boundary_values_are_accepted(make_client, payload):
    client, _ = make_client()
    assert client.post("/generate", json=payload).status_code == 200


def test_estimated_cost_uses_configured_prices(make_client):
    provider = FakeProvider(usage=TokenUsage(input_tokens=1_000, output_tokens=2_000))
    client, _ = make_client(
        provider=provider, input_usd_per_million=0.5, output_usd_per_million=2.0
    )
    body = client.post("/generate", json={"prompt": "price me"}).json()
    # 1,000 * 0.5 / 1e6 + 2,000 * 2.0 / 1e6
    assert body["estimated_cost_usd"] == pytest.approx(0.0045)
