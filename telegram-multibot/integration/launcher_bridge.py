#!/usr/bin/env python3
"""Reverse proxy in front of picoclaw-launcher with TsingPaws cloud channel UI/API.

Security constraints (internal-test hardening):

- Management APIs require an authenticated Launcher session cookie.
- State-changing POSTs require a same-origin CSRF check (Origin/Referer).
- Request bodies are size-capped.
- Upstream and agent targets are fixed to loopback; env overrides that leave
  127.0.0.1 / localhost are rejected at startup.
- No shell command interface is exposed beyond the fixed restart init script.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import zipfile
import hashlib
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

# Fixed loopback targets. Env may change the port, never the host.
_DEFAULT_UPSTREAM = "http://127.0.0.1:18880"
_DEFAULT_AGENT = "http://127.0.0.1:18791"
_DEFAULT_PICO_UPSTREAM = "http://127.0.0.1:18790"
_DEFAULT_MULTIBOT_UPSTREAM = "http://127.0.0.1:18792"
_DEFAULT_SKILL_LIBRARY_UPSTREAM = "http://127.0.0.1:8080"
LISTEN_HOST = os.environ.get("BRIDGE_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("BRIDGE_LISTEN_PORT", "18800"))
STATIC_DIR = os.environ.get("BRIDGE_STATIC_DIR", "/opt/tsingpaws-agent/static")
RESTART_COOLDOWN = int(os.environ.get("RESTART_COOLDOWN_SEC", "15"))
CLAIM_COOLDOWN = float(os.environ.get("CLAIM_COOLDOWN_SEC", "2"))
MAX_JSON_BODY = 2048
MAX_MULTIBOT_BODY = 64 * 1024
MAX_PROXY_BODY = 2 * 1024 * 1024
MAX_SKILL_PACKAGE_BYTES = 50 * 1024 * 1024
MAX_SKILL_FILES = 2000
SKILLS_DIR = os.environ.get("SKILLS_DIR", "/opt/tsingpaw/data/workspace/skills")
ALLOWED_LOOPBACK = {"127.0.0.1", "localhost", "::1"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tsingpaws-bridge")

_restart_lock = threading.Lock()
_last_restart_at = 0.0
_claim_lock = threading.Lock()
_claim_inflight = False
_last_claim_at = 0.0
_bridge_sessions: Dict[str, Tuple[str, float]] = {}
_BRIDGE_COOKIE = "tp_bridge_auth"
_BRIDGE_SESSION_TTL = 1800.0

PAIRING_MESSAGES = {
    "claimed": "绑定成功，这台 TsingPaws 已添加到 APP",
    "invalid_or_expired_pairing_code": "绑定码无效或已过期，请在 APP 中重新生成",
    "device_already_bound": "这台设备已绑定其他账号，请先在原账号中解绑",
    "unauthorized": "设备凭证失效，请联系管理员",
    "not_registered": "本机尚未完成内部测试版注册，请联系管理员",
    "internal_test_not_enabled": "公网内部测试版尚未启用，请稍后再试",
    "relay_unreachable": "无法连接 TsingPaws Relay，请检查网络后重试",
    "relay_error": "TsingPaws Relay 返回异常，请稍后再试",
    "agent_unreachable": "本机云连接服务未运行，请先重启云连接服务",
    "busy": "正在提交，请稍候",
    "csrf_rejected": "请求来源不被允许，请刷新页面后重试",
    "payload_too_large": "请求过大，请重试",
}


def pairing_message(code: str, retry_after: int = 0, account_hint: str = "") -> str:
    if code == "rate_limited":
        return f"尝试次数过多，请在 {max(1, int(retry_after or 30))} 秒后重试"
    if code == "device_already_bound" and account_hint:
        return f"这台 TsingPaws 已绑定，请使用 {account_hint} 账号在原 APP 中解除绑定"
    return PAIRING_MESSAGES.get(code, "绑定失败，请稍后再试")


def assert_loopback_url(name: str, value: str) -> str:
    """Reject any non-loopback management target so the bridge cannot be pointed off-box."""
    raw = (value or "").strip().rstrip("/")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise SystemExit(f"{name} must be http(s), got scheme={parts.scheme!r}")
    host = (parts.hostname or "").lower()
    if host not in ALLOWED_LOOPBACK:
        raise SystemExit(f"{name} must target loopback, got host={host!r}")
    if parts.username or parts.password:
        raise SystemExit(f"{name} must not embed credentials")
    return raw


UPSTREAM = assert_loopback_url("LAUNCHER_UPSTREAM", os.environ.get("LAUNCHER_UPSTREAM", _DEFAULT_UPSTREAM))
AGENT_STATUS = assert_loopback_url("AGENT_STATUS_BASE", os.environ.get("AGENT_STATUS_BASE", _DEFAULT_AGENT))
PICO_UPSTREAM = assert_loopback_url(
    "PICO_HTTP_UPSTREAM",
    os.environ.get("PICO_HTTP_UPSTREAM", _DEFAULT_PICO_UPSTREAM),
)
MULTIBOT_UPSTREAM = assert_loopback_url(
    "TELEGRAM_MULTIBOT_UPSTREAM",
    os.environ.get("TELEGRAM_MULTIBOT_UPSTREAM", _DEFAULT_MULTIBOT_UPSTREAM),
)
SKILL_LIBRARY_UPSTREAM = os.environ.get(
    "SKILL_LIBRARY_UPSTREAM", _DEFAULT_SKILL_LIBRARY_UPSTREAM
).strip().rstrip("/")

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}

CLOUD_CHANNEL = {
    "name": "tsingpaws_cloud",
    "config_key": "tsingpaws_cloud",
    "has_local_doc": False,
    "display_name": "TsingPaws",
}


def inject_cloud_channel_catalog(raw: bytes) -> bytes:
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return raw
    channels = data.get("channels")
    if not isinstance(channels, list):
        return raw
    channels = [c for c in channels if not (isinstance(c, dict) and c.get("name") == CLOUD_CHANNEL["name"])]
    channels.insert(0, dict(CLOUD_CHANNEL))
    data["channels"] = channels
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def inject_cloud_channel_config(raw: bytes) -> bytes:
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return raw
    if not isinstance(data, dict):
        return raw
    channels = data.get("channels")
    if not isinstance(channels, dict):
        channels = {}
        data["channels"] = channels
    entry = channels.get("tsingpaws_cloud")
    if not isinstance(entry, dict):
        entry = {}
    entry = dict(entry)
    entry["enabled"] = True
    channels["tsingpaws_cloud"] = entry
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def rewrite_public_urls(raw: bytes, host_header: str) -> bytes:
    """Rewrite loopback/private launcher URLs so the browser stays on bridge origin."""
    try:
        text = raw.decode("utf-8")
    except Exception:
        return raw
    host = (host_header or "").strip()
    if not host:
        return raw
    bridge_http = f"http://{host}"
    bridge_ws = f"ws://{host}"
    candidates = {
        _DEFAULT_UPSTREAM: bridge_http,
        UPSTREAM: bridge_http,
        "http://127.0.0.1:18880": bridge_http,
        "http://localhost:18880": bridge_http,
        "ws://127.0.0.1:18880": bridge_ws,
        "ws://localhost:18880": bridge_ws,
    }
    bare_host = host.split(":", 1)[0]
    candidates[f"http://{bare_host}:18880"] = bridge_http
    candidates[f"ws://{bare_host}:18880"] = bridge_ws
    for src, dst in candidates.items():
        text = text.replace(src, dst)
    return text.encode("utf-8")


def rewrite_upstream_browser_headers(headers: Dict[str, str], upstream) -> Dict[str, str]:
    """Align browser security headers with the loopback Launcher origin.

    A remote browser legitimately sends Origin/Referer for DEVICE_IP:18800.
    The bridge connects to 127.0.0.1:18880, so forwarding those values
    unchanged makes Launcher versions with origin checks reject login as CSRF.
    """
    port = upstream.port or (443 if upstream.scheme == "https" else 80)
    default_port = (upstream.scheme == "http" and port == 80) or (
        upstream.scheme == "https" and port == 443
    )
    authority = upstream.hostname if default_port else f"{upstream.hostname}:{port}"
    origin = f"{upstream.scheme}://{authority}"
    rewritten = dict(headers)
    for key in list(rewritten):
        lower = key.lower()
        if lower == "origin":
            rewritten[key] = origin
        elif lower == "referer":
            try:
                source = urlsplit(rewritten[key])
                suffix = source.path or "/"
                if source.query:
                    suffix += "?" + source.query
            except Exception:
                suffix = "/"
            rewritten[key] = origin + suffix
    return rewritten


def redact(text: str) -> str:
    out = text or ""
    out = re.sub(r"(?i)(token|authorization|bearer|password|secret)=([^&\s]+)", r"\1=***", out)
    out = re.sub(r"(?i)Bearer\s+\S+", "Bearer ***", out)
    return out


def agent_get(path: str) -> Tuple[int, Dict]:
    if not path.startswith("/"):
        return 500, {"error": "bad_path"}
    try:
        with urlopen(AGENT_STATUS + path, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return getattr(resp, "status", 200), data if isinstance(data, dict) else {}
    except Exception as exc:
        return 503, {"error": "agent_unreachable", "detail": redact(str(exc))[:160]}


def agent_process_running() -> bool:
    try:
        out = subprocess.check_output(["pgrep", "-f", "/opt/tsingpaws-agent/agent.py"], text=True)
        return bool(out.strip())
    except Exception:
        return False


def _clean_bridge_sessions() -> None:
    cutoff = time.time()
    stale = [k for k, (_, expires_at) in _bridge_sessions.items() if expires_at <= cutoff]
    for k in stale:
        _bridge_sessions.pop(k, None)


def cache_bridge_session(cookie_header: str) -> Optional[str]:
    if not cookie_header:
        return None
    _clean_bridge_sessions()
    sid = secrets.token_urlsafe(18)
    _bridge_sessions[sid] = (cookie_header, time.time() + _BRIDGE_SESSION_TTL)
    log.info("cached bridge session for websocket handoff")
    return sid


def upstream_cookie_for_request(cookie_header: str) -> str:
    raw = cookie_header or ""
    if raw and check_launcher_auth(raw):
        log.info("launcher cookie accepted for direct upstream access")
        return raw
    if not raw:
        return ""
    try:
        jar = SimpleCookie()
        jar.load(raw)
    except Exception:
        return ""
    morsel = jar.get(_BRIDGE_COOKIE)
    if morsel is None:
        return ""
    _clean_bridge_sessions()
    entry = _bridge_sessions.get(morsel.value)
    if not entry:
        log.info("bridge session cookie not found for websocket handoff")
        return ""
    upstream_cookie, expires_at = entry
    if expires_at <= time.time():
        _bridge_sessions.pop(morsel.value, None)
        log.info("bridge session cookie expired before websocket handoff")
        return ""
    log.info("bridge session cookie restored launcher auth for websocket handoff")
    return upstream_cookie


def pico_websocket_token() -> str:
    """Read the effective Pico token without exposing it to the browser or logs."""
    token = (os.environ.get("PICO_TOKEN") or "").strip()
    path = (
        os.environ.get("PICO_SECURITY_FILE")
        or "/opt/tsingpaw/data/.security.yml"
    ).strip()
    if not token and path:
        try:
            lines = open(path, "r", encoding="utf-8").read().splitlines()
        except OSError:
            lines = []
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
                if candidate and not candidate.startswith("enc://"):
                    token = candidate
                break
    return token


def pico_websocket_subprotocol() -> str:
    """Build the browser protocol value from Pico's effective token."""
    token = pico_websocket_token()
    if not token:
        return ""
    encoded = base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii").rstrip("=")
    return "token.b64." + encoded


