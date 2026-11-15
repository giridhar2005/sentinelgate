# Speculative Latency Hedging

Speculative Latency Hedging in SentinelGate implements the concurrent request hedging pattern described in Jeffrey Dean and Luiz André Barroso's paper *"The Tail at Scale"* (Communications of the ACM, 2013).

---

## Architecture & Mechanics

When a request is submitted with latency hedging enabled (via config or the `X-Speculative-Hedge: true` header):

1. **Primary Dispatch:** SentinelGate immediately dispatches the request to the primary provider (e.g., `openai/gpt-4o-mini`).
2. **SLA Delay Window:** An asynchronous timer begins running for the configured hedging delay window (`hedge_delay_sec`, default: `0.45s`).
3. **Primary Winner:** If the primary provider returns before the SLA timer expires, SentinelGate delivers the response directly (`X-Hedge-Triggered: false`, `X-Hedge-Winner: primary`). No secondary request is spawned.
4. **Hedge Dispatch:** If the primary provider remains uncompleted beyond the SLA timer, SentinelGate triggers a speculative secondary request to a designated fallback provider (e.g., `groq/llama-3.1-8b-instant`).
5. **Race Resolution & Task Cancellation:** Both tasks race concurrently using `asyncio.as_completed`. The moment one task completes successfully:
   - The winning response is returned immediately to the client (`X-Hedge-Winner: primary` or `X-Hedge-Winner: hedge`).
   - Any remaining uncompleted runner task is explicitly cancelled via `task.cancel()`.
   - The cancelled task's underlying HTTP connection (`httpx.AsyncClient`) is closed, terminating in-flight token generation upstream.

---

## Critical Cost & Operational Constraints

> [!WARNING]
> **Token Cost Doubling:** When hedging is triggered, SentinelGate initiates two simultaneous upstream LLM generations. If the secondary request is dispatched, token charges are incurred across both providers until the loser is cancelled. Speculative hedging approximately doubles token cost on requests where latency spikes exceed the SLA window.

> [!CAUTION]
> **Idempotency Restriction (NEVER with Tool Calling):**
> Speculative hedging MUST strictly be restricted to **read-only, idempotent operations** (e.g., semantic search, summarization, categorization, Q&A).
>
> Hedging must **NEVER** be enabled for stateful requests, agent workflows, or requests containing `tools` / `function_call`. Disagreeing completion races on stateful tool invocations could dispatch duplicate mutations, database writes, or shell execution side effects. SentinelGate automatically bypasses speculative hedging on requests where `tools` are present.
