from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any, Callable, Coroutine, Dict, Tuple, Union

logger = logging.getLogger("sentinelgate.router.hedging")


async def execute_hedged_request(
    request_data: Dict[str, Any],
    execute_request_fn: Callable[
        [Dict[str, Any], bool],
        Coroutine[Any, Any, Tuple[Union[Dict[str, Any], AsyncGenerator[str, None]], str, str]],
    ],
    resolve_model_fn: Callable[[str, Any], Tuple[str, str, list[str]]],
    available_providers: Dict[str, Any],
    record_winner_primary_fn: Callable[[], None],
    record_winner_secondary_fn: Callable[[], None],
    record_hedged_triggered_fn: Callable[[], None],
    hedge_delay_sec: float = 0.45,
) -> Tuple[Union[Dict[str, Any], AsyncGenerator[str, None]], str, str, Dict[str, Any]]:
    """Speculative Latency Hedging (Google Tail at Scale p99 Slasher).

    Races the primary upstream request against a concurrent secondary hedge if the primary
    exceeds hedge_delay_sec. Slashes p99 tail latencies caused by upstream queuing delays.
    """
    primary_task = asyncio.create_task(execute_request_fn(request_data, False))
    done, _ = await asyncio.wait([primary_task], timeout=hedge_delay_sec)
    if primary_task in done:
        record_winner_primary_fn()
        res, p, m = await primary_task
        return (
            res,
            p,
            m,
            {"hedged_triggered": False, "winner": "primary", "delay_sec": hedge_delay_sec},
        )

    # Primary delayed beyond SLA threshold: trigger speculative hedge
    record_hedged_triggered_fn()
    requested_model = request_data.get("model", "gpt-4o-mini")
    primary_provider, _, fallbacks = resolve_model_fn(requested_model, request_data.get("messages"))

    hedge_model = requested_model
    if fallbacks:
        hedge_model = fallbacks[0]
    elif primary_provider != "groq" and "groq" in available_providers:
        hedge_model = "groq/llama-3.1-8b-instant"
    elif primary_provider != "gemini" and "gemini" in available_providers:
        hedge_model = "gemini/gemini-1.5-flash"
    elif primary_provider != "openai" and "openai" in available_providers:
        hedge_model = "openai/gpt-4o-mini"

    hedged_payload = dict(request_data)
    hedged_payload["model"] = hedge_model
    hedge_task = asyncio.create_task(execute_request_fn(hedged_payload, False))

    # Race with asyncio.as_completed so if one task raises, the other can still succeed
    first_success = None
    last_exc = None

    tasks = [primary_task, hedge_task]
    for coro in asyncio.as_completed(tasks):
        try:
            res, p, m = await coro
            first_success = (res, p, m)
            break
        except Exception as e:
            logger.warning(f"Task in hedged race failed: {e}")
            last_exc = e

    # Cancel any pending uncompleted tasks immediately
    for t in tasks:
        if not t.done():
            t.cancel()

    if first_success:
        res, p, m = first_success
        is_hedge = (
            (p != primary_provider and not p.endswith("(cached)"))
            or "groq" in p
            or "llama" in m
            or p == "gemini"
        )
        if is_hedge:
            record_winner_secondary_fn()
            return (
                res,
                f"{p}(hedged)",
                m,
                {
                    "hedged_triggered": True,
                    "winner": "hedge",
                    "hedge_provider": p,
                    "delay_sec": hedge_delay_sec,
                },
            )
        else:
            record_winner_primary_fn()
            return (
                res,
                p,
                m,
                {
                    "hedged_triggered": True,
                    "winner": "primary",
                    "delay_sec": hedge_delay_sec,
                },
            )

    raise RuntimeError(f"Hedged execution failed across all runners. Last error: {last_exc}")
