"""Provider selection, startup failure, and live-response normalization."""

import asyncio

import pytest
from conftest import make_settings
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from deployment_project.app import create_app
from deployment_project.models import GenerateRequest
from deployment_project.provider import ChatProvider, DryProvider, build_provider


def test_dry_provider_is_selected_and_deterministic():
    provider = build_provider(make_settings(provider="dry"))
    assert isinstance(provider, DryProvider)
    request = GenerateRequest(prompt="hello world")
    first = asyncio.run(provider.generate(request))
    second = asyncio.run(provider.generate(request))
    assert first == second
    assert first.model == "dry:test-model"


def test_unsupported_provider_is_rejected():
    with pytest.raises(ValueError, match="Unsupported MODEL_PROVIDER"):
        build_provider(make_settings(provider="nope"))


def test_live_provider_without_key_is_rejected():
    with pytest.raises(ValueError, match="GROQ_API_KEY is required"):
        build_provider(make_settings(provider="groq", groq_api_key=""))


def test_app_refuses_to_start_without_a_key():
    app = create_app(config=make_settings(provider="groq", groq_api_key=""))
    # Entering the client runs the startup lifespan.
    with pytest.raises(ValueError, match="GROQ_API_KEY is required"), TestClient(app):
        pass


def test_app_builds_configured_service_at_startup():
    app = create_app(config=make_settings(provider="dry"))
    with TestClient(app) as client:
        body = client.post("/generate", json={"prompt": "hi"}).json()
    assert body["model"] == "dry:test-model"


class FakeChatModel:
    def __init__(self, message: AIMessage) -> None:
        self.message = message
        self.kwargs: dict = {}

    async def ainvoke(self, messages, **kwargs):
        self.kwargs = kwargs
        return self.message


def chat_provider(message: AIMessage) -> ChatProvider:
    """A ChatProvider whose network client is replaced by a fake."""
    provider = ChatProvider.__new__(ChatProvider)
    provider.model = "configured-model"
    provider.token_limit_key = "max_tokens"
    provider.client = FakeChatModel(message)
    return provider


def test_chat_provider_normalizes_a_complete_answer():
    message = AIMessage(
        content="Answer",
        response_metadata={"finish_reason": "stop", "model_name": "served-model"},
        usage_metadata={"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
    )
    provider = chat_provider(message)
    result = asyncio.run(
        provider.generate(GenerateRequest(prompt="q", max_tokens=99, temperature=0.4))
    )
    assert result.text == "Answer"
    assert result.model == "served-model"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (3, 5)
    assert provider.client.kwargs == {"temperature": 0.4, "max_tokens": 99}


def test_chat_provider_rejects_truncated_answers():
    message = AIMessage(
        content="Partial", response_metadata={"finish_reason": "length"}
    )
    with pytest.raises(ValueError, match=r"incomplete.*finish_reason=length"):
        asyncio.run(chat_provider(message).generate(GenerateRequest(prompt="q")))


def test_chat_provider_rejects_empty_answers():
    message = AIMessage(content="  ", response_metadata={"finish_reason": "stop"})
    with pytest.raises(ValueError, match="no answer"):
        asyncio.run(chat_provider(message).generate(GenerateRequest(prompt="q")))


def test_groq_provider_requests_low_reasoning_effort():
    pytest.importorskip("langchain_groq")
    provider = ChatProvider(
        make_settings(
            provider="groq", groq_api_key="test-key", model="openai/gpt-oss-20b"
        )
    )
    # Constructing the client makes no network call.
    assert provider.client.reasoning_effort == "low"
    assert provider.token_limit_key == "max_tokens"
