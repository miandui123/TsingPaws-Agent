#!/usr/bin/env python3
"""Unit / mock tests for the OpenWrt TsingPaws Agent (no live Relay required).

Run from the deploy tree:

  PYTHONPATH=/opt/tsingpaws-agent/vendor:/root/tsingpaws-agent-deploy \
  python3 -m pytest -q /root/tsingpaws-agent-deploy/tests
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent as ag  # noqa: E402


@pytest.fixture
def conf(tmp_path, monkeypatch):
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    monkeypatch.setenv("AGENT_CONF_DIR", str(conf_dir))
    monkeypatch.setenv("RELAY_HTTP_BASE", "http://127.0.0.1:9")
    monkeypatch.setenv("RELAY_WS_URL", "ws://127.0.0.1:9/v1/agent/connect")
    monkeypatch.setenv("RELAY_URL", "ws://127.0.0.1:9/v1/agent/connect")
    monkeypatch.setenv("RELAY_TOKEN", "single-node-relay-token-32chars-min!!")
    monkeypatch.setenv("DEVICE_ID", "home-001")
    monkeypatch.setenv("PICO_TOKEN", "pico-token-for-tests-not-a-secret!!")
    monkeypatch.setenv("PICO_BASE_URL", "ws://127.0.0.1:18790")
    monkeypatch.setenv("STATUS_PORT", "18792")
    (conf_dir / "mode").write_text("single_node\n")
    ag._SECRETS.clear()
    ag._LAUNCHER_OPENER = None
    ag._LAUNCHER_SESSION_BASE = ""
    yield conf_dir


class MockRelay:
    def __init__(self):
        self.calls: List[Tuple[str, str, Optional[Dict], Optional[str]]] = []
        self.health = {"status": "ok", "version": "internal-test-2", "pairing_flow": "app_first_invitation"}
        self.register_status = 201
        self.register_body: Dict[str, Any] = {
            "device_id": "will-be-overwritten",
            "device_token": "device-token-from-mock-relay-32chars!!",
            "created": True,
        }
        self.claim_status = 200
        self.claim_body: Dict[str, Any] = {"status": "claimed", "device_id": "x"}
        self.claim_headers: Dict[str, str] = {}
        self.last_claim_auth: Optional[str] = None
        self.last_claim_body: Optional[Dict] = None

    def handle(self, method: str, url: str, payload, bearer):
        self.calls.append((method, url, payload, bearer))
        if url.endswith("/health"):
            return 200, dict(self.health), {}
        if url.endswith("/v1/devices/register"):
            body = dict(self.register_body)
            if payload and payload.get("device_id"):
                body["device_id"] = payload["device_id"]
            return self.register_status, body, {}
        if url.endswith("/v1/pairing-invitations/claim"):
            self.last_claim_auth = bearer
            self.last_claim_body = payload
            return self.claim_status, dict(self.claim_body), dict(self.claim_headers)
        return 404, {"error": "not_found"}, {}


@pytest.fixture
def mock_relay(monkeypatch):
    mock = MockRelay()

    def fake_http(method, url, payload=None, bearer=None, timeout=10.0):
        return mock.handle(method, url, payload, bearer)

    monkeypatch.setattr(ag, "http_json", fake_http)
    return mock


def test_01_mode_defaults_to_single_node(conf):
    assert ag.read_mode() == "single_node"
    (conf / "mode").write_text("internal_test\n")
    assert ag.read_mode() == "internal_test"
    (conf / "mode").write_text("garbage\n")
    assert ag.read_mode() == "single_node"


def test_02_atomic_identity_write_and_permissions(conf):
    data = {
        "version": 1,
        "device_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "device_token": "tok_" + "x" * 40,
        "registered_at": "2026-01-01T00:00:00+08:00",
        "relay_base": "http://example",
    }
    ag.write_identity_atomic(data)
    path = ag.device_file()
    assert os.path.exists(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    loaded = ag.load_identity()
    assert loaded["device_id"] == data["device_id"]
    assert loaded["device_token"] == data["device_token"]
    # crash mid-write never leaves a .tmp behind after success
    leftovers = list(Path(conf).glob("device.json.tmp.*"))
    assert leftovers == []


def test_03_register_requires_internal_test_health(conf, mock_relay):
    (conf / "mode").write_text("internal_test\n")
    mock_relay.health = {"status": "ok", "version": "single-node", "apps": 1}
    enroll = conf / "enrollment.env"
    enroll.write_text("DEVICE_ENROLLMENT_TOKEN=" + ("E" * 40) + "\n")
    os.chmod(enroll, 0o600)
    ok, code, _ = ag.register_device()
    assert not ok and code == "internal_test_not_enabled"
    assert not (conf / "device.json").exists()
    assert enroll.exists()  # enrollment kept for retry


def test_04_register_success_writes_device_and_wipes_enrollment(conf, mock_relay):
    (conf / "mode").write_text("internal_test\n")
    enroll = conf / "enrollment.env"
    enroll.write_text("DEVICE_ENROLLMENT_TOKEN=" + ("E" * 40) + "\n")
    os.chmod(enroll, 0o600)
    ok, code, info = ag.register_device()
    assert ok and code == "registered"
    assert "device_id" in info
    assert not enroll.exists()
    identity = ag.load_identity()
    assert identity is not None
    assert identity["device_token"].startswith("device-token")
    assert (conf / "device.json").stat().st_mode & 0o777 == 0o600
    # second call is a no-op
    ok2, code2, _ = ag.register_device()
    assert ok2 and code2 == "already_registered"


def test_05_register_409_stops_without_new_id(conf, mock_relay):
    (conf / "mode").write_text("internal_test\n")
    enroll = conf / "enrollment.env"
    enroll.write_text("DEVICE_ENROLLMENT_TOKEN=" + ("E" * 40) + "\n")
    os.chmod(enroll, 0o600)
    mock_relay.register_status = 409
    mock_relay.register_body = {"error": "device_exists"}
    ok, code, info = ag.register_device()
    assert not ok and code == "device_already_registered"
    assert enroll.exists()
    assert not (conf / "device.json").exists()
    # pending id preserved so we do not mint forever
    pending = (conf / "pending-device-id").read_text().strip()
    assert pending == info["device_id"]


def test_06_claim_error_mapping(conf, mock_relay):
    (conf / "mode").write_text("internal_test\n")
    ag.write_identity_atomic(
        {
            "version": 1,
            "device_id": "dev-uuid-1",
            "device_token": "device-token-aaaaaaaaaaaaaaaaaaaa",
            "registered_at": "t",
            "relay_base": "http://127.0.0.1:9",
        }
    )
    agent = ag.Agent()

    cases = [
        (400, {"error": "invalid_or_expired_pairing_code"}, {}, 400, "invalid_or_expired_pairing_code"),
        (409, {"error": "device_already_bound"}, {}, 409, "device_already_bound"),
        (401, {"error": "unauthorized"}, {}, 401, "unauthorized"),
        (429, {"error": "rate_limited"}, {"Retry-After": "42"}, 429, "rate_limited"),
        (0, {"error": "network_error"}, {}, 503, "relay_unreachable"),
    ]
    for status, body, headers, expect_status, expect_error in cases:
        mock_relay.claim_status = status
        mock_relay.claim_body = body
        mock_relay.claim_headers = headers
        got_status, out = asyncio.run(agent.claim_pairing("123456"))
        assert got_status == expect_status
        assert out["error"] == expect_error
        if expect_error == "rate_limited":
            assert out["retry_after"] == 42

    # success
    mock_relay.claim_status = 200
    mock_relay.claim_body = {"status": "claimed", "device_id": "dev-uuid-1"}
    got_status, out = asyncio.run(agent.claim_pairing("123456"))
    assert got_status == 200 and out["ok"] is True
    assert mock_relay.last_claim_body == {"pairing_code": "123456"}
    assert "device_id" not in mock_relay.last_claim_body
    assert mock_relay.last_claim_auth == "device-token-aaaaaaaaaaaaaaaaaaaa"


def test_07_claim_blocked_in_single_node(conf, mock_relay):
    agent = ag.Agent()
    status, out = asyncio.run(agent.claim_pairing("123456"))
    assert status == 503 and out["error"] == "internal_test_not_enabled"


def test_08_claim_rejects_non_six_digit(conf):
    (conf / "mode").write_text("internal_test\n")
    ag.write_identity_atomic(
        {
            "version": 1,
            "device_id": "dev",
            "device_token": "device-token-bbbbbbbbbbbbbbbbbbbb",
            "registered_at": "t",
            "relay_base": "http://x",
        }
    )
    agent = ag.Agent()
    for bad in ("12345", "1234567", "12ab56", "", None, "12 34"):
        status, out = asyncio.run(agent.claim_pairing(bad))
        assert status == 400
        assert out["error"] == "invalid_or_expired_pairing_code"


def test_09_status_never_returns_tokens(conf):
    (conf / "mode").write_text("internal_test\n")
    token = "super-secret-device-token-never-leak!!"
    enroll = "super-secret-enrollment-token-never!!"
    ag.write_identity_atomic(
        {
            "version": 1,
            "device_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "device_token": token,
            "registered_at": "t",
            "relay_base": "http://x",
        }
    )
    ag.remember_secret(enroll)
    agent = ag.Agent()
    payload = json.dumps(agent.status_payload())
    health = json.dumps(agent.health_payload())
    assert token not in payload and token not in health
    assert enroll not in payload
    assert "device_token" not in payload
    assert "Bearer" not in payload
    assert agent.status_payload()["device_id_short"] == "aaaaaaaa…eeee"


def test_10_redact_keeps_tokens_out_of_logs(conf):
    secret = "RELAY_TOKEN_VALUE_SHOULD_NOT_APPEAR_XX"
    pico = "PICO_TOKEN_VALUE_SHOULD_NOT_APPEAR_YY"
    os.environ["RELAY_TOKEN"] = secret
    os.environ["PICO_TOKEN"] = pico
    ag.remember_secret("device-token-ccccccccccccccc")
    sample = (
        f"Authorization: Bearer {secret} pico={pico} "
        f"token=device-token-ccccccccccccccc url=?token={secret}"
    )
    cleaned = ag.redact(sample)
    assert secret not in cleaned
    assert pico not in cleaned
    assert "device-token-ccccccccccccccc" not in cleaned
    assert "Bearer ***" in cleaned
    assert "token=***" in cleaned


def test_11_js_sanitize_and_submit_gate():
    # Mirror the front-end helpers without a browser.
    js = (ROOT / "static" / "cloud-channel.js").read_text(encoding="utf-8")
    assert "生成/刷新配对码" not in js
    assert "查看配对二维码" not in js
    assert "确认绑定" in js
    assert "添加到 TsingPaws APP" in js
    assert "一台 TsingPaws 同时只能绑定一个 APP 账号" in js
    assert "一台小主机" not in js
    assert "!status.bound" in js
    assert "status.pairing_enabled" in js
    assert "localStorage.setItem" not in js
    assert "localStorage.getItem" not in js
    assert "history.pushState" in js or "popstate" in js
    assert 'replace(/\\D/g, "")' in js

    # Evaluate the pure helpers via a tiny node-less reimplementation check
    # (same algorithm as the shipped JS).
    def sanitize_code(raw):
        return "".join(ch for ch in str("" if raw is None else raw) if ch.isdigit())[:6]

    def can_submit(code, busy, pairing_enabled=True):
        return bool(pairing_enabled) and len(sanitize_code(code)) == 6 and not busy

    assert sanitize_code("583 921") == "583921"
    assert sanitize_code(" 583-921 ") == "583921"
    assert sanitize_code("5839212") == "583921"
    assert sanitize_code("abc") == ""
    assert can_submit("583921", False, True) is True
    assert can_submit("58392", False, True) is False
    assert can_submit("583921", True, True) is False
    assert can_submit("583921", False, False) is False
    assert "refs.bind.disabled" in js


def test_14_bridge_loopback_guard():
    import launcher_bridge as bridge

    assert bridge.UPSTREAM.startswith("http://127.0.0.1") or "localhost" in bridge.UPSTREAM
    assert bridge.AGENT_STATUS.startswith("http://127.0.0.1") or "localhost" in bridge.AGENT_STATUS
    with pytest.raises(SystemExit):
        bridge.assert_loopback_url("X", "http://193.112.152.197:18791")
    with pytest.raises(SystemExit):
        bridge.assert_loopback_url("X", "http://evil.example/steal")
    assert bridge.same_origin_ok("http://192.168.100.180:18800", "192.168.100.180:18800")
    assert not bridge.same_origin_ok("http://evil.example", "192.168.100.180:18800")
    assert not bridge.same_origin_ok("", "192.168.100.180:18800")
    assert "Token" not in bridge.pairing_message("invalid_or_expired_pairing_code")
    assert "42" in bridge.pairing_message("rate_limited", 42)
    assert "解绑" in bridge.pairing_message("device_already_bound")
    assert "尚未启用" in bridge.pairing_message("internal_test_not_enabled")


def test_13_short_device_id():
    assert ag.short_device_id("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") == "aaaaaaaa…eeee"
    assert ag.short_device_id("home-001") == "home-001"
    assert ag.short_device_id(None) is None


def test_15_status_request_is_local_and_correlated(conf, monkeypatch):
    agent = ag.Agent()
    captured = []

    async def capture(obj):
        captured.append(obj)

    monkeypatch.setattr(agent, "send_to_app", capture)
    monkeypatch.setattr(
        agent,
        "device_status_payload",
        lambda: {"lan_ip": "192.168.100.12", "scheduled_tasks": []},
    )
    asyncio.run(
        agent.handle_relay_message(
            json.dumps(
                {
                    "type": "device.status.request",
                    "id": "envelope-id",
                    "session_id": "session-status-test",
                    "payload": {"request_id": "request-123"},
                }
            )
        )
    )
    assert len(captured) == 1
    assert captured[0]["type"] == "device.status.response"
    assert captured[0]["session_id"] == "session-status-test"
    assert captured[0]["payload"]["request_id"] == "request-123"
    assert captured[0]["payload"]["lan_ip"] == "192.168.100.12"


def test_15_control_allowlist_and_secret_sanitizer(conf):
    assert ag.control_request_allowed("GET", "/api/models")
    assert ag.control_request_allowed("PATCH", "/api/config")
    assert ag.control_request_allowed("GET", "/api/usage/stats?days=30")
    assert not ag.control_request_allowed("GET", "http://evil.example/api/models")
    assert not ag.control_request_allowed("GET", "/api/auth/status")
    assert not ag.control_request_allowed("POST", "/api/gateway/stop?next=http://evil")
    sanitized = ag.sanitize_control_payload(
        {
            "api_key": "secret-value",
            "token": "token-value",
            "token_configured": True,
            "total_tokens": 123,
            "nested": [{"password": "hidden", "name": "safe"}],
        }
    )
    assert "api_key" not in sanitized
    assert sanitized["api_key_configured"] is True
    assert "token" not in sanitized
    assert sanitized["token_configured"] is True
    assert sanitized["total_tokens"] == 123
    assert sanitized["nested"] == [{"password_configured": True, "name": "safe"}]


def test_15a_launcher_control_uses_the_internal_launcher_port(conf, monkeypatch):
    monkeypatch.delenv("LAUNCHER_API_BASE", raising=False)
    assert ag.launcher_api_base() == "http://127.0.0.1:18880"

    monkeypatch.setenv("LAUNCHER_API_BASE", "http://127.0.0.1:19999/")
    assert ag.launcher_api_base() == "http://127.0.0.1:19999"

    monkeypatch.setenv("LAUNCHER_API_BASE", "http://example.com:18880")
    with pytest.raises(ValueError, match="loopback"):
        ag.launcher_api_base()


def test_15aa_launcher_uses_a_separate_management_token(conf, monkeypatch):
    monkeypatch.setenv("PICO_TOKEN", "pico-channel-token")
    monkeypatch.delenv("LAUNCHER_TOKEN", raising=False)
    monkeypatch.setattr(ag, "_pico_security_token", lambda: "")
    assert ag.launcher_token() == "pico-channel-token"

    monkeypatch.setenv("LAUNCHER_TOKEN", "launcher-management-token")
    assert ag.launcher_token() == "launcher-management-token"


def test_15_control_request_is_correlated_and_requires_confirmation(conf, monkeypatch):
    agent = ag.Agent()
    captured = []
    calls = []

    async def capture(obj):
        captured.append(obj)

    def fake_launcher(method, path, body=None, multipart=None, timeout=35.0):
        calls.append((method, path, body, multipart))
        return 200, {"models": [{"model_name": "safe-model"}]}

    monkeypatch.setattr(agent, "send_to_app", capture)
    monkeypatch.setattr(ag, "launcher_control_request", fake_launcher)
    asyncio.run(
        agent.handle_relay_message(
            json.dumps(
                {
                    "type": "control.request",
                    "id": "control-envelope",
                    "session_id": "session-control-test",
                    "payload": {
                        "request_id": "control-123",
                        "method": "GET",
                        "path": "/api/models",
                    },
                }
            )
        )
    )
    assert calls == [("GET", "/api/models", None, None)]
    assert captured[-1]["type"] == "control.response"
    assert captured[-1]["payload"]["request_id"] == "control-123"
    assert captured[-1]["payload"]["ok"] is True

    asyncio.run(
        agent.handle_relay_message(
            json.dumps(
                {
                    "type": "control.request",
                    "session_id": "session-control-test",
                    "payload": {
                        "request_id": "delete-123",
                        "method": "DELETE",
                        "path": "/api/models/safe-model",
                    },
                }
            )
        )
    )
    assert captured[-1]["payload"]["status"] == 409
    assert captured[-1]["payload"]["error"] == "confirmation_required"
    assert len(calls) == 1


def test_15b_relay_storage_error_keeps_file_and_enables_retry(conf):
    agent = ag.Agent()
    transfer_id = "12345678-1234-1234-1234-123456789012"
    outbox = Path(agent._outbox_dir())
    outbox.mkdir(parents=True, exist_ok=True)
    data_path = outbox / f"{transfer_id}.bin"
    meta_path = outbox / f"{transfer_id}.json"
    data_path.write_bytes(b"docx")
    meta_path.write_text(
        json.dumps(
            {
                "transfer_id": transfer_id,
                "session_id": "session-storage-retry",
                "name": "test.docx",
                "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "last_sent_at": int(time.time()),
            }
        ),
        encoding="utf-8",
    )

    asyncio.run(
        agent.handle_relay_message(
            json.dumps(
                {
                    "type": "relay.storage_error",
                    "session_id": "session-storage-retry",
                    "payload": {
                        "kind": "file",
                        "transfer_id": transfer_id,
                    },
                }
            )
        )
    )

    assert data_path.is_file()
    assert meta_path.is_file()
    assert json.loads(meta_path.read_text(encoding="utf-8"))["last_sent_at"] == 0


def test_16_scheduled_tasks_reads_picoclaw_jobs(conf, monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    cron = workspace / "cron"
    cron.mkdir(parents=True)
    (cron / "jobs.json").write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "job-1",
                        "name": "每日汇报",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 900000},
                        "payload": {"message": "生成并发送汇报"},
                        "state": {
                            "lastRunAtMs": 123,
                            "nextRunAtMs": 456,
                            "lastStatus": "ok",
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PICO_WORKSPACE", str(workspace))
    tasks = ag._scheduled_tasks()
    assert len(tasks) == 1
    assert tasks[0]["id"] == "job-1"
    assert tasks[0]["enabled"] is True
    assert tasks[0]["every_ms"] == 900000
    assert tasks[0]["last_status"] == "ok"


def test_17_machine_lan_ip_prefers_configured_override(monkeypatch):
    monkeypatch.setenv("LAN_IP_OVERRIDE", "192.168.100.211")

    assert ag._machine_lan_ip() == "192.168.100.211"


def test_18_pico_security_file_token_overrides_stale_env(monkeypatch, tmp_path):
    security = tmp_path / ".security.yml"
    security.write_text(
        "channels:\n"
        "  weixin:\n"
        "    token: unrelated\n"
        "  pico:\n"
        "    token: 'current-pico-token'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PICO_SECURITY_FILE", str(security))
    monkeypatch.setenv("PICO_TOKEN", "stale-pico-token")

    assert ag.pico_token() == "current-pico-token"


def _write_tool_call_session(workspace: Path, session_id: str, name: str, arguments: Dict[str, Any]):
    sessions = workspace / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"agent_main_pico_direct_pico_{session_id}.jsonl"
    path.write_text(
        json.dumps(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments, ensure_ascii=False),
                        }
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def test_19_generate_document_recovers_docx_from_inbox(conf, monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    inbox = workspace / "inbox"
    inbox.mkdir(parents=True)
    monkeypatch.setenv("PICO_WORKSPACE", str(workspace))
    session_id = "document-recovery-session"
    _write_tool_call_session(
        workspace,
        session_id,
        "generate_document",
        {"filename": "深圳攻略.docx"},
    )
    generated = inbox / "深圳攻略-2.docx"
    generated.write_bytes(b"PK-test-docx")
    agent = ag.Agent()
    pushed = []

    async def capture(sid, path, name, mime):
        pushed.append((sid, path, name, mime))

    monkeypatch.setattr(agent, "_push_local_file_to_app", capture)
    state = ag.SessionState(task_started_at=generated.stat().st_mtime - 1)
    assert asyncio.run(agent._push_generated_document_fallback(session_id, state))
    assert pushed == [
        (
            session_id,
            str(generated.resolve()),
            "深圳攻略-2.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    ]
    assert not asyncio.run(agent._push_generated_document_fallback(session_id, state))


def test_20_outbound_file_spool_is_durable_and_ack_removes_it(conf, tmp_path):
    os.environ.pop("AGENT_DATA_DIR", None)
    source = tmp_path / "报告.docx"
    source.write_bytes(b"PK-reliable-document")
    os.chmod(source, 0o640)
    original_mode = stat.S_IMODE(source.stat().st_mode)
    agent = ag.Agent()
    transfer_id = "12345678-1234-1234-1234-123456789abc"

    agent._queue_outbound_file(
        transfer_id,
        "session-file-reliability",
        str(source),
        source.name,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    outbox = conf / "outbox"
    data_path = outbox / f"{transfer_id}.bin"
    meta_path = outbox / f"{transfer_id}.json"
    assert data_path.read_bytes() == source.read_bytes()
    assert json.loads(meta_path.read_text(encoding="utf-8"))["session_id"] == "session-file-reliability"
    assert stat.S_IMODE(source.stat().st_mode) == original_mode

    agent._ack_outbound_file(transfer_id)
    assert not data_path.exists()
    assert not meta_path.exists()
    assert source.exists()


def test_20a_outbound_file_spool_uses_data_disk_and_migrates(conf, monkeypatch, tmp_path):
    legacy = conf / "outbox"
    legacy.mkdir()
    transfer_id = "87654321-4321-4321-4321-cba987654321"
    (legacy / f"{transfer_id}.bin").write_bytes(b"queued-before-upgrade")
    (legacy / f"{transfer_id}.json").write_text(
        json.dumps({"transfer_id": transfer_id, "session_id": "session-before-upgrade"}),
        encoding="utf-8",
    )
    data_dir = tmp_path / "large-data-disk"
    monkeypatch.setenv("AGENT_DATA_DIR", str(data_dir))

    agent = ag.Agent()

    assert agent._outbox_dir() == str(data_dir / "outbox")
    assert (data_dir / "outbox" / f"{transfer_id}.bin").read_bytes() == b"queued-before-upgrade"
    assert not (legacy / f"{transfer_id}.bin").exists()


def test_19b_generate_document_matches_pico_sanitized_chinese_punctuation(
    conf, monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    inbox = workspace / "inbox"
    inbox.mkdir(parents=True)
    monkeypatch.setenv("PICO_WORKSPACE", str(workspace))
    session_id = "document-punctuation-session"
    requested = "后海的傍晚，是吃饱了最该去的地方.docx"
    _write_tool_call_session(
        workspace,
        session_id,
        "generate_document",
        {"filename": requested},
    )
    generated = inbox / "后海的傍晚_是吃饱了最该去的地方-2.docx"
    generated.write_bytes(b"PK-test-docx")
    agent = ag.Agent()
    pushed = []

    async def capture(sid, path, name, mime):
        pushed.append((sid, path, name, mime))

    monkeypatch.setattr(agent, "_push_local_file_to_app", capture)
    state = ag.SessionState(task_started_at=generated.stat().st_mtime - 1)
    assert asyncio.run(agent._push_generated_document_fallback(session_id, state))
    assert pushed[0][2] == generated.name


def test_20_send_file_relative_path_resolves_inbox(conf, monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    inbox = workspace / "inbox"
    inbox.mkdir(parents=True)
    monkeypatch.setenv("PICO_WORKSPACE", str(workspace))
    session_id = "send-file-inbox-session"
    _write_tool_call_session(
        workspace,
        session_id,
        "send_file",
        {"path": "会议纪要.docx"},
    )
    generated = inbox / "会议纪要.docx"
    generated.write_bytes(b"PK-test-docx")
    agent = ag.Agent()
    pushed = []

    async def capture(sid, path, name, mime):
        pushed.append((sid, path, name, mime))

    monkeypatch.setattr(agent, "_push_local_file_to_app", capture)
    state = ag.SessionState(task_started_at=generated.stat().st_mtime - 1)
    assert asyncio.run(agent._push_send_file_fallback(session_id, state))
    assert pushed[0][1] == str(generated.resolve())


def test_20b_send_file_resends_older_pico_sanitized_document(
    conf, monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    inbox = workspace / "inbox"
    inbox.mkdir(parents=True)
    monkeypatch.setenv("PICO_WORKSPACE", str(workspace))
    session_id = "send-file-old-punctuation-session"
    generated = inbox / "后海的傍晚_是吃饱了最该去的地方-2.docx"
    generated.write_bytes(b"PK-test-docx")
    old_time = time.time() - 3600
    os.utime(generated, (old_time, old_time))
    sessions = workspace / "sessions"
    sessions.mkdir()
    entries = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "send_file",
                        "arguments": json.dumps({"path": "unrelated-old.docx"}),
                    }
                }
            ],
        },
        {"role": "user", "content": "请把刚才的文件重新发给我"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "send_file",
                        "arguments": json.dumps(
                            {"path": "后海的傍晚，是吃饱了最该去的地方.docx"},
                            ensure_ascii=False,
                        ),
                    }
                }
            ],
        },
    ]
    (sessions / f"agent_{session_id}.jsonl").write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )
    agent = ag.Agent()
    pushed = []

    async def capture(sid, path, name, mime):
        pushed.append((sid, path, name, mime))

    monkeypatch.setattr(agent, "_push_local_file_to_app", capture)
    state = ag.SessionState(task_started_at=time.time() - 2)
    assert asyncio.run(agent._push_send_file_fallback(session_id, state))
    assert len(pushed) == 1
    assert pushed[0][1] == str(generated.resolve())


def test_20c_scheduled_attachment_false_failure_is_reconciled_once(
    conf, monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    cron = workspace / "cron"
    inbox = workspace / "inbox"
    sessions = workspace / "sessions"
    cron.mkdir(parents=True)
    inbox.mkdir()
    sessions.mkdir()
    (cron / "runs.json").write_text(
        json.dumps({"version": 1, "runs": []}),
        encoding="utf-8",
    )
    (cron / "jobs.json").write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "scheduled-doc-job",
                        "state": {
                            "lastStatus": "error",
                            "lastError": "scheduled agent task returned an empty response",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PICO_WORKSPACE", str(workspace))
    agent = ag.Agent()

    message = "Create and deliver the scheduled Word file."
    generated = inbox / "scheduled-report.docx"
    generated.write_bytes(b"PK-scheduled-docx")
    modified_ms = int(generated.stat().st_mtime * 1000)
    (sessions / "agent_main_cron_scheduled-doc-job.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "content": message}),
                json.dumps(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "generate_document",
                                    "arguments": json.dumps(
                                        {"filename": "scheduled-report.docx"}
                                    ),
                                }
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "📎 附件：scheduled-report.docx\n路径：scheduled-report.docx",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (cron / "runs.json").write_text(
        json.dumps(
            {
                "version": 1,
                "runs": [
                    {
                        "run_id": "scheduled-run-001",
                        "job_id": "scheduled-doc-job",
                        "status": "error",
                        "error": "scheduled agent task returned an empty response",
                        "started_at_ms": modified_ms - 1000,
                        "finished_at_ms": modified_ms + 1000,
                        "message": message,
                        "to": "pico:session-scheduled-001",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    pushed = []

    async def capture(sid, path, name, mime):
        pushed.append((sid, path, name, mime))

    monkeypatch.setattr(agent, "_push_local_file_to_app", capture)
    assert asyncio.run(agent._reconcile_scheduled_runs_once()) == 1
    assert len(pushed) == 1
    assert pushed[0][0] == "session-scheduled-001"
    assert pushed[0][2] == "scheduled-report.docx"
    assert asyncio.run(agent._reconcile_scheduled_runs_once()) == 0
    assert len(pushed) == 1

    run = json.loads((cron / "runs.json").read_text(encoding="utf-8"))["runs"][0]
    assert run["status"] == "ok"
    assert run["delivery_status"] == "delivered"
    assert "error" not in run
    job_state = json.loads((cron / "jobs.json").read_text(encoding="utf-8"))["jobs"][0]["state"]
    assert job_state["lastStatus"] == "ok"
    assert "lastError" not in job_state
    ledger = json.loads((conf / "scheduled-delivery.json").read_text(encoding="utf-8"))
    assert "scheduled-run-001" in ledger["run_ids"]


def test_20d_scheduled_false_failure_waits_for_run_record(conf, monkeypatch):
    agent = ag.Agent()
    session_id = "session-scheduled-delay"
    attempts = 0
    forwarded = []

    async def delayed_reconcile():
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            agent._recent_scheduled_delivery[session_id] = time.time()
        return 1 if attempts == 2 else 0

    async def capture(obj):
        forwarded.append(obj)

    monkeypatch.setattr(agent, "_reconcile_scheduled_runs_once", delayed_reconcile)
    monkeypatch.setattr(agent, "send_to_app", capture)
    asyncio.run(
        agent.handle_pico_message(
            session_id,
            json.dumps(
                {
                    "type": "message.create",
                    "id": "scheduled-failure-message",
                    "session_id": session_id,
                    "payload": {
                        "content": "Scheduled task failed: the agent returned no response"
                    },
                }
            ),
        )
    )
    assert attempts == 2
    assert forwarded == []


def test_20e_real_scheduled_empty_response_is_translated_to_chinese(
    conf, monkeypatch
):
    agent = ag.Agent()
    forwarded = []

    async def not_reconciled(_session_id, timeout=4.0):
        return False

    async def capture(obj):
        forwarded.append(obj)

    monkeypatch.setattr(
        agent, "_scheduled_failure_was_reconciled", not_reconciled
    )
    monkeypatch.setattr(agent, "send_to_app", capture)
    asyncio.run(
        agent.handle_pico_message(
            "session-real-scheduled-failure",
            json.dumps(
                {
                    "type": "message.create",
                    "id": "real-scheduled-failure-message",
                    "payload": {
                        "content": "Scheduled task failed: the agent returned no response"
                    },
                }
            ),
        )
    )
    assert len(forwarded) >= 1
    message = next(item for item in forwarded if item.get("type") == "message.create")
    assert message["payload"]["content"] == ag.SCHEDULED_FAILURE_MESSAGE_ZH
    assert "Scheduled task failed" not in json.dumps(message, ensure_ascii=False)


def test_21_send_file_still_rejects_unapproved_path(conf, monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("PICO_WORKSPACE", str(workspace))
    session_id = "send-file-reject-session"
    _write_tool_call_session(
        workspace,
        session_id,
        "send_file",
        {"path": "/etc/passwd"},
    )
    agent = ag.Agent()
    pushed = []

    async def capture(*args):
        pushed.append(args)

    monkeypatch.setattr(agent, "_push_local_file_to_app", capture)
    state = ag.SessionState(task_started_at=time.time() - 1)
    assert not asyncio.run(agent._push_send_file_fallback(session_id, state))
    assert pushed == []


def test_22_recent_session_is_kept_connected_for_scheduled_output(
    conf, monkeypatch
):
    agent = ag.Agent()
    agent.last_session_id = "scheduled-output-session"
    calls = []

    class Session:
        async def ensure(self):
            calls.append("ensure")

    async def get_session(session_id):
        calls.append(session_id)
        return Session()

    monkeypatch.setattr(agent, "get_session", get_session)
    assert asyncio.run(agent.ensure_recent_pico_session())
    assert calls == ["scheduled-output-session", "ensure"]
    assert agent.pico_reachable is True


def test_23_outbox_retention_is_thirty_days(conf, tmp_path):
    source = tmp_path / "scheduled-report.docx"
    source.write_bytes(b"PK-scheduled-report")
    agent = ag.Agent()
    transfer_id = "22345678-1234-1234-1234-123456789abc"
    agent._queue_outbound_file(
        transfer_id,
        "scheduled-output-session",
        str(source),
        source.name,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    outbox = conf / "outbox"
    paths = [
        outbox / f"{transfer_id}.bin",
        outbox / f"{transfer_id}.json",
    ]
    retained_time = time.time() - 8 * 24 * 60 * 60
    for path in paths:
        os.utime(path, (retained_time, retained_time))
    agent._cleanup_outbox()
    assert all(path.exists() for path in paths)

    expired_time = time.time() - 31 * 24 * 60 * 60
    for path in paths:
        os.utime(path, (expired_time, expired_time))
    agent._cleanup_outbox()
    assert not any(path.exists() for path in paths)


def test_24_launcher_control_reuses_one_session_for_concurrent_requests(
    conf, monkeypatch
):
    monkeypatch.setenv("LAUNCHER_TOKEN", "launcher-test-token")
    monkeypatch.setenv("LAUNCHER_API_BASE", "http://127.0.0.1:18880")
    calls = {"login": 0, "request": 0}

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, body):
            self.body = json.dumps(body).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return self.body

    class Opener:
        def open(self, request, timeout=0):
            if request.full_url.endswith("/api/auth/login"):
                calls["login"] += 1
                return Response({"ok": True})
            calls["request"] += 1
            return Response({"ok": True, "path": request.full_url})

    monkeypatch.setattr(ag, "build_opener", lambda *_args: Opener())
    results = []

    def invoke(path):
        results.append(ag.launcher_control_request("GET", path))

    threads = [
        threading.Thread(target=invoke, args=(path,))
        for path in (
            "/api/monitor/performance",
            "/api/usage/stats?days=30",
            "/api/monitor/logs",
        )
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == {"login": 1, "request": 3}
    assert len(results) == 3
    assert all(status == 200 and body["ok"] for status, body in results)