def ui_state(agent_running: bool, payload: Dict) -> Dict:
    if not agent_running:
        return {
            "ui_status": "offline",
            "ui_label": "离线",
            "ui_color": "gray",
            "ui_detail": "云连接服务未运行",
        }
    if payload.get("config_error"):
        return {
            "ui_status": "config_error",
            "ui_label": "配置错误",
            "ui_color": "red",
            "ui_detail": str(payload.get("config_error")),
        }
    last_error = (payload.get("last_error") or "").lower()
    if any(x in last_error for x in ("401", "403", "unauthorized", "forbidden", "invalid token")):
        return {
            "ui_status": "config_error",
            "ui_label": "配置错误",
            "ui_color": "red",
            "ui_detail": "认证失败，请检查配置",
        }
    relay_ok = bool(payload.get("relay_connected"))
    pico_ok = bool(payload.get("pico_reachable"))
    if relay_ok and pico_ok:
        return {
            "ui_status": "connected",
            "ui_label": "已连接",
            "ui_color": "green",
            "ui_detail": "Relay 与 PicoClaw 均可用",
        }
    if relay_ok and not pico_ok:
        return {
            "ui_status": "pico_down",
            "ui_label": "PicoClaw 不可用",
            "ui_color": "red",
            "ui_detail": "已连接 Relay，但本机 PicoClaw 不可达",
        }
    if payload.get("relay_connecting") or not relay_ok:
        return {
            "ui_status": "connecting",
            "ui_label": "正在连接",
            "ui_color": "orange",
            "ui_detail": "正在重连公网 Relay",
        }
    return {
        "ui_status": "offline",
        "ui_label": "离线",
        "ui_color": "gray",
        "ui_detail": "未连接",
    }


