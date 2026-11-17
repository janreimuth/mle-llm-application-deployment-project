# Deployment Report: Containerized LLM Service

**Date:** 17 November 2026
**Service:** <https://mle-llm-application-deployment-project.onrender.com>
**Repository:** `janreimuth/mle-llm-application-deployment-project`, deployed commit `b8e552a`
**Configuration:** Render Free web service (Docker), provider Groq, model `openai/gpt-oss-20b`, `MAX_CONCURRENT_REQUESTS=4`, `REQUEST_TIMEOUT_SECONDS=30`, `CACHE_TTL_SECONDS=300`

## Recommendation

**No-go for production in the current configuration.**

The service itself is sound. Its contracts are typed and tested, provider calls are asynchronous, bounded and replaceable, the cache works on the deployed instance, the same container runs locally and on Render, and secrets stay out of the repository. It fails the supplied workload for one dominant reason that lies outside the code: the free Groq tier allows 8,000 tokens per minute for `gpt-oss-20b`, which is roughly 8 to 9 requests per minute at about 920 tokens each. The load profile sends 40 requests at a concurrency of 5, which needs about 37,000 tokens, more than four minutes of quota. In the load run, 32 of 40 requests failed (80% against a 2% budget), and mean answer quality was 0.56 against a 0.80 objective.

The project is restricted to the free tier, so this verdict stands as final for that tier. The service is acceptable as a demo or staging deployment for low, sequential traffic of a few requests per minute. The conditions that would change the verdict are listed at the end of this report.

## Architecture

A FastAPI application (`app.py`) exposes `GET /health`, `GET /ready` and `POST /generate`. Requests are validated by Pydantic models (`models.py`) and handled by `GenerationService` (`service.py`), which checks an exact-match TTL cache, then calls the provider under an `asyncio.Semaphore` with a deadline that includes queueing time. Provider timeouts map to HTTP 504 and other provider failures to HTTP 502, with internal details kept out of the response. `ChatProvider` (`provider.py`) wraps Groq through LangChain and rejects truncated or empty answers rather than returning partial text. A deterministic `DryProvider` serves local checks without a network.

The image is built from `python:3.13.17-slim` with uv 0.13.5 and the locked dependency set, and runs as a non-root user. Render builds the same `Dockerfile`, with `.env`, `.venv` and `.git` excluded by `.dockerignore`.

## Acceptance Checklist

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | API input and output contracts are typed and tested | Met | `models.py`; 40 pytest tests, including 9 invalid payloads rejected with 422 and boundary values accepted |
| 2 | Provider calls are asynchronous, bounded and replaceable in tests | Met | Provider injected through `create_app(service=...)`; tests prove in-flight calls never exceed the bound, calls run in parallel up to it, and queue time counts toward the deadline |
| 3 | Repeated identical requests demonstrate a cache hit | Met | Unit tests; on Render the repeated refund case returned `cache_hit=False` then `cache_hit=True` (`reports/render_functional_v2.txt`) |
| 4 | `/health` returns 200 from the running container, locally and on Render | Met | Local `curl` against Docker Compose; `reports/render_health.txt` (`HTTP/2 200`) |
| 5 | The same container runs as a Free Render web service | Met | Render deploy log building the repository `Dockerfile`; `plan: free` in `render.yaml` |
| 6 | Render secrets are not committed | Met | `GROQ_API_KEY` declared with `sync: false`; `.env` gitignored and untracked |
| 7 | Observations include nested stages, usage, cost, release and quality | Met (synthetic) | Langfuse traces, see Observability |
| 8 | All shared evaluation cases run without editing their labels | Met | All five cases, including the repeat, ran unchanged from `data/evaluation/cases.json` |
| 9 | Load evidence is compared with all supplied objectives | Met | See Load Test |
| 10 | The final report makes an explicit deployment recommendation | Met | This document |

## Tests

