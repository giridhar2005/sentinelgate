from __future__ import annotations

import pytest

from sentinelgate.services.agent_guard import AgentToolGuard
from sentinelgate.services.agent_guard.network import inspect_url
from sentinelgate.services.agent_guard.shell import inspect_shell
from sentinelgate.services.agent_guard.sql import inspect_sql
from sentinelgate.services.guardrails import (
    JSONSelfHealer,
    PIIGuardrail,
    PromptOptimizer,
    SecretRedactor,
)
from sentinelgate.services.pipeline_helpers import (
    extract_system_prompt,
    extract_user_prompt,
)
from sentinelgate.services.pipeline_streaming import convert_openai_to_anthropic
from sentinelgate.services.router.providers.gemini import (
    build_gemini_payload,
    convert_gemini_to_openai,
)
from sentinelgate.services.router.providers.openai import prepare_openai_request
from sentinelgate.services.router.routing import get_all_models, resolve_model
from sentinelgate.services.streaming_redactor import (
    SlidingWindowStreamRedactor,
    sse_sliding_window_redactor,
)


def test_sql_guard_edge_cases():
    assert inspect_sql("") == (True, None, None)
    assert inspect_sql("   ") == (True, None, None)

    # Tautology update with binary equality
    safe, threat, detail = inspect_sql("UPDATE accounts SET balance = 0 WHERE id = id")
    assert not safe
    assert "TAUTOLOGY" in str(threat)

    # ALTER TABLE DROP
    safe, threat, detail = inspect_sql("ALTER TABLE users DROP COLUMN password")
    assert not safe
    assert "DROP" in str(threat)

    # Regex fallback catches (e.g. invalid syntax containing DROP TABLE)
    safe, threat, detail = inspect_sql("SYNTAX ERROR DROP TABLE users /* comment */")
    assert not safe
    assert "DROP" in str(threat)

    safe, threat, detail = inspect_sql("SYNTAX ERROR TRUNCATE TABLE logs")
    assert not safe
    assert "TRUNCATE" in str(threat)

    safe, threat, detail = inspect_sql("SYNTAX ERROR DELETE FROM secrets WHERE 1=1")
    assert not safe
    assert "DELETE" in str(threat)

    safe, threat, detail = inspect_sql("SYNTAX ERROR DELETE FROM secrets;")
    assert not safe
    assert "DELETE" in str(threat)


def test_network_guard_edge_cases():
    assert inspect_url("") == (True, None, None)
    assert inspect_url("  ") == (True, None, None)

    # Malformed URL
    safe, threat, _ = inspect_url("https://[invalid-ipv6-bracket")
    assert not safe
    assert "MALFORMED" in str(threat)

    # Unsupported schemes
    safe, threat, _ = inspect_url("customscheme://example.com")
    assert not safe
    assert "SCHEME" in str(threat)

    safe, threat, _ = inspect_url("ftp://example.com")
    assert not safe

    # URL without hostname
    safe, threat, _ = inspect_url("https://")
    assert safe

    # Obfuscated IP formats
    safe, threat, _ = inspect_url("http://2130706433/path")  # 127.0.0.1 in dword
    assert not safe
    assert "LOOPBACK" in str(threat)

    # Overflow / invalid numeric IPs
    safe, _, _ = inspect_url("http://999999999999999999999999999999999999/path")
    safe, _, _ = inspect_url("http://0xffffffffffffffffffffffffffffffffff/path")
    safe, _, _ = inspect_url("http://0999.0.0.1/path")

    # Multicast and unspecified (0.0.0.0)
    safe, threat, _ = inspect_url("http://0.0.0.0/api")
    assert not safe
    safe, threat, _ = inspect_url("http://224.0.0.1/api")
    assert not safe

    safe, threat, _ = inspect_url("http://0x7f000001/path")  # 127.0.0.1 in hex
    assert not safe
    assert "LOOPBACK" in str(threat)

    safe, threat, _ = inspect_url("http://0177.0.0.1/path")  # 127.0.0.1 in octal
    assert not safe
    assert "LOOPBACK" in str(threat)

    # CGNAT
    safe, threat, _ = inspect_url("http://100.64.1.1/data")
    assert not safe
    assert "PRIVATE_CIDR" in str(threat)

    # Domain allowlist
    safe, threat, _ = inspect_url("https://evil.com/api", allowed_domains=["trusted.com"])
    assert not safe
    assert "DOMAIN_NOT_ALLOWLISTED" in str(threat)

    safe, threat, _ = inspect_url("https://api.trusted.com/api", allowed_domains=["trusted.com"])
    assert safe


def test_shell_guard_edge_cases():
    assert inspect_shell("") == (True, None, None)
    safe, threat, detail = inspect_shell("echo hello", allowed_prefixes=["echo"])
    assert safe

    # rm with no-preserve-root
    safe, threat, _ = inspect_shell("rm --no-preserve-root /tmp/test")
    assert not safe
    assert "NO_PRESERVE_ROOT" in str(threat)

    safe, threat, _ = inspect_shell("rm --no-preserve-root -f /tmp/test")
    assert not safe
    assert "DESTRUCTIVE_SHELL" in str(threat)


