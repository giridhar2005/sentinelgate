"""Real HTTP Benchmark Harness for SentinelGate.

Measures:
1. Raw proxy overhead (Direct Mock Provider vs SentinelGate Gateway Proxy).
2. Semantic cache hit latency across concurrency levels (c=1, c=10, c=50).
3. Agent firewall pre-execution inspection overhead per call.

Spins up an in-process Uvicorn gateway and mock upstream provider,
measures latency distributions via httpx.AsyncClient with connection pooling,
and exports machine-readable JSON to benchmarks/results.json.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx
import uvicorn

from sentinelgate.main import create_app
from sentinelgate.services.agent_guard import AgentToolGuard


def get_git_commit() -> str:
    """Retrieve current git commit hash."""
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL)
        return out.decode("utf-8").strip()
    except Exception:
        return "unknown"


def find_free_port() -> int:
    """Find an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def compute_stats(latencies_ms: List[float], wall_time_sec: float) -> Dict[str, float]:
    """Compute standard latency percentiles and RPS from a list of latencies."""
    if not latencies_ms:
        return {
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
            "min_ms": 0.0,
            "max_ms": 0.0,
            "avg_ms": 0.0,
            "rps": 0.0,
            "count": 0,
        }
    arr = np.array(latencies_ms)
    count = len(latencies_ms)
    rps = round(count / max(0.001, wall_time_sec), 1)
    return {
        "p50_ms": round(float(np.percentile(arr, 50)), 2),
        "p95_ms": round(float(np.percentile(arr, 95)), 2),
        "p99_ms": round(float(np.percentile(arr, 99)), 2),
        "min_ms": round(float(np.min(arr)), 2),
        "max_ms": round(float(np.max(arr)), 2),
        "avg_ms": round(float(np.mean(arr)), 2),
        "rps": rps,
        "count": count,
    }


