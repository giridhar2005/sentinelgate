"""Deterministic Pre-Execution Agent Tool-Call & MCP Security Firewall.

Inspects assistant tool_calls / function_call payloads and MCP JSON-RPC messages before
returning them to autonomous agents (Cursor, Claude Code, Cline, LangGraph).

Defenses:
1. Shell: AST & lexical parsing via shlex, flag normalization (-r -f -> -rf), subshell/pipeline
   traversal, tilde/variable resolution. Blocks rm -rf, mkfs, dd, fork bombs, reverse shells,
   and sensitive credential theft.
2. SQL: AST parsing via sqlglot. Blocks destructive DDL/DML (DROP, TRUNCATE, unconstrained
   DELETE/UPDATE without WHERE or with tautologies like 1=1).
3. SSRF: RFC 3986 URL parsing via urllib.parse. Rejects embedded credentials (userinfo),
   resolves DNS via socket.getaddrinfo, and blocks loopback, private RFC 1918, link-local,
   CGNAT (100.64.0.0/10), and cloud metadata endpoints (169.254.169.254, [fd00:ec2::254], 100.100.100.200).
4. Argument Scoping: Scoped strictly to execution parameters (command, query, url) without
   flagging legitimate file write payloads (eliminating false positives on source code writes).
5. Configurable Allowlists & Policy Modes: Supports allowed_shell_prefixes, allowed_sql_tables,
   allowed_external_domains, and configurable block vs audit mode.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import shlex
import socket
import urllib.parse
from typing import Any, Dict, List, Optional, Set, Tuple

import sqlglot
from sqlglot import exp

logger = logging.getLogger("sentinelgate.agent_guard")

# -----------------------------------------------------------------------------
# CONSTANTS & PATTERNS
# -----------------------------------------------------------------------------

EXECUTION_ARG_KEYS: Set[str] = {
    "command",
    "cmd",
    "query",
    "sql",
    "sql_query",
    "url",
    "endpoint",
    "script",
    "bash_command",
    "shell_command",
    "exec",
    "code",
}

NON_EXEC_CONTENT_KEYS: Set[str] = {
    "content",
    "text",
    "file_content",
    "body",
    "file_text",
    "data",
    "diff",
    "patch",
    "comment",
    "explanation",
    "message",
    "replacement",
}

FILE_WRITE_TOOLS: Set[str] = {
    "write_file",
    "create_file",
    "edit_file",
    "patch_file",
    "save_file",
    "append_file",
    "write_to_file",
    "apply_diff",
}

EXECUTION_TOOLS: Set[str] = {
    "bash",
    "sh",
    "terminal",
    "run_command",
    "execute_command",
    "exec",
    "execute",
    "shell",
    "run_shell",
    "cmd",
    "sql",
    "sql_query",
    "database_query",
    "db_query",
    "execute_sql",
    "fetch_url",
    "http_request",
    "curl",
    "get_url",
    "web_fetch",
    "browser",
}

CRITICAL_PATHS: Set[str] = {
    "/",
    "/*",
    "*",
    "~",
    "~/*",
    "..",
    ".",
    "$HOME",
    "${HOME}",
    "/etc",
    "/var",
    "/usr",
    "/boot",
    "/dev",
    "/root",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/sys",
    "/proc",
}

SENSITIVE_FILES_PATTERN = re.compile(
    r"(?:^|/)(?:id_rsa|id_ed25519|id_ecdsa|id_dsa|\.env|\.aws/credentials|\.aws/config|\.docker/config\.json|etc/shadow|etc/sudoers)$",
    re.IGNORECASE,
)

CLOUD_METADATA_HOSTS: Set[str] = {
    "169.254.169.254",
    "fd00:ec2::254",
    "100.100.100.200",
    "metadata.google.internal",
    "metadata.goog",
    "metadata.azure.com",
}

PROHIBITED_SCHEMES: Set[str] = {
    "file",
    "gopher",
    "dict",
    "ldap",
    "ldaps",
    "sftp",
    "tftp",
    "data",
    "expect",
    "php",
}

CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")

URL_EXTRACTOR_REGEX = re.compile(
    r"(?:https?|file|gopher|dict|ldap|sftp)://[^\s'\"<>]+", re.IGNORECASE
)


class AgentToolGuard:
    """Deterministic Pre-Execution Tool-Call and MCP Security Inspector."""

    # Default policy allowlists & mode
    mode: str = "block"  # "block" (default) or "audit"
    allowed_shell_prefixes: List[str] = []
    allowed_sql_tables: List[str] = []
    allowed_external_domains: List[str] = []

    # -------------------------------------------------------------------------
    # 1. SHELL COMMAND INSPECTOR
    # -------------------------------------------------------------------------
    @classmethod
    def inspect_shell(
        cls,
        command: str,
        allowed_prefixes: Optional[List[str]] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Inspect a shell command using lexical parsing and flag normalization."""
        if not command or not command.strip():
            return True, None, None

        raw_cmd = command.strip()

        # Check policy allowlist prefixes
        prefixes = allowed_prefixes if allowed_prefixes is not None else cls.allowed_shell_prefixes
        if prefixes:
            if any(raw_cmd.startswith(p) for p in prefixes):
                return True, None, None

        # A. Preprocessing: Normalize $IFS, carriage returns, and newlines
        norm_cmd = re.sub(r"\$IFS\b|\$\{IFS\}", " ", raw_cmd)
        norm_cmd = norm_cmd.replace("\r\n", "\n").replace("\r", "\n")

        # Resolve simple shell variable assignments within the same command line (e.g. TARGET=/; rm -rf $TARGET)
        var_assignments = dict(
            re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)=['\"]?([^'\"\s;]+)['\"]?", norm_cmd)
        )
        if var_assignments:
            for v_name, v_val in var_assignments.items():
                norm_cmd = re.sub(rf"\$(?:{v_name}\b|\{{{v_name}\}})", v_val, norm_cmd)

        # B. Check for classic fork bombs
        if re.search(r":\(\)\s*\{\s*:\|:&\s*\};:", norm_cmd) or re.search(
            r"\b[a-zA-Z0-9_]+\(\)\s*\{\s*[a-zA-Z0-9_]+\s*\|\s*[a-zA-Z0-9_]+\s*&\s*\};\s*[a-zA-Z0-9_]+",
            norm_cmd,
        ):
            return False, "DESTRUCTIVE_SHELL_FORK_BOMB", "Detected destructive fork bomb."

        # C. Check for /dev/tcp or /dev/udp reverse shells
        if "/dev/tcp/" in norm_cmd or "/dev/udp/" in norm_cmd:
            return (
                False,
                "REVERSE_SHELL_TCP_DEVICE",
                f"Detected /dev/tcp device redirection in '{norm_cmd[:80]}'.",
            )

        # Check for eval base64 execution
        if re.search(r"\beval\s+.*base64\s+(-[a-zA-Z]*d|--decode)", norm_cmd, re.IGNORECASE):
            return (
                False,
                "REMOTE_CODE_EXECUTION_BASE64_DECODE_PIPE",
                f"Detected base64 decode execution via eval in '{norm_cmd[:80]}'.",
            )

        # D. Check for here-string / here-doc pipe to shell: sh <<< "...", bash <<< "..."
        if re.search(r"\b(sh|bash|zsh)\s*<<<\s*", norm_cmd):
            # Recursively inspect the string being fed
            rhs = norm_cmd.split("<<<", 1)[1].strip().strip("'\"")
            is_sub_safe, threat, detail = cls.inspect_shell(rhs, allowed_prefixes)
            if not is_sub_safe:
                return is_sub_safe, threat, detail

        # E. Check for subshell command substitutions: $(...) and `...`
        subshell_patterns = [
            r"\$\((.*?)\)",
            r"`(.*?)`",
            r"<\((.*?)\)",
        ]
        for pattern in subshell_patterns:
            for sub_match in re.finditer(pattern, norm_cmd, re.DOTALL):
                sub_inner = sub_match.group(1).strip()
                if sub_inner:
                    is_sub_safe, threat, detail = cls.inspect_shell(sub_inner, allowed_prefixes)
                    if not is_sub_safe:
                        return is_sub_safe, threat, detail

        # F. Split command pipelines and sequences (|, ;, &&, ||, &, \n)
        # Using a regex that splits on shell operators outside quotes
        split_segments = re.split(r"[;&\n]+|(?<!<)\|(?!=)", norm_cmd)

        # G. Pipe to shell check (e.g. curl ... | sh, base64 -d | bash)
        if "|" in norm_cmd:
            pipeline_parts = [p.strip() for p in norm_cmd.split("|") if p.strip()]
            for i in range(len(pipeline_parts) - 1):
                left = pipeline_parts[i]
                right = pipeline_parts[i + 1]

                # Check curl/wget pipe to shell
                if re.search(r"\b(curl|wget|fetch)\b", left, re.IGNORECASE) and re.search(
                    r"\b(bash|sh|zsh|python|perl|ruby)\b", right, re.IGNORECASE
                ):
                    return (
                        False,
                        "REMOTE_CODE_EXECUTION_PIPE_TO_SHELL",
                        f"Detected direct curl/wget pipe to shell interpreter: '{left[:40]} | {right[:40]}'.",
                    )

                # Check base64 decode pipe to shell
                if re.search(
                    r"\bbase64\s+(-[a-zA-Z]*d|--decode)\b", left, re.IGNORECASE
                ) and re.search(r"\b(bash|sh|zsh|python|perl|ruby)\b", right, re.IGNORECASE):
                    return (
                        False,
                        "REMOTE_CODE_EXECUTION_BASE64_DECODE_PIPE",
                        f"Detected base64 decode pipe to shell: '{left[:40]} | {right[:40]}'.",
                    )

        # H. Inspect each isolated command segment via shlex
        for seg in split_segments:
            seg = seg.strip()
            if not seg:
                continue

            try:
                tokens = shlex.split(seg, comments=True, posix=True)
            except ValueError:
                # Malformed quoting / syntax error in shell command
                # Clean unbalanced quotes and re-parse
                cleaned_seg = seg.replace("'", " ").replace('"', " ")
                try:
                    tokens = shlex.split(cleaned_seg, comments=True, posix=True)
                except ValueError:
                    tokens = [t for t in cleaned_seg.split() if t]

            if not tokens:
                continue

            # Strip command wrappers (sudo, env, nohup, time, exec, command, builtin)
            cmd_idx = 0
            while cmd_idx < len(tokens) and tokens[cmd_idx] in (
                "sudo",
                "env",
                "nohup",
                "time",
                "exec",
                "command",
                "builtin",
            ):
                cmd_idx += 1

            if cmd_idx >= len(tokens):
                continue

            binary = tokens[cmd_idx].lower()
            binary_base = binary.split("/")[-1]
            args = tokens[cmd_idx + 1 :]

            # --- 1. RM / DELETION INSPECTION ---
            if binary_base == "rm":
                flags: Set[str] = set()
                targets: List[str] = []
                has_no_preserve_root = False

                for arg in args:
                    if arg == "--no-preserve-root":
                        has_no_preserve_root = True
                    elif arg.startswith("--"):
                        flags.add(arg.lower())
                    elif arg.startswith("-") and len(arg) > 1:
                        for ch in arg[1:]:
                            flags.add(f"-{ch.lower()}")
                    else:
                        targets.append(arg)

                is_recursive = (
                    "-r" in flags
                    or "-R" in flags
                    or "--recursive" in flags
                    or any("r" in f for f in flags if f.startswith("-") and not f.startswith("--"))
                )
                is_force = (
                    "-f" in flags
                    or "--force" in flags
                    or any("f" in f for f in flags if f.startswith("-") and not f.startswith("--"))
                )

                # Check rm -f /
                if is_force and ("/" in targets or has_no_preserve_root):
                    return (
                        False,
                        "DESTRUCTIVE_SHELL_ROOT_DELETE",
                        f"Detected root deletion in command: '{seg[:80]}'.",
                    )

                if has_no_preserve_root:
                    return (
                        False,
                        "DESTRUCTIVE_SHELL_NO_PRESERVE_ROOT",
                        f"Detected --no-preserve-root in command: '{seg[:80]}'.",
                    )

                if is_recursive:
                    if not targets:
                        # unconstrained recursive rm without targets or with wildcard
                        return (
                            False,
                            "DESTRUCTIVE_SHELL_RECURSIVE_DELETE",
                            f"Detected unconstrained recursive deletion: '{seg[:80]}'.",
                        )
                    for target in targets:
                        clean_target = target.rstrip("/")
                        if (
                            clean_target in CRITICAL_PATHS
                            or target in CRITICAL_PATHS
                            or clean_target == ""
                            or clean_target.startswith("/*")
                            or clean_target.startswith("~/*")
                            or clean_target == ".."
                            or clean_target.startswith("../..")
                        ):
                            return (
                                False,
                                "DESTRUCTIVE_SHELL_RECURSIVE_DELETE",
                                f"Destructive recursive deletion of critical path '{target}': '{seg[:80]}'.",
                            )

            # --- 2. DISK FORMAT / OVERWRITE INSPECTION ---
            if binary_base in ("mkfs", "fdisk", "parted") or binary_base.startswith("mkfs."):
                return (
                    False,
                    "DESTRUCTIVE_SHELL_DISK_FORMAT",
                    f"Detected disk formatting utility '{binary_base}': '{seg[:80]}'.",
                )

            if binary_base == "dd":
                for arg in args:
                    if arg.startswith("of=/dev/"):
                        return (
                            False,
                            "DESTRUCTIVE_SHELL_DISK_OVERWRITE",
                            f"Detected raw disk overwrite '{arg}': '{seg[:80]}'.",
                        )

            # --- 3. DANGEROUS PERMISSION CHANGES ---
            if binary_base == "chmod":
                mode_args = [a for a in args if not a.startswith("-")]
                for mode in mode_args:
                    if mode in (
                        "777",
                        "0777",
                        "a+w",
                        "o+w",
                        "u=rwx,g=rwx,o=rwx",
                        "a=rwx",
                        "ugo=rwx",
                    ):
                        return (
                            False,
                            "DANGEROUS_PERMISSIONS_CHMOD_777",
                            f"Detected dangerously permissive chmod mode '{mode}': '{seg[:80]}'.",
                        )

            # --- 4. REVERSE SHELLS (nc, ncat, netcat, python, perl) ---
            if binary_base in ("nc", "ncat", "netcat"):
                for arg in args:
                    if arg in ("-e", "-c") or (arg.startswith("-") and ("e" in arg or "c" in arg)):
                        return (
                            False,
                            "REVERSE_SHELL_NETCAT",
                            f"Detected netcat reverse shell flag '{arg}': '{seg[:80]}'.",
                        )

            # --- 5. SENSITIVE CREDENTIAL EXFILTRATION ---
            if binary_base in (
                "cat",
                "head",
                "tail",
                "less",
                "more",
                "cp",
                "scp",
                "rsync",
                "curl",
                "wget",
            ):
                for arg in args:
                    # Check if target matches sensitive files pattern
                    clean_path = arg.strip("'\"")
                    if SENSITIVE_FILES_PATTERN.search(clean_path):
                        threat_type = (
                            "CREDENTIAL_EXFILTRATION_PRIVATE_KEY"
                            if "id_" in clean_path or "shadow" in clean_path
                            else "CREDENTIAL_EXFILTRATION_DOTENV"
                        )
                        return (
                            False,
                            threat_type,
                            f"Detected sensitive credential access on '{clean_path}': '{seg[:80]}'.",
                        )

        return True, None, None

    # -------------------------------------------------------------------------
    # 2. SQL QUERY INSPECTOR
    # -------------------------------------------------------------------------
    @classmethod
    def inspect_sql(
        cls,
        sql_query: str,
        allowed_tables: Optional[List[str]] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Inspect a SQL query for destructive mutations (DROP, TRUNCATE, unconstrained DELETE/UPDATE)."""
        if not sql_query or not sql_query.strip():
            return True, None, None

        raw_sql = sql_query.strip()

        # 1. AST Parsing via sqlglot
        try:
            expressions = sqlglot.parse(raw_sql)
        except Exception:
            expressions = []

        # Check policy allowlist tables
        tables_allowlist = allowed_tables if allowed_tables is not None else cls.allowed_sql_tables
        if tables_allowlist and expressions:
            for expr in expressions:
                if expr is not None:
                    for tbl_node in expr.find_all(exp.Table):
                        tbl_name = tbl_node.name.lower()
                        if tbl_name and not any(
                            tbl_name == t.lower() or tbl_name.startswith(t.lower())
                            for t in tables_allowlist
                        ):
                            return (
                                False,
                                "DESTRUCTIVE_SQL_TABLE_NOT_ALLOWLISTED",
                                f"Table '{tbl_name}' is not in allowed_sql_tables.",
                            )

        if expressions:
            for expression in expressions:
                if expression is None:
                    continue

                # Check DROP statement
                if isinstance(expression, exp.Drop) or expression.key == "drop":
                    return (
                        False,
                        "DESTRUCTIVE_SQL_DROP",
                        f"Destructive SQL statement: '{raw_sql[:80]}'",
                    )

                # Check TRUNCATE statement
                if isinstance(expression, exp.TruncateTable) or "truncate" in expression.key:
                    return (
                        False,
                        "DESTRUCTIVE_SQL_TRUNCATE",
                        f"Destructive SQL statement: '{raw_sql[:80]}'",
                    )

                # Check DELETE statement
                if isinstance(expression, exp.Delete) or expression.key == "delete":
                    where = expression.find(exp.Where)
                    if where is None:
                        return (
                            False,
                            "DESTRUCTIVE_SQL_UNCONSTRAINED_DELETE",
                            f"Unconstrained DELETE statement without WHERE clause: '{raw_sql[:80]}'",
                        )
                    # Check for tautology in WHERE clause
                    where_sql = where.sql().upper().replace(" ", "")
                    if where_sql in (
                        "WHERE1=1",
                        "WHERE'A'='A'",
                        "WHERETRUE",
                        "WHERE1",
                        "WHERE(1=1)",
                        "WHERE(TRUE)",
                    ):
                        return (
                            False,
                            "DESTRUCTIVE_SQL_TAUTOLOGY_DELETE",
                            f"Tautology DELETE statement: '{raw_sql[:80]}'",
                        )
                    # Check for binary equality tautologies (x = x)
                    for eq in where.find_all(exp.EQ):
                        if eq.this.sql().strip() == eq.expression.sql().strip():
                            return (
                                False,
                                "DESTRUCTIVE_SQL_TAUTOLOGY_DELETE",
                                f"Tautology DELETE condition '{eq.sql()}': '{raw_sql[:80]}'",
                            )

                # Check UPDATE statement
                if isinstance(expression, exp.Update) or expression.key == "update":
                    where = expression.find(exp.Where)
                    if where is None:
                        return (
                            False,
                            "DESTRUCTIVE_SQL_UNCONSTRAINED_UPDATE",
                            f"Unconstrained UPDATE statement without WHERE clause: '{raw_sql[:80]}'",
                        )
                    where_sql = where.sql().upper().replace(" ", "")
                    if where_sql in (
                        "WHERE1=1",
                        "WHERE'A'='A'",
                        "WHERETRUE",
                        "WHERE1",
                        "WHERE(1=1)",
                        "WHERE(TRUE)",
                    ):
                        return (
                            False,
                            "DESTRUCTIVE_SQL_TAUTOLOGY_UPDATE",
                            f"Tautology UPDATE statement: '{raw_sql[:80]}'",
                        )
                    for eq in where.find_all(exp.EQ):
                        if eq.this.sql().strip() == eq.expression.sql().strip():
                            return (
                                False,
                                "DESTRUCTIVE_SQL_TAUTOLOGY_UPDATE",
                                f"Tautology UPDATE condition '{eq.sql()}': '{raw_sql[:80]}'",
                            )

                # Check ALTER TABLE ... DROP COLUMN
                if isinstance(expression, exp.Alter):
                    if expression.find(exp.Drop):
                        return (
                            False,
                            "DESTRUCTIVE_SQL_DROP_COLUMN",
                            f"Destructive ALTER TABLE DROP: '{raw_sql[:80]}'",
                        )

        # 2. Fallback Regex Parsing (Catches syntax evasions, comments like DROP/**/TABLE)
        clean_sql = re.sub(r"/\*.*?\*/", " ", raw_sql, flags=re.DOTALL)
        clean_sql = re.sub(r"--[^\n]*", " ", clean_sql)
        clean_sql = re.sub(r"\s+", " ", clean_sql).strip()

        if re.search(r"\bDROP\s+(TABLE|DATABASE|SCHEMA|VIEW)\b", clean_sql, re.IGNORECASE):
            return (
                False,
                "DESTRUCTIVE_SQL_DROP",
                f"Destructive SQL statement: '{raw_sql[:80]}'",
            )

        if re.search(r"\bTRUNCATE\s+(TABLE\s+)?", clean_sql, re.IGNORECASE):
            return (
                False,
                "DESTRUCTIVE_SQL_TRUNCATE",
                f"Destructive SQL statement: '{raw_sql[:80]}'",
            )

        # Tautology delete
        if re.search(
            r"\bDELETE\s+FROM\s+[`\"'\w\.]+\s+WHERE\s+(?:1\s*=\s*1|'[^']+'\s*=\s*'[^']+'|true)(?:\b|;|--|\s|$)",
            clean_sql,
            re.IGNORECASE,
        ):
            return (
                False,
                "DESTRUCTIVE_SQL_TAUTOLOGY_DELETE",
                f"Destructive SQL statement: '{raw_sql[:80]}'",
            )

        # Unconstrained delete
        if re.search(r"\bDELETE\s+FROM\s+[`\"'\w\.]+\s*(;|--|$)", clean_sql, re.IGNORECASE):
            return (
                False,
                "DESTRUCTIVE_SQL_UNCONSTRAINED_DELETE",
                f"Destructive SQL statement: '{raw_sql[:80]}'",
            )

        return True, None, None

    # -------------------------------------------------------------------------
    # 3. URL & SSRF INSPECTOR
    # -------------------------------------------------------------------------
    @classmethod
    def inspect_url(
        cls,
        url_string: str,
        allowed_domains: Optional[List[str]] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Inspect a URL for SSRF, metadata access, private CIDRs, and obfuscated IPs."""
        if not url_string or not url_string.strip():
            return True, None, None

        clean_url = url_string.strip()

        # Reject protocol scheme violations
        try:
            parsed = urllib.parse.urlsplit(clean_url)
        except Exception:
            return False, "SSRF_MALFORMED_URL", f"Malformed URL: '{clean_url[:80]}'"

        scheme = parsed.scheme.lower() if parsed.scheme else ""
        if scheme in PROHIBITED_SCHEMES:
            return (
                False,
                "SSRF_DISALLOWED_PROTOCOL_SCHEME",
                f"Prohibited URL scheme '{scheme}://' in '{clean_url[:80]}'.",
            )

        if scheme and scheme not in ("http", "https"):
            return (
                False,
                "SSRF_DISALLOWED_PROTOCOL_SCHEME",
                f"Unsupported URL scheme '{scheme}://'. Only http and https are permitted.",
            )

        # Reject embedded userinfo (credentials)
        if parsed.username or parsed.password:
            return (
                False,
                "SSRF_USERINFO_DISALLOWED",
                f"Embedded userinfo credentials disallowed in URL: '{clean_url[:80]}'.",
            )

        hostname = parsed.hostname
        if not hostname:
            return True, None, None

        hostname_clean = hostname.strip("[]").lower()

        # Check known cloud metadata hosts
        if hostname_clean in CLOUD_METADATA_HOSTS:
            return (
                False,
                "SSRF_CLOUD_METADATA_AWS_AZURE"
                if "169.254" in hostname_clean
                else "SSRF_CLOUD_METADATA_GCP",
                f"Prohibited cloud metadata host '{hostname_clean}'.",
            )

        # Check obfuscated numeric IP formats (Dword, Hex, Octal)
        parsed_ip: Optional[ipaddress.IPv4Address | ipaddress.IPv6Address] = None

        # Dword integer format: e.g. 2130706433 or 2852039166
        if hostname_clean.isdigit():
            try:
                parsed_ip = ipaddress.IPv4Address(int(hostname_clean))
            except Exception:
                pass
        # Hex format: e.g. 0x7f000001
        elif hostname_clean.startswith("0x") or hostname_clean.startswith("0X"):
            try:
                parsed_ip = ipaddress.IPv4Address(int(hostname_clean, 16))
            except Exception:
                pass
        # Octal format: e.g. 0177.0.0.1 or 017700000001
        elif hostname_clean.startswith("0") and "." in hostname_clean:
            try:
                oct_parts = [int(p, 8) for p in hostname_clean.split(".")]
                if len(oct_parts) == 4:
                    parsed_ip = ipaddress.IPv4Address(bytes(oct_parts))
            except Exception:
                pass

        if parsed_ip is None:
            try:
                parsed_ip = ipaddress.ip_address(hostname_clean)
            except ValueError:
                pass

        # If it parsed directly as an IP, evaluate it immediately
        resolved_ips: List[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        if parsed_ip is not None:
            resolved_ips.append(parsed_ip)
        else:
            # Perform DNS resolution via socket.getaddrinfo
            try:
                addr_info = socket.getaddrinfo(hostname_clean, None)
                for entry in addr_info:
                    ip_str = entry[4][0]
                    try:
                        resolved_ips.append(ipaddress.ip_address(ip_str))
                    except ValueError:
                        pass
            except socket.gaierror:
                pass

        # Evaluate all resolved IP addresses
        for ip in resolved_ips:
            # Check loopback
            if ip.is_loopback:
                return (
                    False,
                    "SSRF_LOCAL_LOOPBACK",
                    f"Prohibited loopback target '{ip}' in '{clean_url[:80]}'.",
                )

            # Check link-local / cloud metadata (169.254.0.0/16 or fe80::/10)
            if ip.is_link_local or str(ip) == "169.254.169.254" or str(ip) == "100.100.100.200":
                return (
                    False,
                    "SSRF_CLOUD_METADATA_AWS_AZURE",
                    f"Prohibited link-local/cloud metadata IP '{ip}' in '{clean_url[:80]}'.",
                )

            # Check private RFC 1918
            if ip.is_private:
                return (
                    False,
                    "SSRF_INTERNAL_PRIVATE_CIDR",
                    f"Prohibited private network IP '{ip}' in '{clean_url[:80]}'.",
                )

            # Check CGNAT (100.64.0.0/10)
            if isinstance(ip, ipaddress.IPv4Address) and ip in CGNAT_NETWORK:
                return (
                    False,
                    "SSRF_INTERNAL_PRIVATE_CIDR",
                    f"Prohibited Carrier-Grade NAT (CGNAT) IP '{ip}' in '{clean_url[:80]}'.",
                )

            # Check multicast, reserved, unspecified (0.0.0.0)
            if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                return (
                    False,
                    "SSRF_LOCAL_LOOPBACK" if ip.is_unspecified else "SSRF_DISALLOWED_IP_TARGET",
                    f"Prohibited reserved/unspecified IP '{ip}' in '{clean_url[:80]}'.",
                )

        # Domain allowlist validation
        domains_allowlist = (
            allowed_domains if allowed_domains is not None else cls.allowed_external_domains
        )
        if domains_allowlist:
            matched = any(
                hostname_clean == d.lower() or hostname_clean.endswith("." + d.lower())
                for d in domains_allowlist
            )
            if not matched:
                return (
                    False,
                    "SSRF_DOMAIN_NOT_ALLOWLISTED",
                    f"Domain '{hostname_clean}' is not in allowed_external_domains.",
                )

        return True, None, None

    # -------------------------------------------------------------------------
    # 4. UNIFIED TEXT PAYLOAD SCANNER
    # -------------------------------------------------------------------------
    @classmethod
    def inspect_text_payload(cls, text: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """Scan a raw string against shell, SQL, and SSRF security rule sets."""
        if not text or not text.strip():
            return True, None, None

        # 1. Shell check
        is_safe, threat, detail = cls.inspect_shell(text)
        if not is_safe:
            return is_safe, threat, detail

        # 2. SQL check
        is_safe, threat, detail = cls.inspect_sql(text)
        if not is_safe:
            return is_safe, threat, detail

        # 3. SSRF check on URLs embedded in the text
        for m in URL_EXTRACTOR_REGEX.finditer(text):
            candidate_url = m.group(0)
            is_safe, threat, detail = cls.inspect_url(candidate_url)
            if not is_safe:
                return is_safe, threat, detail

        return True, None, None

    # -------------------------------------------------------------------------
    # 5. TOOL CALL & ARGUMENT SCOPER
    # -------------------------------------------------------------------------
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

        # If tool is known to write files, never inspect file content
        clean_tool_name = tool_name.lower().strip()
        is_file_writer = clean_tool_name in FILE_WRITE_TOOLS

        if parsed_dict is not None:
            # 1. Check if execution keys are present (e.g. command, query, url)
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

            # 2. If it's a file write tool, skip non-exec content arguments
            for k, v in parsed_dict.items():
                k_lower = k.lower()
                if is_file_writer and k_lower in NON_EXEC_CONTENT_KEYS:
                    continue
                if isinstance(v, str):
                    is_safe, threat, detail = cls.inspect_text_payload(v)
                    if not is_safe:
                        return False, threat, f"In tool '{tool_name}' ({k}): {detail}"

            return True, None, None

        # Non-dict / raw string payload
        if isinstance(arguments, str):
            is_safe, threat, detail = cls.inspect_text_payload(arguments)
            if not is_safe:
                return False, threat, f"In tool '{tool_name}': {detail}"

        return True, None, None

    # -------------------------------------------------------------------------
    # 6. RESPONSE & REFUSAL SYNTHESIS
    # -------------------------------------------------------------------------
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

            # Check modern tool_calls
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

            # Check legacy function_call
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

    # -------------------------------------------------------------------------
    # 7. MCP JSON-RPC INSPECTOR
    # -------------------------------------------------------------------------
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
