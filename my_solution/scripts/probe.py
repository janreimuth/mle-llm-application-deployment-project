"""Run the functional cases and the load profile against the API, separately.

Usage (from the repository root, with the API already running):
    python scripts/probe.py --base-url http://127.0.0.1:8000
    python scripts/probe.py --base-url URL \
        --cases data/evaluation/cases.json \
        --load-profile data/operations/load_profile.json

Functional results and load results go to two different files and are
never mixed: the functional cases check behavior, the load run checks the
service under the workload in load_profile.json.
"""

import argparse
import asyncio
import json
import math
import time
from pathlib import Path

import httpx

CASES = Path("data/evaluation/cases.json")
PROFILE = Path("data/operations/load_profile.json")
OUT = Path("data/generated")


def quality(text: str, terms: list[str]) -> float:
    """Share of expected terms present in the answer (case-insensitive)."""
    lowered = text.lower()
    return sum(term.lower() in lowered for term in terms) / len(terms)


async def call(client: httpx.AsyncClient, prompt: str) -> dict:
    started = time.perf_counter()
    try:
        response = await client.post("/generate", json={"prompt": prompt})
        body = (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        error = (
            None
            if response.status_code == 200
            else body.get("detail", response.text[:200])
        )
        status = response.status_code
    except httpx.HTTPError as exc:
        body, status, error = {}, None, f"{type(exc).__name__}: {exc}"
    return {
        "http_status": status,
        "ok": status == 200,
        "error": error,
        "client_latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "server_latency_ms": body.get("latency_ms"),
        "cache_hit": body.get("cache_hit"),
        "model": body.get("model"),
        "text": body.get("text", ""),
    }


async def run_functional(client: httpx.AsyncClient, cases: list[dict]) -> list[dict]:
    """Run each case sequentially, honouring its repeat count."""
    results = []
    for case in cases:
        for attempt in range(1, case.get("repeat", 1) + 1):
            result = await call(client, case["prompt"])
            missing = [
                t
                for t in case["expected_terms"]
                if t.lower() not in result["text"].lower()
            ]
            result.update(
                case_id=case["case_id"],
                attempt=attempt,
                quality_score=quality(result["text"], case["expected_terms"])
                if result["ok"]
                else None,
                missing_terms=missing if result["ok"] else case["expected_terms"],
                passed=result["ok"] and not missing,
            )
            results.append(result)
    return results


async def run_load(
    client: httpx.AsyncClient, cases: list[dict], profile: dict, unique: bool
) -> dict:
    """Fire `requests` attempts with at most `concurrency` in flight."""
    gate = asyncio.Semaphore(profile["concurrency"])

    async def one(index: int) -> dict:
        case = cases[index % len(cases)]
        # A unique suffix keeps every attempt off the exact-match cache, so the
        # run measures the provider path rather than cache lookups.
        prompt = (
            f"{case['prompt']} (load request {index})" if unique else case["prompt"]
        )
        async with gate:
            result = await call(client, prompt)
        result.update(index=index, case_id=case["case_id"])
        result["quality_score"] = (
            quality(result["text"], case["expected_terms"]) if result["ok"] else None
        )
        return result

    started = time.perf_counter()
    attempts = await asyncio.gather(*(one(i) for i in range(profile["requests"])))
    wall = time.perf_counter() - started

    latencies = sorted(a["client_latency_ms"] for a in attempts)
    p95 = latencies[math.ceil(len(latencies) * 0.95) - 1]
    errors = sum(not a["ok"] for a in attempts)
    scores = [a["quality_score"] for a in attempts if a["ok"]]
    mean_quality = sum(scores) / len(scores) if scores else None
    slo = profile["service_objectives"]
    return {
        "profile": profile,
        "cache_busting": unique,
        "summary": {
            "attempts": len(attempts),
            "errors": errors,
            "error_rate": errors / len(attempts),
            "p95_latency_ms": p95,
            "mean_quality": mean_quality,
            "cache_hits": sum(bool(a["cache_hit"]) for a in attempts),
            "wall_seconds": round(wall, 2),
            "throughput_rps": round(len(attempts) / wall, 2),
        },
        "objectives_met": {
            "p95_latency_ms": p95 <= slo["p95_latency_ms"],
            "max_error_rate": errors / len(attempts) <= slo["max_error_rate"],
            "min_quality_score": mean_quality is not None
            and mean_quality >= slo["min_quality_score"],
        },
        "attempts": attempts,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--allow-cache",
        action="store_true",
        help="Reuse case prompts verbatim in the load run",
    )
    parser.add_argument("--only", choices=["functional", "load"])
    parser.add_argument(
        "--cases", type=Path, default=CASES, help="Evaluation cases (read-only)"
    )
    parser.add_argument(
        "--load-profile", type=Path, default=PROFILE, help="Load profile (read-only)"
    )
    args = parser.parse_args()

    cases = json.loads(args.cases.read_text())
    profile = json.loads(args.load_profile.read_text())
    OUT.mkdir(parents=True, exist_ok=True)
    timeout = httpx.Timeout(60.0)

    async with httpx.AsyncClient(base_url=args.base_url, timeout=timeout) as client:
        ready = (await client.get("/ready")).json()
        print(
            f"Target: {args.base_url}  provider={ready['provider']}  model={ready['model']}\n"
        )

        if args.only in (None, "functional"):
            functional = await run_functional(client, cases)
            (OUT / "functional_results.json").write_text(
                json.dumps(functional, indent=2)
            )
            print("FUNCTIONAL CASES")
            for r in functional:
                mark = "PASS" if r["passed"] else "FAIL"
                q = "-" if r["quality_score"] is None else f"{r['quality_score']:.2f}"
                detail = r["error"] or (
                    f"missing {r['missing_terms']}" if r["missing_terms"] else ""
                )
                print(
                    f"  {mark}  {r['case_id']:<20} #{r['attempt']}  http={r['http_status']}  "
                    f"cache_hit={r['cache_hit']}  quality={q}  {detail}"
                )
            print(
                f"  -> {sum(r['passed'] for r in functional)}/{len(functional)} passed\n"
            )

        if args.only in (None, "load"):
            load = await run_load(client, cases, profile, unique=not args.allow_cache)
            (OUT / "load_results.json").write_text(json.dumps(load, indent=2))
            s, met = load["summary"], load["objectives_met"]
            slo = profile["service_objectives"]
            print(
                f"LOAD RUN ({profile['requests']} attempts, concurrency {profile['concurrency']})"
            )
            print(
                f"  p95 latency  {s['p95_latency_ms']:.1f} ms   (objective <= {slo['p95_latency_ms']})  {'met' if met['p95_latency_ms'] else 'MISSED'}"
            )
            print(
                f"  error rate   {s['error_rate']:.1%}       (objective <= {slo['max_error_rate']:.0%})  {'met' if met['max_error_rate'] else 'MISSED'}"
            )
            mq = "n/a" if s["mean_quality"] is None else f"{s['mean_quality']:.2f}"
            print(
                f"  mean quality {mq}        (objective >= {slo['min_quality_score']})  {'met' if met['min_quality_score'] else 'MISSED'}"
            )
            print(
                f"  cache hits {s['cache_hits']}, wall {s['wall_seconds']} s, {s['throughput_rps']} req/s"
            )


if __name__ == "__main__":
    asyncio.run(main())
