# Security Policy

## Supported Versions

SentinelGate security patches and vulnerability updates are released for the active minor version branch:

| Version | Supported          |
| :------ | :----------------- |
| `0.3.x` | :white_check_mark: |
| `< 0.3` | :x:                |

---

## Reporting a Vulnerability

Security is a foundational tenet of SentinelGate. The gateway handles sensitive prompt streams, secret redaction, and API credentials.

If you discover a security vulnerability (such as a guardrail regex bypass, token leakage, authentication flaw, or injection vector):

1. **Do NOT open a public GitHub issue.**
2. Send an advisory report with reproduction steps to the project maintainer:
   - **Maintainer:** Giridhar ([@giridhar2005](https://github.com/giridhar2005))
   - Open a private security advisory through [GitHub Security Advisories](https://github.com/giridhar2005/sentinelgate/security/advisories/new).
3. Include in your report:
   - Gateway version and environment details.
   - Minimal reproduction script or HTTP curl payload.
   - Potential impact and threat assessment.

You will receive an initial response within 24 hours acknowledging receipt and coordinating remediation.

---

## Pre-Flight Guardrails & Threat Scope

SentinelGate includes sub-millisecond heuristic guardrails for leaked secrets and prompt injections. While these patterns are tuned against known credential formats (OpenAI, AWS, GitHub, DB URIs, JWTs, SSNs) and injection vectors:

- Guardrails run entirely on CPU in-process to maintain `<0.5ms` response time.
- Security updates to compiled regex dictionaries are prioritized for rapid point releases.
