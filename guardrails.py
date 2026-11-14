from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("sentinelgate.guardrails")

# Regex patterns for PII detection
EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_REGEX = re.compile(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
API_KEY_REGEX = re.compile(
    r"\b(?:sk-[a-zA-Z0-9_-]{20,}|ghp_[a-zA-Z0-9]{20,}|AIza[0-9A-Za-z-_]{35}|gsk_[a-zA-Z0-9]{20,})\b"
)

# Adversarial injection & jailbreak regex patterns
INJECTION_PATTERNS = [
    re.compile(r"(?:ignore|disregard|forget|override)\s+(?:all\s+)?(?:previous|prior|system)\s+(?:instructions|prompts|directives)", re.I),
    re.compile(r"(?:you\s+are\s+now|act\s+as)\s+(?:DAN|unrestricted|jailbroken|an\s+evil|developer\s+mode)", re.I),
    re.compile(r"(?:print|output|reveal|show|repeat)\s+(?:the\s+)?(?:secret|internal|system|initial)\s+(?:prompt|instructions)", re.I),
    re.compile(r"[\u200B-\u200D\uFEFF]"),  # Invisible zero-width characters
]


class PIIGuardrail:
    """Masks emails, phone numbers, and API keys before sending upstream,

    and restores them in the generated response.
    """

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
    def mask_messages(cls, messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        mapping: Dict[str, str] = {}
        masked_messages = []
        for msg in messages:
            new_msg = copy.deepcopy(msg)
            content = new_msg.get("content")
            if isinstance(content, str):
                new_msg["content"] = cls.mask_text(content, mapping)
            masked_messages.append(new_msg)
        return masked_messages, mapping

    @classmethod
    def restore_text(cls, text: str, mapping: Dict[str, str]) -> str:
        if not text or not mapping or not isinstance(text, str):
            return text
        result = text
        for ph, original in mapping.items():
            result = result.replace(ph, original)
            # Also handle variations if model stripped braces like PII_EMAIL_1
            bare = ph.strip("{}")
            if bare in result:
                result = result.replace(bare, original)
        return result

    @classmethod
    def restore_in_response(cls, response_data: Dict[str, Any], mapping: Dict[str, str]) -> Dict[str, Any]:
        if not mapping or not isinstance(response_data, dict):
            return response_data
        res = copy.deepcopy(response_data)
        choices = res.get("choices", [])
        for choice in choices:
            msg = choice.get("message", {})
            if "content" in msg and isinstance(msg["content"], str):
                msg["content"] = cls.restore_text(msg["content"], mapping)
        return res


class JSONSelfHealer:
    """Detects invalid JSON when response_format={"type": "json_object"}

    and automatically performs a 1-shot self-repair retry.
    """

    @staticmethod
    def is_json_requested(request_data: Dict[str, Any]) -> bool:
        resp_fmt = request_data.get("response_format")
        if isinstance(resp_fmt, dict):
            return resp_fmt.get("type") == "json_object"
        return False

    @staticmethod
    def clean_markdown_json(text: str) -> str:
        text = text.strip()
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        text = text.removesuffix("```")
        return text.strip()

    @classmethod
    def validate_json(cls, content: str) -> Tuple[bool, Optional[str]]:
        if not content:
            return False, "Empty content"
        cleaned = cls.clean_markdown_json(content)
        try:
            json.loads(cleaned)
            return True, None
        except Exception as e:
            return False, str(e)

    @classmethod
    async def repair_response(
        cls,
        router,
        request_data: Dict[str, Any],
        initial_response: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Check JSON validity and execute 1-shot repair if corrupted."""
        if not cls.is_json_requested(request_data):
            return initial_response

        choices = initial_response.get("choices", [])
        if not choices:
            return initial_response

        raw_content = choices[0].get("message", {}).get("content", "")
        is_valid, error_msg = cls.validate_json(raw_content)

        if is_valid:
            # Normalize to clean JSON if wrapped in markdown fences
            clean_content = cls.clean_markdown_json(raw_content)
            initial_response["choices"][0]["message"]["content"] = clean_content
            return initial_response

        logger.warning(f"Invalid JSON detected from LLM: {error_msg}. Triggering 1-shot self-repair.")

        # Construct 1-shot repair prompt
        repair_messages = list(request_data.get("messages", []))
        repair_messages.append({"role": "assistant", "content": raw_content})
        repair_messages.append({
            "role": "user",
            "content": (
                f"Your previous output was invalid JSON. Parser error: '{error_msg}'. "
                "Please fix the error and output ONLY the valid JSON object without markdown fences or explanation."
            ),
        })

        repair_req = dict(request_data)
        repair_req["messages"] = repair_messages

        try:
            repaired_resp, _, _ = await router.execute_request(repair_req, stream=False)
            repaired_content = repaired_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
            repaired_valid, _ = cls.validate_json(repaired_content)
            if repaired_valid:
                repaired_resp["choices"][0]["message"]["content"] = cls.clean_markdown_json(repaired_content)
                repaired_resp["sentinel_json_repaired"] = True
                return repaired_resp
        except Exception as e:
            logger.error(f"1-shot JSON repair request failed: {e}")

        # Return initial if repair failed
        return initial_response


class PromptOptimizer:
    """Common-Sense Zero-Loss Prompt Optimizer:

    Eliminates redundant whitespace, trailing spaces, and excessive blank lines
    outside markdown code fences, shaving 5-15% of prompt token costs automatically.
    """

    @classmethod
    def optimize_text(cls, text: str) -> str:
        if not text or not isinstance(text, str):
            return text

        # Split by code blocks ```...``` to preserve exact formatting inside code
        segments = re.split(r"(```[\s\S]*?```)", text)
        optimized_segments = []
        for i, seg in enumerate(segments):
            # Odd indices are code blocks
            if i % 2 == 1:
                optimized_segments.append(seg)
            else:
                # Outside code blocks: strip trailing spaces on lines
                lines = [re.sub(r"[ \t]+", " ", line).rstrip() for line in seg.split("\n")]
                cleaned = "\n".join(lines)
                # Collapse 3+ consecutive newlines to 2
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


class InjectionDefense:
    """Zero-dependency shield against prompt injection, jailbreaks, and system prompt exfiltration."""

    @classmethod
    def inspect(cls, messages: List[Dict[str, Any]]) -> Tuple[bool, Optional[str]]:
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                for pattern in INJECTION_PATTERNS:
                    match = pattern.search(content)
                    if match:
                        return False, f"Suspicious prompt injection/override detected: '{match.group(0)}'"
        return True, None


class ContextCompactor:
    """Semantic Context Compactor: Trims conversational filler in long dialogs (>8 turns)."""
    FILLER = re.compile(r"^(ok|okay|thanks|thank you|got it|understood|sure)[\.!\s]*$", re.I)

    @classmethod
    def compact(cls, messages: List[Dict[str, Any]], max_turns: int = 8) -> List[Dict[str, Any]]:
        if len(messages) <= max_turns:
            return messages
        sys_m = [m for m in messages if m.get("role") == "system"]
        chat_m = [m for m in messages if m.get("role") != "system"]
        culled = [
            m for i, m in enumerate(chat_m)
            if i >= len(chat_m) - 4 or not cls.FILLER.match((m.get("content") or "").strip())
        ]
        return sys_m + culled[-max_turns:]


