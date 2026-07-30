#!/usr/bin/env python3
"""One-shot: push a plain test message.create to the last APP session via Relay."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from urllib.parse import urlsplit, urlunparse


def load_env() -> dict:
    env = dict(os.environ)
    with open("/etc/tsingpaws-agent.env", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k] = v.strip().strip("\"'")
    return env


async def main() -> int:
    env = load_env()
    device = json.load(open("/etc/tsingpaws-agent/device.json", "r", encoding="utf-8"))
    token = str(device.get("device_token") or "").strip()
    relay_base = str(device.get("relay_base") or env.get("RELAY_HTTP_BASE") or "http://193.112.152.197:8787").rstrip("/")
    ws_url = (env.get("RELAY_WS_URL") or "").strip()
    if not ws_url:
        parts = urlsplit(relay_base)
        scheme = "wss" if parts.scheme == "https" else "ws"
        ws_url = urlunparse((scheme, parts.netloc, "/v1/agent/connect", "", "", ""))
    sid = open("/etc/tsingpaws-agent/last-session-id", "r", encoding="utf-8").read().strip()
    if not token or not sid:
        print("missing_token_or_session")
        return 2

    import websockets

    text = "测试消息：来自小主机的连通性检查 " + time.strftime("%H:%M:%S")
    envelope = {
        "type": "message.create",
        "id": str(uuid.uuid4()),
        "session_id": sid,
        "timestamp": int(time.time() * 1000),
        "payload": {
            "content": text,
            "message_id": "test-" + uuid.uuid4().hex[:10],
            "role": "assistant",
        },
    }
    print("session=" + sid)
    print("connecting_agent_ws")
    async with websockets.connect(
        ws_url,
        extra_headers={"Authorization": "Bearer " + token},
        open_timeout=8,
        close_timeout=3,
        max_size=2 * 1024 * 1024,
    ) as ws:
        print("agent_ws_open")
        await ws.send(json.dumps(envelope, ensure_ascii=False))
        print("sent_test_message")
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=2.5)
            try:
                obj = json.loads(raw)
                print("reply_type=" + str(obj.get("type")))
            except Exception:
                print("reply_non_json")
        except asyncio.TimeoutError:
            print("no_immediate_reply_ok")
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