The suite contains 40 tests in four files and runs without network access or API keys. `test_contracts.py` covers the response schema, defaults, whitespace stripping, field bounds and cost estimation. `test_service.py` covers the concurrency bound, parallelism, timeouts (504), provider failures (502) and blank answers. `test_cache.py` covers cache hits, misses on changed parameters, TTL zero, expiry and eviction. `test_provider.py` covers provider selection, startup failure without an API key, normalization of live responses, rejection of truncated and empty answers, and the Groq reasoning setting.

To confirm the tests detect regressions, the semaphore, the cache write and the timeout were each removed from `service.py` in turn. Each removal caused the corresponding tests to fail.

## Functional Evaluation

The five shared cases were run against the deployed service with `scripts/probe.py`, which scores each answer as the share of expected terms it contains (case-insensitive). Labels were not edited. A case passes when every expected term appears.

| Case | Run 1 (before fix) | Run 2 (after fix) | Missing terms in run 2 |
|---|---|---|---|
| `refund_supported` #1 | 200, cache hit, 0.00 | 200, miss, 1.00 | none |
| `refund_supported` #2 | 200, cache hit, 0.00 | 200, hit, 1.00 | none |
| `account_access` | 502 | 200, 1.00 | none |
| `privacy_handoff` | 502 | 200, 0.50 | "human review" |
| `incident_status` | 502 | 502 | (request failed) |
| `unsupported_policy` | 200, 0.00 | 200, 0.00 | "cannot confirm", "approved policy" |
| **Passed** | **0 of 6** | **3 of 6** | |

In run 1, both refund attempts were cache hits because a manual request with the same prompt had been sent minutes earlier, and the cached answer was a clarifying question rather than an answer. The three 502s were logged as `Provider returned an incomplete answer`: `gpt-oss-20b` spent its 1,024-token budget on reasoning before finishing. Setting `reasoning_effort="low"` for Groq (commit `b8e552a`) cut functional failures from three to one. Run 2 also shows the cache working as designed, with a miss followed by a hit.

Mean quality across the five successful answers in run 2 is 0.70, below the 0.80 objective. The two quality misses share one cause: the service sends the user's prompt with no system prompt or support policies, so the model has no basis for routing privacy requests to human review or refusing unapproved promises.

## Load Test

The load profile (`data/operations/load_profile.json`) specifies 40 HTTP attempts with up to 5 in flight. It was run against Render with `scripts/probe.py --only load` after a warm-up request. Each attempt cycles through the case prompts with a unique suffix, so no request is served from the cache and every attempt reaches the provider. Results were kept separate from the functional runs (`reports/render_load.txt`, `data/generated/load_results.json`).

| Objective | Target | Result | Verdict |
|---|---|---|---|
| p95 latency | ≤ 2,000 ms | 1,491.7 ms | Met, weak evidence |
| Error rate | ≤ 2% | 80.0% (32 of 40) | Missed |
| Mean quality | ≥ 0.80 | 0.56 (8 answers) | Missed |

Wall time was 4.19 s. All 40 attempts returned HTTP 200 or 502, with two distinct failure patterns:

| Attempts | Outcome | Latency | Cause |
|---|---|---|---|
| 0–12 | 8 successes, 5 failures (3, 6, 8, 11, 12) | 0.2–1.9 s | Truncated answers on open-ended prompts, consistent with the earlier logged `incomplete answer` errors |
| 13–39 | 27 failures | 0.08–0.3 s | Groq `429 rate_limit_exceeded`: "tokens per minute (TPM): Limit 8000, Used 7875, Requested 920" |

The fast failures came back before any generation could have run, which matches the rate-limit error confirmed in the Render logs. The functional run just before the load run had already consumed part of the same minute's quota.

The p95 figure needs care. It is computed over all 40 attempts, including the failures that returned in milliseconds. It happens to equal the slowest of the 8 successful requests, so successful requests did stay under 2 s, but 8 samples are too few to support a p95 claim. The quality figure rests on the same 8 answers.

