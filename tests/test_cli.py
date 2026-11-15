"""Tests for SentinelGate command-line interface entrypoints."""

from __future__ import annotations

import os
import sys

import pytest

from sentinelgate.cli import main


def test_cli_help_flag(capsys):
    """Verify sentinelgate CLI prints help cleanly."""
    sys_argv_orig = sys.argv
    sys.argv = ["sentinelgate", "--help"]
    try:
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Zero-bloat, OpenAI-compatible LLM Gateway" in captured.out
    finally:
        sys.argv = sys_argv_orig


def test_cli_argument_parsing(monkeypatch):
    """Verify CLI flags properly populate environment variables."""
    import uvicorn

    called_config = {}

    def fake_run(app_str, **kwargs):
        called_config["app"] = app_str
        called_config.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.delenv("SENTINELGATE_CONFIG", raising=False)
    monkeypatch.delenv("SENTINELGATE_DB", raising=False)

    sys_argv_orig = sys.argv
    sys.argv = ["sentinelgate", "--port", "9090", "--host", "127.0.0.1", "--demo", "--workers", "2"]
    try:
        main()
        assert called_config["port"] == 9090
        assert called_config["host"] == "127.0.0.1"
        assert called_config["workers"] == 2
    finally:
        sys.argv = sys_argv_orig
        # Ensure DEMO_MODE does not leak into subsequent tests
        os.environ.pop("DEMO_MODE", None)
        os.environ.pop("SENTINELGATE_CONFIG", None)
        os.environ.pop("SENTINELGATE_DB", None)
