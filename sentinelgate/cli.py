from __future__ import annotations

import argparse
import os

import uvicorn


def main():
    parser = argparse.ArgumentParser(
        prog="sentinelgate",
        description="Zero-bloat, OpenAI-compatible LLM Gateway with local semantic caching, secret redaction, and auto-routing.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    parser.add_argument("--config", default=None, help="Path to config.yaml (optional)")
    parser.add_argument("--db", default=None, help="Path to SQLite database file (optional)")
    parser.add_argument("--demo", action="store_true", help="Force zero-key sandbox mode")
    parser.add_argument(
        "--workers", type=int, default=1, help="Number of worker processes (default: 1)"
    )

    args = parser.parse_args()

    if args.demo and args.host not in ("127.0.0.1", "localhost"):
        parser.error("Keyless demo mode (--demo) can only be bound to 127.0.0.1 or localhost.")

    if args.config:
        os.environ["SENTINELGATE_CONFIG"] = args.config
    if args.db:
        os.environ["SENTINELGATE_DB"] = args.db
    if args.demo:
        os.environ["DEMO_MODE"] = "true"

    uvicorn.run(
        "sentinelgate.main:app",
        host=args.host,
        port=args.port,
        workers=args.workers,
        log_level="info",
    )


if __name__ == "__main__":
    main()
