"""Exact-match TTL cache, through the API and directly."""

from conftest import FakeProvider

from deployment_project import cache as cache_module
from deployment_project.cache import TTLCache
from deployment_project.models import GenerateRequest, ProviderResult, TokenUsage


def test_repeated_identical_request_is_a_cache_hit(make_client):
    client, provider = make_client(
        input_usd_per_million=1.0, output_usd_per_million=1.0
    )
    payload = {"prompt": "What evidence is required for a duplicate-payment refund?"}

    first = client.post("/generate", json=payload).json()
    second = client.post("/generate", json=payload).json()

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert len(provider.requests) == 1
    assert second["text"] == first["text"]
    assert second["usage"] == first["usage"]
    assert first["estimated_cost_usd"] > 0
    assert second["estimated_cost_usd"] == 0


def test_changed_generation_parameters_miss_the_cache(make_client):
    client, provider = make_client()
    client.post("/generate", json={"prompt": "same"})
    client.post("/generate", json={"prompt": "same", "temperature": 0.9})
    client.post("/generate", json={"prompt": "same", "max_tokens": 10})
    assert len(provider.requests) == 3


def test_ttl_zero_disables_caching(make_client):
    client, provider = make_client(cache_ttl_seconds=0)
    for _ in range(2):
        body = client.post("/generate", json={"prompt": "x"}).json()
        assert body["cache_hit"] is False
    assert len(provider.requests) == 2


def test_failed_generation_is_not_cached(make_client):
    provider = FakeProvider(error=RuntimeError("down"))
    client, _ = make_client(provider=provider)
    client.post("/generate", json={"prompt": "x"})
    provider.error = None
    body = client.post("/generate", json={"prompt": "x"}).json()
    assert body["cache_hit"] is False


def _result() -> ProviderResult:
    return ProviderResult(
        text="t", model="m", usage=TokenUsage(input_tokens=1, output_tokens=1)
    )


def test_entries_expire_after_ttl(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: now[0])
    cache = TTLCache(ttl_seconds=10)
    key = TTLCache.key(GenerateRequest(prompt="x"), "m")
    cache.set(key, _result())
    now[0] += 9.9
    assert cache.get(key) is not None
    now[0] += 0.1
    assert cache.get(key) is None


def test_oldest_entry_is_evicted_at_capacity():
    cache = TTLCache(ttl_seconds=60, max_entries=2)
    cache.set("a", _result())
    cache.set("b", _result())
    cache.set("c", _result())
    assert cache.get("a") is None
    assert cache.get("b") is not None and cache.get("c") is not None


def test_key_includes_model():
    request = GenerateRequest(prompt="x")
    assert TTLCache.key(request, "model-a") != TTLCache.key(request, "model-b")
