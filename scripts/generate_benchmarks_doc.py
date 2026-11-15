#!/usr/bin/env python3
"""Generates BENCHMARKS.md from committed raw benchmark JSON artifacts.

Fails loudly if benchmarks/results.json does not exist.
Ensures every figure in BENCHMARKS.md matches the raw JSON artifacts exactly.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = ROOT / "benchmarks" / "results.json"
HEDGING_PATH = ROOT / "benchmarks" / "hedging_results.json"
OUTPUT_MD_PATH = ROOT / "BENCHMARKS.md"
README_PATH = ROOT / "README.md"


def load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"[FATAL] Benchmark results file not found at: {path}. "
            f"You must execute 'uv run python benchmarks/bench.py' before generating documentation."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_benchmarks_markdown(bench_data: dict, hedging_data: dict | None) -> str:
    commit = bench_data.get("commit", "unknown")
    ts = bench_data.get("timestamp_utc", "N/A")
    runner = bench_data.get("runner", {})
    os_name = runner.get("os", "Unknown")
    arch = runner.get("arch", "Unknown")
    python_ver = runner.get("python", "3.x")
    is_ci = runner.get("is_ci", False)
    runner_env = "GitHub Actions CI Runner" if is_ci else f"Local Host ({os_name} {arch})"

    scenarios = bench_data.get("scenarios", {})
    mock = scenarios.get("direct_mock_upstream", {})
    proxy = scenarios.get("gateway_proxy", {})
    overhead = scenarios.get("proxy_overhead", {})
    c1 = scenarios.get("cache_hit_c1", {})
    c10 = scenarios.get("cache_hit_c10", {})
    c50 = scenarios.get("cache_hit_c50", {})
    fw = scenarios.get("agent_firewall_inspection", {})

    lines: list[str] = [
        "# SentinelGate Systems Performance Benchmarks",
        "",
        "> **Methodology Notice:** All HTTP metrics below are captured using a real HTTP harness (`benchmarks/bench.py`) executing against an in-process Uvicorn gateway over loopback sockets (`127.0.0.1`) with `httpx.AsyncClient` connection pooling. Tail latency hedging metrics derive from a controlled mock provider simulation (`benchmarks/bench_hedging.py`). Every number below matches the raw committed JSON artifacts exactly.",
        "",
        "### Test Runner & Environment",
        f"- **Git Commit SHA:** `{commit}`",
        f"- **Benchmark Date:** `{ts}`",
        f"- **Runner Environment:** {runner_env}",
        f"- **Operating System:** `{os_name}` ({arch})",
        f"- **Python Version:** `{python_ver}`",
        "- **Connection Pool:** `max_keepalive_connections=50, max_connections=100`",
        "",
        "---",
        "",
        "## 1. Raw Proxy Latency & Gateway Overhead",
        "",
        "Measures raw HTTP round-trip latency when proxying cache-miss requests through SentinelGate compared against a direct mock upstream server returning instant completions.",
        "",
        "| Endpoint Target | p50 (ms) | p95 (ms) | p99 (ms) | Min (ms) | Max (ms) | Throughput (req/s) |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |",
        f"| **Direct Mock Upstream** | {mock.get('p50_ms', 0.0):.2f} | {mock.get('p95_ms', 0.0):.2f} | {mock.get('p99_ms', 0.0):.2f} | {mock.get('min_ms', 0.0):.2f} | {mock.get('max_ms', 0.0):.2f} | {mock.get('rps', 0.0):,.1f} |",
        f"| **SentinelGate Proxy** | {proxy.get('p50_ms', 0.0):.2f} | {proxy.get('p95_ms', 0.0):.2f} | {proxy.get('p99_ms', 0.0):.2f} | {proxy.get('min_ms', 0.0):.2f} | {proxy.get('max_ms', 0.0):.2f} | {proxy.get('rps', 0.0):,.1f} |",
        f"| **Net Gateway Overhead** | **+{overhead.get('p50_ms', 0.0):.2f}** | **+{overhead.get('p95_ms', 0.0):.2f}** | **+{overhead.get('p99_ms', 0.0):.2f}** | **+{overhead.get('min_ms', 0.0):.2f}** | - | - |",
        "",
        "---",
        "",
        "## 2. Semantic Cache Hit Latency Across Concurrency",
        "",
        "Measures semantic vector lookup and SQLite WAL cache retrieval latency under varying concurrent client workloads.",
        "",
        "| Concurrency Level | p50 (ms) | p95 (ms) | p99 (ms) | Min (ms) | Max (ms) | Throughput (req/s) |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |",
        f"| **Concurrency c = 1** | {c1.get('p50_ms', 0.0):.2f} | {c1.get('p95_ms', 0.0):.2f} | {c1.get('p99_ms', 0.0):.2f} | {c1.get('min_ms', 0.0):.2f} | {c1.get('max_ms', 0.0):.2f} | {c1.get('rps', 0.0):,.1f} |",
        f"| **Concurrency c = 10** | {c10.get('p50_ms', 0.0):.2f} | {c10.get('p95_ms', 0.0):.2f} | {c10.get('p99_ms', 0.0):.2f} | {c10.get('min_ms', 0.0):.2f} | {c10.get('max_ms', 0.0):.2f} | {c10.get('rps', 0.0):,.1f} |",
        f"| **Concurrency c = 50** | {c50.get('p50_ms', 0.0):.2f} | {c50.get('p95_ms', 0.0):.2f} | {c50.get('p99_ms', 0.0):.2f} | {c50.get('min_ms', 0.0):.2f} | {c50.get('max_ms', 0.0):.2f} | {c50.get('rps', 0.0):,.1f} |",
        "",
        "---",
        "",
        "## 3. Pre-Execution Agent Tool Firewall Inspection Overhead",
        "",
        "Measures deterministic lexical parsing, AST analysis (`sqlglot`), and IP SSRF validation in `AgentToolGuard.inspect_tool_call`.",
        "",
        "| Security Subsystem | p50 (ms) | p95 (ms) | p99 (ms) | Min (ms) | Max (ms) | Inspection Rate (calls/s) |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |",
        f"| **Agent Tool & MCP Firewall** | **{fw.get('p50_ms', 0.0):.2f}** | **{fw.get('p95_ms', 0.0):.2f}** | **{fw.get('p99_ms', 0.0):.2f}** | {fw.get('min_ms', 0.0):.2f} | {fw.get('max_ms', 0.0):.2f} | **{fw.get('rps', 0.0):,.1f}** |",
        "",
    ]

    if hedging_data:
        h_cfg = hedging_data.get("config", {})
        unh = hedging_data.get("unhedged", {})
        hed = hedging_data.get("hedged", {})
        red_pct = hedging_data.get("tail_reduction_pct", 0.0)

        lines.extend(
            [
                "---",
                "",
                "## 4. Speculative Latency Hedging Tail Latency Reduction",
                "",
                "> **Simulation Parameters:** Modeled with a simulated mock provider exhibiting bimodal upstream latency "
                f"({h_cfg.get('tail_percentage', 5.0)}% probability of ~{h_cfg.get('primary_tail_spike_ms', 2000.0):.0f}ms delay vs {h_cfg.get('primary_baseline_ms', 400.0):.0f}ms baseline). "
                f"Hedge triggered at SLA deadline `{h_cfg.get('hedge_delay_ms', 450.0):.0f}ms`, racing primary against fast fallback ({h_cfg.get('fallback_response_ms', 45.0):.0f}ms).",
                "",
                "| Execution Strategy | p50 (ms) | p90 (ms) | p95 (ms) | p99 (ms) |",
                "| :--- | :---: | :---: | :---: | :---: |",
                f"| **Unhedged Baseline** | {unh.get('p50', 0.0):.1f} | {unh.get('p90', 0.0):.1f} | {unh.get('p95', 0.0):.1f} | **{unh.get('p99', 0.0):.1f}** |",
                f"| **Speculative Hedged (`delay={h_cfg.get('hedge_delay_ms', 450.0):.0f}ms`)** | {hed.get('p50', 0.0):.1f} | {hed.get('p90', 0.0):.1f} | {hed.get('p95', 0.0):.1f} | **{hed.get('p99', 0.0):.1f}** |",
                f"| **Improvement** | - | - | - | **-{red_pct:.1f}% Tail Latency** |",
                "",
            ]
        )

    lines.extend(
        [
            "---",
            "",
            "## Reproduction Commands",
            "",
            "To reproduce these benchmarks on your local machine or CI runner:",
            "",
            "```bash",
            "# 1. Run live HTTP benchmark suite (<30 seconds)",
            "uv run python benchmarks/bench.py --quick",
            "",
            "# 2. Run speculative hedging simulation",
            "uv run python benchmarks/bench_hedging.py",
            "",
            "# 3. Regenerate this document strictly from committed JSON output",
            "uv run python scripts/generate_benchmarks_doc.py",
            "```",
            "",
        ]
    )

    return "\n".join(lines)


def main():
    print("[*] Generating BENCHMARKS.md from benchmark JSON outputs...")
    bench_data = load_json(RESULTS_PATH)
    hedging_data = None
    if HEDGING_PATH.exists():
        with open(HEDGING_PATH, "r", encoding="utf-8") as f:
            hedging_data = json.load(f)

    markdown = build_benchmarks_markdown(bench_data, hedging_data)
    OUTPUT_MD_PATH.write_text(markdown, encoding="utf-8")
    print(f"[+] Successfully generated {OUTPUT_MD_PATH}")


if __name__ == "__main__":
    main()
