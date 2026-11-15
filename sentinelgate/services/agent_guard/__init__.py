from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from sentinelgate.services.agent_guard.models import (
    EXECUTION_ARG_KEYS,
    FILE_WRITE_TOOLS,
    NON_EXEC_CONTENT_KEYS,
    URL_EXTRACTOR_REGEX,
)
from sentinelgate.services.agent_guard.network import inspect_url as _inspect_url
from sentinelgate.services.agent_guard.shell import inspect_shell as _inspect_shell
from sentinelgate.services.agent_guard.sql import inspect_sql as _inspect_sql

logger = logging.getLogger("sentinelgate.agent_guard")


class AgentToolGuard:
    """Deterministic Pre-Execution Tool-Call and MCP Security Inspector."""

    mode: str = "block"  # "block" (default) or "audit"
    allowed_shell_prefixes: List[str] = []
    allowed_sql_tables: List[str] = []
    allowed_external_domains: List[str] = []

    @classmethod
    def inspect_shell(
        cls,
        command: str,
        allowed_prefixes: Optional[List[str]] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        prefixes = allowed_prefixes if allowed_prefixes is not None else cls.allowed_shell_prefixes
        return _inspect_shell(command, prefixes)

    @classmethod
    def inspect_sql(
        cls,
        sql_query: str,
        allowed_tables: Optional[List[str]] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        tables = allowed_tables if allowed_tables is not None else cls.allowed_sql_tables
        return _inspect_sql(sql_query, tables)

    @classmethod
    def inspect_url(
        cls,
        url_string: str,
        allowed_domains: Optional[List[str]] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        domains = allowed_domains if allowed_domains is not None else cls.allowed_external_domains
        return _inspect_url(url_string, domains)

    @classmethod
    def inspect_text_payload(cls, text: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """Scan a raw string against shell, SQL, and SSRF security rule sets."""
        if not text or not text.strip():
            return True, None, None

        is_safe, threat, detail = cls.inspect_shell(text)
        if not is_safe:
            return is_safe, threat, detail

        is_safe, threat, detail = cls.inspect_sql(text)
        if not is_safe:
            return is_safe, threat, detail

        for m in URL_EXTRACTOR_REGEX.finditer(text):
            candidate_url = m.group(0)
            is_safe, threat, detail = cls.inspect_url(candidate_url)
            if not is_safe:
                return is_safe, threat, detail

        return True, None, None

    @classmethod
    def inspect_tool_call(
        cls,
        tool_name: str,
        arguments: Any,
        firewall_mode: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Deeply inspect a tool invocation with parameter scoping.

        File writing and editing tools do NOT inspect file content parameters,
        preventing false positives on developer source code edits.
        """
        parsed_dict: Optional[Dict[str, Any]] = None

        if isinstance(arguments, str):
            try:
                loaded = json.loads(arguments)
                if isinstance(loaded, dict):
                    parsed_dict = loaded
            except Exception:
                pass
        elif isinstance(arguments, dict):
            parsed_dict = arguments

        clean_tool_name = tool_name.lower().strip()
        is_file_writer = clean_tool_name in FILE_WRITE_TOOLS

        if parsed_dict is not None:
            exec_found = False
            for k, v in parsed_dict.items():
                k_lower = k.lower()
                if k_lower in EXECUTION_ARG_KEYS and isinstance(v, str):
                    exec_found = True
                    is_safe, threat, detail = cls.inspect_text_payload(v)
                    if not is_safe:
                        return False, threat, f"In tool '{tool_name}' ({k}): {detail}"

            if exec_found:
                return True, None, None

            for k, v in parsed_dict.items():
                k_lower = k.lower()
                if is_file_writer and k_lower in NON_EXEC_CONTENT_KEYS:
                    continue
                if isinstance(v, str):
                    is_safe, threat, detail = cls.inspect_text_payload(v)
                    if not is_safe:
                        return False, threat, f"In tool '{tool_name}' ({k}): {detail}"

            return True, None, None

        if isinstance(arguments, str):
            is_safe, threat, detail = cls.inspect_text_payload(arguments)
            if not is_safe:
                return False, threat, f"In tool '{tool_name}': {detail}"

        return True, None, None

    @classmethod
    def inspect_response(
        cls,
        response_json: Dict[str, Any],
        firewall_mode: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[str], Dict[str, Any]]:
        """Inspect all choices in an OpenAI chat completion response for malicious tool_calls.

        In 'block' mode, returns an OpenAI-compatible sanitized refusal structure.
        In 'audit' mode, logs violation but returns the unmodified response.
        """
        active_mode = firewall_mode or cls.mode
        choices = response_json.get("choices", [])

        for choice in choices:
            message = choice.get("message", {})

            tool_calls = message.get("tool_calls", []) or []
            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "")
                args = func.get("arguments", "")
                is_safe, threat_type, detail = cls.inspect_tool_call(name, args)
                if not is_safe:
                    if active_mode == "audit":
                        return False, threat_type, detail, response_json

                    sanitized = dict(response_json)
                    sanitized_choices = [dict(c) for c in choices]
                    idx = choice.get("index", 0)
                    sanitized_choices[idx] = {
                        "index": idx,
                        "message": {
                            "role": "assistant",
                            "content": f"SentinelGate Agent Security Firewall blocked tool execution: {detail}. Operations violating security policies are halted before dispatch.",
                            "refusal": f"Tool call blocked by SentinelGate Agent Security Firewall: {detail}",
                            "tool_calls": None,
                        },
                        "finish_reason": "stop",
                    }
                    sanitized["choices"] = sanitized_choices
                    return False, threat_type, detail, sanitized

            function_call = message.get("function_call")
            if function_call:
                name = function_call.get("name", "")
                args = function_call.get("arguments", "")
                is_safe, threat_type, detail = cls.inspect_tool_call(name, args)
                if not is_safe:
                    if active_mode == "audit":
                        return False, threat_type, detail, response_json

                    sanitized = dict(response_json)
                    sanitized_choices = [dict(c) for c in choices]
                    idx = choice.get("index", 0)
                    sanitized_choices[idx] = {
                        "index": idx,
                        "message": {
                            "role": "assistant",
                            "content": f"SentinelGate Agent Security Firewall blocked function execution: {detail}.",
                            "function_call": None,
                        },
                        "finish_reason": "stop",
                    }
                    sanitized["choices"] = sanitized_choices
                    return False, threat_type, detail, sanitized

        return True, None, None, response_json

    @classmethod
    def inspect_mcp_message(
        cls, mcp_request: Dict[str, Any]
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Inspect an incoming Model Context Protocol (MCP) JSON-RPC request."""
        method = mcp_request.get("method", "")
        params = mcp_request.get("params", {})

        if method in ("tools/call", "prompts/get", "resources/read"):
            tool_name = params.get("name", method)
            args = params.get("arguments", params)
            return cls.inspect_tool_call(tool_name, args)

        return True, None, None


__all__ = ["AgentToolGuard"]
