from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("sentinelgate.guardrails")

# Secret & Credential Leak Patterns
SECRET_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9_-]{20,}", re.IGNORECASE),  # OpenAI / Standard API key
    re.compile(r"sk-ant-[a-zA-Z0-9_-]{20,}", re.IGNORECASE),  # Anthropic API key
    re.compile(r"AKIA[0-9A-Z]{16}", re.IGNORECASE),  # AWS Access Key
    re.compile(r"eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}", re.IGNORECASE),  # JWT token
    re.compile(r"gh[pousr]_[a-zA-Z0-9]{36}", re.IGNORECASE),  # GitHub Token
    re.compile(
        r"(?:postgres(?:ql)?|mongodb(?:\+srv)?|mysql|redis):\/\/[^\s\"'>]+", re.IGNORECASE
    ),  # Database URIs
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN
    re.compile(r"\b(?:\d{4}[ -]?){3}\d{4}\b"),  # Credit Card
]

# Comprehensive Secret & PII Redaction Rules for Safe Local/Cloud Relay
SECRET_REDACTION_RULES = [
    ("KEY_OPENAI", re.compile(r"\b(?:sk-[a-zA-Z0-9_-]{20,})\b", re.IGNORECASE)),
    ("KEY_ANTHROPIC", re.compile(r"\b(?:sk-ant-[a-zA-Z0-9_-]{20,})\b", re.IGNORECASE)),
    ("KEY_AWS", re.compile(r"\b(?:AKIA[0-9A-Z]{16})\b")),
    ("KEY_GITHUB", re.compile(r"\b(?:gh[pousr]_[a-zA-Z0-9]{36})\b")),
    (
        "DB_URI",
        re.compile(
            r"\b(?:postgres(?:ql)?|mongodb(?:\+srv)?|mysql|redis):\/\/[^\s\"'`<>]+", re.IGNORECASE
        ),
    ),
    ("JWT", re.compile(r"\beyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]+\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CREDIT_CARD", re.compile(r"\b(?:\d{4}[ -]?){3}\d{4}\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")),
]


class SecretRedactor:
    """Reversible Secret & Credential Redaction Firewall.

    Scrubs API keys, DB URIs, JWTs, and PII from prompts before they leave localhost,
    and automatically restores them in returned LLM responses/streams.
    """

    @classmethod
    def redact_text(
        cls,
        text: str,
        mapping: Dict[str, str],
        reverse_mapping: Dict[str, str],
    ) -> Tuple[str, int]:
        if not text or not isinstance(text, str):
            return text, 0

        scrubbed = 0
        for tag, pattern in SECRET_REDACTION_RULES:

            def _replace(m):
                nonlocal scrubbed
                val = m.group(0)
                if val in reverse_mapping:
                    return reverse_mapping[val]
                scrubbed += 1
                idx = len(mapping) + 1
                token = f"__SG_SECRET_{tag}_{idx}__"
                mapping[token] = val
                reverse_mapping[val] = token
                return token

            text = pattern.sub(_replace, text)
        return text, scrubbed

    @classmethod
    def redact_messages(
        cls, messages: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], Dict[str, str], int]:
        mapping: Dict[str, str] = {}
        reverse_mapping: Dict[str, str] = {}
        total_scrubbed = 0
        sanitized = []
        for m in messages:
            new_m = copy.deepcopy(m)
            content = new_m.get("content")
            if isinstance(content, str):
                new_content, count = cls.redact_text(content, mapping, reverse_mapping)
                new_m["content"] = new_content
                total_scrubbed += count
            sanitized.append(new_m)
        return sanitized, mapping, total_scrubbed

    @classmethod
    def restore_text(cls, text: str, mapping: Dict[str, str]) -> str:
        if not text or not mapping or not isinstance(text, str):
            return text
        for token, original in mapping.items():
            if token in text:
                text = text.replace(token, original)
        return text

    @classmethod
    def restore_in_response(
        cls, response_data: Dict[str, Any], mapping: Dict[str, str]
    ) -> Dict[str, Any]:
        if not mapping or not isinstance(response_data, dict):
            return response_data
        resp = copy.deepcopy(response_data)
        choices = resp.get("choices", [])
        for c in choices:
            msg = c.get("message")
            if msg and isinstance(msg.get("content"), str):
                msg["content"] = cls.restore_text(msg["content"], mapping)
            delta = c.get("delta")
            if delta and isinstance(delta.get("content"), str):
                delta["content"] = cls.restore_text(delta["content"], mapping)
        return resp