def build_status() -> Dict:
    running = agent_process_running()
    code, payload = agent_get("/status") if running else (503, {})
    if code >= 400:
        payload = {
            "mode": "single_node",
            "registered": False,
            "relay_connected": False,
            "relay_connecting": False,
            "pico_reachable": False,
            "device_id": os.environ.get("DEVICE_ID", "home-001"),
            "device_id_short": None,
            "relay_host": "",
            "reconnect_attempt": 0,
            "uptime_seconds": 0,
            "last_connected_at": None,
            "last_error": payload.get("detail") if isinstance(payload, dict) else None,
            "config_error": None,
            "credential_invalid": False,
            "last_pairing_result": None,
            "last_pairing_at": None,
            "auto_reconnect": True,
            "autostart": True,
            "active_sessions": 0,
            "binding_known": False,
            "bound": False,
            "account_hint": "",
        }
    for key in list(payload.keys()):
        lk = key.lower()
        if "token" in lk or "authorization" in lk or "password" in lk or "secret" in lk:
            payload.pop(key, None)
    ui = ui_state(running, payload)
    mode = payload.get("mode") or "single_node"
    last_pairing = payload.get("last_pairing_result")
    if last_pairing == "claimed":
        pairing_label = "最近绑定成功"
    elif last_pairing:
        pairing_label = "最近绑定失败"
    else:
        pairing_label = "未进行绑定"
    binding_known = bool(payload.get("binding_known"))
    bound = bool(payload.get("bound"))
    pairing_enabled = (
        mode == "internal_test"
        and bool(payload.get("registered"))
        and binding_known
        and not bound
    )
    return {
        "agent_running": running,
        "service": "tsingpaws-agent",
        **payload,
        **ui,
        "mode_label": "内部测试版" if mode == "internal_test" else "单机版",
        "registered_label": "已注册" if payload.get("registered") else "未注册",
        "pairing_label": pairing_label,
        "pairing_hint": pairing_message(last_pairing) if last_pairing else "",
        "pairing_enabled": pairing_enabled,
        "pairing_disabled_reason": None
        if pairing_enabled
        else (
            pairing_message("device_already_bound", account_hint=str(payload.get("account_hint") or ""))
            if bound
            else "正在确认 TsingPaws 的绑定状态"
            if mode == "internal_test" and payload.get("registered") and not binding_known
            else
            "当前仍为单机版，绑定码输入将在切换内部测试版后可用"
            if mode != "internal_test"
            else "设备尚未完成内部测试版注册"
        ),
    }


