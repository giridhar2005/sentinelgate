"""Backwards-compatibility shim redirecting legacy app.* imports to sentinelgate.*"""

from __future__ import annotations

import sys
import sentinelgate
from sentinelgate.main import app, create_app

__all__ = ["app", "create_app"]

sys.modules["app.core"] = sentinelgate.core
sys.modules["app.routers"] = sentinelgate.routers
sys.modules["app.services"] = sentinelgate.services
sys.modules["app.main"] = sentinelgate.main
