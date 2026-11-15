from __future__ import annotations

import uvicorn

from sentinelgate.main import app, create_app

# Export state instances for backwards compatibility
router = app.state.router
cache = app.state.cache
evals = app.state.evals
budget = app.state.budget
virtual_keys = app.state.virtual_keys

__all__ = ["app", "create_app", "router", "cache", "evals", "budget", "virtual_keys"]

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