def check_launcher_auth(cookie: str) -> bool:
    if not cookie:
        return False
    try:
        req = Request(
            UPSTREAM + "/api/auth/status",
            headers={"Cookie": cookie, "Accept": "application/json"},
            method="GET",
        )
        with urlopen(req, timeout=3) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            if isinstance(data, dict):
                if data.get("authenticated") is True or data.get("status") == "ok":
                    return True
                if data.get("error"):
                    return False
            return 200 <= getattr(resp, "status", 200) < 300
    except HTTPError as exc:
        return exc.code not in (401, 403)
    except Exception:
        return False


def same_origin_ok(origin_or_referer: str, host_header: str) -> bool:
    """Accept only browser requests whose Origin/Referer host matches this bridge."""
    if not origin_or_referer or not host_header:
        return False
    try:
        parts = urlsplit(origin_or_referer)
    except Exception:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    req_host = (parts.netloc or "").lower()
    expect = (host_header or "").lower()
    if not req_host or not expect:
        return False
    # Compare host[:port] exactly; strip default ports for equality.
    def norm(h: str) -> str:
        if h.endswith(":80") and parts.scheme == "http":
            return h[:-3]
        if h.endswith(":443") and parts.scheme == "https":
            return h[:-4]
        return h

    return norm(req_host) == norm(expect)


