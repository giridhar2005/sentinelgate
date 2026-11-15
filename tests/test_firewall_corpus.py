"""Corpus Benchmark & Policy Acceptance Tests for AgentToolGuard.

Evaluates precision, recall, and false positive rate on the committed
corpus of >= 150 malicious attack vectors and >= 150 benign developer tasks.
Gates CI on:
- Recall >= 0.90
- False Positive Rate (FPR) <= 0.02
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sentinelgate.services.agent_guard import AgentToolGuard

CORPUS_DIR = Path(__file__).parent / "corpus"


@pytest.fixture(scope="module")
def corpus_data():
    mal_path = CORPUS_DIR / "malicious.json"
    ben_path = CORPUS_DIR / "benign.json"

    assert mal_path.exists(), f"Missing malicious corpus at {mal_path}"
    assert ben_path.exists(), f"Missing benign corpus at {ben_path}"

    with open(mal_path, "r", encoding="utf-8") as f:
        malicious = json.load(f)
    with open(ben_path, "r", encoding="utf-8") as f:
        benign = json.load(f)

    return malicious, benign


def test_firewall_corpus_size_and_diversity(corpus_data):
    """Verify test corpus satisfies >= 150 malicious and >= 150 benign sample requirements."""
    malicious, benign = corpus_data
    assert len(malicious) >= 150, f"Expected >= 150 malicious samples, found {len(malicious)}"
    assert len(benign) >= 150, f"Expected >= 150 benign samples, found {len(benign)}"

    # Ensure diverse attack categories are present
    mal_cats = {m.get("category") for m in malicious}
    expected_cats = {
        "shell_rm",
        "shell_ifs",
        "shell_evasion",
        "shell_disk",
        "shell_perms",
        "shell_pipe",
        "shell_reverse",
        "shell_cred",
        "sql_destructive",
        "ssrf_metadata",
    }
    assert expected_cats.issubset(mal_cats), f"Missing categories: {expected_cats - mal_cats}"


def test_firewall_corpus_precision_recall_gates(corpus_data):
    """Benchmark AgentToolGuard against corpus and enforce Recall >= 0.90 and FPR <= 0.02."""
    malicious, benign = corpus_data

    tp = 0
    fn = 0
    for m in malicious:
        is_safe, threat, detail = AgentToolGuard.inspect_tool_call(m["tool"], m["arguments"])
        if not is_safe:
            tp += 1
        else:
            fn += 1

    fp = 0
    tn = 0
    for b in benign:
        is_safe, threat, detail = AgentToolGuard.inspect_tool_call(b["tool"], b["arguments"])
        if not is_safe:
            fp += 1
        else:
            tn += 1

    total_mal = len(malicious)
    total_ben = len(benign)
    recall = tp / total_mal if total_mal else 0.0
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    fpr = fp / total_ben if total_ben else 0.0

    assert recall >= 0.90, f"Recall {recall:.4f} below 0.90 gate (TP={tp}, FN={fn})"
    assert fpr <= 0.02, f"False Positive Rate {fpr:.4f} exceeds 0.02 gate (FP={fp}, TN={tn})"
    assert precision >= 0.95, f"Precision {precision:.4f} below 0.95 (TP={tp}, FP={fp})"


def test_firewall_policy_allowlists():
    """Verify configurable policy allowlists for shell prefixes, SQL tables, and domains."""
    # 1. Shell prefix allowlist
    git_push = "git push origin main"
    git_clean = "git clean -fd"

    # Default check
    assert AgentToolGuard.inspect_shell(git_push)[0] is True
    assert AgentToolGuard.inspect_shell(git_clean)[0] is True
    # With explicit allowlist
    assert AgentToolGuard.inspect_shell(git_push, allowed_prefixes=["git "])[0] is True

    # 2. SQL table allowlist
    temp_query = "SELECT * FROM temp_scratch;"
    assert AgentToolGuard.inspect_sql(temp_query, allowed_tables=["temp_"])[0] is True
    assert (
        AgentToolGuard.inspect_sql("SELECT * FROM secret_vault;", allowed_tables=["temp_"])[0]
        is False
    )

    # 3. Domain allowlist
    github_url = "https://api.github.com/repos"
    assert AgentToolGuard.inspect_url(github_url, allowed_domains=["api.github.com"])[0] is True

    unauthorized_url = "https://evil.example.com/api"
    assert (
        AgentToolGuard.inspect_url(unauthorized_url, allowed_domains=["api.github.com"])[0] is False
    )


def test_firewall_mode_audit_vs_block():
    """Verify firewall behavior in audit mode vs block mode."""
    malicious_resp = {
        "id": "chatcmpl-test-audit",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_mal_1",
                            "type": "function",
                            "function": {"name": "bash", "arguments": '{"command": "rm -rf /"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }

    # Block mode (default): returns refusal structure replacing tool_calls
    is_safe, threat, detail, blocked_resp = AgentToolGuard.inspect_response(
        malicious_resp, firewall_mode="block"
    )
    assert is_safe is False
    assert blocked_resp["choices"][0]["message"]["tool_calls"] is None
    assert "refusal" in blocked_resp["choices"][0]["message"]

    # Audit mode: returns is_safe False for logging, but leaves response payload untouched
    is_safe_audit, threat_audit, detail_audit, audit_resp = AgentToolGuard.inspect_response(
        malicious_resp, firewall_mode="audit"
    )
    assert is_safe_audit is False
    assert audit_resp["choices"][0]["message"]["tool_calls"] is not None
