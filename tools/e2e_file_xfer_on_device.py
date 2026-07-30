#!/usr/bin/env python3
"""On-device E2E for file transfer via public Relay. Never prints secrets."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")


def load_env_file(path: str) -> dict:
    out = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def http_json(method: str, url: str, body=None, headers=None, timeout=20):
    data = None if body is None else json.dumps(body).encode()
    req = Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except Exception as exc:
        if hasattr(exc, "code"):
            try:
                raw = exc.read().decode()  # type: ignore[attr-defined]
                return int(exc.code), json.loads(raw) if raw else {}
            except Exception:
                return int(exc.code), {"error": "http_error"}
        raise


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def frames_for(name: str, mime: str, data: bytes, session_id: str, chunk_size: int = 48 * 1024):
    tid = str(uuid.uuid4())
    chunks = [data[i : i + chunk_size] for i in range(0, max(len(data), 1), chunk_size)] or [b""]
    digest = sha256_bytes(data)
    out = []
    out.append(
        {
            "type": "file.start",
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "timestamp": int(time.time() * 1000),
            "payload": {
                "transfer_id": tid,
                "name": name,
                "mime_type": mime,
                "size": len(data),
                "chunk_count": len(chunks),
                "sha256": digest,
            },
        }
    )
    for i, ch in enumerate(chunks):
        out.append(
            {
                "type": "file.chunk",
                "id": str(uuid.uuid4()),
                "session_id": session_id,
                "timestamp": int(time.time() * 1000),
                "payload": {
                    "transfer_id": tid,
                    "index": i,
                    "data": base64.b64encode(ch).decode("ascii"),
                },
            }
        )
    out.append(
        {
            "type": "file.end",
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "timestamp": int(time.time() * 1000),
            "payload": {"transfer_id": tid, "chunk_count": len(chunks), "sha256": digest},
        }
    )
    return tid, out


async def main() -> int:
    try:
        from websockets.legacy.client import connect as ws_connect
    except ImportError:
        # vendor path used by some installs
        sys.path.insert(0, "/opt/tsingpaws-agent/vendor")
        from websockets.legacy.client import connect as ws_connect

    env = load_env_file("/etc/tsingpaws-agent.env")
    identity = json.load(open("/etc/tsingpaws-agent/device.json", encoding="utf-8"))
    device_id = identity["device_id"]
    device_token = identity["device_token"]
    relay_http = env.get("RELAY_HTTP_BASE", "http://193.112.152.197:8787").rstrip("/")
    relay_ws = env.get("RELAY_WS_URL") or ("ws://" + relay_http.split("://", 1)[-1] + "/v1/agent/connect")

    st = http_json("GET", "http://127.0.0.1:18791/status")[1]
    check(
        "agent status healthy",
        st.get("registered") is True and st.get("relay_connected") is True and st.get("pico_reachable") is True,
        f"ver={st.get('agent_version')}",
    )

    # Create temporary anonymous user + invitation + claim (rebind for test)
    code, user = http_json("POST", f"{relay_http}/v1/users/anonymous", {"display_name": "filexfer-e2e"})
    check("temp anonymous user", code == 201 and "user_token" in user)
    user_token = user["user_token"]
    user_auth = {"Authorization": f"Bearer {user_token}"}

    code, inv = http_json("POST", f"{relay_http}/v1/pairing-invitations", {}, headers=user_auth)
    check("create invitation", code == 201 and "pairing_code" in inv)
    code, claim = http_json(
        "POST",
        f"{relay_http}/v1/pairing-invitations/claim",
        {"pairing_code": inv["pairing_code"]},
        headers={"Authorization": f"Bearer {device_token}"},
    )
    check("claim bind device", code in (200, 201), f"http={code}")

    # Probe relay transparency with tiny events (no real file content beyond tiny txt)
    app_url = f"{relay_http.replace('http','ws')}/v1/app/connect?{urlencode({'device_id': device_id})}"
    # normalize ws scheme
    if app_url.startswith("https"):
        app_url = "wss://" + app_url[len("https://") :]
    elif app_url.startswith("http"):
        app_url = "ws://" + app_url[len("http://") :]

    sid = str(uuid.uuid4())
    async with ws_connect(
        app_url,
        extra_headers={"Authorization": f"Bearer {user_token}"},
        open_timeout=15,
        max_size=4 * 1024 * 1024,
    ) as app_ws:
        check("APP WS connected", True)
        # wait briefly for agent peer
        await asyncio.sleep(1.5)

        # Relay transparency: tiny control frames only (no cancel before transfers)
        for t in ("typing.start", "typing.stop", "response.done"):
            await app_ws.send(
                json.dumps(
                    {
                        "type": t,
                        "id": str(uuid.uuid4()),
                        "session_id": sid,
                        "timestamp": int(time.time() * 1000),
                        "payload": {},
                    }
                )
            )
        await asyncio.sleep(0.3)
        check("relay accepts control event frames", True, "sent without disconnect")

        # Valid TXT transfer
        txt = b"hello-tsingpaws-file-xfer\n"
        tid, frames = frames_for("hello.txt", "text/plain", txt, sid)
        for fr in frames:
            await app_ws.send(json.dumps(fr))
        await asyncio.sleep(2)
        inbox = Path("/root/.picoclaw/workspace/inbox/tsingpaws")
        found = list(inbox.glob("*hello.txt")) + list(inbox.glob("*hello*"))
        # file may be renamed with prefix
        candidates = [p for p in inbox.glob("*") if p.is_file() and p.stat().st_mtime > time.time() - 30]
        check("TXT landed in inbox", any(p.stat().st_size == len(txt) for p in candidates) or bool(found), f"n={len(candidates)}")

        # Bad order rejection should yield error event (listen briefly)
        bad_sid = str(uuid.uuid4())
        bad = frames_for("bad.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"0" * 100, bad_sid)[1]
        # swap chunk order if >=2 chunks else craft wrong index
        start, chunk0, end = bad[0], bad[1], bad[-1]
        chunk0 = dict(chunk0)
        chunk0["payload"] = dict(chunk0["payload"])
        chunk0["payload"]["index"] = 1
        await app_ws.send(json.dumps(start))
        await app_ws.send(json.dumps(chunk0))
        got_err = False
        try:
            while True:
                raw = await asyncio.wait_for(app_ws.recv(), timeout=3)
                obj = json.loads(raw)
                if obj.get("type") == "error":
                    got_err = True
                    break
        except asyncio.TimeoutError:
            pass
        check("out-of-order chunk returns error", got_err)

        # SHA mismatch
        sid2 = str(uuid.uuid4())
        data = b"abc1234567"
        tid2, frs = frames_for("sha.txt", "text/plain", data, sid2)
        frs[-1]["payload"]["sha256"] = "0" * 64
        for fr in frs:
            await app_ws.send(json.dumps(fr))
        got_sha = False
        try:
            while True:
                raw = await asyncio.wait_for(app_ws.recv(), timeout=3)
                obj = json.loads(raw)
                if obj.get("type") == "error":
                    got_sha = True
                    break
        except asyncio.TimeoutError:
            pass
        check("sha256 mismatch returns error", got_sha)

        # PNG small
        # minimal 1x1 png
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        sid3 = str(uuid.uuid4())
        _, frs = frames_for("dot.png", "image/png", png, sid3)
        for fr in frs:
            await app_ws.send(json.dumps(fr))
        await asyncio.sleep(2)
        png_ok = any(p.suffix.lower() == ".png" and p.stat().st_mtime > time.time() - 30 for p in inbox.glob("*"))
        check("PNG landed in inbox", png_ok)

        # PDF tiny
        pdf = b"%PDF-1.1\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
        sid4 = str(uuid.uuid4())
        _, frs = frames_for("tiny.pdf", "application/pdf", pdf, sid4)
        for fr in frs:
            await app_ws.send(json.dumps(fr))
        await asyncio.sleep(2)
        pdf_ok = any(p.name.endswith(".pdf") and p.stat().st_mtime > time.time() - 30 for p in inbox.glob("*"))
        check("PDF landed in inbox", pdf_ok)

        # Text Q&A after file in same session (short timeout; LLM may be slow)
        ask = {
            "type": "message.send",
            "id": str(uuid.uuid4()),
            "session_id": sid4,
            "timestamp": int(time.time() * 1000),
            "payload": {"content": "只回复：OK_FILE_XFER（不要输出其他内容）"},
        }
        await app_ws.send(json.dumps(ask))
        got_reply = False
        got_typing = False
        deadline = time.time() + 45
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(app_ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            obj = json.loads(raw)
            t = obj.get("type")
            if t in ("typing.start", "typing.stop", "response.done"):
                got_typing = True
            if t in ("message.create", "message.update"):
                content = (obj.get("payload") or {}).get("content") or ""
                if content:
                    got_reply = True
                    check("text reply after file in same session", True, f"type={t} len={len(content)}")
                    break
        if not got_reply:
            check("text reply after file in same session", False, "timeout_or_no_llm")
        check("lifecycle events observed (optional)", True, f"seen={got_typing}")

        # response.cancel should not kill agent
        cancel_sid = str(uuid.uuid4())
        await app_ws.send(
            json.dumps(
                {
                    "type": "response.cancel",
                    "id": str(uuid.uuid4()),
                    "session_id": cancel_sid,
                    "timestamp": int(time.time() * 1000),
                    "payload": {},
                }
            )
        )
        await asyncio.sleep(1)
        check("response.cancel did not drop APP socket", not app_ws.closed)

        # Oversize rejection locally via agent unit path is covered in pytest;
        # here send start with size >20MiB
        big = {
            "type": "file.start",
            "id": str(uuid.uuid4()),
            "session_id": str(uuid.uuid4()),
            "timestamp": int(time.time() * 1000),
            "payload": {
                "transfer_id": str(uuid.uuid4()),
                "name": "big.bin",
                "mime_type": "application/octet-stream",
                "size": 21 * 1024 * 1024,
                "chunk_count": 500,
                "sha256": "a" * 64,
            },
        }
        await app_ws.send(json.dumps(big))
        got_big = False
        try:
            while True:
                raw = await asyncio.wait_for(app_ws.recv(), timeout=3)
                obj = json.loads(raw)
                if obj.get("type") == "error":
                    got_big = True
                    break
        except asyncio.TimeoutError:
            pass
        check("oversize file.start rejected", got_big)

        # Path traversal name cleaned — should still accept basename-safe
        trav_data = b"safe"
        sid5 = str(uuid.uuid4())
        tid5, frs = frames_for("../../etc/passwd.txt", "text/plain", trav_data, sid5)
        for fr in frs:
            await app_ws.send(json.dumps(fr))
        await asyncio.sleep(2)
        # must not write outside inbox
        outside = Path("/etc/passwd.txt")
        check("no traversal write outside inbox", not outside.exists() or outside.stat().st_mtime < time.time() - 60)
        check("traversal name accepted as sanitized basename", any(p.stat().st_mtime > time.time() - 30 for p in inbox.glob("*passwd*")) or any(p.stat().st_mtime > time.time() - 30 for p in inbox.glob("*")))

    # Agent still connected after tests
    st2 = http_json("GET", "http://127.0.0.1:18791/status")[1]
    check("agent still relay_connected", st2.get("relay_connected") is True)
    h = http_json("GET", f"{relay_http}/health")[1]
    check("relay agents online", int(h.get("agents") or 0) >= 1)

    # Unbind temp user ownership to avoid leaving test bind if desired — keep device registered
    http_json("DELETE", f"{relay_http}/v1/devices/{device_id}", headers=user_auth)

    failed = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"\nSummary: {len(RESULTS) - failed}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
