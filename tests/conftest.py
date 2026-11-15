from __future__ import annotations

import os
import tempfile

import pytest
import yaml
from starlette.testclient import TestClient

from sentinelgate.main import create_app


@pytest.fixture
def test_app():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_f:
        db_path = db_f.name

    test_config = {
        "server": {
            "host": "127.0.0.1",
            "port": 8000,
            "admin_token": "test-admin-token",
            "cors_origins": [],
        },
        "cache": {
            "enabled": True,
            "similarity_threshold": 0.90,
            "ttl_seconds": 3600,
            "db_path": db_path,
        },
        "guardrails": {
            "pii_masking": True,
            "block_secrets": True,
            "prompt_injection_block": True,
            "context_compaction": True,
        },
        "budget": {"enabled": True, "daily_limit_usd": 10.0},
        "evals": {"enabled": True, "sample_rate": 0.0},  # Deterministic test runs
        "providers": {
            "openai": {
                "type": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_keys": ["key-alpha", "key-beta"],
                "models": {
                    "gpt-4o-mini": {
                        "input_cost_per_m": 0.15,
                        "output_cost_per_m": 0.60,
                        "fallbacks": ["groq/llama-3.1-8b-instant"],
                    }
                },
            },
            "groq": {
                "type": "openai_compatible",
                "base_url": "https://api.groq.com/openai/v1",
                "api_keys": ["groq-key-1"],
                "models": {
                    "llama-3.1-8b-instant": {
                        "input_cost_per_m": 0.05,
                        "output_cost_per_m": 0.08,
                        "fallbacks": [],
                    }
                },
            },
            "anthropic": {
                "type": "anthropic",
                "base_url": "https://api.anthropic.com/v1",
                "api_keys": ["ant-key-1"],
                "models": {
                    "claude-3-5-haiku-20241022": {
                        "input_cost_per_m": 0.80,
                        "output_cost_per_m": 4.0,
                        "fallbacks": ["groq/llama-3.1-8b-instant"],
                    }
                },
            },
        },
    }

    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as cfg_f:
        yaml.safe_dump(test_config, cfg_f)
        cfg_path = cfg_f.name

    app_instance = create_app(config_path=cfg_path, db_path=db_path)
    client = TestClient(app_instance, headers={"Authorization": "Bearer test-admin-token"})

    yield client, app_instance

    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass
    if os.path.exists(cfg_path):
        try:
            os.remove(cfg_path)
        except OSError:
            pass