The earlier local smoke run with `scripts/load_test.py` (139.8 requests per second, p95 9.06 ms, 0% errors) is excluded as evidence. Its sub-10 ms latencies indicate the dry provider, and because that script repeats three prompts, nearly all of its requests were served from the cache.

## Observability

Traces were recorded with `deployment_project.cli run-demo` and exported to Langfuse Cloud with SDK 5.2.0, using release `v1.0.0` and environment `development`. Each `support-request` trace contains nested `retrieve-context`, `generate-answer` and `llm-generation` observations. The generation carries token usage and cost (for example 32 tokens and $0.000013), and successful answers carry a `quality` score (0.90 healthy, 0.35 degraded). The error trace is marked ERROR and has no quality score, since only successful answers are scored.

These traces are synthetic. `pipeline.py` generates them with fixed latencies, and `retrieve-context` does not exist in the deployed service. Langfuse shows durations of 0.00 s because the demo records the traces instantly; the simulated latencies are stored in metadata. Local analysis of the same 10 traces (`cli diagnose`) reported a 10.0% error rate, low quality on 2 of 9 successful responses and a p95 of 1,160 ms, matching the generator's design. The deployed `/generate` endpoint does not emit traces.

<!-- Insert screenshots: trace list with release and scores; one opened trace showing the tree and the llm-generation detail. -->
![Langfuse trace list](screenshots/langfuse_traces.png)
![Langfuse trace detail](screenshots/langfuse_trace_detail.png)

## Incidents During Deployment

Four problems surfaced while deploying, each found through logs rather than guesswork. Locally, the container exited at startup because `GROQ_API_KEY` was missing; the fail-fast check worked as intended, and the key was added to `.env`. On Render, the first deployment ran with `MODEL_PROVIDER=dry`, detected through `/ready` and corrected in the service environment. On Groq, half the functional requests were truncated, fixed by lowering reasoning effort, with the finish reason now included in the error message. Under load, the free tier's token limit rejected most requests, which is documented above and not fixed.

## Known Limitations

Rate-limit errors from Groq are returned as 502 "provider unavailable" instead of 503 with a `Retry-After` header, which hides the cause from clients and prevents sensible retries. Open-ended prompts can still exceed the default `max_tokens` of 1,024. The service has no system prompt or policy grounding, which explains the quality misses. The cache is process-local, so it empties on every restart and whenever the free service sleeps, and concurrent identical requests both miss. Free Render services sleep after inactivity, so the first request after a pause can take tens of seconds. Live cost tracking in the API depends on `INPUT_USD_PER_MILLION_TOKENS` and `OUTPUT_USD_PER_MILLION_TOKENS`; these were not set for the recorded runs, so `estimated_cost_usd` reads 0. Finally, gpt-oss counts reasoning as output tokens, so a two-sentence answer was billed as 234 output tokens.

## What Would Change the Verdict

The decision would move to a go if three conditions were met. First, provider capacity must cover the workload: about 37,000 tokens per minute for this profile, through a higher Groq tier or a provider with higher limits, confirmed by a load run with an error rate at or below 2%. Second, answer quality must reach 0.80 on the shared cases, most plausibly through a system prompt containing the actual support policies; because such a prompt would be written with the expected terms known, the evaluation should be extended with new, unseen cases before the result is trusted. Third, rate-limit responses should return 503 with `Retry-After`, and truncation should be eliminated through concise-answer instructions or a larger token budget, with the token cost of either choice measured against the provider limit.

## Reproduction

```bash
uv run pytest
docker compose up -d --build
curl -i http://127.0.0.1:8000/health

export URL=https://mle-llm-application-deployment-project.onrender.com
uv run python scripts/probe.py --base-url $URL --only functional
curl -s $URL/health > /dev/null
uv run python scripts/probe.py --base-url $URL --only load

PYTHONPATH=src uv run python -m deployment_project.cli run-demo --sink langfuse --count 10
```

Leave at least one minute between the functional and load runs so they do not share a rate-limit window.