# Prompt Injection & Jailbreak Override Patterns
INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(previous|prior)\s+instructions", re.IGNORECASE),
    re.compile(r"system\s+override", re.IGNORECASE),
    re.compile(
        r"reveal\s+(your\s+)?(internal\s+)?(system\s+)?(prompt|instructions)", re.IGNORECASE
    ),
    re.compile(r"output\s+(your\s+)?(entire\s+)?system\s+prompt", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+in\s+DAN\s+mode", re.IGNORECASE),
    re.compile(r"bypass\s+(all\s+)?(safety|security)\s+filters", re.IGNORECASE),
    re.compile(r"[\u200B-\u200D\uFEFF]"),  # Zero-width steganography characters
]

EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
PHONE_REGEX = re.compile(r"(?:\+?\d{1,3}[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}\b")
API_KEY_REGEX = re.compile(r"\b(?:sk-[a-zA-Z0-9_-]{20,}|ghp_[a-zA-Z0-9]{36}|AKIA[0-9A-Z]{16})\b")


class PIIGuardrail:
    """Reversible regex-based PII & Secret masking and restoration."""

    @classmethod
    def mask_text(cls, text: str, mapping: Dict[str, str]) -> str:
        if not text or not isinstance(text, str):
            return text

        def _replace_entity(match, tag: str) -> str:
            val = match.group(0)
            for ph, orig in mapping.items():
                if orig == val and tag in ph:
                    return ph
            idx = sum(1 for k in mapping if tag in k) + 1
            ph = f"{{{{PII_{tag}_{idx}}}}}"
            mapping[ph] = val
            return ph

        text = EMAIL_REGEX.sub(lambda m: _replace_entity(m, "EMAIL"), text)
        text = PHONE_REGEX.sub(lambda m: _replace_entity(m, "PHONE"), text)
        text = API_KEY_REGEX.sub(lambda m: _replace_entity(m, "KEY"), text)
        return text

    @classmethod
    def mask_messages(
        cls, messages: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        mapping: Dict[str, str] = {}
        masked = []
        for m in messages:
            new_m = copy.deepcopy(m)
            content = new_m.get("content")
            if isinstance(content, str):
                new_m["content"] = cls.mask_text(content, mapping)
            masked.append(new_m)
        return masked, mapping

    @classmethod
    def restore_text(cls, text: str, mapping: Dict[str, str]) -> str:
        if not text or not mapping:
            return text
        for ph, orig in mapping.items():
            text = text.replace(ph, orig)
        return text

    @classmethod
    def restore_in_response(
        cls, response_data: Dict[str, Any], mapping: Dict[str, str]
    ) -> Dict[str, Any]:
        if not mapping:
            return response_data
        resp = copy.deepcopy(response_data)
        choices = resp.get("choices", [])
        for c in choices:
            msg = c.get("message")
            if msg and isinstance(msg.get("content"), str):
                msg["content"] = cls.restore_text(msg["content"], mapping)
            delta = c.get("delta")
            if delta and isinstance(delta.get("content"), str):
                delta["content"] = cls.restore_text(delta["content"], mapping)
        return resp


class SecurityInspector:
    """Pre-flight security inspector for secrets leak and prompt injections."""

    @classmethod
    def inspect_secrets(cls, messages: List[Dict[str, Any]]) -> Tuple[bool, Optional[str]]:
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                for pattern in SECRET_PATTERNS:
                    match = pattern.search(content)
                    if match:
                        return (
                            False,
                            f"Sensitive secret or API key credential detected: '{match.group(0)[:12]}...'",
                        )
        return True, None

    @classmethod
    def inspect_injection(cls, messages: List[Dict[str, Any]]) -> Tuple[bool, Optional[str]]:
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                for pattern in INJECTION_PATTERNS:
                    match = pattern.search(content)
                    if match:
                        return (
                            False,
                            f"Prompt injection / system override detected: '{match.group(0)}'",
                        )
        return True, None


class ContextCompactor:
    """Trims conversational filler in long dialogs (>8 turns) while preserving instructions."""

    FILLER = re.compile(r"^(ok|okay|thanks|thank you|got it|understood|sure)[\.!\s]*$", re.I)

    @classmethod
    def compact(cls, messages: List[Dict[str, Any]], max_turns: int = 8) -> List[Dict[str, Any]]:
        if len(messages) <= max_turns:
            return messages
        sys_m = [m for m in messages if m.get("role") == "system"]
        chat_m = [m for m in messages if m.get("role") != "system"]
        culled = [
            m
            for i, m in enumerate(chat_m)
            if i >= len(chat_m) - 4 or not cls.FILLER.match((m.get("content") or "").strip())
        ]
        return sys_m + culled[-max_turns:]


class PromptOptimizer:
    """Eliminates redundant whitespace outside code fences, saving tokens without altering code."""

    @classmethod
    def optimize_text(cls, text: str) -> str:
        if not text or not isinstance(text, str):
            return text
        segments = re.split(r"(```[\s\S]*?```)", text)
        optimized_segments = []
        for i, seg in enumerate(segments):
            if i % 2 == 1:
                optimized_segments.append(seg)
            else:
                lines = [re.sub(r"[ \t]+", " ", line).rstrip() for line in seg.split("\n")]
                cleaned = "\n".join(lines)
                cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
                optimized_segments.append(cleaned.strip())
        return "".join(optimized_segments)

    @classmethod
    def optimize_messages(cls, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        optimized = []
        for m in messages:
            new_m = copy.deepcopy(m)
            content = new_m.get("content")
            if isinstance(content, str):
                new_m["content"] = cls.optimize_text(content)
            optimized.append(new_m)
        return optimized


class JSONSelfHealer:
    """Intercepts invalid JSON outputs and triggers 1-shot self-repair retry."""

    @classmethod
    def is_json_requested(cls, request_data: Dict[str, Any]) -> bool:
        rf = request_data.get("response_format")
        return isinstance(rf, dict) and rf.get("type") == "json_object"

    @classmethod
    async def repair_response(
        cls,
        router,
        request_data: Dict[str, Any],
        initial_response: Dict[str, Any],
    ) -> Dict[str, Any]:
        choice = initial_response.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "")
        parse_error_msg = ""
        try:
            json.loads(content)
            return initial_response
        except Exception as e:
            parse_error_msg = str(e)
            logger.warning(f"Invalid JSON detected from LLM: {e}. Triggering 1-shot repair...")

        repair_messages = list(request_data.get("messages", []))
        repair_messages.append({"role": "assistant", "content": content})
        repair_messages.append(
            {
                "role": "user",
                "content": f"CRITICAL: Your output was invalid JSON ({parse_error_msg}). Return ONLY valid, well-formed JSON.",
            }
        )

        repair_req = dict(request_data)
        repair_req["messages"] = repair_messages
        repair_req["temperature"] = 0.0

        try:
            repaired_resp, _, _ = await router.execute_request(repair_req, stream=False)
            repaired_content = (
                repaired_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
            )
            json.loads(repaired_content)
            repaired_resp["sentinel_json_repaired"] = True
            return repaired_resp
        except Exception as e:
            logger.error(f"1-shot JSON repair failed: {e}")
            return initial_response
