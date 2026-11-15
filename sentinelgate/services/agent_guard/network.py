from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from typing import List, Optional, Tuple

from sentinelgate.services.agent_guard.models import (
    CGNAT_NETWORK,
    CLOUD_METADATA_HOSTS,
    PROHIBITED_SCHEMES,
)


def inspect_url(
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

    if hostname_clean.isdigit():
        try:
            parsed_ip = ipaddress.IPv4Address(int(hostname_clean))
        except Exception:
            pass
    elif hostname_clean.startswith("0x") or hostname_clean.startswith("0X"):
        try:
            parsed_ip = ipaddress.IPv4Address(int(hostname_clean, 16))
        except Exception:
            pass
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

    resolved_ips: List[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    if parsed_ip is not None:
        resolved_ips.append(parsed_ip)
    else:
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

    for ip in resolved_ips:
        if ip.is_loopback:
            return (
                False,
                "SSRF_LOCAL_LOOPBACK",
                f"Prohibited loopback target '{ip}' in '{clean_url[:80]}'.",
            )

        if ip.is_link_local or str(ip) == "169.254.169.254" or str(ip) == "100.100.100.200":
            return (
                False,
                "SSRF_CLOUD_METADATA_AWS_AZURE",
                f"Prohibited link-local/cloud metadata IP '{ip}' in '{clean_url[:80]}'.",
            )

        if ip.is_private:
            return (
                False,
                "SSRF_INTERNAL_PRIVATE_CIDR",
                f"Prohibited private network IP '{ip}' in '{clean_url[:80]}'.",
            )

        if isinstance(ip, ipaddress.IPv4Address) and ip in CGNAT_NETWORK:
            return (
                False,
                "SSRF_INTERNAL_PRIVATE_CIDR",
                f"Prohibited Carrier-Grade NAT (CGNAT) IP '{ip}' in '{clean_url[:80]}'.",
            )

        if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return (
                False,
                "SSRF_LOCAL_LOOPBACK" if ip.is_unspecified else "SSRF_DISALLOWED_IP_TARGET",
                f"Prohibited reserved/unspecified IP '{ip}' in '{clean_url[:80]}'.",
            )

    if allowed_domains:
        matched = any(
            hostname_clean == d.lower() or hostname_clean.endswith("." + d.lower())
            for d in allowed_domains
        )
        if not matched:
            return (
                False,
                "SSRF_DOMAIN_NOT_ALLOWLISTED",
                f"Domain '{hostname_clean}' is not in allowed_external_domains.",
            )

    return True, None, None