# ---------------------------------------------------------
# Mock Upstream Provider (Instant OpenAI Responses)
# ---------------------------------------------------------
class MockUpstreamApp:
    """Ultra-lightweight ASGI app simulating an instant upstream LLM provider."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path in ("/health", "/"):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [[b"content-type", b"text/plain"]],
                    }
                )
                await send({"type": "http.response.body", "body": b"mock provider ready"})
                return

            body = json.dumps(
                {
                    "id": "chatcmpl-mock-bench",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "mock-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "Simulated instant response from mock provider.",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 22},
                }
            ).encode("utf-8")

            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        [b"content-type", b"application/json"],
                        [b"content-length", str(len(body)).encode("utf-8")],
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})


class BackgroundServer:
    """Manages an in-process Uvicorn server running in a daemon thread."""

    def __init__(self, app: Any, host: str, port: int):
        self.host = host
        self.port = port
        self.config = uvicorn.Config(
            app=app, host=host, port=port, log_level="error", access_log=False
        )
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=3.0)


async def wait_for_ready(url: str, timeout: float = 10.0) -> bool:
    """Poll an HTTP endpoint until it answers with 200."""
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=1.0) as client:
        while time.perf_counter() - t0 < timeout:
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.05)
    return False


# ---------------------------------------------------------
# Benchmarking Execution
# ---------------------------------------------------------
async def measure_direct_mock(
    client: httpx.AsyncClient, mock_url: str, num_requests: int
) -> Tuple[List[float], float]:
    """Measure raw latency directly against the mock upstream provider."""
    latencies: List[float] = []
    payload = {
        "model": "mock-model",
        "messages": [{"role": "user", "content": "benchmark direct test"}],
    }
    t_start = time.perf_counter()
    for _ in range(num_requests):
        t0 = time.perf_counter()
        resp = await client.post(f"{mock_url}/v1/chat/completions", json=payload)
        lat = (time.perf_counter() - t0) * 1000.0
        if resp.status_code == 200:
            latencies.append(lat)
    wall_time = time.perf_counter() - t_start
    return latencies, wall_time


async def measure_gateway_proxy(
    client: httpx.AsyncClient, gateway_url: str, headers: Dict[str, str], num_requests: int
) -> Tuple[List[float], float]:
    """Measure end-to-end proxy latency through SentinelGate on cache-miss requests."""
    latencies: List[float] = []
    t_start = time.perf_counter()
    for i in range(num_requests):
        payload = {
            "model": "mock-model",
            "messages": [
                {"role": "user", "content": f"unique cache-busting query {i} {time.time_ns()}"}
            ],
        }
        t0 = time.perf_counter()
        resp = await client.post(
            f"{gateway_url}/v1/chat/completions", json=payload, headers=headers
        )
        lat = (time.perf_counter() - t0) * 1000.0
        if resp.status_code == 200:
            latencies.append(lat)
    wall_time = time.perf_counter() - t_start
    return latencies, wall_time


async def measure_cache_hits(
    client: httpx.AsyncClient,
    gateway_url: str,
    headers: Dict[str, str],
    concurrency: int,
    num_requests: int,
) -> Tuple[List[float], float]:
    """Measure cache hit latency at a given concurrency level."""
    latencies: List[float] = []
    sem = asyncio.Semaphore(concurrency)
    payload = {
        "model": "mock-model",
        "messages": [{"role": "user", "content": "What is connection pooling in HTTP clients?"}],
    }

    async def single_hit():
        async with sem:
            t0 = time.perf_counter()
            resp = await client.post(
                f"{gateway_url}/v1/chat/completions", json=payload, headers=headers
            )
            lat = (time.perf_counter() - t0) * 1000.0
            if resp.status_code == 200 and resp.headers.get("x-cache") == "HIT":
                latencies.append(lat)

    t_start = time.perf_counter()
    await asyncio.gather(*[single_hit() for _ in range(num_requests)])
    wall_time = time.perf_counter() - t_start
    return latencies, wall_time


def measure_agent_firewall(iterations: int) -> Tuple[List[float], float]:
    """Measure deterministic CPU inspection overhead of AgentToolGuard per call."""
    tool_calls = [
        ("bash", {"command": "git status"}),
        ("sql", {"query": "SELECT id, name FROM users WHERE id = 1"}),
        ("curl", {"url": "https://api.github.com/repos"}),
        ("bash", {"command": "cat /etc/hosts"}),
    ]

    latencies: List[float] = []
    t_start = time.perf_counter()
    for _ in range(iterations):
        for name, args in tool_calls:
            t0 = time.perf_counter()
            _ = AgentToolGuard.inspect_tool_call(name, args)
            lat = (time.perf_counter() - t0) * 1000.0
            latencies.append(lat)
    wall_time = time.perf_counter() - t_start
    return latencies, wall_time


async def run_benchmark_suite(
    gateway_url: Optional[str] = None,
    output_path: str = "benchmarks/results.json",
    quick: bool = False,
) -> Dict[str, Any]:
    print("==================================================")
    print("      SentinelGate Live HTTP Benchmark Suite      ")
    print("==================================================")
    print(f"Mode                : {'Quick (<30s)' if quick else 'Standard Comprehensive'}")
    print(f"Git Commit          : {get_git_commit()}")
    print(
        f"Platform            : {platform.system()} {platform.machine()} (Python {platform.python_version()})"
    )

    mock_server: Optional[BackgroundServer] = None
    gw_server: Optional[BackgroundServer] = None
    bench_db = "bench_http_temp.db"

    # Cleanup temp db
    for ext in ["", "-wal", "-shm"]:
        p = f"{bench_db}{ext}"
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass

    try:
        if gateway_url:
            target_gw_url = gateway_url.rstrip("/")
            mock_url = "http://127.0.0.1:8765"
            admin_token = os.getenv("SENTINELGATE_ADMIN_TOKEN", "admin-secret-token")
        else:
            mock_port = find_free_port()
            gw_port = find_free_port()
            mock_url = f"http://127.0.0.1:{mock_port}"
            target_gw_url = f"http://127.0.0.1:{gw_port}"
            admin_token = "bench-admin-secret"

            print(f"[*] Starting Mock Upstream Provider on {mock_url}...")
            mock_server = BackgroundServer(MockUpstreamApp(), "127.0.0.1", mock_port)
            mock_server.start()

            # Create benchmark configuration pointing to mock upstream
            import yaml

            cfg_dict = {
                "server": {
                    "host": "127.0.0.1",
                    "port": gw_port,
                    "admin_token": admin_token,
                    "cors_origins": [],
                },
                "cache": {
                    "enabled": True,
                    "similarity_threshold": 0.92,
                    "ttl_seconds": 3600,
                    "db_path": bench_db,
                    "exact_match_fallback": True,
                },
                "guardrails": {
                    "secret_action": "redact",
                    "pii_masking": True,
                    "block_secrets": False,
                    "prompt_injection_block": True,
                    "prompt_optimization": False,
                    "context_compaction": False,
                },
                "evals": {"enabled": False, "sample_rate": 0.0},
                "budget": {"enabled": False, "daily_limit_usd": 1000.0},
                "providers": {
                    "openai": {
                        "type": "openai",
                        "base_url": f"{mock_url}/v1",
                        "api_keys": ["mock-key-12345"],
                        "models": {
                            "mock-model": {
                                "input_cost_per_m": 0.0,
                                "output_cost_per_m": 0.0,
                                "fallbacks": [],
                            }
                        },
                    }
                },
            }
            temp_cfg = "bench_temp_config.yaml"
            with open(temp_cfg, "w", encoding="utf-8") as f:
                yaml.dump(cfg_dict, f)

            print(f"[*] Starting SentinelGate Gateway on {target_gw_url}...")
            gw_app = create_app(config_path=temp_cfg, db_path=bench_db)
            gw_server = BackgroundServer(gw_app, "127.0.0.1", gw_port)
            gw_server.start()

            # Wait for readiness
            assert await wait_for_ready(f"{mock_url}/health"), "Mock server failed to start"
            assert await wait_for_ready(f"{target_gw_url}/health"), "Gateway failed to start"
            print("[+] In-process HTTP servers verified online.")

        # HTTP Client setup with keepalive connection pool
        limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
        async with httpx.AsyncClient(limits=limits, timeout=30.0) as client:
            headers = {
                "Authorization": f"Bearer {admin_token}",
                "Content-Type": "application/json",
            }

            # Warmup
            print("[*] Warming up gateway and ONNX vector runtime...")
            warm_payload = {
                "model": "mock-model",
                "messages": [
                    {"role": "user", "content": "What is connection pooling in HTTP clients?"}
                ],
            }
            warm_res = await client.post(
                f"{target_gw_url}/v1/chat/completions", json=warm_payload, headers=headers
            )
            assert warm_res.status_code == 200, f"Warmup request failed: {warm_res.text}"

            # Verify cache hit on identical query
            check_hit = await client.post(
                f"{target_gw_url}/v1/chat/completions", json=warm_payload, headers=headers
            )
            assert check_hit.headers.get("x-cache") == "HIT", "Cache hit verification failed"
            print("[+] Semantic cache verified online (seed warmed).")

            # Request counts based on quick mode
            n_direct = 25 if quick else 100
            n_proxy = 25 if quick else 100
            n_cache_c1 = 25 if quick else 100
            n_cache_c10 = 50 if quick else 200
            n_cache_c50 = 50 if quick else 250
            n_firewall = 100 if quick else 500

            # 1. Direct Mock Upstream Latency
            print(f"[*] 1/4 Measuring direct mock upstream ({n_direct} reqs)...")
            dir_lat, dir_time = await measure_direct_mock(client, mock_url, n_direct)
            dir_stats = compute_stats(dir_lat, dir_time)

            # 2. Gateway Proxy Latency (Cache Miss)
            print(f"[*] 2/4 Measuring gateway proxy overhead ({n_proxy} reqs)...")
            proxy_lat, proxy_time = await measure_gateway_proxy(
                client, target_gw_url, headers, n_proxy
            )
            proxy_stats = compute_stats(proxy_lat, proxy_time)

            overhead_p50 = max(0.0, round(proxy_stats["p50_ms"] - dir_stats["p50_ms"], 2))
            overhead_p95 = max(0.0, round(proxy_stats["p95_ms"] - dir_stats["p95_ms"], 2))
            overhead_p99 = max(0.0, round(proxy_stats["p99_ms"] - dir_stats["p99_ms"], 2))
            overhead_min = max(0.0, round(proxy_stats["min_ms"] - dir_stats["min_ms"], 2))

            # 3. Cache Hit Latency at c=1, c=10, c=50
            print("[*] 3/4 Measuring cache hit latency at c=1, c=10, c=50...")
            c1_lat, c1_time = await measure_cache_hits(
                client, target_gw_url, headers, concurrency=1, num_requests=n_cache_c1
            )
            c1_stats = compute_stats(c1_lat, c1_time)

            c10_lat, c10_time = await measure_cache_hits(
                client, target_gw_url, headers, concurrency=10, num_requests=n_cache_c10
            )
            c10_stats = compute_stats(c10_lat, c10_time)

            c50_lat, c50_time = await measure_cache_hits(
                client, target_gw_url, headers, concurrency=50, num_requests=n_cache_c50
            )
            c50_stats = compute_stats(c50_lat, c50_time)

            # 4. Agent Firewall Overhead per call
            print(f"[*] 4/4 Measuring agent tool firewall inspection ({n_firewall} calls)...")
            fw_lat, fw_time = measure_agent_firewall(n_firewall)
            fw_stats = compute_stats(fw_lat, fw_time)

            # Check if LiteLLM is installed
            litellm_installed = False
            try:
                import litellm  # noqa: F401

                litellm_installed = True
            except ImportError:
                litellm_installed = False

            results = {
                "commit": get_git_commit(),
                "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                ),
                "runner": {
                    "os": platform.system(),
                    "release": platform.release(),
                    "arch": platform.machine(),
                    "python": platform.python_version(),
                    "is_ci": os.getenv("GITHUB_ACTIONS") == "true",
                },
                "scenarios": {
                    "direct_mock_upstream": dir_stats,
                    "gateway_proxy": proxy_stats,
                    "proxy_overhead": {
                        "p50_ms": overhead_p50,
                        "p95_ms": overhead_p95,
                        "p99_ms": overhead_p99,
                        "min_ms": overhead_min,
                    },
                    "cache_hit_c1": c1_stats,
                    "cache_hit_c10": c10_stats,
                    "cache_hit_c50": c50_stats,
                    "agent_firewall_inspection": fw_stats,
                    "litellm_status": "installed" if litellm_installed else "not_installed",
                },
            }

            print("\n---------------- Live Benchmark Results ----------------")
            print(
                f"Direct Mock Upstream     p50 / p95 / p99: {dir_stats['p50_ms']} / {dir_stats['p95_ms']} / {dir_stats['p99_ms']} ms ({dir_stats['rps']} req/s)"
            )
            print(
                f"Gateway Proxy (Cache Miss) p50 / p95 / p99: {proxy_stats['p50_ms']} / {proxy_stats['p95_ms']} / {proxy_stats['p99_ms']} ms ({proxy_stats['rps']} req/s)"
            )
            print(
                f"Net Gateway Overhead      p50 / p95 / p99: {overhead_p50} / {overhead_p95} / {overhead_p99} ms"
            )
            print(
                f"Cache Hit (c=1)           p50 / p95 / p99: {c1_stats['p50_ms']} / {c1_stats['p95_ms']} / {c1_stats['p99_ms']} ms ({c1_stats['rps']} req/s)"
            )
            print(
                f"Cache Hit (c=10)          p50 / p95 / p99: {c10_stats['p50_ms']} / {c10_stats['p95_ms']} / {c10_stats['p99_ms']} ms ({c10_stats['rps']} req/s)"
            )
            print(
                f"Cache Hit (c=50)          p50 / p95 / p99: {c50_stats['p50_ms']} / {c50_stats['p95_ms']} / {c50_stats['p99_ms']} ms ({c50_stats['rps']} req/s)"
            )
            print(
                f"Agent Firewall Inspection p50 / p95 / p99: {fw_stats['p50_ms']} / {fw_stats['p95_ms']} / {fw_stats['p99_ms']} ms ({fw_stats['rps']} calls/s)"
            )
            print("--------------------------------------------------------")

            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2)
            print(f"[+] Exported benchmark telemetry to {output_path}")

            return results

    finally:
        if gw_server:
            gw_server.stop()
        if mock_server:
            mock_server.stop()
        for ext in ["", "-wal", "-shm"]:
            p = f"{bench_db}{ext}"
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        if os.path.exists("bench_temp_config.yaml"):
            try:
                os.remove("bench_temp_config.yaml")
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description="SentinelGate Real HTTP Benchmark Suite")
    parser.add_argument("--url", type=str, default=None, help="URL to running SentinelGate gateway")
    parser.add_argument(
        "--output", type=str, default="benchmarks/results.json", help="Path for JSON output"
    )
    parser.add_argument(
        "--quick", action="store_true", help="Run fast pass completing in <30 seconds"
    )
    args = parser.parse_args()

    asyncio.run(
        run_benchmark_suite(gateway_url=args.url, output_path=args.output, quick=args.quick)
    )


if __name__ == "__main__":
    main()
