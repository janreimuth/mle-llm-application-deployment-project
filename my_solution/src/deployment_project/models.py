"""Typed request, usage, and response contracts."""

from typing import Literal
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field


class GenerateRequest(BaseModel):
    """Generation parameters accepted by the public endpoint."""

    model_config = ConfigDict(str_strip_whitespace=True)

    prompt: str = Field(min_length=1, max_length=8000)
    max_tokens: int = Field(default=1024, ge=1, le=2048)
    temperature: float = Field(default=0.2, ge=0, le=2)


class TokenUsage(BaseModel):
    """Provider-independent token accounting."""

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class ProviderResult(BaseModel):
    """Normalized output returned by a provider implementation."""

    text: str
    model: str
    usage: TokenUsage


class GenerateResponse(BaseModel):
    """Observable response returned to API clients."""

    text: str
    model: str
    usage: TokenUsage
    cache_hit: bool
    latency_ms: float = Field(ge=0)
    estimated_cost_usd: float = Field(ge=0)
    

"""Auditable trace and summary contracts."""

class SpanRecord(BaseModel):
    """One timed stage within an LLM request."""

    model_config = ConfigDict(allow_inf_nan=False)

    name: str
    latency_ms: float = Field(ge=0)
    status: Literal["ok", "error"] = "ok"


class TraceRecord(BaseModel):
    """Provider-independent observability record for one request."""

    model_config = ConfigDict(allow_inf_nan=False)

    trace_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    environment: str
    release: str
    user_id: str
    session_id: str
    model: str
    prompt: str
    output: str
    latency_ms: float = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    status: Literal["ok", "error"]
    quality_score: float = Field(ge=0, le=1)
    spans: list[SpanRecord]
    tags: list[str] = Field(default_factory=list)


class MetricsSummary(BaseModel):
    """Small dashboard-ready aggregate over trace records."""

    requests: int
    error_rate: float
    total_tokens: int
    total_cost_usd: float
    quality_samples: int
    mean_quality: float | None
    p95_latency_ms: float
