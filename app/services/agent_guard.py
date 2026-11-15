"""Deterministic Pre-Execution Agent Tool-Call & MCP Security Firewall.

Inspects assistant tool_calls / function_call payloads and MCP JSON-RPC messages before
returning them to autonomous agents (Cursor, Claude Code, Cline, LangGraph).
Blocks:
1. Destructive shell commands (rm -rf, mkfs, dd, fork bombs, reverse shells, SSH key theft)
2. Destructive SQL mutations (DROP TABLE/DATABASE, TRUNCATE, unconstrained DELETE FROM)
3. SSRF / unauthorized outbound domains (cloud metadata 169.254.169.254, loopbacks, private CIDRs)
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("sentinelgate.agent_guard")

# Shell command attack patterns
DESTRUCTIVE_SHELL_PATTERNS = [
    # Recursive deletion of critical paths or unconstrained rm -rf (handles standard spacing or $IFS)
    (
        re.compile(
            r"\brm(\s+|\$IFS\s*)(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|--recursive)(\s+|\$IFS\s*).*?(/|\*|~|/\*|\.\.)",
            re.IGNORECASE,
        ),
        "DESTRUCTIVE_SHELL_RECURSIVE_DELETE",
    ),
    (
        re.compile(
            r"\brm(\s+|\$IFS\s*)(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)(\s+|\$IFS\s*).*?--no-preserve-root",
            re.IGNORECASE,
        ),
        "DESTRUCTIVE_SHELL_NO_PRESERVE_ROOT",
    ),
    (
        re.compile(r"\brm(\s+|\$IFS\s*)-[a-zA-Z]*f(\s+|\$IFS\s*)/\b", re.IGNORECASE),
        "DESTRUCTIVE_SHELL_ROOT_DELETE",
    ),
    # Raw filesystem/disk formatting or overwriting
    (re.compile(r"\bmkfs(\.[a-z0-9]+)?\s+", re.IGNORECASE), "DESTRUCTIVE_SHELL_DISK_FORMAT"),
    (
        re.compile(
            r"\bdd\s+.*of=/dev/(sd[a-z]|nvme[0-9]n[0-9]|hd[a-z]|vd[a-z]|null|zero)", re.IGNORECASE
        ),
        "DESTRUCTIVE_SHELL_DISK_OVERWRITE",
    ),
    # Fork bomb
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:", re.IGNORECASE), "DESTRUCTIVE_SHELL_FORK_BOMB"),
    # Dangerously permissive permission changes
    (
        re.compile(r"\bchmod\s+(-[a-zA-Z]*R\s+)?(777|o\+w|a\+w)\b", re.IGNORECASE),
        "DANGEROUS_PERMISSIONS_CHMOD_777",
    ),
    # Direct curl/wget pipe to shell
    (
        re.compile(r"\b(curl|wget|fetch)\s+[^|;]+\|\s*(bash|sh|zsh|python|perl)\b", re.IGNORECASE),
        "REMOTE_CODE_EXECUTION_PIPE_TO_SHELL",
    ),
    # Base64 decode pipe to shell
    (
        re.compile(
            r"\bbase64\s+(-[a-zA-Z]*d|--decode)\s*\|\s*(bash|sh|zsh|python|perl)\b", re.IGNORECASE
        ),
        "REMOTE_CODE_EXECUTION_BASE64_DECODE_PIPE",
    ),
    # Reverse shells
    (
        re.compile(r"\b(bash|sh|zsh)\s+-i\s+>&\s*/dev/tcp/", re.IGNORECASE),
        "REVERSE_SHELL_TCP_DEVICE",
    ),
    (
        re.compile(
            r"\b(nc|ncat|netcat)\s+-[a-zA-Z]*[ec]\s+(/bin/|/usr/bin/)?(bash|sh|zsh)", re.IGNORECASE
        ),
        "REVERSE_SHELL_NETCAT",
    ),
    # Sensitive credential and key exfiltration
    (
        re.compile(
            r"\b(cat|head|tail|less|more|cp|scp|rsync)\s+.*(~?/\.ssh/id_[a-z0-9_]+|/etc/shadow|/etc/sudoers)",
            re.IGNORECASE,
        ),
        "CREDENTIAL_EXFILTRATION_PRIVATE_KEY",
    ),
    (
        re.compile(
            r"\b(cat|head|tail|less|more)\s+.*~?/\.(aws/credentials|env|docker/config\.json)",
            re.IGNORECASE,
        ),
        "CREDENTIAL_EXFILTRATION_DOTENV",
    ),
]

# SQL mutation patterns (handles optional inline comments like DROP/**/TABLE)
DESTRUCTIVE_SQL_PATTERNS = [
    (
        re.compile(r"\bDROP\s*(?:/\*.*?\*/\s*)*(TABLE|DATABASE|SCHEMA|VIEW)\b", re.IGNORECASE),
        "DESTRUCTIVE_SQL_DROP",
    ),
    (
        re.compile(r"\bTRUNCATE\s*(?:/\*.*?\*/\s*)*(TABLE\s+)?", re.IGNORECASE),
        "DESTRUCTIVE_SQL_TRUNCATE",
    ),
    # DELETE without WHERE clause
    (
        re.compile(r"\bDELETE\s*(?:/\*.*?\*/\s*)*FROM\s+([`\"'\w\.]+)\s*(;|--|$)", re.IGNORECASE),
        "DESTRUCTIVE_SQL_UNCONSTRAINED_DELETE",
    ),
    # Tautology DELETE (WHERE 1=1 or 'a'='a' or true)
    (
        re.compile(
            r"\bDELETE\s*(?:/\*.*?\*/\s*)*FROM\s+([`\"'\w\.]+)\s+WHERE\s+(?:1\s*=\s*1|'[^']+'\s*=\s*'[^']+'|true)(?:\b|;|--|\s|$)",
            re.IGNORECASE,
        ),
        "DESTRUCTIVE_SQL_TAUTOLOGY_DELETE",
    ),
    (
        re.compile(
            r"\bALTER\s*(?:/\*.*?\*/\s*)*TABLE\s+([`\"'\w\.]+)\s+DROP\s+COLUMN\b", re.IGNORECASE
        ),
        "DESTRUCTIVE_SQL_DROP_COLUMN",
    ),
]

# SSRF and cloud metadata patterns
SSRF_PATTERNS = [
    (re.compile(r"https?://169\.254\.169\.254", re.IGNORECASE), "SSRF_CLOUD_METADATA_AWS_AZURE"),
    (re.compile(r"https?://metadata\.google\.internal", re.IGNORECASE), "SSRF_CLOUD_METADATA_GCP"),
    (
        re.compile(r"https?://(127\.0\.0\.1|localhost|0\.0\.0\.0|::1)\b", re.IGNORECASE),
        "SSRF_LOCAL_LOOPBACK",
    ),
    (
        re.compile(r"https?://\[(::1|0:0:0:0:0:0:0:1|::ffff:127\.0\.0\.1)\]", re.IGNORECASE),
        "SSRF_LOCAL_LOOPBACK_IPV6",
    ),
    (
        re.compile(
            r"https?://(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b",
            re.IGNORECASE,
        ),
        "SSRF_INTERNAL_PRIVATE_CIDR",
    ),
    (
        re.compile(
            r"https?://(2852039166|2130706433|0x7f000001|017700000001|0177\.0\.0\.1)\b",
            re.IGNORECASE,
        ),
        "SSRF_OBFUSCATED_IP",
    ),
    (
        re.compile(r"\b(file|gopher|dict|ldap|sftp)://", re.IGNORECASE),
        "SSRF_DISALLOWED_PROTOCOL_SCHEME",
    ),
]

URL_HOST_EXTRACTOR = re.compile(r"https?://(\[[0-9a-fA-F:\.]+\]|[^/:\s]+)", re.IGNORECASE)


def _check_ip_safety(host_str: str) -> Tuple[bool, Optional[str]]:
    """Helper to evaluate if a hostname resolves to a prohibited loopback/private/link-local IP."""
    clean_host = host_str.strip("[]")
    ip = None
    try:
        ip = ipaddress.ip_address(clean_host)
    except ValueError:
        try:
            raw_bytes = socket.inet_aton(clean_host)
            ip = ipaddress.IPv4Address(raw_bytes)
        except (OSError, ValueError):
            pass

    if ip is not None:
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False, f"Prohibited IP address literal: {ip}"
    return True, None


class AgentToolGuard:
    """Pre-Execution Tool-Call and MCP Security Inspector."""

    @classmethod
    def inspect_text_payload(cls, text: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """Scan a single raw argument or command string against all security rule sets."""
        if not text:
            return True, None, None

        # 1. Shell checks
        for pattern, threat_type in DESTRUCTIVE_SHELL_PATTERNS:
            match = pattern.search(text)
            if match:
                return False, threat_type, f"Destructive shell pattern: '{match.group(0)[:80]}'"

        # 2. SQL checks
        for pattern, threat_type in DESTRUCTIVE_SQL_PATTERNS:
            match = pattern.search(text)
            if match:
                return False, threat_type, f"Destructive SQL statement: '{match.group(0)[:80]}'"

        # 3. SSRF checks (static regex patterns + extracted URL host parsing)
        for pattern, threat_type in SSRF_PATTERNS:
            match = pattern.search(text)
            if match:
                return (
                    False,
                    threat_type,
                    f"Unauthorized SSRF/internal network target: '{match.group(0)[:80]}'",
                )

        for url_match in URL_HOST_EXTRACTOR.finditer(text):
            host_candidate = url_match.group(1)
            is_safe, err = _check_ip_safety(host_candidate)
            if not is_safe:
                return False, "SSRF_DISALLOWED_IP_TARGET", f"Unauthorized SSRF target: {err}"

        return True, None, None

    @classmethod
    def inspect_tool_call(
        cls, tool_name: str, arguments: Any
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Deeply inspect a tool invocation (name + structured or JSON arguments)."""
        # Convert arguments to string representations for scanning
        text_reprs: List[str] = []
        if isinstance(arguments, str):
            text_reprs.append(arguments)
            try:
                parsed = json.loads(arguments)
                if isinstance(parsed, dict):
                    text_reprs.extend(str(v) for v in parsed.values())
            except Exception:
                pass
        elif isinstance(arguments, dict):
            text_reprs.extend(str(v) for v in arguments.values())
            text_reprs.append(json.dumps(arguments))
        else:
            text_reprs.append(str(arguments))

        for text in text_reprs:
            is_safe, threat_type, detail = cls.inspect_text_payload(text)
            if not is_safe:
                return False, threat_type, f"In tool '{tool_name}': {detail}"

        return True, None, None

    @classmethod
    def inspect_response(
        cls, response_json: Dict[str, Any]
    ) -> Tuple[bool, Optional[str], Optional[str], Dict[str, Any]]:
        """Inspect all choices in an OpenAI chat completion response for malicious tool_calls.

        If a violation is found, returns an OpenAI-compatible sanitized refusal structure
        so client agents safely self-correct without crashing.
        """
        choices = response_json.get("choices", [])
        for choice in choices:
            message = choice.get("message", {})

            # Check modern tool_calls
            tool_calls = message.get("tool_calls", []) or []
            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "")
                args = func.get("arguments", "")
                is_safe, threat_type, detail = cls.inspect_tool_call(name, args)
                if not is_safe:
                    sanitized = dict(response_json)
                    sanitized_choices = [dict(c) for c in choices]
                    sanitized_choices[choice.get("index", 0)] = {
                        "index": choice.get("index", 0),
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

            # Check legacy function_call
            function_call = message.get("function_call")
            if function_call:
                name = function_call.get("name", "")
                args = function_call.get("arguments", "")
                is_safe, threat_type, detail = cls.inspect_tool_call(name, args)
                if not is_safe:
                    sanitized = dict(response_json)
                    sanitized_choices = [dict(c) for c in choices]
                    sanitized_choices[choice.get("index", 0)] = {
                        "index": choice.get("index", 0),
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
