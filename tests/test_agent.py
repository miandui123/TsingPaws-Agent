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
    assert "确认添加" in js
    assert "添加到 TsingPaws APP" in js
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
    assert "status.registered" in js


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
