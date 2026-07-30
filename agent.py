#!/usr/bin/env python3
"""TsingPaws OpenWrt agent: public Relay <-> local PicoClaw bridge.

Two modes, selected by /etc/tsingpaws-agent/mode:

  single_node    legacy: global RELAY_TOKEN + fixed DEVICE_ID query parameter
  internal_test  per-device DEVICE_TOKEN from /etc/tsingpaws-agent/device.json,
                 APP-first pairing (the APP shows a six digit code, this device
                 claims it), no global token and no device_id query parameter

Registration and pairing never expose credentials to the Launcher UI or the log.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import shutil
import signal
import stat
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from websockets.exceptions import ConnectionClosed
from websockets.legacy.client import connect as ws_connect

import file_transfer as xfer

AGENT_VERSION = "2.1.1-filexfer"
MAX_MESSAGE_SIZE = 4 * 1024 * 1024
MIN_BACKOFF = 2.0
MAX_BACKOFF = 30.0
AUTH_BACKOFF = 300.0
STATUS_HOST = "127.0.0.1"
TZ_CN = timezone(timedelta(hours=8))
SESSION_TASK_TIMEOUT = 20 * 60  # image gen can exceed 8m; typing.start refreshes idle timer

MODE_SINGLE = "single_node"
MODE_INTERNAL = "internal_test"
VALID_MODES = (MODE_SINGLE, MODE_INTERNAL)

REQUIRED_RELAY_VERSION = "internal-test-2"
REQUIRED_RELAY_VERSIONS = frozenset({"internal-test-auth-1", "internal-test-2"})
REQUIRED_PAIRING_FLOW = "app_first_invitation"

FILE_MSG_TYPES = frozenset({"file.start", "file.chunk", "file.end"})
FILE_ACK_TYPE = "file.ack"
MESSAGE_ACK_TYPE = "message.ack"

PAIRING_CODE_RE = re.compile(r"^\d{6}$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tsingpaws-agent")

# Secrets that must never reach the log or any API response.
_SECRETS: set = set()


# --------------------------------------------------------------------------- #
# configuration (read lazily so tests can point at a temporary directory)
# --------------------------------------------------------------------------- #


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name, default) or "").strip()


def conf_dir() -> str:
    return env("AGENT_CONF_DIR", "/etc/tsingpaws-agent")


def mode_file() -> str:
    return os.path.join(conf_dir(), "mode")


def device_file() -> str:
    return os.path.join(conf_dir(), "device.json")


def enrollment_file() -> str:
    return os.path.join(conf_dir(), "enrollment.env")


def pending_id_file() -> str:
    return os.path.join(conf_dir(), "pending-device-id")


def last_session_file() -> str:
    return os.path.join(conf_dir(), "last-session-id")


def status_port() -> int:
    try:
        return int(env("STATUS_PORT", "18791"))
    except ValueError:
        return 18791


def relay_url() -> str:
    return env("RELAY_URL")


def relay_token() -> str:
    return env("RELAY_TOKEN")


def legacy_device_id() -> str:
    return env("DEVICE_ID", "home-001")


def pico_base_url() -> str:
    return env("PICO_BASE_URL", "ws://127.0.0.1:18790").rstrip("/")


def pico_token() -> str:
    return env("PICO_TOKEN")


def pico_ws_path() -> str:
    return env("PICO_WS_PATH", "/pico/ws") or "/pico/ws"


def pico_workspace() -> str:
    return env("PICO_WORKSPACE", xfer.DEFAULT_WORKSPACE) or xfer.DEFAULT_WORKSPACE


def pico_http_base() -> str:
    base = urlparse(pico_base_url())
    host = base.hostname or "127.0.0.1"
    port = base.port or 18790
    return f"http://{host}:{port}"


def relay_http_base() -> str:
    """http://host:port of the Relay; derived from RELAY_URL when not set."""
    explicit = env("RELAY_HTTP_BASE")
    if explicit:
        return explicit.rstrip("/")
    parsed = urlparse(relay_url())
    if not parsed.hostname:
        return ""
    scheme = "https" if parsed.scheme in ("wss", "https") else "http"
    netloc = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
    return f"{scheme}://{netloc}"


def relay_ws_url() -> str:
    """WebSocket endpoint used in internal_test mode."""
    explicit = env("RELAY_WS_URL")
    if explicit:
        return explicit
    base = urlparse(relay_http_base())
    scheme = "wss" if base.scheme == "https" else "ws"
    return urlunparse((scheme, base.netloc, "/v1/agent/connect", "", "", ""))


def device_model() -> str:
    return env("DEVICE_MODEL", "TsingPaws")


def device_name() -> str:
    return env("DEVICE_NAME", "TsingPaws")


