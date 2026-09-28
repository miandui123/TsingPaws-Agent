#!/usr/bin/env python3
"""Multi-Telegram adapter for one local PicoClaw Gateway.

The service deliberately has two trust boundaries:

* Telegram Bot tokens live only in a root-readable JSON file.
* The management API listens on loopback and never returns a token.

Each Telegram bot/chat pair owns a distinct Pico WebSocket session, so replies
cannot cross bot or chat boundaries. HTTP, Telegram long polling, the Pico
WebSocket client and the management API all use the Python standard library.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.cookiejar
import json
import logging
import os
import re
import signal
import ssl
import stat
import struct
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

VERSION = "1.0.0"
MAX_HTTP_BODY = 64 * 1024
MAX_WS_MESSAGE = 4 * 1024 * 1024
TELEGRAM_TEXT_LIMIT = 4096
BOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
TOKEN_RE = re.compile(r"^\d{5,20}:[A-Za-z0-9_-]{20,}$")
SESSION_SAFE_RE = re.compile(r"[^A-Za-z0-9._:-]+")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("telegram-multibot-adapter")


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name, default) or "").strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def config_path() -> str:
    return env("TELEGRAM_MULTIBOT_CONFIG", "/etc/tsingpaws/telegram-bots.json")


def api_host() -> str:
    # Never allow accidental exposure on a LAN interface.
    requested = env("TELEGRAM_MULTIBOT_HOST", "127.0.0.1")
    return requested if requested in ("127.0.0.1", "::1", "localhost") else "127.0.0.1"


def api_port() -> int:
    try:
        return int(env("TELEGRAM_MULTIBOT_PORT", "18792"))
    except ValueError:
        return 18792


def pico_base_url() -> str:
    return env("PICO_BASE_URL", "ws://127.0.0.1:18790").rstrip("/")


def pico_ws_path() -> str:
    value = env("PICO_WS_PATH", "/pico/ws")
    return value if value.startswith("/") else "/" + value


def _yaml_scalar(value: str) -> str:
    raw = value.strip()
    if not raw or raw in ("|", ">"):
        return ""
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            decoded = json.loads(raw)
            return decoded if isinstance(decoded, str) else ""
        except ValueError:
            return ""
    return raw.split(" #", 1)[0].strip()


def _token_from_security_file(path: str) -> str:
    """Parse only channels.pico.token; OpenWrt need not have PyYAML."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return ""
    channels_indent: Optional[int] = None
    pico_indent: Optional[int] = None
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
        elif (
            channels_indent is not None
            and pico_indent is None
            and indent > channels_indent
            and key == "pico"
            and not value.strip()
        ):
            pico_indent = indent
        elif pico_indent is not None and indent > pico_indent and key == "token":
            token = _yaml_scalar(value)
            return "" if token.startswith("enc://") else token
    return ""