def test_agent_guard_tool_scoping_and_mcp():
    assert AgentToolGuard.inspect_text_payload("") == (True, None, None)

    # File writer ignores content but execution tool checks everything
    safe, _, _ = AgentToolGuard.inspect_tool_call(
        "write_file", {"path": "/app/test.py", "content": "rm -rf /"}
    )
    assert safe

    # Execution tool detects command inside dict
    safe, threat, _ = AgentToolGuard.inspect_tool_call("run_command", {"command": "rm -rf /"})
    assert not safe

    # Non-dict string argument
    safe, threat, _ = AgentToolGuard.inspect_tool_call("bash", "rm -rf /")
    assert not safe

    # Legacy function_call in response
    resp_with_fn = {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "function_call": {"name": "bash", "arguments": "rm -rf /"},
                },
            }
        ]
    }
    safe, threat, detail, sanitized = AgentToolGuard.inspect_response(
        resp_with_fn, firewall_mode="block"
    )
    assert not safe
    assert sanitized["choices"][0]["message"]["function_call"] is None

    # MCP inspect message
    safe, _, _ = AgentToolGuard.inspect_mcp_message({"method": "ping"})
    assert safe
    safe, threat, _ = AgentToolGuard.inspect_mcp_message(
        {"method": "tools/call", "params": {"name": "bash", "arguments": "rm -rf /"}}
    )
    assert not safe


def test_guardrails_edge_cases():
    # Secret restoration empty/non-dict handling
    assert SecretRedactor.redact_text("", {}, {}) == ("", 0)
    assert SecretRedactor.restore_text("", {}) == ""
    assert SecretRedactor.restore_in_response({}, {}) == {}
    assert PIIGuardrail.restore_text("", {}) == ""
    assert PIIGuardrail.restore_in_response({}, {}) == {}

    # Prompt optimizer empty string
    assert PromptOptimizer.optimize_text("") == ""

    # JSON self healer
    assert not JSONSelfHealer.is_json_requested({})
    assert JSONSelfHealer.is_json_requested({"response_format": {"type": "json_object"}})


@pytest.mark.asyncio
async def test_json_self_healer_repair():
    class DummyRouter:
        async def execute_request(self, req, stream=False):
            return {"choices": [{"message": {"content": '{"repaired": true}'}}]}, "openai", "gpt-4o"

    router = DummyRouter()
    initial_resp = {"choices": [{"message": {"content": "INVALID JSON {"}}]}
    req_data = {"messages": [{"role": "user", "content": "output json"}]}
    repaired = await JSONSelfHealer.repair_response(router, req_data, initial_resp)
    assert repaired.get("sentinel_json_repaired") is True


@pytest.mark.asyncio
async def test_streaming_redactor_edge_cases():
    # Restoration in stream redactor
    mapping = {"__SG_SECRET_1__": "my-secret-key"}
    redactor = SlidingWindowStreamRedactor(secret_mapping=mapping)
    redactor.process_delta("Some text with __SG_SECRET_1__ here", is_final=True)

    async def gen_lines():
        yield "event: message\n"
        yield "data: [DONE]\n"

    collected = []
    async for chunk in sse_sliding_window_redactor(gen_lines()):
        collected.append(chunk)
    assert len(collected) > 0


def test_pipeline_helpers_edge_cases():
    assert extract_user_prompt([]) == ""
    assert (
        extract_user_prompt([{"role": "user", "content": [{"type": "text", "text": "hello list"}]}])
        == "hello list"
    )
    assert (
        extract_system_prompt([], system_param=[{"type": "text", "text": "sys list"}]) == "sys list"
    )


def test_gemini_and_openai_adapters():
    payload = build_gemini_payload(
        {
            "messages": [
                {"role": "system", "content": "Be concise"},
                {"role": "user", "content": "Hi"},
            ],
            "temperature": 0.7,
            "max_tokens": 100,
        }
    )
    assert "contents" in payload
    assert "system_instruction" in payload

    converted = convert_gemini_to_openai(
        {"candidates": [{"content": {"parts": [{"text": "Gemini answer"}]}}]},
        model="gemini-1.5-flash",
    )
    assert converted["choices"][0]["message"]["content"] == "Gemini answer"

    url, headers, p = prepare_openai_request(
        "openai", "https://api.openai.com/v1", "key123", {"messages": []}, "gpt-4o"
    )
    assert url == "https://api.openai.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer key123"


def test_routing_edge_cases():
    providers = {"custom": {"models": {"m1": {"fallbacks": ["groq/llama-3.1-8b-instant"]}}}}
    models = get_all_models(providers)
    assert any(m["id"] == "m1" for m in models)

    p, m, fb, _ = resolve_model("custom/m1", providers, lambda x: True)
    assert p == "custom"
    assert m == "m1"
    assert fb == ["groq/llama-3.1-8b-instant"]


def test_anthropic_converter():
    openai_tc = {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Using tools",
                    "tool_calls": [
                        {
                            "id": "tc1",
                            "type": "function",
                            "function": {"name": "calculator", "arguments": '{"a": 1}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    anth = convert_openai_to_anthropic(openai_tc, "req-1", "claude-3-5-sonnet", "Calculate")
    assert anth["stop_reason"] == "tool_use"
    assert len(anth["content"]) == 2
    assert anth["content"][1]["type"] == "tool_use"
