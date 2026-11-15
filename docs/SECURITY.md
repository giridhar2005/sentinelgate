# Security Policy & Threat Model

SentinelGate provides an application-layer LLM gateway with heuristic payload inspection, semantic caching, and streaming secret redaction.

This document describes SentinelGate's **Threat Model**, what it defends against, and its **explicit architectural limitations**.

---

## 1. Threat Model & Security Boundaries

### What SentinelGate Is
- **An Application-Layer Gateway & Proxy:** SentinelGate sits between client applications (coding agents, developers, internal tools) and upstream LLM providers (OpenAI, Anthropic, Groq, Ollama).
- **Heuristic Response Inspector:** Before returning non-streaming LLM responses to autonomous agents, SentinelGate parses the assistant's `tool_calls` / `function_call` JSON payloads to intercept commonly observed destructive shell commands, unconstrained SQL mutations, and SSRF targets.
- **MCP Relay Guardrail:** When acting as a reverse proxy for Model Context Protocol (`/v1/mcp/proxy`), SentinelGate validates target URLs against SSRF patterns and scans incoming `tools/call` parameters.
- **In-Flight Data Sanitizer:** SentinelGate masks PII before forwarding prompts upstream and redacts leaked API keys and credentials in streaming and non-streaming responses.

### What SentinelGate Is NOT
- **NOT an Operating System Sandbox:** SentinelGate does not run agent tools inside isolated kernel namespaces, gVisor containers, Docker seccomp profiles, or microVMs (e.g., Firecracker).
- **NOT an Egress Network Firewall:** SentinelGate cannot govern or monitor network traffic generated directly by client applications outside the gateway transport.
- **NOT an AST Shell Interpreter:** The tool-call inspector uses regular expression patterns and heuristic lexical normalization. It does not execute a full POSIX shell parser or dynamic taint analysis.

---

## 2. Guardrail Capabilities & Coverage

### 2.1 Destructive Shell Interception
SentinelGate inspects `tool_calls` arguments for patterns matching:
- **Unconstrained deletion:** `rm -rf /`, `rm -rf ~`, `rm -rf ..`, `rm --no-preserve-root` (including `$IFS` whitespace substitution).
- **Disk overwriting & formatting:** `mkfs`, `dd of=/dev/sd*`, `dd of=/dev/nvme*`.
- **Denial of service:** Classical fork bombs (`:(){ :|:& };:`).
- **Dangerous permissions:** `chmod 777`, `chmod -R 777`, `chmod a+w`.
- **Remote code execution pipes:** `curl ... | sh`, `wget ... | bash`, `base64 -d | sh`.
- **Reverse shells:** `bash -i >& /dev/tcp/...`, `nc -e /bin/bash`.
- **Credential exfiltration:** Attempts to read `~/.ssh/id_*`, `/etc/shadow`, `/etc/sudoers`, `.aws/credentials`, or `.env`.

### 2.2 Destructive SQL Mutation Interception
SentinelGate intercepts high-risk DDL and DML operations in tool arguments:
- `DROP TABLE`, `DROP DATABASE`, `DROP SCHEMA`, `DROP VIEW`.
- `TRUNCATE TABLE`.
- Unconstrained deletions (`DELETE FROM <table>` without a `WHERE` clause).
- Tautology deletions (`DELETE FROM <table> WHERE 1=1` or `WHERE 'a'='a'`).
- `ALTER TABLE <table> DROP COLUMN`.
- Handles inline SQL comment evasion (`DROP/**/TABLE`, `DROP/*comment*/TABLE`).

### 2.3 SSRF & Metadata Protection
Both `/v1/mcp/proxy` and the tool guard validate outbound destinations:
- **Cloud Metadata Services:** Intercepts `169.254.169.254` (AWS/Azure/GCP metadata) and `metadata.google.internal`.
- **Loopback & Private Networks:** Rejects `127.0.0.1`, `localhost`, `0.0.0.0`, `::1`, and RFC 1918 private ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`).
- **Obfuscated IP Literals:** Resolves bracketed IPv6 literals (`[::1]`, `[::ffff:127.0.0.1]`), integer decimal IPs (`http://2852039166`), hex IPs (`http://0x7f000001`), and octal representations via C-level `inet_aton`.
- **Dangerous Schemes:** Blocks `file://`, `gopher://`, `dict://`, `ldap://`, `sftp://`.

---

## 3. Known Limitations & Bypass Risks

Deploying SentinelGate does not replace OS-level isolation. You should be aware of the following known evasion risks:

### 1. Command Obfuscation & Shell Dynamic Evaluation
Heuristic regex scanning can be bypassed by determined adversaries using:
- **String concatenation:** `a="r"; b="m"; $a$b -rf /`
- **Variable indirection / Subshells:** `eval $(echo cm0gLXJmIC8= | base64 -d)`
- **Polymorphic Python / Node / Ruby scripts:** A tool call that executes `python -c "import os, shutil; shutil.rmtree('/')"` will not match `rm -rf` regex rules.

### 2. Streaming Tool Calls (`stream=True`)
In OpenAI-compatible streaming mode, model output is emitted token-by-token over Server-Sent Events (`SSE`). Tool calls are fragmented across multiple `delta.tool_calls` chunks:
- SentinelGate's stateful sliding window redactor scrubs secrets in streaming text chunks.
- However, tool-call payload inspection only executes on completed non-streaming responses (`/v1/chat/completions`). Clients streaming tool calls should re-assemble arguments and validate them locally before execution.

### 3. Client Agent Compliance
SentinelGate modifies the LLM response to return a standard `refusal` message when a dangerous tool call is detected:
- Compliant agents (e.g. standard LangChain, Cursor, Claude Code) read the refusal content and halt or retry.
- If a client agent ignores model refusals or executes commands through channels not routed through SentinelGate, the gateway cannot prevent the action.

### 4. DNS Rebinding Attacks
When checking external hostnames in `/v1/mcp/proxy`, SentinelGate verifies the hostname syntax and rejects known private suffixes. However:
- A malicious external domain may resolve to a safe public IP during DNS check, and subsequently resolve to `127.0.0.1` upon socket connection (Time-of-Check to Time-of-Use DNS rebinding).
- For high-security environments, run SentinelGate behind an egress proxy that pins resolved socket IPs, or provide a strict allowlist of authorized hostnames.

---

## 4. Recommended Defense-in-Depth Architecture

For production agent deployments, use SentinelGate as the **first-mile application filter**, paired with:
1. **Container / Sandboxing Isolation:** Run agent execution workers inside unprivileged Docker containers, gVisor sandboxes, or temporary Firecracker microVMs.
2. **Read-Only Root Filesystems:** Mount execution filesystems with `read_only: true` and discardable `/tmp` tmpfs.
3. **Restricted IAM & DB Roles:** Ensure database connections used by agents lack `DROP`, `TRUNCATE`, or `ALTER` permissions.
4. **Network Segmentation:** Enforce iptables or cloud security groups blocking outbound access to metadata endpoints (`169.254.169.254`).

---

## 5. Reporting a Security Vulnerability

If you discover a security vulnerability in SentinelGate, please report it privately via GitHub Security Advisories or by emailing the project maintainers directly. Please do not open public GitHub issues for undisclosed security vulnerabilities.
