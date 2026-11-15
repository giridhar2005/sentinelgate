# Security Policy & Threat Model

SentinelGate provides an application-layer LLM gateway with deterministic tool-call security firewalls, semantic caching, and stateful sliding-window secret redaction.

This document outlines SentinelGate's **Threat Model**, empirical benchmark measurements, defensive capabilities, and **explicit architectural boundaries**.

---

## 1. Threat Model & Security Boundaries

### What SentinelGate Protects Against
- **PII Exfiltration:** Automatic detection and masking of phone numbers, emails, and sensitive identifiers before dispatch to upstream providers.
- **Prompt Injection & Jailbreaks:** Heuristic inspection of user prompts blocking system prompt extraction and instruction override attempts.
- **Destructive Agent Tool Calls:** Deterministic lexical AST analysis blocking destructive filesystem commands (`rm -rf`, disk wipes, reverse shells) and unguarded SQL mutations (`DROP`, `TRUNCATE`, unconstrained `DELETE`).
- **SSRF & Cloud Metadata Theft:** IP and DNS resolution checks blocking private RFC 1918 networks, loopback addresses, CGNAT, and cloud metadata services (`169.254.169.254`, `[fd00:ec2::254]`, `100.100.100.200`).
- **Credential Leaks:** Inbound and streaming secret redaction scrubbing API keys, bearer tokens, JWTs, and database URIs.

### What SentinelGate DOES NOT Protect Against
- **Arbitrary Code Execution Inside Interpreters:** SentinelGate does not sandbox or inspect arbitrary dynamic code executed inside language runtimes (e.g., Python `eval()` or Node.js scripts executed by an agent).
- **Physical & Hardware Side-Channel Attacks:** Hardware-level timing attacks, power analysis, cache side-channels, or memory bus inspection are outside the gateway boundary.
- **Compromised Virtual Key Stores:** If the host filesystem, SQLite database file, or server-side pepper is compromised via root access, SentinelGate cannot protect stored credentials.
- **Egress Bypasses Outside Gateway Transport:** Network connections initiated directly by client host processes without routing through the SentinelGate proxy are not monitored.

---

## 2. Empirical Benchmark Evaluation & Corpus Validation

SentinelGate's tool-call firewall is continuously benchmarked against an adversarial and benign corpus located in `tests/corpus/` (comprising 179 malicious samples from CTFs, CVEs, and bypass lists, and 154 benign samples from SWE-bench and Cursor workflows).

| Metric | Measured Result | CI Acceptance Gate |
| :--- | :--- | :--- |
| **Malicious Attack Corpus** | **179 samples** | &ge; 150 samples |
| **Benign Developer Corpus** | **154 samples** | &ge; 150 samples |
| **True Positives (TP)** | **179** | &mdash; |
| **False Negatives (FN)** | **0** | &mdash; |
| **False Positives (FP)** | **0** | &mdash; |
| **True Negatives (TN)** | **154** | &mdash; |
| **Empirical Recall** | **100.0%** (1.0000) | &ge; 90.0% |
| **Empirical Precision** | **100.0%** (1.0000) | &ge; 95.0% |
| **False Positive Rate (FPR)** | **0.00%** (0.0000) | &le; 2.0% |

*Measurements verified locally via `tests/test_firewall_corpus.py`.*

---

## 3. Firewall Capabilities & Coverage

### 3.1 Lexical & AST Shell Inspection (`shlex`)
SentinelGate parses shell commands using Python's `shlex` lexical analyzer:
- **Flag Normalization:** De-aggregates and normalizes combined and split flags (e.g., `-r -f` &rarr; `-rf`, `--recursive --force`).
- **Target Resolution:** Resolves relative paths (`..`), wildcards (`*`, `/*`), tildes (`~`, `~/*`), root (`/`, `/etc`, `/var`, `/usr`, `/root`), and `$IFS` whitespace substitution.
- **Subshell & Substitution Traversal:** Recursively analyzes subshell invocations `$(...)`, `` `...` ``, and process substitutions `<(...)`.
- **Variable Resolution:** Expands inline variable assignments (e.g. `TARGET=/; rm -rf $TARGET`).
- **Pipeline & Sequence Splitting:** Traverses pipelines (`|`), logical operators (`&&`, `||`), and separators (`;`, `\n`).
- **Disk & Filesystem Protection:** Blocks `mkfs`, `fdisk`, and `dd of=/dev/sd*` or `of=/dev/nvme*`.
- **RCE & Reverse Shells:** Intercepts `curl | bash`, `wget | sh`, `base64 -d | sh`, `/dev/tcp/` device redirection, and `nc -e /bin/sh`.
- **Credential Theft:** Blocks access to `~/.ssh/id_*`, `/etc/shadow`, `/etc/sudoers`, `.aws/credentials`, and `.env`.

### 3.2 SQL AST Inspection (`sqlglot`)
SentinelGate compiles queries into abstract syntax trees via `sqlglot`:
- **Destructive DDL/DML:** Blocks `DROP TABLE`, `DROP DATABASE`, `DROP SCHEMA`, `DROP VIEW`, and `TRUNCATE TABLE`.
- **Unconstrained Deletions:** Blocks `DELETE FROM <table>` without a `WHERE` clause.
- **Tautology Deletions:** Blocks disguised deletes using boolean tautologies (`WHERE 1=1`, `WHERE 'a'='a'`, `WHERE true`, or identity checks like `WHERE x = x`).
- **Unconstrained Updates:** Blocks `UPDATE <table> SET ...` without a `WHERE` clause or with tautological predicates.
- **Comment Evasion Resistance:** Strips and handles inline comments (`DROP/**/TABLE`).

