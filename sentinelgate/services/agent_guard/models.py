from __future__ import annotations

import ipaddress
import re
from typing import Set

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
