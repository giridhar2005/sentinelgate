from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

logger = logging.getLogger("sentinelgate.circuit")


class ProviderCircuitBreaker:
    """Production-grade circuit breaker & health tracker for LLM upstream providers.

    Trips into cooldown when a provider exhausts its keys or returns repeated 429/500/503 errors.
    """

    def __init__(self, failure_threshold: int = 2, cooldown_seconds: float = 30.0):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        # provider -> {"failures": int, "cooldown_until": float}
        self.statuses: Dict[str, Dict[str, Any]] = {}

    def is_available(self, provider: str) -> bool:
        st = self.statuses.get(provider)
        if not st:
            return True
        return time.time() >= st.get("cooldown_until", 0.0)

    def record_failure(self, provider: str, status_code: int = 500):
        st = self.statuses.setdefault(provider, {"failures": 0, "cooldown_until": 0.0})
        st["failures"] += 1
        if st["failures"] >= self.failure_threshold or status_code in (429, 503):
            st["cooldown_until"] = time.time() + self.cooldown_seconds
            logger.warning(
                f"Provider '{provider}' tripped circuit breaker for {self.cooldown_seconds}s (HTTP {status_code})."
            )

    def record_success(self, provider: str):
        if provider in self.statuses:
            self.statuses[provider] = {"failures": 0, "cooldown_until": 0.0}

    def filter_available_candidates(self, candidates: List[tuple[str, str]]) -> List[tuple[str, str]]:
        """Return available candidates, or all candidates if all are in cooldown."""
        avail = [c for c in candidates if self.is_available(c[0])]
        return avail if avail else candidates

    def get_statuses(self, configured_providers: Dict[str, Any]) -> Dict[str, Any]:
        now = time.time()
        res = {}
        for p, p_cfg in configured_providers.items():
            st = self.statuses.get(p, {"failures": 0, "cooldown_until": 0.0})
            cd = max(0.0, st.get("cooldown_until", 0.0) - now)
            keys = [k for k in getattr(p_cfg, "api_keys", []) if k and not str(k).startswith("${")]
            res[p] = {
                "status": "healthy" if cd == 0.0 else "cooldown",
                "failures": st.get("failures", 0),
                "cooldown_remaining_sec": round(cd, 1),
                "keys_count": len(keys),
            }
        return res
