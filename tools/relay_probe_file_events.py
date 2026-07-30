#!/usr/bin/env python3
"""Probe Relay transparency for file.* / typing.* / response.* events.

Reads credentials from environment only — never prints tokens or Authorization.

Required env:
  RELAY_WS_URL          e.g. ws://host:8787/v1/agent/connect
  DEVICE_TOKEN          internal_test device token
  # OR for single_node:
  RELAY_URL + RELAY_TOKEN + DEVICE_ID

Optional:
  PEER_WAIT_SEC         seconds to wait for peer echo (default 3)
  SESSION_ID            fixed session id (default random UUID)

Usage:
  DEVICE_TOKEN=... RELAY_WS_URL=ws://193.112.152.197:8787/v1/agent/connect \\
    python3 tools/relay_probe_file_events.py

Exit 0 when the local send of each probe type succeeds (Relay accepted the frame).
Does not require a live APP peer; if a peer is connected, any echo is noted
without dumping payloads.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from typing import List, Optional, Tuple
from urllib.parse import urlencode, urlparse, urlunparse

try:
    from websockets.legacy.client import connect as ws_connect
except ImportError:
    print("FAIL: websockets not installed", file=sys.stderr)
    raise SystemExit(2)


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name, default) or "").strip()


def connect_target() -> Tuple[str, dict]:
    token = env("DEVICE_TOKEN")
    ws_url = env("RELAY_WS_URL")
    if token and ws_url:
        return ws_url, {"Authorization": f"Bearer {token}"}
    relay_url = env("RELAY_URL")
    relay_token = env("RELAY_TOKEN")
    device_id = env("DEVICE_ID", "home-001")
    if not relay_url or len(relay_token) < 16:
        print("FAIL: set DEVICE_TOKEN+RELAY_WS_URL or RELAY_URL+RELAY_TOKEN", file=sys.stderr)
        raise SystemExit(2)
    parsed = urlparse(relay_url)
    q = {}
    if parsed.query:
        for part in parsed.query.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                q[k] = v
    q["device_id"] = device_id
    url = urlunparse(parsed._replace(query=urlencode(q)))
    return url, {"Authorization": f"Bearer {relay_token}"}


def tiny_envelope(msg_type: str, session_id: str, payload: dict) -> dict:
    return {
        "type": msg_type,
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "timestamp": int(time.time() * 1000),
        "payload": payload,
    }


async def run() -> int:
    url, headers = connect_target()
    # Never print headers / token
    session_id = env("SESSION_ID") or str(uuid.uuid4())
    wait_sec = float(env("PEER_WAIT_SEC", "3") or "3")
    transfer_id = str(uuid.uuid4())
    # Tiny harmless payload (not a real file / not a secret).
    probes = [
        tiny_envelope(
            "file.start",
            session_id,
            {
                "transfer_id": transfer_id,
                "name": "probe.txt",
                "mime_type": "text/plain",
                "size": 4,
                "chunk_count": 1,
                "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            },
        ),
        tiny_envelope(
            "file.chunk",
            session_id,
            {"transfer_id": transfer_id, "index": 0, "data": "cHJvYmU="},  # "probe" truncated ok
        ),
        tiny_envelope(
            "file.end",
            session_id,
            {
                "transfer_id": transfer_id,
                "chunk_count": 1,
                "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            },
        ),
        tiny_envelope("typing.start", session_id, {}),
        tiny_envelope("typing.stop", session_id, {}),
        tiny_envelope("response.done", session_id, {}),
        tiny_envelope("response.cancel", session_id, {}),
    ]

    sent_ok: List[str] = []
    send_fail: List[str] = []
    recv_types: List[str] = []

    print(f"probe connecting (session={session_id[:8]}… types={len(probes)})")
    try:
        async with ws_connect(url, extra_headers=headers, open_timeout=15, max_size=1024 * 1024) as ws:
            print("probe connected")
            for msg in probes:
                kind = msg["type"]
                try:
                    await ws.send(json.dumps(msg, ensure_ascii=False))
                    sent_ok.append(kind)
                    print(f"  SEND ok type={kind}")
                except Exception as exc:
                    send_fail.append(kind)
                    print(f"  SEND fail type={kind} err={type(exc).__name__}")
                await asyncio.sleep(0.05)

            # Optional peer echo window — do not print bodies.
            deadline = time.time() + wait_sec
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
                except asyncio.TimeoutError:
                    break
                except Exception:
                    break
                if isinstance(raw, bytes):
                    continue
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict) and isinstance(obj.get("type"), str):
                        recv_types.append(obj["type"])
                except Exception:
                    pass
    except Exception as exc:
        # Avoid leaking auth details from exception strings when possible.
        text = str(exc)
        for secret in (env("DEVICE_TOKEN"), env("RELAY_TOKEN")):
            if secret and len(secret) >= 8:
                text = text.replace(secret, "***")
        print(f"FAIL: connect/send error: {type(exc).__name__}: {text[:200]}")
        return 1

    print(f"summary sent_ok={len(sent_ok)}/{len(probes)} send_fail={send_fail or '-'} recv_types={recv_types or '-'}")
    if send_fail:
        print("FAIL: Relay rejected or closed on some file/typing probes — pause file联调 and inspect Relay limits.")
        return 1
    print("PASS: all probe frames accepted by Relay (transparency smoke).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
