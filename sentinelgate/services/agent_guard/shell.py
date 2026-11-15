from __future__ import annotations

import re
import shlex
from typing import List, Optional, Set, Tuple

from sentinelgate.services.agent_guard.models import CRITICAL_PATHS, SENSITIVE_FILES_PATTERN


def inspect_shell(
    command: str,
    allowed_prefixes: Optional[List[str]] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Inspect a shell command using lexical parsing and flag normalization."""
    if not command or not command.strip():
        return True, None, None

    raw_cmd = command.strip()

    # Check policy allowlist prefixes
    if allowed_prefixes:
        if any(raw_cmd.startswith(p) for p in allowed_prefixes):
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
        rhs = norm_cmd.split("<<<", 1)[1].strip().strip("'\"")
        is_sub_safe, threat, detail = inspect_shell(rhs, allowed_prefixes)
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
                is_sub_safe, threat, detail = inspect_shell(sub_inner, allowed_prefixes)
                if not is_sub_safe:
                    return is_sub_safe, threat, detail

    # F. Split command pipelines and sequences (|, ;, &&, ||, &, \n)
    split_segments = re.split(r"[;&\n]+|(?<!<)\|(?!=)", norm_cmd)

    # G. Pipe to shell check (e.g. curl ... | sh, base64 -d | bash)
    if "|" in norm_cmd:
        pipeline_parts = [p.strip() for p in norm_cmd.split("|") if p.strip()]
        for i in range(len(pipeline_parts) - 1):
            left = pipeline_parts[i]
            right = pipeline_parts[i + 1]

            if re.search(r"\b(curl|wget|fetch)\b", left, re.IGNORECASE) and re.search(
                r"\b(bash|sh|zsh|python|perl|ruby)\b", right, re.IGNORECASE
            ):
                return (
                    False,
                    "REMOTE_CODE_EXECUTION_PIPE_TO_SHELL",
                    f"Detected direct curl/wget pipe to shell interpreter: '{left[:40]} | {right[:40]}'.",
                )

            if re.search(r"\bbase64\s+(-[a-zA-Z]*d|--decode)\b", left, re.IGNORECASE) and re.search(
                r"\b(bash|sh|zsh|python|perl|ruby)\b", right, re.IGNORECASE
            ):
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

        # --- 4. REVERSE SHELLS (nc, ncat, netcat) ---
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