class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.address_string(), redact(fmt % args))

    def _cookie(self) -> str:
        return self.headers.get("Cookie", "")

    def _require_auth(self) -> bool:
        if check_launcher_auth(self._cookie()):
            return True
        self.close_connection = True
        self._send_json(401, {"error": "unauthorized"})
        return False

    def _require_csrf(self) -> bool:
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin", "")
        referer = self.headers.get("Referer", "")
        if origin and same_origin_ok(origin, host):
            return True
        if referer and same_origin_ok(referer, host):
            return True
        self.close_connection = True
        self._send_json(
            403,
            {"ok": False, "error": "csrf_rejected", "message": pairing_message("csrf_rejected")},
        )
        return False

    def _send_json(self, status: int, payload: Dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self, limit: int) -> Optional[bytes]:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = -1
        if length < 0:
            self._send_json(400, {"ok": False, "error": "invalid_json"})
            return None
        if length > limit:
            self._send_json(
                413,
                {"ok": False, "error": "payload_too_large", "message": pairing_message("payload_too_large")},
            )
            return None
        if length == 0:
            return b""
        try:
            return self.rfile.read(length)
        except Exception:
            self._send_json(400, {"ok": False, "error": "invalid_json"})
            return None

    def _serve_static(self, rel: str, content_type: str) -> None:
        base = os.path.abspath(STATIC_DIR)
        path = os.path.normpath(os.path.join(base, rel))
        if not path.startswith(base + os.sep) or not os.path.isfile(path):
            self.send_error(404)
            return
        data = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/telegram-bots" or path.startswith("/api/telegram-bots/"):
            if not self._require_auth():
                return
            self._proxy_multibot()
            return
        if path.startswith("/api/skill-library/"):
            if not self._require_auth():
                return
            self._proxy_skill_library()
            return
        if path == "/api/tsingpaws/status":
            if not self._require_auth():
                return
            self._send_json(200, build_status())
            return
        if path == "/tsingpaws-cloud/cloud-channel.js":
            self._serve_static("cloud-channel.js", "application/javascript; charset=utf-8")
            return
        if path == "/tsingpaws-cloud/cloud-channel.css":
            self._serve_static("cloud-channel.css", "text/css; charset=utf-8")
            return
        if path == "/tsingpaws-cloud/skill-library.js":
            self._serve_static("skill-library.js", "application/javascript; charset=utf-8")
            return
        if path == "/tsingpaws-cloud/skill-library.css":
            self._serve_static("skill-library.css", "text/css; charset=utf-8")
            return
        if path == "/tsingpaws-cloud/telegram-multibot.js":
            self._serve_static("telegram-multibot.js", "application/javascript; charset=utf-8")
            return
        if path == "/tsingpaws-cloud/telegram-multibot.css":
            self._serve_static("telegram-multibot.css", "text/css; charset=utf-8")
            return
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/telegram-bots" or path.startswith("/api/telegram-bots/"):
            if not self._require_auth() or not self._require_csrf():
                return
            self._proxy_multibot()
            return
        if path == "/api/skill-library/install":
            if not self._require_auth() or not self._require_csrf():
                return
            self._install_library_skill()
            return
        if path.startswith("/api/skill-library/"):
            if not self._require_auth() or not self._require_csrf():
                return
            self._proxy_skill_library()
            return
        if path == "/api/tsingpaws/reconnect":
            if not self._require_auth() or not self._require_csrf():
                return
            body = self._read_body(MAX_JSON_BODY)
            if body is None:
                return
            try:
                req = Request(AGENT_STATUS + "/reconnect", data=b"{}", method="POST")
                req.add_header("Content-Type", "application/json")
                with urlopen(req, timeout=3) as resp:
                    raw = resp.read().decode("utf-8")
                    payload = json.loads(raw) if raw else {"ok": True}
                    self._send_json(200, payload)
            except Exception as exc:
                self._send_json(503, {"ok": False, "error": redact(str(exc))[:160]})
            return
        if path == "/api/tsingpaws/pairing/claim":
            if not self._require_auth() or not self._require_csrf():
                return
            self._handle_pairing_claim()
            return
        if path == "/api/tsingpaws/restart":
            if not self._require_auth() or not self._require_csrf():
                return
            body = self._read_body(MAX_JSON_BODY)
            if body is None:
                return
            global _last_restart_at
            with _restart_lock:
                now = time.time()
                if now - _last_restart_at < RESTART_COOLDOWN:
                    self._send_json(429, {"ok": False, "error": "too_many_requests", "retry_after": RESTART_COOLDOWN})
                    return
                _last_restart_at = now
            try:
                # Fixed argv only — never interpolate user input into a shell.
                subprocess.check_call(["/etc/init.d/tsingpaws-agent", "restart"], timeout=30)
                self._send_json(200, {"ok": True, "action": "restart"})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": redact(str(exc))[:160]})
            return
        self._proxy()

    def _install_library_skill(self) -> None:
        raw = self._read_body(MAX_JSON_BODY)
        if raw is None:
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"ok": False, "error": "invalid_json"})
            return
        slug = str(payload.get("slug", "")).strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", slug):
            self._send_json(400, {"ok": False, "error": "invalid_slug"})
            return
        temp_root = tempfile.mkdtemp(prefix="skill-install-", dir="/tmp")
        archive = os.path.join(temp_root, "package.zip")
        try:
            with urlopen(f"{SKILL_LIBRARY_UPSTREAM}/api/v1/skills/{slug}", timeout=15) as resp:
                detail = json.loads(resp.read(MAX_JSON_BODY).decode("utf-8"))
            version = detail.get("latestVersion") or {}
            expected = str(version.get("sha256", "")).lower()
            download_url = f"{SKILL_LIBRARY_UPSTREAM}/api/v1/download?slug={slug}&version=latest"
            digest = hashlib.sha256()
            size = 0
            with urlopen(download_url, timeout=60) as resp, open(archive, "wb") as out:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_SKILL_PACKAGE_BYTES:
                        raise ValueError("package_too_large")
                    digest.update(chunk)
                    out.write(chunk)
            if expected and digest.hexdigest().lower() != expected:
                raise ValueError("checksum_mismatch")
            unpacked = os.path.join(temp_root, "unpacked")
            os.makedirs(unpacked)
            with zipfile.ZipFile(archive) as bundle:
                members = bundle.infolist()
                if len(members) > MAX_SKILL_FILES:
                    raise ValueError("too_many_files")
                for member in members:
                    name = member.filename.replace("\\", "/")
                    if name.startswith("/") or ".." in name.split("/") or (((member.external_attr >> 16) & 0o170000) == 0o120000):
                        raise ValueError("unsafe_archive")
                bundle.extractall(unpacked)
            roots = [unpacked]
            entries = [x for x in os.listdir(unpacked) if x not in {"__MACOSX"}]
            if len(entries) == 1 and os.path.isdir(os.path.join(unpacked, entries[0])):
                roots.insert(0, os.path.join(unpacked, entries[0]))
            source = next((root for root in roots if os.path.isfile(os.path.join(root, "SKILL.md"))), None)
            if not source:
                matches = []
                for base, dirs, files in os.walk(unpacked):
                    if "SKILL.md" in files:
                        matches.append(base)
                    if len(os.path.relpath(base, unpacked).split(os.sep)) >= 3:
                        dirs[:] = []
                if len(matches) == 1:
                    source = matches[0]
            if not source:
                raise ValueError("missing_skill_md")
            os.makedirs(SKILLS_DIR, mode=0o755, exist_ok=True)
            target = os.path.join(SKILLS_DIR, slug)
            staging = os.path.join(SKILLS_DIR, f".{slug}.installing")
            backup = os.path.join(SKILLS_DIR, f".{slug}.backup-{int(time.time())}")
            if os.path.exists(staging):
                shutil.rmtree(staging)
            shutil.copytree(source, staging)
            replaced = os.path.exists(target)
            if replaced:
                os.replace(target, backup)
            try:
                os.replace(staging, target)
            except Exception:
                if replaced and os.path.exists(backup):
                    os.replace(backup, target)
                raise
            self._send_json(200, {"ok": True, "slug": slug, "version": version.get("version", "latest"), "replaced": replaced})
        except HTTPError as exc:
            self._send_json(exc.code if exc.code < 500 else 502, {"ok": False, "error": "library_error"})
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            self._send_json(400, {"ok": False, "error": str(exc)[:80]})
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def _proxy_skill_library(self) -> None:
        upstream = urlsplit(SKILL_LIBRARY_UPSTREAM)
        if upstream.scheme != "http" or not upstream.hostname:
            self.send_error(500, "Misconfigured skill library")
            return
        parsed = urlsplit(self.path)
        target_path = parsed.path.removeprefix("/api/skill-library") or "/"
        if parsed.query:
            target_path += "?" + parsed.query
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self.send_error(400, "Bad Request")
            return
        if length > MAX_PROXY_BODY:
            self.send_error(413, "Payload Too Large")
            return
        body = self.rfile.read(length) if length else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP and key.lower() not in {"cookie", "origin", "referer"}
        }
        headers["Host"] = f"{upstream.hostname}:{upstream.port or 80}"
        headers["Connection"] = "close"
        conn = HTTPConnection(upstream.hostname, upstream.port or 80, timeout=60)
        try:
            conn.request(self.command, target_path, body=body, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status)
            for key, value in resp.getheaders():
                if key.lower() not in {"transfer-encoding", "connection"}:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except Exception as exc:
            log.warning("skill library proxy error: %s", redact(str(exc)))
            try:
                self.send_error(502, "Skill library unavailable")
            except Exception:
                pass
        finally:
            conn.close()

    def _handle_pairing_claim(self) -> None:
        """Forwards a six digit code to the local agent. The code is never logged."""
        global _claim_inflight, _last_claim_at
        raw = self._read_body(MAX_JSON_BODY)
        if raw is None:
            return
        if not raw:
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": "invalid_or_expired_pairing_code",
                    "message": pairing_message("invalid_or_expired_pairing_code"),
                },
            )
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = None
        code = ""
        if isinstance(payload, dict) and isinstance(payload.get("pairing_code"), str):
            code = re.sub(r"\D", "", payload["pairing_code"])
        if len(code) != 6:
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": "invalid_or_expired_pairing_code",
                    "message": pairing_message("invalid_or_expired_pairing_code"),
                },
            )
            return

        # Refuse claim attempts while the agent is still on the single-node protocol.
        st = build_status()
        if st.get("bound"):
            self._send_json(
                409,
                {
                    "ok": False,
                    "error": "device_already_bound",
                    "message": pairing_message(
                        "device_already_bound",
                        account_hint=str(st.get("account_hint") or ""),
                    ),
                },
            )
            return
        if not st.get("pairing_enabled"):
            self._send_json(
                503,
                {
                    "ok": False,
                    "error": "internal_test_not_enabled",
                    "message": st.get("pairing_disabled_reason") or pairing_message("internal_test_not_enabled"),
                },
            )
            return

        with _claim_lock:
            now = time.time()
            if _claim_inflight or now - _last_claim_at < CLAIM_COOLDOWN:
                self._send_json(
                    429,
                    {
                        "ok": False,
                        "error": "busy",
                        "message": pairing_message("busy"),
                        "retry_after": max(1, int(CLAIM_COOLDOWN)),
                    },
                )
                return
            _claim_inflight = True
            _last_claim_at = now
        try:
            req = Request(
                AGENT_STATUS + "/pairing/claim",
                data=json.dumps({"pairing_code": code}).encode("utf-8"),
                method="POST",
            )
            req.add_header("Content-Type", "application/json")
            try:
                with urlopen(req, timeout=25) as resp:
                    status = getattr(resp, "status", 200)
                    body = json.loads(resp.read().decode("utf-8") or "{}")
            except HTTPError as exc:
                status = exc.code
                try:
                    body = json.loads(exc.read().decode("utf-8") or "{}")
                except Exception:
                    body = {}
            except (URLError, OSError):
                self._send_json(
                    503,
                    {"ok": False, "error": "agent_unreachable", "message": pairing_message("agent_unreachable")},
                )
                return
            if not isinstance(body, dict):
                body = {}
            if status == 200 and body.get("ok"):
                out = {"ok": True, "status": "claimed", "message": pairing_message("claimed")}
                log.info("pairing claim accepted")
                self._send_json(200, out)
                return
            error = str(body.get("error") or "relay_error")
            retry_after = body.get("retry_after") or 0
            account_hint = str(body.get("account_hint") or "")
            out = {
                "ok": False,
                "error": error,
                "message": pairing_message(error, retry_after, account_hint),
            }
            if retry_after:
                out["retry_after"] = retry_after
            log.info("pairing claim rejected result=%s", error)
            self._send_json(status if status in (400, 401, 409, 429, 503) else 502, out)
        finally:
            del code
            with _claim_lock:
                _claim_inflight = False

    def do_PUT(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path.startswith("/api/telegram-bots/"):
            if not self._require_auth() or not self._require_csrf():
                return
            self._proxy_multibot()
            return
        self._proxy()

    def do_PATCH(self) -> None:  # noqa: N802
        self._proxy()

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path.startswith("/api/telegram-bots/"):
            if not self._require_auth() or not self._require_csrf():
                return
            self._proxy_multibot()
            return
        self._proxy()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._proxy()

    def do_HEAD(self) -> None:  # noqa: N802
        self._proxy()

    def _is_websocket_upgrade(self) -> bool:
        return (
            self.command == "GET"
            and self.headers.get("Upgrade", "").lower() == "websocket"
            and "upgrade" in self.headers.get("Connection", "").lower()
        )

    def _pipe_socket(self, src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                chunk = src.recv(65536)
                if not chunk:
                    break
                dst.sendall(chunk)
        except Exception:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except Exception:
                pass

    def _proxy_websocket(self, upstream) -> None:
        # Keep the native Launcher as the authenticated Gateway proxy. The
        # private 1.26.4 build synchronizes its runtime Gateway credential here;
        # connecting straight to 18790 with a persisted token can be rejected.
        upstream = urlsplit(UPSTREAM)
        port = upstream.port or (443 if upstream.scheme == "https" else 80)
        req = [f"{self.command} {self.path} HTTP/1.1\r\n"]
        upstream_cookie = upstream_cookie_for_request(self.headers.get("Cookie", ""))
        local_token = pico_websocket_token() if upstream_cookie else ""
        local_subprotocol = pico_websocket_subprotocol() if local_token else ""
        log.info(
            "websocket proxy attempt path=%s incoming_cookie=%s upstream_cookie=%s subprotocol=%s synchronized=%s",
            self.path,
            bool(self.headers.get("Cookie")),
            bool(upstream_cookie),
            bool(self.headers.get("Sec-WebSocket-Protocol")),
            bool(local_subprotocol),
        )
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in HOP_BY_HOP or lk in ("host", "cookie", "authorization", "origin"):
                continue
            if lk == "sec-websocket-protocol" and local_subprotocol:
                req.append(f"{k}: {local_subprotocol}\r\n")
                continue
            req.append(f"{k}: {v}\r\n")
        req.append(f"Host: {upstream.hostname}:{port}\r\n")
        req.append("Connection: Upgrade\r\n")
        req.append("Upgrade: websocket\r\n")
        if upstream_cookie:
            req.append(f"Cookie: {upstream_cookie}\r\n")
            req.append(f"Origin: http://{upstream.hostname}:{port}\r\n")
        req.append("\r\n")
        upstream_sock: Optional[socket.socket] = None
        try:
            upstream_sock = socket.create_connection((upstream.hostname, port), timeout=10)
            upstream_sock.settimeout(None)
            self.connection.settimeout(None)
            upstream_sock.sendall("".join(req).encode("utf-8"))
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = upstream_sock.recv(4096)
                if not chunk:
                    break
                response += chunk
            status_line = response.split(b"\r\n", 1)[0].decode("utf-8", "replace") if response else "no response"
            log.info("websocket upstream handshake result=%s", status_line)
            if response:
                self.connection.sendall(response)
            if not response.startswith(b"HTTP/1.1 101") and not response.startswith(b"HTTP/1.0 101"):
                return
            self.close_connection = True
            t1 = threading.Thread(target=self._pipe_socket, args=(self.connection, upstream_sock), daemon=True)
            t2 = threading.Thread(target=self._pipe_socket, args=(upstream_sock, self.connection), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        except Exception as exc:
            log.warning("websocket proxy error: %s", redact(str(exc)))
            try:
                self.send_error(502, "Bad Gateway")
            except Exception:
                pass
        finally:
            if upstream_sock is not None:
                try:
                    upstream_sock.close()
                except Exception:
                    pass

    def _proxy_multibot(self) -> None:
        """Proxy the authenticated management API to the loopback-only adapter."""
        upstream = urlsplit(MULTIBOT_UPSTREAM)
        if (upstream.hostname or "").lower() not in ALLOWED_LOOPBACK:
            self._send_json(500, {"ok": False, "error": "misconfigured_multibot_upstream"})
            return
        body = self._read_body(MAX_MULTIBOT_BODY)
        if body is None:
            return
        conn = HTTPConnection(upstream.hostname, upstream.port or 80, timeout=20)
        headers = {
            "Accept": "application/json",
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "Connection": "close",
        }
        try:
            conn.request(self.command, self.path, body=body or None, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(MAX_MULTIBOT_BODY + 1)
            if len(raw) > MAX_MULTIBOT_BODY:
                self._send_json(502, {"ok": False, "error": "multibot_response_too_large"})
                return
            self.send_response(resp.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)
        except Exception as exc:
            log.warning("telegram multibot upstream unavailable: %s", redact(str(exc)))
            self._send_json(503, {"ok": False, "error": "telegram_multibot_unavailable"})
        finally:
            conn.close()

    def _proxy(self) -> None:
        upstream = urlsplit(UPSTREAM)
        if (upstream.hostname or "").lower() not in ALLOWED_LOOPBACK:
            self.send_error(500, "Misconfigured upstream")
            return
        if self._is_websocket_upgrade():
            self._proxy_websocket(upstream)
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self.send_error(400, "Bad Request")
            return
        if length > MAX_PROXY_BODY:
            self.send_error(413, "Payload Too Large")
            return
        body = self.rfile.read(length) if length > 0 else None
        conn = HTTPConnection(upstream.hostname, upstream.port or 80, timeout=60)
        headers = {}
        for k, v in self.headers.items():
            if k.lower() in HOP_BY_HOP:
                continue
            headers[k] = v
        headers = rewrite_upstream_browser_headers(headers, upstream)
        headers["Host"] = f"{upstream.hostname}:{upstream.port or 80}"
        headers["Connection"] = "close"
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            content_type = (resp.getheader("Content-Type") or "").lower()
            req_path = urlsplit(self.path).path
            if any(x in content_type for x in ("json", "javascript", "text/html")):
                raw = rewrite_public_urls(raw, self.headers.get("Host", ""))
            if "text/html" in content_type and b"</body>" in raw.lower():
                inject = (
                    b'<link rel="stylesheet" href="/tsingpaws-cloud/skill-library.css?v=4" />'
                    b'<script src="/tsingpaws-cloud/skill-library.js?v=4" defer></script>'
                    b'<link rel="stylesheet" href="/tsingpaws-cloud/telegram-multibot.css?v=1" />'
                    b'<script src="/tsingpaws-cloud/telegram-multibot.js?v=1" defer></script>'
                )
                raw_l = raw.lower()
                idx = raw_l.rfind(b"</body>")
                if idx >= 0:
                    raw = raw[:idx] + inject + raw[idx:]
            self.send_response(resp.status)
            skip = {"transfer-encoding", "content-length", "connection", "content-encoding"}
            for k, v in resp.getheaders():
                if k.lower() in skip:
                    continue
                self.send_header(k, v)
            upstream_cookie = upstream_cookie_for_request(self.headers.get("Cookie", ""))
            if upstream_cookie:
                bridge_sid = cache_bridge_session(upstream_cookie)
                if bridge_sid:
                    self.send_header(
                        "Set-Cookie",
                        f"{_BRIDGE_COOKIE}={bridge_sid}; Path=/; HttpOnly; SameSite=Lax",
                    )
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)
        except Exception as exc:
            log.warning("proxy error: %s", redact(str(exc)))
            try:
                self.send_error(502, "Bad Gateway")
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), BridgeHandler)
    log.info("launcher bridge listening on %s:%s -> %s", LISTEN_HOST, LISTEN_PORT, UPSTREAM)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
