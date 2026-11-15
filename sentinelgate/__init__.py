from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.main import app, create_app

__all__ = ["app", "create_app"]


def __getattr__(name: str):
    if name in ("app", "create_app"):
        from app.main import app, create_app
        return app if name == "app" else create_app
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