def pico_token() -> str:
    direct = env("PICO_TOKEN")
    if direct:
        return direct
    explicit = env("PICO_SECURITY_FILE")
    candidates = [explicit] if explicit else [
        "/opt/tsingpaw/data/.security.yml",
        "/root/.picoclaw/.security.yml",
    ]
    for path in candidates:
        if path:
            token = _token_from_security_file(path)
            if token:
                return token
    pid_path = env("PICO_PID_FILE", "/opt/tsingpaw/data/.picoclaw.pid")
    if pid_path:
        try:
            with open(pid_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            runtime_token = payload.get("token") if isinstance(payload, dict) else ""
            if isinstance(runtime_token, str) and runtime_token.strip():
                return runtime_token.strip()
        except (OSError, ValueError):
            pass
    return ""


def pico_connection_headers() -> Dict[str, str]:
    """Build private Gateway headers, optionally through the authenticated Launcher."""
    parsed = urlparse(pico_base_url())
    via_launcher = env("PICO_VIA_LAUNCHER", "0").lower() in ("1", "true", "yes")
    if not via_launcher:
        token = pico_token()
        if not token:
            raise RuntimeError("Pico token is not configured")
        return {"Authorization": "Bearer " + token}

    launcher_config = env("PICO_LAUNCHER_CONFIG", "/opt/tsingpaw/data/launcher-config.json")
    try:
        with open(launcher_config, "r", encoding="utf-8") as handle:
            launcher = json.load(handle)
        dashboard_token = launcher.get("dashboard_token") if isinstance(launcher, dict) else ""
    except (OSError, ValueError) as exc:
        raise RuntimeError("cannot read Launcher credentials: " + redact(exc)) from None
    if not isinstance(dashboard_token, str) or not dashboard_token:
        raise RuntimeError("Launcher dashboard token is not configured")

    scheme = "https" if parsed.scheme == "wss" else "http"
    origin = urlunparse((scheme, parsed.netloc, "", "", "", "")).rstrip("/")
    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    request = Request(
        origin + "/api/auth/login",
        data=json.dumps({"token": dashboard_token}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Origin": origin},
        method="POST",
    )
    try:
        with opener.open(request, timeout=5) as response:
            response.read(4096)
    except Exception as exc:
        raise RuntimeError("Launcher authentication failed: " + redact(exc, (dashboard_token,))) from None
    cookie = "; ".join(item.name + "=" + item.value for item in jar)
    if not cookie:
        raise RuntimeError("Launcher authentication did not return a session cookie")
    channel_token = pico_token()
    encoded = base64.urlsafe_b64encode(channel_token.encode("utf-8")).decode("ascii").rstrip("=")
    return {
        "Cookie": cookie,
        "Origin": origin,
        "Sec-WebSocket-Protocol": "token.b64." + encoded,
    }


def pico_connect_url(session_id: str) -> str:
    base = urlparse(pico_base_url())
    return urlunparse(
        (base.scheme or "ws", base.netloc, pico_ws_path(), "", urlencode({"session_id": session_id}), "")
    )


def make_envelope(kind: str, session_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "type": kind,
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "timestamp": int(time.time() * 1000),
        "payload": payload if isinstance(payload, dict) else {},
    }


def redact(text: Any, secrets: Iterable[str] = ()) -> str:
    value = str(text or "")
    for secret in secrets:
        if secret and len(secret) >= 8:
            value = value.replace(secret, "***")
    value = re.sub(r"(?i)(bot)\d{5,20}:[A-Za-z0-9_-]{20,}", r"\1***", value)
    value = re.sub(r"(?i)(token|authorization|bearer|password|secret)=([^&\s]+)", r"\1=***", value)
    value = re.sub(r"(?i)Bearer\s+\S+", "Bearer ***", value)
    return value[:500]


class ValidationError(ValueError):
    pass


class TelegramError(RuntimeError):
    pass


class ConnectionClosed(RuntimeError):
    """Raised when the local RFC 6455 peer closes the connection."""


class MiniWebSocket:
    """Dependency-free RFC 6455 client sufficient for Pico loopback I/O."""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, max_size: int):
        self.reader = reader
        self.writer = writer
        self.max_size = max_size
        self.closed = False
        self._write_lock = asyncio.Lock()

    def __aiter__(self) -> "MiniWebSocket":
        return self

    async def __anext__(self) -> Any:
        try:
            return await self.recv()
        except ConnectionClosed:
            raise StopAsyncIteration

    @classmethod
    async def connect(
        cls,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        max_size: int = MAX_WS_MESSAGE,
        open_timeout: float = 15.0,
    ) -> "MiniWebSocket":
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
            raise ValueError("PICO_BASE_URL must be ws:// or wss://")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        ssl_context = ssl.create_default_context() if parsed.scheme == "wss" else None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    parsed.hostname,
                    port,
                    ssl=ssl_context,
                    server_hostname=parsed.hostname if ssl_context else None,
                ),
                timeout=open_timeout,
            )
        except Exception as exc:
            raise ConnectionError("Pico WebSocket connection failed: " + redact(exc)) from None
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        host_header = parsed.hostname
        if port != (443 if parsed.scheme == "wss" else 80):
            host_header += ":%s" % port
        request_headers = {
            "Host": host_header,
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": key,
            "Sec-WebSocket-Version": "13",
        }
        request_headers.update(headers or {})
        request = "GET %s HTTP/1.1\r\n%s\r\n\r\n" % (
            path,
            "\r\n".join("%s: %s" % item for item in request_headers.items()),
        )
        writer.write(request.encode("latin1"))
        await writer.drain()
        try:
            raw_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=open_timeout)
        except Exception:
            writer.close()
            raise ConnectionError("Pico WebSocket handshake timed out") from None
        lines = raw_head.decode("latin1", errors="replace").split("\r\n")
        if not lines or not re.match(r"^HTTP/\d(?:\.\d)?\s+101(?:\s|$)", lines[0]):
            status = lines[0][:120] if lines else "invalid response"
            writer.close()
            raise ConnectionError("Pico WebSocket upgrade rejected: " + status)
        response_headers: Dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                response_headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + cls.GUID).encode("ascii")).digest()).decode("ascii")
        if response_headers.get("sec-websocket-accept") != expected:
            writer.close()
            raise ConnectionError("Pico WebSocket handshake validation failed")
        return cls(reader, writer, max_size)

    async def _read_frame(self) -> Tuple[bool, int, bytes]:
        try:
            first, second = await self.reader.readexactly(2)
        except (asyncio.IncompleteReadError, ConnectionError):
            self.closed = True
            raise ConnectionClosed("WebSocket peer disconnected") from None
        fin = bool(first & 0x80)
        rsv = first & 0x70
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if rsv:
            raise ConnectionClosed("unsupported WebSocket extension frame")
        if length == 126:
            length = struct.unpack("!H", await self.reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self.reader.readexactly(8))[0]
        if length > self.max_size:
            await self.close(1009, "message too large")
            raise ConnectionClosed("WebSocket message too large")
        if masked:
            raise ConnectionClosed("server WebSocket frames must not be masked")
        mask = await self.reader.readexactly(4) if masked else b""
        payload = await self.reader.readexactly(length)
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        if opcode >= 0x8 and (not fin or length > 125):
            raise ConnectionClosed("invalid WebSocket control frame")
        return fin, opcode, payload

    async def recv(self) -> Any:
        message_opcode: Optional[int] = None
        parts: List[bytes] = []
        total = 0
        while not self.closed:
            fin, opcode, payload = await self._read_frame()
            if opcode == 0x8:
                if not self.closed:
                    await self._send_frame(0x8, payload[:125])
                self.closed = True
                self.writer.close()
                raise ConnectionClosed("WebSocket peer closed")
            if opcode == 0x9:
                await self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                if message_opcode is not None:
                    raise ConnectionClosed("interleaved WebSocket data frames")
                message_opcode = opcode
                parts = [payload]
                total = len(payload)
            elif opcode == 0x0:
                if message_opcode is None:
                    raise ConnectionClosed("unexpected WebSocket continuation frame")
                parts.append(payload)
                total += len(payload)
            else:
                raise ConnectionClosed("unsupported WebSocket opcode")
            if total > self.max_size:
                await self.close(1009, "message too large")
                raise ConnectionClosed("WebSocket message too large")
            if fin and message_opcode is not None:
                message = b"".join(parts)
                if message_opcode == 0x2:
                    return message
                try:
                    return message.decode("utf-8")
                except UnicodeDecodeError:
                    await self.close(1007, "invalid UTF-8")
                    raise ConnectionClosed("invalid WebSocket UTF-8") from None
        raise ConnectionClosed("WebSocket is closed")

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.closed and opcode != 0x8:
            raise ConnectionClosed("WebSocket is closed")
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        async with self._write_lock:
            self.writer.write(bytes(header) + mask + masked)
            await self.writer.drain()

    async def send(self, message: str) -> None:
        if not isinstance(message, str):
            raise TypeError("only text WebSocket messages are supported")
        payload = message.encode("utf-8")
        if len(payload) > self.max_size:
            raise ValueError("WebSocket message too large")
        await self._send_frame(0x1, payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        payload = struct.pack("!H", code) + reason.encode("utf-8")[:123]
        try:
            await self._send_frame(0x8, payload)
        except Exception:
            pass
        self.closed = True
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass


async def ws_connect(
    url: str,
    extra_headers: Optional[Dict[str, str]] = None,
    ping_interval: Optional[float] = None,
    ping_timeout: Optional[float] = None,
    max_size: int = MAX_WS_MESSAGE,
    open_timeout: float = 15.0,
) -> MiniWebSocket:
    # Pico traffic itself detects closed peers. Arguments remain compatible
    # with the reference implementation's call site.
    del ping_interval, ping_timeout
    return await MiniWebSocket.connect(url, extra_headers, max_size, open_timeout)


def _normalize_allow_from(value: Any) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError("allow_from must be an array")
    result: List[str] = []
    for item in value:
        if not isinstance(item, (str, int)):
            raise ValidationError("allow_from entries must be strings or integers")
        normalized = str(item).strip()
        if not normalized or len(normalized) > 128:
            raise ValidationError("allow_from contains an invalid entry")
        if normalized not in result:
            result.append(normalized)
    return result


def validate_bot(raw: Any, *, existing: Optional[Dict[str, Any]] = None, partial: bool = False) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("JSON object required")
    allowed = {"id", "name", "token", "enabled", "allow_from", "base_url"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValidationError("unknown fields: " + ", ".join(sorted(unknown)))
    merged = dict(existing or {})
    if not partial or "id" in raw:
        bot_id = str(raw.get("id") or merged.get("id") or "").strip().lower()
        if not bot_id and not existing:
            seed = re.sub(r"[^a-z0-9]+", "-", str(raw.get("name") or "").lower()).strip("-")[:40]
            bot_id = "%s-%s" % (seed or "bot", uuid.uuid4().hex[:8])
        if not BOT_ID_RE.fullmatch(bot_id):
            raise ValidationError("id must match [a-z0-9][a-z0-9_-]{0,63}")
        merged["id"] = bot_id
    if existing and "id" in raw and str(raw["id"]).strip().lower() != existing.get("id"):
        raise ValidationError("id cannot be changed")
    if not partial or "name" in raw:
        name = str(raw.get("name") or merged.get("name") or merged.get("id") or "").strip()
        if not name or len(name) > 120:
            raise ValidationError("name must contain 1-120 characters")
        merged["name"] = name
    if "token" in raw:
        token = str(raw.get("token") or "").strip()
        if not TOKEN_RE.fullmatch(token):
            raise ValidationError("invalid Telegram Bot token format")
        merged["token"] = token
    elif not existing:
        raise ValidationError("token is required")
    if not partial or "enabled" in raw:
        enabled = raw.get("enabled", merged.get("enabled", True))
        if not isinstance(enabled, bool):
            raise ValidationError("enabled must be boolean")
        merged["enabled"] = enabled
    if not partial or "allow_from" in raw:
        merged["allow_from"] = _normalize_allow_from(raw.get("allow_from", merged.get("allow_from", [])))
    if not partial or "base_url" in raw:
        base_url = str(raw.get("base_url") or merged.get("base_url") or "https://api.telegram.org").strip().rstrip("/")
        parsed = urlparse(base_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValidationError("base_url must be an HTTP(S) origin without credentials, path, query or fragment")
        merged["base_url"] = base_url
    return merged


def public_bot(bot: Dict[str, Any], status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result = {
        "id": bot["id"],
        "name": bot["name"],
        "enabled": bool(bot.get("enabled")),
        "allow_from": list(bot.get("allow_from") or []),
        "base_url": bot.get("base_url", "https://api.telegram.org"),
        "token_configured": bool(bot.get("token")),
    }
    if status:
        # Keep the list response directly consumable by the launcher UI while
        # still ensuring no runtime field can overwrite persisted identity.
        result.update({key: value for key, value in status.items() if key not in result})
    return result


class ConfigStore:
    def __init__(self, path: Optional[str] = None):
        self.path = path or config_path()

    def load(self) -> List[Dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise RuntimeError("cannot read Telegram bot configuration: " + redact(exc)) from exc
        if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("bots"), list):
            raise RuntimeError("invalid Telegram bot configuration")
        bots: List[Dict[str, Any]] = []
        seen = set()
        for item in document["bots"]:
            bot = validate_bot(item)
            if bot["id"] in seen:
                raise RuntimeError("duplicate bot id in configuration")
            seen.add(bot["id"])
            bots.append(bot)
        self._ensure_private_mode()
        return bots

    def _ensure_private_mode(self) -> None:
        try:
            mode = stat.S_IMODE(os.stat(self.path).st_mode)
            if mode != 0o600:
                os.chmod(self.path, 0o600)
        except OSError:
            pass

    def save(self, bots: List[Dict[str, Any]]) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, mode=0o700, exist_ok=True)
        payload = json.dumps({"version": 1, "bots": bots}, ensure_ascii=False, indent=2) + "\n"
        temporary = os.path.join(directory, ".%s.tmp.%s.%s" % (os.path.basename(self.path), os.getpid(), uuid.uuid4().hex))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            try:
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


class TelegramClient:
    def __init__(self, token: str, base_url: str = "https://api.telegram.org"):
        self.token = token
        self.base_url = base_url.rstrip("/")

    def _call_sync(self, method: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 40.0) -> Any:
        url = "%s/bot%s/%s" % (self.base_url, self.token, method)
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
                body = json.loads(raw)
        except HTTPError as exc:
            try:
                raw = exc.read(64 * 1024).decode("utf-8", errors="replace")
                body = json.loads(raw)
                description = str(body.get("description") or "HTTP %s" % exc.code)
            except Exception:
                description = "HTTP %s" % exc.code
            raise TelegramError(redact(description, (self.token,))) from None
        except (URLError, OSError, ValueError) as exc:
            raise TelegramError(redact(exc, (self.token,))) from None
        if not isinstance(body, dict) or body.get("ok") is not True:
            detail = body.get("description") if isinstance(body, dict) else "invalid Telegram response"
            raise TelegramError(redact(detail, (self.token,)))
        return body.get("result")

    async def call(self, method: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 40.0) -> Any:
        return await asyncio.to_thread(self._call_sync, method, payload, timeout)

    async def get_me(self) -> Dict[str, Any]:
        result = await self.call("getMe", timeout=12.0)
        if not isinstance(result, dict):
            raise TelegramError("invalid getMe response")
        return result

    async def get_updates(self, offset: int, timeout: int) -> List[Dict[str, Any]]:
        result = await self.call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message"]},
            timeout=float(timeout + 10),
        )
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    async def send_message(self, chat_id: Any, text: str) -> None:
        for chunk in split_telegram_text(text):
            await self.call("sendMessage", {"chat_id": chat_id, "text": chunk}, timeout=20.0)

    async def send_typing(self, chat_id: Any) -> None:
        await self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10.0)


def split_telegram_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> List[str]:
    remaining = text.strip()
    if not remaining:
        return []
    chunks: List[str] = []
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def sender_allowed(allow_from: List[str], message: Dict[str, Any]) -> bool:
    # Match PicoClaw's existing behavior: empty means open, while "*" makes
    # that choice explicit.  Numeric Telegram IDs are the recommended entries.
    if not allow_from or "*" in allow_from:
        return True
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    sender_id = str(sender.get("id") or "")
    username = str(sender.get("username") or "").strip()
    candidates = {sender_id, "telegram:" + sender_id}
    if username:
        candidates.update((username, "@" + username, username.casefold(), "@" + username.casefold()))
    allowed = {entry.casefold() for entry in allow_from}
    return any(candidate.casefold() in allowed for candidate in candidates if candidate)


@dataclass
class BotState:
    state: str = "stopped"
    username: str = ""
    last_error: Optional[str] = None
    last_poll_at: Optional[str] = None
    last_message_at: Optional[str] = None
    restart_count: int = 0
    rejected_messages: int = 0

    def public(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "username": self.username or None,
            "last_error": self.last_error,
            "last_poll_at": self.last_poll_at,
            "last_message_at": self.last_message_at,
            "restart_count": self.restart_count,
            "rejected_messages": self.rejected_messages,
        }


class PicoSession:
    def __init__(self, manager: "Adapter", bot_id: str, chat_id: str):
        self.manager = manager
        self.bot_id = bot_id
        self.chat_id = chat_id
        raw_id = "tg:%s:%s" % (bot_id, chat_id)
        self.session_id = SESSION_SAFE_RE.sub("_", raw_id)[:190]
        self.ws: Any = None
        self.reader_task: Optional[asyncio.Task] = None
        self.send_lock = asyncio.Lock()
        self.busy = False
        self.active_request_id = ""
        self.seen_events: Dict[str, float] = {}

    async def ensure(self) -> None:
        if self.ws is not None and not getattr(self.ws, "closed", False):
            return
        headers = await asyncio.to_thread(pico_connection_headers)
        self.ws = await ws_connect(
            pico_connect_url(self.session_id),
            extra_headers=headers,
            ping_interval=25,
            ping_timeout=45,
            max_size=MAX_WS_MESSAGE,
            open_timeout=15,
        )
        self.reader_task = asyncio.create_task(self._reader(), name="pico-" + self.session_id[:40])

    async def send_task(self, text: str) -> bool:
        if self.busy:
            return False
        request_id = str(uuid.uuid4())
        envelope = make_envelope("message.send", self.session_id, {"content": text, "message_id": request_id})
        self.busy = True
        self.active_request_id = request_id
        try:
            await self.ensure()
            async with self.send_lock:
                await self.ws.send(json.dumps(envelope, ensure_ascii=False))
            return True
        except Exception:
            self.busy = False
            self.active_request_id = ""
            await self.close()
            raise

    def mark_done(self) -> None:
        self.busy = False
        self.active_request_id = ""

    async def _reader(self) -> None:
        current = self.ws
        try:
            async for raw in current:
                if isinstance(raw, bytes) or len(raw) > MAX_WS_MESSAGE:
                    continue
                await self.manager.handle_pico_message(self, raw)
        except asyncio.CancelledError:
            raise
        except ConnectionClosed:
            pass
        except Exception as exc:
            await self.manager.note_session_error(self, exc)
        finally:
            if self.ws is current:
                self.ws = None
            if self.busy:
                self.mark_done()

    async def close(self) -> None:
        task = self.reader_task
        self.reader_task = None
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        current = self.ws
        self.ws = None
        if current is not None:
            try:
                await current.close()
            except Exception:
                pass
        self.mark_done()


@dataclass
class BotRuntime:
    config: Dict[str, Any]
    client: TelegramClient
    state: BotState = field(default_factory=BotState)
    task: Optional[asyncio.Task] = None


class Adapter:
    def __init__(
        self,
        store: Optional[ConfigStore] = None,
        client_factory: Callable[[str, str], TelegramClient] = TelegramClient,
    ):
        self.store = store or ConfigStore()
        self.client_factory = client_factory
        self.bots: Dict[str, Dict[str, Any]] = {}
        self.runtimes: Dict[str, BotRuntime] = {}
        self.sessions: Dict[Tuple[str, str], PicoSession] = {}
        self.lock = asyncio.Lock()
        self.started_at = time.time()

    async def start(self) -> None:
        bots = await asyncio.to_thread(self.store.load)
        self.bots = {bot["id"]: bot for bot in bots}
        self._ensure_unique_tokens(self.bots)
        await self._reconcile()

    async def stop(self) -> None:
        for runtime in list(self.runtimes.values()):
            await self._stop_runtime(runtime)
        for session in list(self.sessions.values()):
            await session.close()
        self.sessions.clear()

    def _new_runtime(self, bot: Dict[str, Any]) -> BotRuntime:
        return BotRuntime(bot, self.client_factory(bot["token"], bot["base_url"]))

    async def _reconcile(self) -> None:
        for bot_id, runtime in list(self.runtimes.items()):
            desired = self.bots.get(bot_id)
            if desired != runtime.config or not desired or not desired.get("enabled"):
                await self._stop_runtime(runtime)
                self.runtimes.pop(bot_id, None)
        for bot_id, bot in self.bots.items():
            if bot.get("enabled") and bot_id not in self.runtimes:
                runtime = self._new_runtime(bot)
                self.runtimes[bot_id] = runtime
                runtime.task = asyncio.create_task(self._poll_forever(runtime), name="telegram-" + bot_id)

    async def _stop_runtime(self, runtime: BotRuntime) -> None:
        if runtime.task and not runtime.task.done():
            runtime.task.cancel()
            try:
                await runtime.task
            except (asyncio.CancelledError, Exception):
                pass
        runtime.state.state = "stopped"
        for key, session in list(self.sessions.items()):
            if key[0] == runtime.config["id"]:
                await session.close()
                self.sessions.pop(key, None)

    def status_for(self, bot_id: str) -> Dict[str, Any]:
        runtime = self.runtimes.get(bot_id)
        if runtime:
            return runtime.state.public()
        return BotState(state="disabled" if bot_id in self.bots and not self.bots[bot_id].get("enabled") else "stopped").public()

    def list_public(self) -> List[Dict[str, Any]]:
        return [public_bot(bot, self.status_for(bot_id)) for bot_id, bot in sorted(self.bots.items())]

    @staticmethod
    def _ensure_unique_tokens(bots: Dict[str, Dict[str, Any]]) -> None:
        owners: Dict[str, str] = {}
        for bot_id, bot in bots.items():
            token = bot.get("token", "")
            if token in owners:
                raise ValidationError("token is already configured for another bot")
            owners[token] = bot_id

    async def create_bot(self, payload: Any) -> Dict[str, Any]:
        async with self.lock:
            bot = validate_bot(payload)
            if bot["id"] in self.bots:
                raise ValidationError("bot id already exists")
            updated = dict(self.bots)
            updated[bot["id"]] = bot
            self._ensure_unique_tokens(updated)
            await asyncio.to_thread(self.store.save, list(updated.values()))
            self.bots = updated
            await self._reconcile()
            return public_bot(bot, self.status_for(bot["id"]))

    async def update_bot(self, bot_id: str, payload: Any) -> Dict[str, Any]:
        async with self.lock:
            current = self.bots.get(bot_id)
            if current is None:
                raise KeyError(bot_id)
            bot = validate_bot(payload, existing=current, partial=True)
            updated = dict(self.bots)
            updated[bot_id] = bot
            self._ensure_unique_tokens(updated)
            await asyncio.to_thread(self.store.save, list(updated.values()))
            self.bots = updated
            await self._reconcile()
            return public_bot(bot, self.status_for(bot_id))

    async def delete_bot(self, bot_id: str) -> None:
        async with self.lock:
            if bot_id not in self.bots:
                raise KeyError(bot_id)
            updated = dict(self.bots)
            updated.pop(bot_id)
            await asyncio.to_thread(self.store.save, list(updated.values()))
            self.bots = updated
            await self._reconcile()

    async def test_bot(self, bot_id: str) -> Dict[str, Any]:
        bot = self.bots.get(bot_id)
        if bot is None:
            raise KeyError(bot_id)
        client = self.client_factory(bot["token"], bot["base_url"])
        me = await client.get_me()
        return {
            "ok": True,
            "bot": {
                "id": me.get("id"),
                "username": me.get("username"),
                "first_name": me.get("first_name"),
            },
        }

    async def _poll_forever(self, runtime: BotRuntime) -> None:
        offset = 0
        backoff = 2.0
        secrets = (runtime.config["token"],)
        while True:
            try:
                runtime.state.state = "starting"
                me = await runtime.client.get_me()
                runtime.state.username = str(me.get("username") or "")
                runtime.state.state = "running"
                runtime.state.last_error = None
                while True:
                    updates = await runtime.client.get_updates(offset, 30)
                    runtime.state.last_poll_at = now_iso()
                    for update in updates:
                        try:
                            update_id = int(update.get("update_id"))
                            offset = max(offset, update_id + 1)
                        except (TypeError, ValueError):
                            pass
                        await self._handle_update(runtime, update)
                    backoff = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                runtime.state.state = "error"
                runtime.state.last_error = redact(exc, secrets)
                runtime.state.restart_count += 1
                log.warning("bot poll failed bot=%s err=%s", runtime.config["id"], runtime.state.last_error)
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    async def _handle_update(self, runtime: BotRuntime, update: Dict[str, Any]) -> None:
        message = update.get("message") if isinstance(update.get("message"), dict) else None
        if not message:
            return
        text = message.get("text")
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = chat.get("id")
        if not isinstance(text, str) or not text.strip() or chat_id is None:
            return
        if not sender_allowed(runtime.config.get("allow_from", []), message):
            runtime.state.rejected_messages += 1
            return
        runtime.state.last_message_at = now_iso()
        key = (runtime.config["id"], str(chat_id))
        session = self.sessions.get(key)
        if session is None:
            session = PicoSession(self, *key)
            self.sessions[key] = session
        try:
            accepted = await session.send_task(text.strip())
            if not accepted:
                await runtime.client.send_message(chat_id, "上一项任务仍在处理，请等待完成后再发送。")
        except Exception as exc:
            runtime.state.last_error = redact(exc, (runtime.config["token"], pico_token()))
            log.warning("forward to Pico failed bot=%s session=%s err=%s", runtime.config["id"], session.session_id[:40], runtime.state.last_error)
            await runtime.client.send_message(chat_id, "任务暂时无法转发到 TsingPaws，请稍后重试。")

    async def handle_pico_message(self, session: PicoSession, raw: str) -> None:
        try:
            obj = json.loads(raw)
        except ValueError:
            return
        if not isinstance(obj, dict):
            return
        runtime = self.runtimes.get(session.bot_id)
        if runtime is None:
            return
        kind = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        event_id = str(obj.get("id") or "")
        if event_id:
            if event_id in session.seen_events:
                return
            session.seen_events[event_id] = time.time()
            if len(session.seen_events) > 500:
                cutoff = time.time() - 3600
                session.seen_events = {key: value for key, value in session.seen_events.items() if value >= cutoff}
        if kind == "typing.start":
            try:
                await runtime.client.send_typing(session.chat_id)
            except Exception:
                pass
            return
        if kind in ("message.create", "message.update"):
            if payload.get("thought") is True or payload.get("delta"):
                return
            content = payload.get("content")
            if isinstance(content, str) and content.strip():
                await runtime.client.send_message(session.chat_id, content)
                session.mark_done()
            return
        if kind == "error":
            message = str(payload.get("message") or "TsingPaws 处理任务失败。")
            await runtime.client.send_message(session.chat_id, message[:TELEGRAM_TEXT_LIMIT])
            session.mark_done()
            return
        if kind == "response.done":
            session.mark_done()

    async def note_session_error(self, session: PicoSession, exc: BaseException) -> None:
        runtime = self.runtimes.get(session.bot_id)
        if runtime:
            runtime.state.last_error = redact(exc, (runtime.config["token"], pico_token()))

    def health(self) -> Dict[str, Any]:
        states = {bot_id: self.status_for(bot_id)["state"] for bot_id in self.bots}
        unhealthy = [bot_id for bot_id, state in states.items() if state == "error"]
        return {
            "status": "degraded" if unhealthy else "ok",
            "service": "telegram-multibot-adapter",
            "version": VERSION,
            "uptime_seconds": int(time.time() - self.started_at),
            "bots_total": len(self.bots),
            "bots_enabled": sum(1 for bot in self.bots.values() if bot.get("enabled")),
            "bots_error": unhealthy,
        }


class ApiServer:
    def __init__(self, adapter: Adapter, host: Optional[str] = None, port: Optional[int] = None):
        self.adapter = adapter
        self.host = host or api_host()
        self.port = api_port() if port is None else port
        self.server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, self.host, self.port)
        sockets = self.server.sockets or []
        if sockets:
            self.port = int(sockets[0].getsockname()[1])
        log.info("management API listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            text = head.decode("latin1", errors="ignore")
            request_line = text.split("\r\n", 1)[0].split()
            if len(request_line) < 2:
                await self._json(writer, 400, {"error": "invalid_request"})
                return
            method, target = request_line[0].upper(), request_line[1]
            path = target.split("?", 1)[0]
            length = 0
            for line in text.split("\r\n")[1:]:
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1].strip())
            if length < 0 or length > MAX_HTTP_BODY:
                await self._json(writer, 413, {"error": "payload_too_large"})
                return
            body = await asyncio.wait_for(reader.readexactly(length), timeout=5) if length else b""
            payload: Any = {}
            if body:
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    await self._json(writer, 400, {"error": "invalid_json"})
                    return
            status, response = await self._dispatch(method, path, payload)
            await self._json(writer, status, response)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            try:
                await self._json(writer, 400, {"error": "invalid_request"})
            except Exception:
                pass
        except Exception as exc:
            log.warning("management API error: %s", redact(exc, [bot.get("token", "") for bot in self.adapter.bots.values()]))
            try:
                await self._json(writer, 500, {"error": "internal_error"})
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(self, method: str, path: str, payload: Any) -> Tuple[int, Dict[str, Any]]:
        if method == "GET" and path == "/health":
            return 200, self.adapter.health()
        if path == "/api/telegram-bots" and method == "GET":
            return 200, {"bots": self.adapter.list_public()}
        if path == "/api/telegram-bots" and method == "POST":
            try:
                return 201, {"bot": await self.adapter.create_bot(payload)}
            except ValidationError as exc:
                return 400, {"error": "validation_error", "detail": str(exc)}
        match = re.fullmatch(r"/api/telegram-bots/([a-z0-9][a-z0-9_-]{0,63})(/test)?", path)
        if match:
            bot_id, suffix = match.groups()
            try:
                if method == "PUT" and not suffix:
                    return 200, {"bot": await self.adapter.update_bot(bot_id, payload)}
                if method == "DELETE" and not suffix:
                    await self.adapter.delete_bot(bot_id)
                    return 200, {"ok": True}
                if method == "POST" and suffix == "/test":
                    return 200, await self.adapter.test_bot(bot_id)
            except KeyError:
                return 404, {"error": "not_found"}
            except ValidationError as exc:
                return 400, {"error": "validation_error", "detail": str(exc)}
            except TelegramError as exc:
                secrets = [bot.get("token", "") for bot in self.adapter.bots.values()]
                return 502, {"ok": False, "error": "telegram_error", "detail": redact(exc, secrets)}
        return 404, {"error": "not_found"}

    @staticmethod
    async def _json(writer: asyncio.StreamWriter, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        reason = {200: "OK", 201: "Created", 400: "Bad Request", 404: "Not Found", 413: "Payload Too Large", 500: "Internal Server Error", 502: "Bad Gateway"}.get(status, "OK")
        headers = (
            "HTTP/1.1 %s %s\r\n" % (status, reason)
            + "Content-Type: application/json; charset=utf-8\r\n"
            + "Content-Length: %s\r\n" % len(body)
            + "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
        ).encode("latin1")
        writer.write(headers + body)
        await writer.drain()


async def amain() -> None:
    adapter = Adapter()
    await adapter.start()
    server = ApiServer(adapter)
    await server.start()
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopped.set)
        except (NotImplementedError, RuntimeError):
            pass
    await stopped.wait()
    await server.stop()
    await adapter.stop()


def main() -> int:
    try:
        asyncio.run(amain())
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        log.error("startup failed: %s", redact(exc, (pico_token(),)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