def firmware_version() -> str:
    explicit = env("FIRMWARE_VERSION")
    if explicit:
        return explicit
    try:
        with open("/etc/openwrt_release", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("DISTRIB_RELEASE="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return "unknown"


def read_mode() -> str:
    """The mode file wins over the environment; unknown values fall back safely."""
    try:
        with open(mode_file(), "r", encoding="utf-8") as fh:
            value = fh.read().strip()
        if value in VALID_MODES:
            return value
        if value:
            log.warning("invalid mode file content, falling back to %s", MODE_SINGLE)
    except OSError:
        pass
    value = env("AGENT_MODE", MODE_SINGLE)
    return value if value in VALID_MODES else MODE_SINGLE


# --------------------------------------------------------------------------- #
# redaction
# --------------------------------------------------------------------------- #


def remember_secret(value: Optional[str]) -> None:
    if value and len(value) >= 8:
        _SECRETS.add(value)


def redact(text: str) -> str:
    out = text or ""
    for secret in list(_SECRETS) + [relay_token(), pico_token()]:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "***")
    out = re.sub(r"(?i)(token|authorization|bearer|password|secret)=([^&\s]+)", r"\1=***", out)
    out = re.sub(r"(?i)Bearer\s+\S+", "Bearer ***", out)
    out = re.sub(r"\?[^.\s]*", "?***", out)
    return out


def sanitize_error(exc: "BaseException | str | None") -> Optional[str]:
    if exc is None:
        return None
    text = redact(str(exc)).strip()
    return text[:300] if text else None


# --------------------------------------------------------------------------- #
# device identity
# --------------------------------------------------------------------------- #


def load_identity() -> Optional[Dict[str, Any]]:
    """Returns the persisted identity, or None when this device is not registered."""
    try:
        with open(device_file(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    device_id = data.get("device_id")
    token = data.get("device_token")
    if not isinstance(device_id, str) or not device_id.strip():
        return None
    if not isinstance(token, str) or len(token) < 16:
        return None
    remember_secret(token)
    return data


def write_identity_atomic(data: Dict[str, Any]) -> None:
    """Temp file -> fsync -> chmod 0600 -> rename, so a crash never leaves a half file."""
    target = device_file()
    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
    tmp = f"{target}.tmp.{os.getpid()}"
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    dir_fd = os.open(os.path.dirname(target), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    os.chmod(target, 0o600)


def short_device_id(device_id: Optional[str]) -> Optional[str]:
    if not device_id:
        return None
    compact = device_id.replace("-", "")
    if len(compact) <= 12:
        return device_id
    return f"{compact[:8]}…{compact[-4:]}"


def file_mode_ok(path: str, expected: int = 0o600) -> bool:
    try:
        return stat.S_IMODE(os.stat(path).st_mode) == expected
    except OSError:
        return False


def read_enrollment_token() -> Tuple[Optional[str], Optional[str]]:
    """Reads the one-shot enrollment token. Returns (token, error_code)."""
    path = enrollment_file()
    if not os.path.exists(path):
        return None, "enrollment_missing"
    if not file_mode_ok(path):
        return None, "enrollment_permissions"
    token = ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("DEVICE_ENROLLMENT_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        return None, "enrollment_unreadable"
    if len(token) < 32:
        return None, "enrollment_invalid"
    remember_secret(token)
    return token, None


def destroy_enrollment_file() -> None:
    """Overwrite before unlinking so the token does not linger in freed blocks."""
    path = enrollment_file()
    try:
        size = os.path.getsize(path)
        with open(path, "r+b") as fh:
            fh.write(b"\x00" * size)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass


def take_pending_device_id() -> str:
    """Reuse the id of an interrupted registration instead of minting a new one."""
    path = pending_id_file()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            existing = fh.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    device_id = str(uuid.uuid4())
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(device_id + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return device_id


def clear_pending_device_id() -> None:
    try:
        os.unlink(pending_id_file())
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Relay HTTP calls
# --------------------------------------------------------------------------- #


def http_json(
    method: str,
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    bearer: Optional[str] = None,
    timeout: float = 10.0,
) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
    """Blocking JSON request. Returns (status, body, headers); status 0 means no answer."""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            body = json.loads(raw) if raw.strip() else {}
            return int(getattr(resp, "status", 200)), body if isinstance(body, dict) else {}, dict(resp.headers)
    except HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            body = json.loads(raw) if raw.strip() else {}
        except ValueError:
            body = {}
        return int(exc.code), body if isinstance(body, dict) else {}, dict(exc.headers or {})
    except (URLError, OSError, ValueError) as exc:
        return 0, {"error": "network_error", "detail": sanitize_error(exc) or "unreachable"}, {}


def relay_health() -> Tuple[bool, Dict[str, Any]]:
    """True only when the public Relay is already a supported internal-test build."""
    base = relay_http_base()
    if not base:
        return False, {"error": "relay_not_configured"}
    status, body, _ = http_json("GET", base + "/health", timeout=8)
    if status != 200:
        return False, {"error": "health_unavailable", "status": status}
    ok = body.get("version") in REQUIRED_RELAY_VERSIONS and body.get("pairing_flow") == REQUIRED_PAIRING_FLOW
    return ok, body


# --------------------------------------------------------------------------- #
# registration (one shot, driven by switch-to-internal-test.sh)
# --------------------------------------------------------------------------- #


def register_device() -> Tuple[bool, str, Dict[str, Any]]:
    """Registers this device once. Returns (ok, code, info); never returns tokens."""
    if load_identity() is not None:
        return True, "already_registered", {}

    ok, health = relay_health()
    if not ok:
        return False, "internal_test_not_enabled", {"relay_version": health.get("version")}

    token, err = read_enrollment_token()
    if err:
        return False, err, {}

    device_id = take_pending_device_id()
    body = {
        "device_id": device_id,
        "name": device_name(),
        "model": device_model(),
        "firmware_version": firmware_version(),
        "agent_version": AGENT_VERSION,
    }
    status, resp, _ = http_json(
        "POST", relay_http_base() + "/v1/devices/register", payload=body, bearer=token, timeout=15
    )

    if status == 201:
        device_token = resp.get("device_token")
        if not isinstance(device_token, str) or len(device_token) < 16:
            return False, "register_bad_response", {}
        remember_secret(device_token)
        write_identity_atomic(
            {
                "version": 1,
                "device_id": resp.get("device_id") or device_id,
                "device_token": device_token,
                "registered_at": datetime.now(TZ_CN).isoformat(timespec="seconds"),
                "relay_base": relay_http_base(),
            }
        )
        verified = load_identity()
        if verified is None or verified.get("device_id") != (resp.get("device_id") or device_id):
            return False, "identity_verify_failed", {}
        if not file_mode_ok(device_file()):
            return False, "identity_permissions", {}
        clear_pending_device_id()
        destroy_enrollment_file()
        return True, "registered", {"device_id": verified["device_id"]}

    if status == 409:
        # Never overwrite a device or mint a new id in a loop: stop and ask for help.
        return False, "device_already_registered", {"device_id": device_id}
    if status == 401:
        return False, "enrollment_unauthorized", {}
    if status == 0:
        return False, "relay_unreachable", {}
    return False, "register_failed", {"status": status}


# --------------------------------------------------------------------------- #
# PicoClaw plumbing
# --------------------------------------------------------------------------- #


def pico_connect_url(session_id: str) -> str:
    base = urlparse(pico_base_url())
    path = pico_ws_path() if pico_ws_path().startswith("/") else "/" + pico_ws_path()
    return urlunparse((base.scheme or "ws", base.netloc, path, "", urlencode({"session_id": session_id}), ""))


def pico_http_health_url() -> str:
    base = urlparse(pico_base_url())
    host = base.hostname or "127.0.0.1"
    port = base.port or 18790
    return f"http://{host}:{port}/health"


def relay_host() -> str:
    for candidate in (relay_http_base(), relay_url()):
        try:
            host = urlparse(candidate).hostname
        except Exception:
            host = None
        if host:
            return host
    return ""


def legacy_relay_connect_url() -> str:
    parsed = urlparse(relay_url())
    q: Dict[str, str] = {}
    if parsed.query:
        for part in parsed.query.split("&"):
            if not part:
                continue
            if "=" in part:
                k, v = part.split("=", 1)
                q[k] = v
            else:
                q[part] = ""
    q["device_id"] = legacy_device_id()
    return urlunparse(parsed._replace(query=urlencode(q)))


def extract_session_id(raw: str) -> Optional[str]:
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    for key in ("session_id", "sessionId"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    payload = obj.get("payload")
    if isinstance(payload, dict):
        for key in ("session_id", "sessionId"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def now_iso() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")


def make_envelope(msg_type: str, session_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "type": msg_type,
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "timestamp": int(time.time() * 1000),
        "payload": payload if isinstance(payload, dict) else {},
    }


def load_last_session_id() -> str:
    try:
        with open(last_session_file(), "r", encoding="utf-8") as fh:
            value = fh.read(256).strip()
    except OSError:
        return ""
    return value if re.fullmatch(r"[A-Za-z0-9._:-]{8,200}", value) else ""


def save_last_session_id(session_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,200}", session_id):
        return
    target = last_session_file()
    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
    tmp = f"{target}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(session_id)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


@dataclass
class SessionState:
    """Per-session busy / cancel / typing state (never a global lock)."""

    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    active_task: Optional[asyncio.Task] = None
    last_activity: float = field(default_factory=time.time)
    typing: bool = False
    busy: bool = False
    timeout_handle: Optional[asyncio.TimerHandle] = None
    pushed_paths: Set[str] = field(default_factory=set)
    task_started_at: float = 0.0


class PicoSession:
    def __init__(self, session_id: str, agent: "Agent"):
        self.session_id = session_id
        self.agent = agent
        self.ws = None
        self.task: Optional[asyncio.Task] = None
        self._send_lock = asyncio.Lock()

    async def ensure(self) -> None:
        if self.ws is not None and not self.ws.closed:
            return
        await self._connect()

    async def _connect(self) -> None:
        url = pico_connect_url(self.session_id)
        log.info("pico connecting session=%s", self.session_id)
        self.ws = await ws_connect(
            url,
            extra_headers={"Authorization": f"Bearer {pico_token()}"},
            ping_interval=25,
            ping_timeout=45,
            max_size=MAX_MESSAGE_SIZE,
            open_timeout=15,
        )
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._reader(), name=f"pico-reader-{self.session_id}")
        log.info("pico connected session=%s", self.session_id)

    async def _reader(self) -> None:
        assert self.ws is not None
        try:
            async for message in self.ws:
                if isinstance(message, bytes):
                    log.warning("drop non-text pico frame session=%s", self.session_id)
                    continue
                if len(message) > MAX_MESSAGE_SIZE:
                    log.warning("drop oversized pico text session=%s", self.session_id)
                    continue
                await self.agent.handle_pico_message(self.session_id, message)
        except ConnectionClosed:
            log.info("pico closed session=%s", self.session_id)
        except Exception as exc:
            log.warning("pico reader error session=%s err=%s", self.session_id, redact(str(exc)))
        finally:
            self.ws = None

    async def send(self, message: str) -> None:
        await self.ensure()
        assert self.ws is not None
        async with self._send_lock:
            await self.ws.send(message)

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None


# --------------------------------------------------------------------------- #
# agent
# --------------------------------------------------------------------------- #


class Agent:
    def __init__(self) -> None:
        self.relay = None
        self.sessions: Dict[str, PicoSession] = {}
        self.session_states: Dict[str, SessionState] = {}
        self._relay_send_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._reconnect_event = asyncio.Event()
        self._claim_lock = asyncio.Lock()
        self._outbox_replay_lock = asyncio.Lock()
        self.started_at = time.time()
        self.mode = read_mode()
        self.identity = load_identity()
        self.relay_connected = False
        self.relay_connecting = False
        self.pico_reachable = False
        self.reconnect_attempt = 0
        self.last_connected_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self.config_error: Optional[str] = None
        self.credential_invalid = False
        self.last_pairing_result: Optional[str] = None
        self.last_pairing_at: Optional[str] = None
        self.last_session_id: str = load_last_session_id()
        self.ack_capable_sessions: Set[str] = set()
        self.transfers = xfer.TransferManager(
            workspace=pico_workspace(),
            send_error=self._transfer_error_cb,
        )

    async def _transfer_error_cb(self, session_id: str, payload: Dict[str, Any]) -> None:
        await self.send_to_app(make_envelope("error", session_id, payload))

    # ---- identity helpers ----

    @property
    def registered(self) -> bool:
        return self.mode == MODE_INTERNAL and self.identity is not None

    def device_id(self) -> str:
        if self.mode == MODE_INTERNAL and self.identity is not None:
            return str(self.identity.get("device_id") or "")
        return legacy_device_id()

    def device_token(self) -> Optional[str]:
        if self.identity is None:
            return None
        token = self.identity.get("device_token")
        return token if isinstance(token, str) else None

    def check_config(self) -> None:
        if not pico_token():
            self.config_error = "PICO_TOKEN 未配置"
            return
        if self.mode == MODE_INTERNAL:
            if not relay_http_base():
                self.config_error = "RELAY_HTTP_BASE 未配置"
            elif self.identity is None:
                self.config_error = "设备尚未注册内部测试版"
            else:
                self.config_error = None
            return
        if not relay_url():
            self.config_error = "RELAY_URL 未配置"
        elif len(relay_token()) < 32:
            self.config_error = "RELAY_TOKEN 未配置"
        elif not legacy_device_id():
            self.config_error = "DEVICE_ID 未配置"
        else:
            self.config_error = None

    # ---- session task lifecycle ----

    def get_session_state(self, session_id: str) -> SessionState:
        state = self.session_states.get(session_id)
        if state is None:
            state = SessionState()
            self.session_states[session_id] = state
        return state

    def touch_session(self, session_id: str) -> SessionState:
        state = self.get_session_state(session_id)
        state.last_activity = time.time()
        return state

    def clear_session_busy(self, session_id: str) -> None:
        state = self.session_states.get(session_id)
        if state is None:
            return
        if state.timeout_handle is not None:
            state.timeout_handle.cancel()
            state.timeout_handle = None
        state.busy = False
        state.typing = False
        state.active_task = None
        state.cancel_event = asyncio.Event()

    def clear_all_session_state(self) -> None:
        for sid in list(self.session_states.keys()):
            self.clear_session_busy(sid)
        self.session_states.clear()
        self.transfers.cleanup_all()

    async def send_to_app(self, obj: Dict[str, Any]) -> None:
        try:
            await self.relay_send(json.dumps(obj, ensure_ascii=False))
        except Exception as exc:
            log.warning("send_to_app failed err=%s", redact(str(exc)))

    async def typing_start(self, session_id: str) -> None:
        state = self.touch_session(session_id)
        if state.typing:
            return
        state.typing = True
        await self.send_to_app(make_envelope("typing.start", session_id, {}))

    async def _typing_stop_force(self, session_id: str) -> None:
        state = self.get_session_state(session_id)
        if not state.typing and not state.busy:
            return
        state.typing = False
        await self.send_to_app(make_envelope("typing.stop", session_id, {}))

    def _arm_session_timeout(self, session_id: str) -> None:
        state = self.get_session_state(session_id)
        if state.timeout_handle is not None:
            state.timeout_handle.cancel()
        loop = asyncio.get_running_loop()

        def _fire() -> None:
            asyncio.create_task(self._on_session_timeout(session_id))

        state.timeout_handle = loop.call_later(SESSION_TASK_TIMEOUT, _fire)

    async def _on_session_timeout(self, session_id: str) -> None:
        state = self.session_states.get(session_id)
        if state is None or not state.busy:
            return
        log.warning("session task timeout session=%s", session_id)
        state.cancel_event.set()
        await self.send_to_app(
            make_envelope("error", session_id, {"code": "task_timeout", "message": "任务超时"})
        )
        await self._typing_stop_force(session_id)
        self.transfers.cleanup_session(session_id)
        # Do NOT close the Pico session here — killing WS mid-reply makes the APP
        # drop into "正在连接对话服务". Clear busy so the next user message can proceed.
        self.clear_session_busy(session_id)

    async def begin_session_task(self, session_id: str) -> SessionState:
        """Mark session busy and emit typing.start (idempotent)."""
        state = self.touch_session(session_id)
        state.busy = True
        state.task_started_at = time.time()
        state.cancel_event = asyncio.Event()
        await self.typing_start(session_id)
        self._arm_session_timeout(session_id)
        if state.active_task is None or state.active_task.done():
            state.active_task = asyncio.create_task(
                self._watch_tool_outputs(session_id, state),
                name=f"tool-output-watch-{session_id[:8]}",
            )
        return state

    async def _watch_tool_outputs(self, session_id: str, state: SessionState) -> None:
        """Recover tool outputs that Pico's direct channel omitted from websocket events."""
        try:
            while state.busy and not state.cancel_event.is_set():
                await asyncio.sleep(0.5)
                if await self._push_send_file_fallback(session_id, state):
                    await self.finish_session_task(session_id, success=True)
                    return
                if await self._push_generated_image_fallback(session_id, "", state, force=True):
                    await self.finish_session_task(session_id, success=True)
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("generated image watcher failed session=%s err=%s", session_id, redact(str(exc)))

    async def finish_session_task(self, session_id: str, *, success: bool) -> None:
        try:
            if success:
                await self.send_to_app(make_envelope("response.done", session_id, {}))
        finally:
            await self._typing_stop_force(session_id)
            self.clear_session_busy(session_id)

    async def cancel_session(self, session_id: str) -> None:
        state = self.get_session_state(session_id)
        state.cancel_event.set()
        if state.active_task and not state.active_task.done():
            state.active_task.cancel()
        self.transfers.cleanup_session(session_id)
        await self._close_pico_session(session_id)
        await self._typing_stop_force(session_id)
        self.clear_session_busy(session_id)
        log.info("session cancelled session=%s", session_id)

    async def _close_pico_session(self, session_id: str) -> None:
        sess = self.sessions.pop(session_id, None)
        if sess is not None:
            try:
                await sess.close()
            except Exception:
                pass

    # ---- relay plumbing ----

    async def relay_send(self, message: str) -> None:
        if self.relay is None or self.relay.closed:
            return
        async with self._relay_send_lock:
            try:
                await self.relay.send(message)
            except ConnectionClosed:
                log.info("relay send failed: closed")

    async def get_session(self, session_id: str) -> PicoSession:
        if self.last_session_id != session_id:
            self.last_session_id = session_id
            try:
                save_last_session_id(session_id)
            except OSError as exc:
                log.warning("persist last session failed err=%s", redact(str(exc)))
        sess = self.sessions.get(session_id)
        if sess is None:
            sess = PicoSession(session_id, self)
            self.sessions[session_id] = sess
        return sess

    async def push_file_to_recent_session(self, path: Any) -> Tuple[int, Dict[str, Any]]:
        """Push one approved local file to the most recently active APP chat."""
        if not isinstance(path, str) or not path.strip():
            return 400, {"ok": False, "error": "invalid_path"}
        session_id = self.last_session_id
        if not session_id:
            return 409, {"ok": False, "error": "no_active_session"}
        candidate = os.path.realpath(path.strip())
        workspace = pico_workspace()
        if not xfer.is_approved_outbound_path(candidate, workspace):
            return 400, {"ok": False, "error": "unapproved_path"}
        if not os.path.isfile(candidate):
            return 404, {"ok": False, "error": "file_not_found"}
        name = xfer.sanitize_filename(os.path.basename(candidate))
        mime = xfer.guess_mime(name)
        try:
            await self._push_local_file_to_app(session_id, candidate, name, mime)
        except Exception as exc:
            log.warning("proactive file push failed session=%s err=%s", session_id, redact(str(exc)))
            return 502, {"ok": False, "error": "push_failed"}
        log.info("proactive file push complete session=%s name=%s", session_id, name)
        return 200, {"ok": True, "session_id": session_id, "name": name, "mime_type": mime}

    async def push_message_to_recent_session(self, content: Any) -> Tuple[int, Dict[str, Any]]:
        if not isinstance(content, str) or not content.strip():
            return 400, {"ok": False, "error": "invalid_content"}
        session_id = self.last_session_id
        if not session_id:
            return 409, {"ok": False, "error": "no_active_session"}
        envelope = make_envelope("message.create", session_id, {"content": content.strip()})
        await asyncio.to_thread(self._queue_outbound_message, envelope)
        await self.send_to_app(envelope)
        return 200, {"ok": True, "session_id": session_id, "message_id": envelope["id"]}

    def _outbox_dir(self) -> str:
        return os.path.join(conf_dir(), "outbox")

    def _message_outbox_dir(self) -> str:
        return os.path.join(conf_dir(), "message-outbox")

    def _queue_outbound_message(self, envelope: Dict[str, Any]) -> None:
        message_id = str(envelope.get("id") or "")
        session_id = str(envelope.get("session_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,200}", message_id):
            return
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,200}", session_id):
            return
        directory = self._message_outbox_dir()
        os.makedirs(directory, mode=0o700, exist_ok=True)
        target = os.path.join(directory, f"{message_id}.json")
        tmp = f"{target}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "message_id": message_id,
                        "session_id": session_id,
                        "created_at": int(time.time()),
                        "envelope": envelope,
                    },
                    fh,
                    ensure_ascii=False,
                )
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _ack_outbound_message(self, message_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,200}", message_id):
            return
        try:
            os.unlink(os.path.join(self._message_outbox_dir(), message_id + ".json"))
        except OSError:
            pass
        log.info("outbound message acknowledged message=%s", message_id[:12])

    def _queue_outbound_file(
        self, transfer_id: str, session_id: str, path: str, name: str, mime: str
    ) -> None:
        directory = self._outbox_dir()
        os.makedirs(directory, mode=0o700, exist_ok=True)
        data_path = os.path.join(directory, f"{transfer_id}.bin")
        meta_path = os.path.join(directory, f"{transfer_id}.json")
        data_tmp = f"{data_path}.tmp.{os.getpid()}"
        meta_tmp = f"{meta_path}.tmp.{os.getpid()}"
        try:
            shutil.copyfile(path, data_tmp)
            os.chmod(data_tmp, 0o600)
            with open(meta_tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "transfer_id": transfer_id,
                        "session_id": session_id,
                        "name": name,
                        "mime": mime,
                        "created_at": int(time.time()),
                    },
                    fh,
                    ensure_ascii=False,
                )
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(meta_tmp, 0o600)
            os.replace(data_tmp, data_path)
            os.replace(meta_tmp, meta_path)
        finally:
            for tmp in (data_tmp, meta_tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _ack_outbound_file(self, transfer_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", transfer_id):
            return
        for suffix in (".json", ".bin"):
            try:
                os.unlink(os.path.join(self._outbox_dir(), transfer_id + suffix))
            except OSError:
                pass
        log.info("outbound file acknowledged transfer=%s", transfer_id[:12])

    async def _replay_outbox(self, session_id: str) -> None:
        if self._outbox_replay_lock.locked():
            return
        async with self._outbox_replay_lock:
            await self._replay_outbox_locked(session_id)

    async def _replay_outbox_locked(self, session_id: str) -> None:
        await self._replay_message_outbox_locked(session_id)
        directory = self._outbox_dir()
        try:
            meta_paths = sorted(glob.glob(os.path.join(directory, "*.json")))
        except OSError:
            return
        for meta_path in meta_paths:
            try:
                with open(meta_path, "r", encoding="utf-8") as fh:
                    meta = json.load(fh)
                if not isinstance(meta, dict) or meta.get("session_id") != session_id:
                    continue
                last_sent_at = int(meta.get("last_sent_at") or 0)
                if last_sent_at and time.time() - last_sent_at < 300:
                    continue
                transfer_id = str(meta.get("transfer_id") or "")
                if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", transfer_id):
                    continue
                data_path = os.path.join(directory, transfer_id + ".bin")
                if not os.path.isfile(data_path):
                    continue
                meta["last_sent_at"] = int(time.time())
                meta_tmp = f"{meta_path}.tmp.{os.getpid()}"
                try:
                    with open(meta_tmp, "w", encoding="utf-8") as fh:
                        json.dump(meta, fh, ensure_ascii=False)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.chmod(meta_tmp, 0o600)
                    os.replace(meta_tmp, meta_path)
                finally:
                    try:
                        os.unlink(meta_tmp)
                    except OSError:
                        pass
                frames = await asyncio.to_thread(
                    xfer.build_file_frames,
                    session_id,
                    data_path,
                    xfer.sanitize_filename(meta.get("name") or "TsingPaws-file"),
                    str(meta.get("mime") or "application/octet-stream"),
                    transfer_id,
                )
                for frame in frames:
                    await self.send_to_app(frame)
                log.info("replayed queued file session=%s transfer=%s", session_id, transfer_id[:12])
            except Exception as exc:
                log.warning("queued file replay failed session=%s err=%s", session_id, redact(str(exc)))

    async def _replay_message_outbox_locked(self, session_id: str) -> None:
        directory = self._message_outbox_dir()
        for meta_path in sorted(glob.glob(os.path.join(directory, "*.json"))):
            try:
                with open(meta_path, "r", encoding="utf-8") as fh:
                    meta = json.load(fh)
                if not isinstance(meta, dict) or meta.get("session_id") != session_id:
                    continue
                last_sent_at = int(meta.get("last_sent_at") or 0)
                if last_sent_at and time.time() - last_sent_at < 300:
                    continue
                envelope = meta.get("envelope")
                if not isinstance(envelope, dict):
                    continue
                meta["last_sent_at"] = int(time.time())
                tmp = f"{meta_path}.tmp.{os.getpid()}"
                try:
                    with open(tmp, "w", encoding="utf-8") as fh:
                        json.dump(meta, fh, ensure_ascii=False)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, meta_path)
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                await self.send_to_app(envelope)
                log.info(
                    "replayed queued message session=%s message=%s",
                    session_id,
                    str(meta.get("message_id") or "")[:12],
                )
            except Exception as exc:
                log.warning("queued message replay failed session=%s err=%s", session_id, redact(str(exc)))

    async def forward_to_pico(self, session_id: str, message: str) -> None:
        try:
            sess = await self.get_session(session_id)
            await sess.send(message)
            self.pico_reachable = True
        except Exception as exc:
            self.last_error = sanitize_error(exc)
            log.warning("forward to pico failed session=%s err=%s", session_id, redact(str(exc)))
            await self._close_pico_session(session_id)
            raise

    async def handle_relay_message(self, message: Any) -> None:
        if isinstance(message, bytes):
            try:
                message = message.decode("utf-8")
            except Exception:
                log.warning("drop non-utf8 relay binary")
                return
        if not isinstance(message, str):
            return
        if len(message) > MAX_MESSAGE_SIZE:
            log.warning("drop oversized relay message")
            return
        try:
            obj = json.loads(message)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            kind = obj.get("type")
            if kind == "relay.peer_offline":
                log.info("relay peer offline")
                return
            if kind in ("relay.device_online", "relay.device_offline", "relay.pairing_claimed"):
                log.info("relay event type=%s", kind)
                return

            session_id = extract_session_id(message)
            if kind == MESSAGE_ACK_TYPE:
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                message_id = payload.get("message_id")
                if isinstance(message_id, str):
                    if session_id:
                        self.ack_capable_sessions.add(session_id)
                    self._ack_outbound_message(message_id)
                return
            if kind == FILE_ACK_TYPE:
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                transfer_id = payload.get("transfer_id")
                if isinstance(transfer_id, str):
                    if session_id:
                        self.ack_capable_sessions.add(session_id)
                    self._ack_outbound_file(transfer_id)
                return
            if kind in FILE_MSG_TYPES:
                if not session_id:
                    log.warning("file message missing session_id")
                    return
                try:
                    await self._handle_file_message(session_id, kind, obj)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("file handler failed err=%s", redact(str(exc)))
                return
            if kind == "response.cancel":
                if session_id:
                    try:
                        await self.cancel_session(session_id)
                    except asyncio.CancelledError:
                        # Cancellation of session work must not tear down the relay loop.
                        log.info("session cancel interrupted session=%s", session_id[:8])
                    except Exception as exc:
                        log.warning("cancel_session failed err=%s", redact(str(exc)))
                return
            if kind == "session.resume":
                # Relay routing is device-based. Re-establish the per-session
                # Pico socket locally after an APP reconnect; never forward
                # this control envelope because Pico does not define it.
                if session_id:
                    try:
                        resume_payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                        capabilities = resume_payload.get("capabilities")
                        if isinstance(capabilities, list) and {
                            "file_ack", "message_ack"
                        }.issubset({str(item) for item in capabilities}):
                            self.ack_capable_sessions.add(session_id)
                        session = await self.get_session(session_id)
                        await session.ensure()
                        self.pico_reachable = True
                        if session_id in self.ack_capable_sessions:
                            await self._replay_outbox(session_id)
                    except Exception as exc:
                        log.warning("session resume failed session=%s err=%s", session_id, redact(str(exc)))
                return
            # APP lifecycle events — handle locally, never forward to Pico.
            if kind in ("typing.start", "typing.stop", "response.done"):
                return

            if not session_id:
                log.warning("relay message missing session_id")
                return

            if kind == "message.send":
                try:
                    await self.begin_session_task(session_id)
                    await self.forward_to_pico(session_id, message)
                except asyncio.CancelledError:
                    await self._typing_stop_force(session_id)
                    self.clear_session_busy(session_id)
                    return
                except Exception as exc:
                    await self.send_to_app(
                        make_envelope(
                            "error",
                            session_id,
                            {"code": "forward_failed", "message": sanitize_error(exc) or "转发失败"},
                        )
                    )
                    await self._typing_stop_force(session_id)
                    self.clear_session_busy(session_id)
                return

            # Other known pico types: forward; unknown types never disconnect.
            try:
                await self.forward_to_pico(session_id, message)
            except asyncio.CancelledError:
                return
            except Exception:
                pass
            return

        session_id = extract_session_id(message)
        if not session_id:
            log.warning("relay message missing session_id")
            return
        try:
            await self.forward_to_pico(session_id, message)
        except Exception:
            pass

    async def _handle_file_message(self, session_id: str, kind: str, obj: Dict[str, Any]) -> None:
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        transfer_id = payload.get("transfer_id") if isinstance(payload.get("transfer_id"), str) else None
        try:
            if kind == "file.start":
                self.transfers.handle_start(session_id, payload)
            elif kind == "file.chunk":
                self.transfers.handle_chunk(session_id, payload)
            elif kind == "file.end":
                completed = self.transfers.handle_end(session_id, payload)
                await self._after_inbound_file(completed)
        except xfer.TransferError as exc:
            log.warning(
                "transfer error code=%s session=%s transfer=%s",
                exc.code,
                session_id[:8] if session_id else "",
                (transfer_id or "")[:8],
            )
            await self.send_to_app(
                make_envelope(
                    "error",
                    session_id,
                    {
                        "code": exc.code,
                        "message": exc.message,
                        **({"transfer_id": transfer_id} if transfer_id else {}),
                    },
                )
            )
            if transfer_id:
                self.transfers.cleanup_transfer(transfer_id)
        except Exception as exc:
            log.warning("transfer unexpected err=%s", redact(str(exc)))
            await self.send_to_app(
                make_envelope(
                    "error",
                    session_id,
                    {"code": "transfer_failed", "message": "文件传输失败"},
                )
            )
            if transfer_id:
                self.transfers.cleanup_transfer(transfer_id)

    async def _after_inbound_file(self, completed: xfer.CompletedTransfer) -> None:
        """Place is already in inbox; notify Pico via message.send (path + optional image media)."""
        session_id = completed.session_id
        note = f"用户发送了附件「{completed.name}」，本地路径：{completed.path}"
        pico_payload: Dict[str, Any] = {"content": note}
        if xfer.is_image_mime(completed.mime_type) and completed.size <= xfer.IMAGE_INLINE_MAX:
            try:
                data_url = xfer.to_data_url(completed.mime_type, completed.path)
                pico_payload["media"] = data_url
                pico_payload["attachments"] = [
                    {
                        "type": "image",
                        "filename": completed.name,
                        "content_type": completed.mime_type,
                        "url": data_url,
                    }
                ]
            except xfer.TransferError:
                pass  # text path reference is enough
        envelope = make_envelope("message.send", session_id, pico_payload)
        raw = json.dumps(envelope, ensure_ascii=False)
        try:
            await self.begin_session_task(session_id)
            await self.forward_to_pico(session_id, raw)
        except Exception as exc:
            await self.send_to_app(
                make_envelope(
                    "error",
                    session_id,
                    {"code": "pico_notify_failed", "message": sanitize_error(exc) or "通知 Pico 失败"},
                )
            )
            await self._typing_stop_force(session_id)
            self.clear_session_busy(session_id)

    # ---- pico outbound ----

    async def handle_pico_message(self, session_id: str, message: str) -> None:
        try:
            obj = json.loads(message)
        except Exception:
            await self.relay_send(message)
            return
        if not isinstance(obj, dict):
            await self.relay_send(message)
            return

        # Ensure session_id present for APP.
        if not obj.get("session_id"):
            obj["session_id"] = session_id

        kind = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

        # Some Pico direct-channel builds omit the generated image from the
        # websocket event entirely. Any event arriving after the image file is
        # committed is enough to recover that task-scoped output.
        current_state = self.session_states.get(session_id)
        if current_state is not None and current_state.busy:
            recovered = await self._push_generated_image_fallback(
                session_id, "", current_state, force=True
            )
            if recovered:
                await self.finish_session_task(session_id, success=True)
                return

        if kind in ("typing.start", "typing.stop"):
            state = self.touch_session(session_id)
            if kind == "typing.start":
                state.typing = True
                # Pico still working — refresh idle timeout so long image gens survive.
                if state.busy:
                    self._arm_session_timeout(session_id)
            else:
                state.typing = False
            await self.send_to_app(obj)
            return

        if kind == "error":
            await self.send_to_app(obj)
            await self._typing_stop_force(session_id)
            self.clear_session_busy(session_id)
            return

        if kind == "message.create":
            # Skip thought-only messages.
            if payload.get("thought") is True:
                log.info("skip thought message session=%s", session_id)
                return
            await self._handle_pico_message_create(session_id, obj, payload)
            return

        if kind == "message.update":
            content = payload.get("content")
            content_text = content if isinstance(content, str) else ""
            has_delivery_marker = "delivered via tool attachment" in content_text.lower()
            has_attachments = isinstance(payload.get("attachments"), list) and bool(payload.get("attachments"))
            has_file_refs = bool(xfer.extract_file_refs(content_text))
            if has_delivery_marker or has_attachments or has_file_refs:
                await self._handle_pico_message_create(session_id, obj, payload)
                return
            if content_text and not payload.get("delta") and obj.get("id"):
                await asyncio.to_thread(self._queue_outbound_message, obj)

        # Forward other types safely.
        await self.send_to_app(obj)

    async def _handle_pico_message_create(
        self, session_id: str, obj: Dict[str, Any], payload: Dict[str, Any]
    ) -> None:
        state = self.touch_session(session_id)
        content = payload.get("content")
        # Forward text with huge inline binary stripped.
        forward = dict(obj)
        fwd_payload = dict(payload)
        if isinstance(content, str):
            fwd_payload["content"] = xfer.strip_huge_binary_from_content(content)
        # Do not forward raw attachment blobs / media data URLs to APP (we push file.* instead).
        fwd_payload.pop("media", None)
        attachments = fwd_payload.get("attachments")
        safe_atts = []
        if isinstance(attachments, list):
            for att in attachments:
                if not isinstance(att, dict):
                    continue
                safe = {k: v for k, v in att.items() if k != "url" or (
                    isinstance(v, str) and v.startswith("/pico/media/")
                )}
                if isinstance(safe.get("url"), str) and safe["url"].startswith("data:"):
                    safe["url"] = "/pico/media/omitted"
                safe_atts.append(safe)
            fwd_payload["attachments"] = safe_atts
        forward["payload"] = fwd_payload
        forward["session_id"] = session_id
        if (
            isinstance(content, str)
            and content.strip()
            and "delivered via tool attachment" not in content.lower()
            and not payload.get("delta")
            and forward.get("id")
        ):
            await asyncio.to_thread(self._queue_outbound_message, forward)
        await self.send_to_app(forward)

        # Push attachment files + content path refs.
        ok = True
        try:
            await self._push_pico_attachments(session_id, payload, state)
            await self._push_content_file_refs(session_id, content if isinstance(content, str) else "", state)
            await self._push_send_file_fallback(session_id, state)
            await self._push_generated_image_fallback(
                session_id, content if isinstance(content, str) else "", state
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ok = False
            log.warning("outbound file push failed session=%s err=%s", session_id, redact(str(exc)))
            await self.send_to_app(
                make_envelope(
                    "error",
                    session_id,
                    {"code": "outbound_file_failed", "message": "推送附件失败"},
                )
            )
        finally:
            await self.finish_session_task(session_id, success=ok)

    async def _push_pico_attachments(
        self, session_id: str, payload: Dict[str, Any], state: SessionState
    ) -> None:
        attachments = payload.get("attachments")
        if not isinstance(attachments, list):
            return
        for att in attachments:
            if state.cancel_event.is_set():
                return
            if not isinstance(att, dict):
                continue
            url = att.get("url")
            ref = xfer.media_ref_from_url(url if isinstance(url, str) else "")
            if not ref:
                continue
            filename = xfer.sanitize_filename(att.get("filename") or f"{ref}.bin")
            mime = xfer.guess_mime(filename, att.get("content_type") if isinstance(att.get("content_type"), str) else None)
            tmp_path = await self._download_pico_media(ref, filename)
            if tmp_path is None:
                continue
            try:
                key = os.path.realpath(tmp_path)
                if key in state.pushed_paths:
                    continue
                await self._push_local_file_to_app(session_id, tmp_path, filename, mime)
                state.pushed_paths.add(key)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    async def _push_content_file_refs(
        self, session_id: str, content: str, state: SessionState
    ) -> None:
        workspace = pico_workspace()
        for raw_path in xfer.extract_file_refs(content):
            if state.cancel_event.is_set():
                return
            path = raw_path.strip().strip("\"'")
            if not path:
                continue
            if not os.path.isabs(path):
                path = os.path.join(workspace, path)
            if not xfer.is_approved_outbound_path(path, workspace):
                log.warning("reject unapproved outbound path session=%s", session_id)
                continue
            if not os.path.isfile(path):
                continue
            try:
                key = os.path.realpath(path)
            except OSError:
                continue
            if key in state.pushed_paths:
                continue
            name = xfer.sanitize_filename(os.path.basename(path))
            mime = xfer.guess_mime(name)
            await self._push_local_file_to_app(session_id, path, name, mime)
            state.pushed_paths.add(key)

    async def _push_send_file_fallback(
        self, session_id: str, state: SessionState
    ) -> bool:
        """Recover files delivered by Pico's send_file tool but omitted from direct-channel events.

        Only accept a regular file explicitly named in a send_file tool call from
        this session, created during the current task, and located either in the
        Pico workspace or in /tmp. This keeps the recovery path task-scoped and
        prevents arbitrary local files from being exposed.
        """
        started = state.task_started_at
        if started <= 0:
            return False
        sessions_dir = os.path.join(pico_workspace(), "sessions")
        patterns = (
            os.path.join(sessions_dir, f"*{session_id}.jsonl"),
            os.path.join("/root/.picoclaw/workspace/sessions", f"*{session_id}.jsonl"),
        )
        session_files = []
        for pattern in patterns:
            session_files.extend(glob.glob(pattern))
        recovered = False
        seen_logs: Set[str] = set()
        for log_path in session_files:
            real_log = os.path.realpath(log_path)
            if real_log in seen_logs:
                continue
            seen_logs.add(real_log)
            try:
                if os.path.getmtime(real_log) + 2 < started:
                    continue
                with open(real_log, "r", encoding="utf-8") as fh:
                    lines = fh.readlines()[-160:]
            except (OSError, UnicodeError):
                continue
            for line in lines:
                try:
                    entry = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(entry, dict) or entry.get("role") != "assistant":
                    continue
                calls = entry.get("tool_calls")
                if not isinstance(calls, list):
                    continue
                for call in calls:
                    fn = call.get("function") if isinstance(call, dict) else None
                    if not isinstance(fn, dict) or fn.get("name") != "send_file":
                        continue
                    args = fn.get("arguments")
                    try:
                        args_obj = json.loads(args) if isinstance(args, str) else args
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(args_obj, dict):
                        continue
                    raw_path = args_obj.get("path")
                    if not isinstance(raw_path, str) or not raw_path.strip():
                        continue
                    path = os.path.realpath(raw_path.strip())
                    try:
                        in_tmp = os.path.commonpath((path, "/tmp")) == "/tmp"
                    except ValueError:
                        in_tmp = False
                    if not in_tmp and not xfer.is_approved_outbound_path(path, pico_workspace()):
                        log.warning("reject unapproved send_file path session=%s", session_id)
                        continue
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    if not stat.S_ISREG(st.st_mode) or st.st_mtime + 2 < started:
                        continue
                    if path in state.pushed_paths:
                        continue
                    requested_name = args_obj.get("filename")
                    name = xfer.sanitize_filename(
                        requested_name if isinstance(requested_name, str) else os.path.basename(path)
                    )
                    mime = xfer.guess_mime(name)
                    state.pushed_paths.add(path)
                    try:
                        await self._push_local_file_to_app(session_id, path, name, mime)
                    except Exception:
                        state.pushed_paths.discard(path)
                        raise
                    recovered = True
                    log.info("recovered send_file session=%s name=%s", session_id, name)
        return recovered

    async def _push_generated_image_fallback(
        self, session_id: str, content: str, state: SessionState, *, force: bool = False
    ) -> bool:
        """Recover image-generator output omitted by Pico's direct-channel event.

        Pico 1.19 writes the image into workspace/inbox, but its final
        message.create currently contains only a delivery marker and no
        attachment/path.  Only inspect generated-image-* files created during
        the current task, so historical or unrelated inbox files cannot leak.
        """
        marker = "delivered via tool attachment"
        if not force and marker not in content.lower():
            return False
        inbox = os.path.join(pico_workspace(), "inbox")
        started = state.task_started_at
        if started <= 0 or not os.path.isdir(inbox):
            return False
        candidates = []
        try:
            for name in os.listdir(inbox):
                if not name.lower().startswith("generated-image-"):
                    continue
                path = os.path.join(inbox, name)
                if not os.path.isfile(path):
                    continue
                mtime = os.path.getmtime(path)
                if mtime + 2 < started:
                    continue
                candidates.append((mtime, path))
        except OSError as exc:
            log.warning("generated image fallback scan failed session=%s err=%s", session_id, redact(str(exc)))
            return False
        recovered = False
        for _mtime, path in sorted(candidates):
            key = os.path.realpath(path)
            if key in state.pushed_paths:
                continue
            name = xfer.sanitize_filename(os.path.basename(path))
            mime = xfer.guess_mime(name)
            if not xfer.is_image_mime(mime):
                continue
            state.pushed_paths.add(key)
            try:
                await self._push_local_file_to_app(session_id, path, name, mime)
            except Exception:
                state.pushed_paths.discard(key)
                raise
            recovered = True
            log.info("recovered generated image session=%s name=%s", session_id, name)
        return recovered

    async def _download_pico_media(self, ref: str, filename: str) -> Optional[str]:
        """Download /pico/media/<ref> to a temp file. Never logs the token."""
        url = f"{pico_http_base()}/pico/media/{ref}"
        dest_dir = os.path.join(xfer.DEFAULT_TRANSFER_ROOT, f"media-{ref[:16]}")
        xfer.ensure_dir(dest_dir, 0o700)
        dest = os.path.join(dest_dir, xfer.sanitize_filename(filename))

        def _fetch() -> Optional[str]:
            req = Request(url, method="GET")
            req.add_header("Authorization", f"Bearer {pico_token()}")
            try:
                with urlopen(req, timeout=60) as resp:
                    # Stream with size cap
                    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    written = 0
                    try:
                        with os.fdopen(fd, "wb") as fh:
                            while True:
                                chunk = resp.read(65536)
                                if not chunk:
                                    break
                                written += len(chunk)
                                if written > xfer.MAX_FILE_SIZE:
                                    raise xfer.TransferError("file_too_large", "媒体超过 20MiB")
                                fh.write(chunk)
                            fh.flush()
                    except Exception:
                        try:
                            os.unlink(dest)
                        except OSError:
                            pass
                        raise
                return dest
            except xfer.TransferError:
                log.warning("pico media too large ref=%s", ref[:12])
                return None
            except Exception as exc:
                log.warning("pico media download failed ref=%s err=%s", ref[:12], redact(str(exc)))
                return None

        return await asyncio.to_thread(_fetch)

    async def _push_local_file_to_app(
        self, session_id: str, path: str, name: str, mime: str
    ) -> None:
        transfer_id = str(uuid.uuid4())
        await asyncio.to_thread(
            self._queue_outbound_file, transfer_id, session_id, path, name, mime
        )
        frames = await asyncio.to_thread(
            xfer.build_file_frames, session_id, path, name, mime, transfer_id
        )
        for frame in frames:
            # Avoid logging payload.data
            await self.send_to_app(frame)

    async def check_pico_reachable(self) -> bool:
        def _probe() -> bool:
            try:
                req = Request(pico_http_health_url(), method="GET")
                with urlopen(req, timeout=2) as resp:
                    return 200 <= getattr(resp, "status", 200) < 500
            except Exception:
                return False

        ok = await asyncio.to_thread(_probe)
        self.pico_reachable = ok
        return ok

    def request_reconnect(self) -> None:
        self.credential_invalid = False
        self._reconnect_event.set()
        if self.relay is not None:
            asyncio.create_task(self._force_close_relay())

    async def _force_close_relay(self) -> None:
        try:
            if self.relay is not None:
                await self.relay.close()
        except Exception:
            pass

    # ---- pairing ----

    async def claim_pairing(self, raw_code: Any) -> Tuple[int, Dict[str, Any]]:
        """Claims an APP-created invitation with this device's DEVICE_TOKEN.

        The six digit code is never logged and never persisted.
        """
        code = re.sub(r"\s+", "", raw_code if isinstance(raw_code, str) else "")
        if not PAIRING_CODE_RE.fullmatch(code):
            return 400, {"ok": False, "error": "invalid_or_expired_pairing_code"}
        if self.mode != MODE_INTERNAL:
            return 503, {"ok": False, "error": "internal_test_not_enabled"}
        token = self.device_token()
        if not token:
            return 409, {"ok": False, "error": "not_registered"}
        if self._claim_lock.locked():
            return 429, {"ok": False, "error": "rate_limited", "retry_after": 3}

        async with self._claim_lock:
            status, body, headers = await asyncio.to_thread(
                http_json,
                "POST",
                relay_http_base() + "/v1/pairing-invitations/claim",
                {"pairing_code": code},
                token,
                15.0,
            )
            del code

        error = body.get("error") if isinstance(body, dict) else None
        if status == 200:
            self.last_pairing_result = "claimed"
            self.last_pairing_at = now_iso()
            log.info("pairing claim succeeded")
            return 200, {"ok": True, "status": "claimed"}

        if status == 400:
            self.last_pairing_result = "invalid_or_expired_pairing_code"
        elif status == 409:
            self.last_pairing_result = "device_already_bound"
        elif status == 401:
            self.last_pairing_result = "unauthorized"
            self.credential_invalid = True
        elif status == 429:
            self.last_pairing_result = "rate_limited"
        elif status == 0:
            self.last_pairing_result = "relay_unreachable"
        else:
            self.last_pairing_result = "relay_error"
        self.last_pairing_at = now_iso()
        log.info("pairing claim failed result=%s", self.last_pairing_result)

        out: Dict[str, Any] = {"ok": False, "error": self.last_pairing_result}
        if status == 429:
            retry_after = 30
            for key, value in (headers or {}).items():
                if key.lower() == "retry-after":
                    try:
                        retry_after = max(1, min(3600, int(str(value).strip())))
                    except ValueError:
                        pass
            out["retry_after"] = retry_after
            return 429, out
        if status == 0:
            return 503, out
        if status in (400, 401, 409):
            return status, out
        _ = error
        return 502, out

    # ---- status API payloads ----

    def status_payload(self) -> Dict[str, Any]:
        device_id = self.device_id()
        return {
            "status": "ok",
            "mode": self.mode,
            "registered": self.registered,
            "relay_connected": bool(self.relay_connected),
            "relay_connecting": bool(self.relay_connecting and not self.relay_connected),
            "pico_reachable": bool(self.pico_reachable),
            "device_id": device_id,
            "device_id_short": short_device_id(device_id),
            "relay_host": relay_host(),
            "reconnect_attempt": int(self.reconnect_attempt),
            "uptime_seconds": int(max(0, time.time() - self.started_at)),
            "last_connected_at": self.last_connected_at,
            "last_error": self.last_error,
            "config_error": self.config_error,
            "credential_invalid": bool(self.credential_invalid),
            "last_pairing_result": self.last_pairing_result,
            "last_pairing_at": self.last_pairing_at,
            "auto_reconnect": True,
            "autostart": True,
            "active_sessions": len(self.sessions),
            "active_transfers": self.transfers.active_count(),
            "agent_version": AGENT_VERSION,
        }

    def health_payload(self) -> Dict[str, Any]:
        st = self.status_payload()
        return {
            "status": "ok",
            "service": "tsingpaws-agent",
            "mode": st["mode"],
            "registered": st["registered"],
            "relay_connected": st["relay_connected"],
            "pico_reachable": st["pico_reachable"],
            "device_id_short": st["device_id_short"],
            "last_connected_at": st["last_connected_at"],
            "last_error": st["last_error"],
        }

    # ---- relay loop ----

    def _relay_target(self) -> Tuple[Optional[str], Dict[str, str]]:
        if self.mode == MODE_INTERNAL:
            token = self.device_token()
            if not token:
                return None, {}
            return relay_ws_url(), {"Authorization": f"Bearer {token}"}
        if not relay_url() or len(relay_token()) < 32:
            return None, {}
        return legacy_relay_connect_url(), {"Authorization": f"Bearer {relay_token()}"}

    @staticmethod
    def _is_auth_failure(exc: BaseException) -> bool:
        code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if code in (401, 403):
            return True
        text = str(exc)
        return "HTTP 401" in text or "HTTP 403" in text

    async def run_relay_forever(self) -> None:
        backoff = MIN_BACKOFF
        while not self._stop.is_set():
            self.mode = read_mode()
            if self.mode == MODE_INTERNAL and self.identity is None:
                self.identity = load_identity()
            self.check_config()

            url, headers = self._relay_target()
            if url is None:
                self.last_error = self.config_error or "Relay 未配置"
                log.warning("relay not configured for mode=%s; waiting", self.mode)
                await self._sleep_or_stop(15)
                continue
            if self.credential_invalid:
                log.warning("device credential rejected; long backoff before retry")
                await self._sleep_or_stop(AUTH_BACKOFF)
                self.credential_invalid = False
                continue

            self.relay_connecting = True
            self.reconnect_attempt += 1
            try:
                log.info("relay connecting mode=%s device=%s", self.mode, short_device_id(self.device_id()))
                async with ws_connect(
                    url,
                    extra_headers=headers,
                    ping_interval=25,
                    ping_timeout=45,
                    max_size=MAX_MESSAGE_SIZE,
                    open_timeout=20,
                ) as ws:
                    self.relay = ws
                    self.relay_connected = True
                    self.relay_connecting = False
                    self.reconnect_attempt = 0
                    self.last_connected_at = now_iso()
                    self.last_error = None
                    self.credential_invalid = False
                    backoff = MIN_BACKOFF
                    log.info("relay connected mode=%s", self.mode)
                    await self.check_pico_reachable()
                    async for message in ws:
                        if self._reconnect_event.is_set():
                            self._reconnect_event.clear()
                            break
                        await self.handle_relay_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._is_auth_failure(exc):
                    self.credential_invalid = True
                    self.last_error = "设备凭证失效"
                    log.warning("relay rejected our credential; not re-registering")
                else:
                    self.last_error = sanitize_error(exc)
                    log.warning("relay disconnected err=%s; retry in %.0fs", redact(str(exc)), backoff)
            finally:
                self.relay = None
                self.relay_connected = False
                self.relay_connecting = False
                # Relay disconnect must not leave permanent busy / cancel leftovers.
                for sid in list(self.sessions.keys()):
                    try:
                        await self.sessions[sid].close()
                    except Exception:
                        pass
                self.sessions.clear()
                self.ack_capable_sessions.clear()
                self.clear_all_session_state()
            if self._stop.is_set():
                break
            self._reconnect_event.clear()
            await self._sleep_or_stop(AUTH_BACKOFF if self.credential_invalid else backoff)
            if self.credential_invalid:
                self.credential_invalid = False
                backoff = MIN_BACKOFF
            elif self._reconnect_event.is_set():
                self._reconnect_event.clear()
                backoff = MIN_BACKOFF
            else:
                backoff = min(MAX_BACKOFF, max(MIN_BACKOFF, backoff * 2))

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def pico_watchdog(self) -> None:
        while not self._stop.is_set():
            try:
                await self.check_pico_reachable()
            except Exception:
                pass
            await self._sleep_or_stop(15)

    async def close_all(self) -> None:
        self._stop.set()
        for sess in list(self.sessions.values()):
            await sess.close()
        self.sessions.clear()
        self.clear_all_session_state()


# --------------------------------------------------------------------------- #
# local management API (127.0.0.1 only)
# --------------------------------------------------------------------------- #


class StatusServer:
    """Minimal HTTP API bound to 127.0.0.1 (stdlib only). No shell endpoints."""

    MAX_BODY = 4096

    def __init__(self, agent: Agent):
        self.agent = agent
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, STATUS_HOST, status_port())
        log.info("status api listening on %s:%s", STATUS_HOST, status_port())

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        except Exception:
            await self._close(writer)
            return
        try:
            head = raw.decode("latin1", errors="ignore")
            request_line = head.split("\r\n", 1)[0]
            parts = request_line.split()
            method = parts[0].upper() if parts else "GET"
            path = (parts[1] if len(parts) > 1 else "/").split("?", 1)[0]

            length = 0
            for line in head.split("\r\n")[1:]:
                if line.lower().startswith("content-length:"):
                    try:
                        length = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        length = 0
            body_bytes = b""
            if 0 < length <= self.MAX_BODY:
                body_bytes = await asyncio.wait_for(reader.readexactly(length), timeout=5)
            elif length > self.MAX_BODY:
                await self._json(writer, 413, {"error": "payload_too_large"})
                await self._close(writer)
                return

            if method == "GET" and path == "/health":
                await self._json(writer, 200, self.agent.health_payload())
            elif method == "GET" and path == "/status":
                await self._json(writer, 200, self.agent.status_payload())
            elif method == "POST" and path == "/reconnect":
                self.agent.request_reconnect()
                await self._json(writer, 200, {"ok": True, "action": "reconnect"})
            elif method == "POST" and path == "/pairing/claim":
                try:
                    payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                except Exception:
                    payload = None
                if not isinstance(payload, dict):
                    await self._json(writer, 400, {"ok": False, "error": "invalid_or_expired_pairing_code"})
                else:
                    status, out = await self.agent.claim_pairing(payload.get("pairing_code"))
                    await self._json(writer, status, out)
            elif method == "POST" and path == "/push/file":
                try:
                    payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                except Exception:
                    payload = None
                if not isinstance(payload, dict):
                    await self._json(writer, 400, {"ok": False, "error": "invalid_payload"})
                else:
                    status, out = await self.agent.push_file_to_recent_session(payload.get("path"))
                    await self._json(writer, status, out)
            elif method == "POST" and path == "/push/message":
                try:
                    payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                except Exception:
                    payload = None
                if not isinstance(payload, dict):
                    await self._json(writer, 400, {"ok": False, "error": "invalid_payload"})
                else:
                    status, out = await self.agent.push_message_to_recent_session(payload.get("content"))
                    await self._json(writer, status, out)
            else:
                await self._json(writer, 404, {"error": "not_found"})
        except Exception as exc:
            log.warning("status api error: %s", redact(str(exc)))
            try:
                await self._json(writer, 500, {"error": "internal_error"})
            except Exception:
                pass
        finally:
            await self._close(writer)

    async def _close(self, writer: asyncio.StreamWriter) -> None:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    async def _json(self, writer: asyncio.StreamWriter, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        reason = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            409: "Conflict",
            413: "Payload Too Large",
            429: "Too Many Requests",
            500: "Internal Server Error",
            502: "Bad Gateway",
            503: "Service Unavailable",
        }.get(status, "OK")
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin1")
        writer.write(headers + body)
        await writer.drain()


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #


def cli_register() -> int:
    """`agent.py register`: one-shot registration used by the switch script."""
    ok, code, info = register_device()
    messages = {
        "registered": "注册成功",
        "already_registered": "本机已注册，跳过",
        "internal_test_not_enabled": "内部测试版尚未启用",
        "enrollment_missing": "缺少注册凭证文件",
        "enrollment_permissions": "注册凭证文件权限必须为 0600",
        "enrollment_invalid": "注册凭证文件内容无效",
        "enrollment_unreadable": "注册凭证文件不可读",
        "enrollment_unauthorized": "注册凭证被拒绝",
        "device_already_registered": "设备已注册但本机凭证缺失，请联系管理员恢复",
        "relay_unreachable": "无法连接 TsingPaws Relay",
        "register_failed": "注册失败",
        "register_bad_response": "注册响应异常",
        "identity_verify_failed": "设备身份写入校验失败",
        "identity_permissions": "设备身份文件权限异常",
    }
    print(json.dumps({"ok": ok, "code": code, "message": messages.get(code, code)}, ensure_ascii=False))
    if ok:
        short = short_device_id(info.get("device_id") or (load_identity() or {}).get("device_id"))
        if short:
            print(json.dumps({"device_id_short": short}, ensure_ascii=False))
        return 0
    return {"internal_test_not_enabled": 3, "device_already_registered": 4}.get(code, 1)


def cli_check_health() -> int:
    ok, body = relay_health()
    print(
        json.dumps(
            {
                "ok": ok,
                "version": body.get("version"),
                "pairing_flow": body.get("pairing_flow"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if ok else 3


async def amain() -> None:
    agent = Agent()
    agent.check_config()
    if agent.config_error:
        log.warning("configuration incomplete: %s", agent.config_error)
    status = StatusServer(agent)
    loop = asyncio.get_running_loop()
    stop_future: asyncio.Future = loop.create_future()

    def _stop() -> None:
        if not stop_future.done():
            stop_future.set_result(True)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    await status.start()
    relay_task = asyncio.create_task(agent.run_relay_forever(), name="relay-loop")
    watchdog_task = asyncio.create_task(agent.pico_watchdog(), name="pico-watchdog")
    await stop_future
    log.info("shutdown requested")
    await agent.close_all()
    await status.stop()
    for task in (relay_task, watchdog_task):
        task.cancel()
        try:
            await task
        except Exception:
            pass


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "register":
        return cli_register()
    if args and args[0] == "check-health":
        return cli_check_health()
    if args and args[0] == "mode":
        print(read_mode())
        return 0
    asyncio.run(amain())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