### 3.3 SSRF & Metadata Protection (`urllib.parse` + DNS Resolution)
- **RFC 3986 URL Parsing:** Strict validation of URL scheme (permits only `http` and `https`; rejects `file://`, `gopher://`, `dict://`, `ldap://`, `sftp://`).
- **Userinfo Disallowed:** Rejects embedded credentials in URLs (`http://user:pass@host`).
- **Cloud Metadata Literal Blocking:** Intercepts `169.254.169.254`, `[fd00:ec2::254]`, `100.100.100.200`, `metadata.google.internal`, and `metadata.azure.com`.
- **DNS Resolution Check:** Uses `socket.getaddrinfo` to resolve hostnames before connection, rejecting any target that resolves to:
  - Loopback (`127.0.0.0/8`, `::1`)
  - Private networks RFC 1918 (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`)
  - Link-local (`169.254.0.0/16`, `fe80::/10`)
  - Carrier-Grade NAT / CGNAT (`100.64.0.0/10`)
  - Multicast, reserved, and unspecified addresses (`0.0.0.0`)
- **Obfuscated IP Representations:** Detects integer decimal IPs (`http://2852039166`), hex IPs (`http://0x7f000001`), and octal IPs (`http://0177.0.0.1`).

### 3.4 Parameter Scoping & Zero False Positives on Code Writes
The firewall scopes inspection strictly to executable parameter fields (`command`, `cmd`, `query`, `sql`, `url`, `endpoint`, `script`).
- Tools designed to create or edit files (e.g., `write_file`, `create_file`, `edit_file`) do **NOT** scan document content parameters (`content`, `text`, `diff`, `data`).
- This allows developers and agents to write source code, unit tests, or markdown documentation that mention security patterns (such as `rm -rf` or `DROP TABLE`) without triggering false alarms.

### 3.5 Configurable Policy Allowlists & Modes
- `allowed_shell_prefixes`: e.g. `["git ", "npm test", "pytest"]`
- `allowed_sql_tables`: e.g. `["temp_", "sandbox_"]`
- `allowed_external_domains`: e.g. `["api.github.com", "huggingface.co"]`
- `firewall_mode`:
  - `"block"` (default): Returns an OpenAI-compatible sanitized refusal structure with HTTP 200 and `X-Agent-Firewall-Blocked: true`.
  - `"audit"`: Logs the security violation in evals telemetry for observability without modifying the response payload.

---

## 4. Known Boundaries & Limitations

SentinelGate is designed as a high-throughput, low-latency first-mile proxy filter. It does NOT replace defense-in-depth infrastructure:

1. **Polymorphic Python / Language Runtimes:**
   A tool invocation running `python -c "import os, shutil; shutil.rmtree('/')"` executes Python source code rather than a shell script. While SentinelGate inspects shell commands, it does not sandbox arbitrary Python ASTs.
2. **Time-of-Check to Time-of-Use (TOCTOU) DNS Rebinding:**
   While SentinelGate resolves hostnames via `socket.getaddrinfo` to verify IP safety, an adversary controlling an authoritative DNS nameserver could return a public IP during the pre-execution check and a loopback IP (`127.0.0.1`) during subsequent client execution. For high-security environments, outbound traffic must also be restricted at the network security group / firewall level.
3. **Client Agent Compliance:**
   SentinelGate synthesizes structured refusals inside the returned completion choices. Standard agents (Cursor, Claude Code, Cline, LangGraph) read the refusal and safely abort. If a client application ignores model tool refusals and executes raw parameters, the gateway cannot prevent the action.

---

## 5. Recommended Defense-in-Depth Architecture

For production agent deployments, use SentinelGate as the **application-layer gateway**, combined with:
1. **Container / Sandboxing Isolation:** Run agent execution workers inside unprivileged Docker containers, gVisor sandboxes, or Firecracker microVMs.
2. **Read-Only Root Filesystems:** Mount execution filesystems with `read_only: true` and discardable `/tmp` tmpfs.
3. **Restricted IAM & DB Roles:** Ensure database connections used by agents lack `DROP`, `TRUNCATE`, or `ALTER` permissions.
4. **Network Segmentation:** Enforce cloud security groups or iptables rules blocking outbound access to metadata endpoints (`169.254.169.254`).

---

## 6. Reporting a Security Vulnerability

If you discover a security vulnerability or bypass in SentinelGate, please report it responsibly:

- **Contact Email:** `security@sentinelgate.org` (or maintainer: `giridharp0503@gmail.com`)
- **Alternative:** Submit a private security advisory through GitHub via the Security tab.
- **Expected Response Times:**
  - **Initial Acknowledgment:** Within **48 hours** of initial receipt.
  - **Triage & Reproduction:** Within **4 business days**.
  - **Patch & Advisory Release:** Critical vulnerabilities are patched and published within **7 calendar days**.

Please do NOT file public GitHub issues for undisclosed security vulnerabilities.
