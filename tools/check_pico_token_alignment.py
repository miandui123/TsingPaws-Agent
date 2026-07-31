#!/usr/bin/env python3
"""Report whether local Pico token sources agree without printing any token."""

from __future__ import annotations

import hashlib
import json
import os
import shlex


def shell_value(path: str, key: str) -> str:
    try:
        lines = open(path, "r", encoding="utf-8").read().splitlines()
    except OSError:
        return ""
    prefix = key + "="
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(prefix):
            value = stripped[len(prefix) :]
            try:
                parsed = shlex.split(value, posix=True)
                return parsed[0] if parsed else ""
            except ValueError:
                return value.strip("'\"")
    return ""


def config_value(path: str) -> str:
    try:
        document = json.load(open(path, "r", encoding="utf-8"))
        return str(document.get("channels", {}).get("pico", {}).get("token", ""))
    except (OSError, ValueError, TypeError, AttributeError):
        return ""

def security_value(path: str) -> str:
    try:
        lines = open(path, "r", encoding="utf-8").read().splitlines()
    except OSError:
        return ""
    channels_indent = None
    pico_indent = None
    for line in lines:
        stripped = line.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        key, separator, value = stripped.partition(":")
        if not separator:
            continue
        key = key.strip()
        if pico_indent is not None and indent <= pico_indent:
            pico_indent = None
        if channels_indent is not None and indent <= channels_indent:
            channels_indent = None
            pico_indent = None
        if channels_indent is None and key == "channels" and not value.strip():
            channels_indent = indent
            continue
        if (
            channels_indent is not None
            and pico_indent is None
            and indent > channels_indent
            and key == "pico"
            and not value.strip()
        ):
            pico_indent = indent
            continue
        if pico_indent is not None and indent > pico_indent and key == "token":
            candidate = value.strip()
            if (
                len(candidate) >= 2
                and candidate[0] == candidate[-1]
                and candidate[0] in ("'", '"')
            ):
                candidate = candidate[1:-1]
            return "" if candidate.startswith("enc://") else candidate
    return ""


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else ""


agent_token = shell_value("/etc/tsingpaws-agent.env", "PICO_TOKEN")
security_token = security_value(
    shell_value("/etc/tsingpaws-agent.env", "PICO_SECURITY_FILE")
    or "/opt/tsingpaw/data/.security.yml"
)
config_token = config_value(
    os.environ.get("PICOCLAW_CONFIG", "/opt/tsingpaw/data/config.json")
)
effective_token = security_token or agent_token
print(
    json.dumps(
        {
            "agent_configured": bool(agent_token),
            "security_configured": bool(security_token),
            "effective_configured": bool(effective_token),
            "tokens_match": bool(
                agent_token and security_token and agent_token == security_token
            ),
            "agent_fingerprint": fingerprint(agent_token),
            "security_fingerprint": fingerprint(security_token),
            "effective_fingerprint": fingerprint(effective_token),
            "config_is_placeholder": config_token in ("", "[NOT_HERE]"),
        },
        ensure_ascii=False,
    )
)
