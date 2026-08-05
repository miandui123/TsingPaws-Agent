#!/usr/bin/env python3
"""Unit tests for file_transfer helpers and session cancel lifecycle."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent as ag  # noqa: E402
import file_transfer as xfer  # noqa: E402


@pytest.fixture
def xfer_env(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    transfers = tmp_path / "transfers"
    workspace.mkdir()
    (workspace / "inbox").mkdir()
    (workspace / "skills" / "demo" / "output").mkdir(parents=True)
    transfers.mkdir()
    monkeypatch.setenv("PICO_WORKSPACE", str(workspace))
    monkeypatch.setenv("TRANSFER_ROOT", str(transfers))
    monkeypatch.setenv("PICO_TOKEN", "pico-token-for-tests-not-a-secret!!")
    monkeypatch.setenv("AGENT_CONF_DIR", str(tmp_path / "conf"))
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "mode").write_text("single_node\n")
    return workspace, transfers


def test_sanitize_filename_strips_traversal():
    assert xfer.sanitize_filename("../../etc/passwd") == "passwd"
    assert xfer.sanitize_filename("/tmp/../secret.key") == "secret.key"
    assert xfer.sanitize_filename("a\\b\\c.txt") == "c.txt"
    assert xfer.sanitize_filename("evil\x00name.pdf") == "evil_name.pdf"
    assert xfer.sanitize_filename("..") == "unnamed"
    assert xfer.sanitize_filename("") == "unnamed"
    assert xfer.sanitize_filename(None) == "unnamed"
    assert "/" not in xfer.sanitize_filename("foo/../../bar.png")


def test_guess_mime_common_types():
    assert xfer.guess_mime("a.png") == "image/png"
    assert xfer.guess_mime("a.pdf") == "application/pdf"
    assert xfer.guess_mime("a.docx").startswith("application/")
    assert xfer.guess_mime("a.txt") == "text/plain"
    assert xfer.guess_mime("a.bin") == "application/octet-stream"
    assert xfer.guess_mime("x", "image/webp") == "image/webp"


def test_transfer_happy_path_and_cleanup(xfer_env):
    workspace, transfers = xfer_env
    mgr = xfer.TransferManager(transfer_root=str(transfers), workspace=str(workspace))
    raw = b"hello-tsingpaws-file"
    digest = hashlib.sha256(raw).hexdigest()
    tid = "11111111-2222-3333-4444-555555555555"
    sid = "sess-1"
    mgr.handle_start(
        sid,
        {
            "transfer_id": tid,
            "name": "note.txt",
            "mime_type": "text/plain",
            "size": len(raw),
            "chunk_count": 1,
            "sha256": digest,
        },
    )
    mgr.handle_chunk(
        sid,
        {"transfer_id": tid, "index": 0, "data": base64.b64encode(raw).decode()},
    )
    done = mgr.handle_end(sid, {"transfer_id": tid, "chunk_count": 1, "sha256": digest})
    assert done.name == "note.txt"
    assert Path(done.path).is_file()
    assert Path(done.path).read_bytes() == raw
    assert done.path.startswith(str(workspace / "inbox" / "tsingpaws"))
    assert mgr.active_count() == 0
    assert not (transfers / tid).exists()


def test_transfer_rejects_out_of_order(xfer_env):
    workspace, transfers = xfer_env
    mgr = xfer.TransferManager(transfer_root=str(transfers), workspace=str(workspace))
    raw = b"abcdef"
    digest = hashlib.sha256(raw).hexdigest()
    tid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    mgr.handle_start(
        "s",
        {
            "transfer_id": tid,
            "name": "x.txt",
            "mime_type": "text/plain",
            "size": len(raw),
            "chunk_count": 1,
            "sha256": digest,
        },
    )
    with pytest.raises(xfer.TransferError) as ei:
        mgr.handle_chunk("s", {"transfer_id": tid, "index": 1, "data": base64.b64encode(raw).decode()})
    assert ei.value.code == "out_of_order"
    assert mgr.get(tid) is None


def test_transfer_rejects_bad_sha256(xfer_env):
    workspace, transfers = xfer_env
    mgr = xfer.TransferManager(transfer_root=str(transfers), workspace=str(workspace))
    raw = b"abc"
    tid = "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee"
    mgr.handle_start(
        "s",
        {
            "transfer_id": tid,
            "name": "x.txt",
            "mime_type": "text/plain",
            "size": len(raw),
            "chunk_count": 1,
            "sha256": "0" * 64,
        },
    )
    mgr.handle_chunk("s", {"transfer_id": tid, "index": 0, "data": base64.b64encode(raw).decode()})
    with pytest.raises(xfer.TransferError) as ei:
        mgr.handle_end("s", {"transfer_id": tid, "chunk_count": 1, "sha256": "0" * 64})
    assert ei.value.code == "sha256_mismatch"
    assert mgr.get(tid) is None


def test_transfer_rejects_oversize(xfer_env):
    workspace, transfers = xfer_env
    mgr = xfer.TransferManager(transfer_root=str(transfers), workspace=str(workspace))
    with pytest.raises(xfer.TransferError) as ei:
        mgr.handle_start(
            "s",
            {
                "transfer_id": "cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee",
                "name": "big.bin",
                "mime_type": "application/octet-stream",
                "size": xfer.MAX_FILE_SIZE + 1,
                "chunk_count": 1,
                "sha256": "a" * 64,
            },
        )
    assert ei.value.code == "file_too_large"


def test_cleanup_stale_transfers(xfer_env):
    _workspace, transfers = xfer_env
    stale = transfers / "old-transfer"
    stale.mkdir()
    (stale / "payload.bin").write_bytes(b"x")
    old = os.path.getmtime(stale) - 3600
    os.utime(stale, (old, old))
    removed = xfer.cleanup_stale_transfers(str(transfers), max_age_sec=1800)
    assert removed == 1
    assert not stale.exists()


def test_approved_outbound_paths(xfer_env):
    workspace, _ = xfer_env
    inbox_file = workspace / "inbox" / "tsingpaws" / "a.txt"
    inbox_file.parent.mkdir(parents=True, exist_ok=True)
    inbox_file.write_text("ok")
    out = workspace / "skills" / "demo" / "output" / "r.pdf"
    out.write_bytes(b"%PDF")
    assert xfer.is_approved_outbound_path(str(inbox_file), str(workspace))
    assert xfer.is_approved_outbound_path(str(out), str(workspace))
    evil = workspace / "skills" / "demo" / "secret.env"
    evil.write_text("no")
    assert not xfer.is_approved_outbound_path(str(evil), str(workspace))
    assert not xfer.is_approved_outbound_path("/etc/passwd", str(workspace))


def test_build_file_frames_roundtrip(xfer_env):
    workspace, _ = xfer_env
    path = workspace / "inbox" / "tsingpaws" / "pic.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
    frames = xfer.build_file_frames("sess", str(path), "pic.png")
    assert frames[0]["type"] == "file.start"
    assert frames[-1]["type"] == "file.end"
    assert frames[0]["payload"]["mime_type"] == "image/png"
    chunks = [f for f in frames if f["type"] == "file.chunk"]
    assert len(chunks) == frames[0]["payload"]["chunk_count"]
    rebuilt = b"".join(base64.b64decode(c["payload"]["data"]) for c in chunks)
    assert rebuilt == path.read_bytes()


def test_media_ref_and_file_refs():
    assert xfer.media_ref_from_url("/pico/media/abc123") == "abc123"
    assert xfer.media_ref_from_url("http://127.0.0.1:18790/pico/media/xyz") == "xyz"
    assert xfer.media_ref_from_url("/pico/media/../etc") is None
    assert xfer.media_ref_from_url("/other/x") is None
    refs = xfer.extract_file_refs("see [image: /root/.picoclaw/workspace/inbox/a.png] and [file: skills/x/output/a.pdf]")
    assert len(refs) == 2


def test_session_cancel_clears_busy(xfer_env, monkeypatch):
    workspace, transfers = xfer_env
    monkeypatch.setenv("RELAY_URL", "ws://127.0.0.1:9/v1/agent/connect")
    monkeypatch.setenv("RELAY_TOKEN", "single-node-relay-token-32chars-min!!")
    monkeypatch.setenv("DEVICE_ID", "home-001")
    agent = ag.Agent()
    # Override transfers root to tmp
    agent.transfers = xfer.TransferManager(transfer_root=str(transfers), workspace=str(workspace))
    sent = []

    async def capture(msg: str):
        sent.append(json.loads(msg))

    agent.relay_send = capture  # type: ignore

    async def run():
        await agent.begin_session_task("sess-cancel")
        st = agent.get_session_state("sess-cancel")
        assert st.busy is True
        assert st.typing is True
        # fake in-flight transfer
        agent.transfers.handle_start(
            "sess-cancel",
            {
                "transfer_id": "dddddddd-bbbb-cccc-dddd-eeeeeeeeeeee",
                "name": "t.txt",
                "mime_type": "text/plain",
                "size": 1,
                "chunk_count": 1,
                "sha256": "a" * 64,
            },
        )
        assert agent.transfers.active_count() == 1
        await agent.cancel_session("sess-cancel")
        st2 = agent.get_session_state("sess-cancel")
        assert st2.busy is False
        assert st2.typing is False
        assert agent.transfers.active_count() == 0
        types = [m["type"] for m in sent]
        assert "typing.start" in types
        assert "typing.stop" in types

    asyncio.run(run())


def test_relay_health_accepts_auth_variant(monkeypatch):
    def fake_http(method, url, payload=None, bearer=None, timeout=10.0):
        return 200, {"version": "internal-test-auth-1", "pairing_flow": "app_first_invitation"}, {}

    monkeypatch.setattr(ag, "http_json", fake_http)
    monkeypatch.setenv("RELAY_HTTP_BASE", "http://127.0.0.1:9")
    ok, body = ag.relay_health()
    assert ok is True
    assert body["version"] == "internal-test-auth-1"


def test_relay_health_accepts_sync_variant(monkeypatch):
    def fake_http(method, url, payload=None, bearer=None, timeout=10.0):
        return 200, {"version": "internal-test-sync-1", "pairing_flow": "app_first_invitation"}, {}

    monkeypatch.setattr(ag, "http_json", fake_http)
    monkeypatch.setenv("RELAY_HTTP_BASE", "http://127.0.0.1:9")
    ok, body = ag.relay_health()
    assert ok is True
    assert body["version"] == "internal-test-sync-1"


def test_agent_version_bumped():
    assert ag.AGENT_VERSION == "2.4.4-storage-ack"
