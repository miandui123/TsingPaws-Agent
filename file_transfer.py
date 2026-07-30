"""APP file.* transfer helpers and TransferManager for the TsingPaws agent.

Never logs Base64, file content, tokens, or Authorization headers.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("tsingpaws-agent.xfer")

MAX_RAW_CHUNK = 48 * 1024
MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_CONCURRENT_TRANSFERS = 3
MAX_TEMP_TOTAL = 100 * 1024 * 1024
IMAGE_INLINE_MAX = 3 * 1024 * 1024
STALE_TRANSFER_AGE_SEC = 30 * 60
DEFAULT_TRANSFER_ROOT = "/tmp/tsingpaws-agent/transfers"
DEFAULT_WORKSPACE = "/root/.picoclaw/workspace"
INBOX_SUBDIR = "inbox/tsingpaws"

FORBIDDEN_WRITE_PREFIXES = (
    "/opt/tsingpaws-agent/static",
    "/opt/tsingpaws-agent/vendor",
    "/www",
    "/wwwroot",
)

# Dangerous / control chars stripped from basenames.
_UNSAFE_NAME_RE = re.compile(r"[\x00-\x1f\x7f<>:\"/\\|?*\u0000]")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FILE_REF_RE = re.compile(r"\[(?:image|file):\s*([^\]]+)\]", re.IGNORECASE)

MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".zip": "application/zip",
    ".json": "application/json",
    ".md": "text/markdown",
}


class TransferError(Exception):
    """User-visible transfer failure (safe message, no secrets)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def sanitize_filename(name: Any) -> str:
    """Basename only; strip path/control/dangerous chars; no traversal."""
    if not isinstance(name, str) or not name.strip():
        return "unnamed"
    base = os.path.basename(name.replace("\\", "/").strip())
    base = _UNSAFE_NAME_RE.sub("_", base)
    base = base.strip(" .")
    if not base or base in (".", ".."):
        return "unnamed"
    # Collapse consecutive underscores from sanitization noise.
    while "__" in base:
        base = base.replace("__", "_")
    return base[:200] or "unnamed"


def guess_mime(filename: str, declared: Optional[str] = None) -> str:
    if isinstance(declared, str) and declared.strip() and "/" in declared:
        return declared.strip().lower()
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in MIME_BY_EXT:
        return MIME_BY_EXT[ext]
    guessed, _ = mimetypes.guess_type(filename or "")
    return (guessed or "application/octet-stream").lower()


def is_image_mime(mime: str) -> bool:
    return isinstance(mime, str) and mime.lower().startswith("image/")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def validate_sha256_hex(value: Any) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value.lower()):
        raise TransferError("invalid_sha256", "sha256 无效")
    return value.lower()


def ensure_dir(path: str, mode: int = 0o700) -> None:
    os.makedirs(path, mode=mode, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def path_is_under(path: str, root: str) -> bool:
    try:
        real_path = os.path.realpath(path)
        real_root = os.path.realpath(root)
    except OSError:
        return False
    return real_path == real_root or real_path.startswith(real_root + os.sep)


def is_forbidden_write_path(path: str) -> bool:
    real = os.path.realpath(path)
    for prefix in FORBIDDEN_WRITE_PREFIXES:
        if path_is_under(real, prefix):
            return True
    return False


def approved_outbound_roots(workspace: str) -> List[str]:
    ws = os.path.realpath(workspace)
    return [
        os.path.join(ws, "inbox"),
        # skills/**/output/** — check separately with glob-style walk
        os.path.join(ws, "skills"),
    ]


def is_approved_outbound_path(path: str, workspace: str) -> bool:
    """Outbound allowed only from inbox/** or skills/**/output/**."""
    try:
        real = os.path.realpath(path)
        ws = os.path.realpath(workspace)
    except OSError:
        return False
    if not path_is_under(real, ws):
        return False
    if os.path.islink(path):
        # Reject symlink sources that escape after resolve (realpath already used).
        pass
    inbox = os.path.join(ws, "inbox")
    if path_is_under(real, inbox):
        return True
    skills = os.path.join(ws, "skills")
    if not path_is_under(real, skills):
        return False
    # Must be under .../skills/<any>/output/...
    rel = os.path.relpath(real, skills)
    parts = rel.split(os.sep)
    return len(parts) >= 3 and parts[1] == "output"


def extract_file_refs(content: str) -> List[str]:
    if not isinstance(content, str) or not content:
        return []
    return [m.group(1).strip() for m in _FILE_REF_RE.finditer(content)]


def media_ref_from_url(url: str) -> Optional[str]:
    """Extract media ref from `/pico/media/<ref>` (absolute or relative)."""
    if not isinstance(url, str) or not url.strip():
        return None
    u = url.strip()
    marker = "/pico/media/"
    idx = u.find(marker)
    if idx < 0:
        return None
    ref = u[idx + len(marker) :].split("?", 1)[0].strip("/")
    if not ref or ".." in ref or "/" in ref or "\\" in ref:
        return None
    return ref


def chunk_count_for_size(size: int, chunk_size: int = MAX_RAW_CHUNK) -> int:
    if size <= 0:
        return 1
    return (size + chunk_size - 1) // chunk_size


def dir_total_size(root: str) -> int:
    total = 0
    if not os.path.isdir(root):
        return 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def cleanup_stale_transfers(transfer_root: str, max_age_sec: int = STALE_TRANSFER_AGE_SEC) -> int:
    """Remove leftover transfer dirs older than max_age_sec. Returns count removed."""
    if not os.path.isdir(transfer_root):
        return 0
    now = time.time()
    removed = 0
    for name in os.listdir(transfer_root):
        path = os.path.join(transfer_root, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if now - mtime < max_age_sec:
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.unlink(path)
            removed += 1
        except OSError:
            pass
    return removed


def atomic_move_no_symlink(src: str, dest: str) -> None:
    """Move/copy src to dest; refuse symlink overwrite and forbidden roots."""
    if is_forbidden_write_path(dest):
        raise TransferError("forbidden_path", "禁止写入 Web 根目录")
    dest_dir = os.path.dirname(dest)
    ensure_dir(dest_dir, 0o700)
    if os.path.lexists(dest):
        if os.path.islink(dest):
            raise TransferError("symlink_refused", "拒绝覆盖符号链接")
        try:
            os.unlink(dest)
        except OSError as exc:
            raise TransferError("write_failed", "无法覆盖目标文件") from exc
    try:
        os.replace(src, dest)
    except OSError:
        # Cross-device: copy then remove.
        tmp = dest + f".tmp.{os.getpid()}"
        try:
            with open(src, "rb") as rf, open(tmp, "wb") as wf:
                shutil.copyfileobj(rf, wf, length=65536)
                wf.flush()
                os.fsync(wf.fileno())
            os.chmod(tmp, 0o600)
            if os.path.islink(dest):
                raise TransferError("symlink_refused", "拒绝覆盖符号链接")
            os.replace(tmp, dest)
            try:
                os.unlink(src)
            except OSError:
                pass
        except TransferError:
            raise
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise TransferError("write_failed", "写入目标失败") from exc
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass


def to_data_url(mime: str, path: str) -> str:
    with open(path, "rb") as fh:
        raw = fh.read(IMAGE_INLINE_MAX + 1)
    if len(raw) > IMAGE_INLINE_MAX:
        raise TransferError("image_too_large", "图片过大，无法内联")
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


@dataclass
class TransferRecord:
    transfer_id: str
    session_id: str
    name: str
    mime_type: str
    size: int
    chunk_count: int
    sha256: str
    dir_path: str
    file_path: str
    next_index: int = 0
    bytes_written: int = 0
    hasher: Any = field(default_factory=hashlib.sha256)
    created_at: float = field(default_factory=time.time)


@dataclass
class CompletedTransfer:
    transfer_id: str
    session_id: str
    name: str
    mime_type: str
    size: int
    sha256: str
    path: str  # final path under inbox


class TransferManager:
    """Inbound APP file.start/chunk/end handling with disk streaming."""

    def __init__(
        self,
        transfer_root: Optional[str] = None,
        workspace: Optional[str] = None,
        send_error: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.transfer_root = transfer_root or os.environ.get("TRANSFER_ROOT", DEFAULT_TRANSFER_ROOT)
        self.workspace = workspace or os.environ.get("PICO_WORKSPACE", DEFAULT_WORKSPACE)
        self._send_error = send_error
        self._transfers: Dict[str, TransferRecord] = {}
        self._lock_note = 0  # placeholder; asyncio lock owned by Agent if needed
        ensure_dir(self.transfer_root, 0o700)
        ensure_dir(self.inbox_dir(), 0o700)
        removed = cleanup_stale_transfers(self.transfer_root)
        if removed:
            log.info("startup cleaned stale transfers count=%s", removed)

    def inbox_dir(self) -> str:
        return os.path.join(self.workspace, INBOX_SUBDIR)

    def active_count(self) -> int:
        return len(self._transfers)

    def get(self, transfer_id: str) -> Optional[TransferRecord]:
        return self._transfers.get(transfer_id)

    def temp_total_bytes(self) -> int:
        return dir_total_size(self.transfer_root)

    def cleanup_transfer(self, transfer_id: str) -> None:
        rec = self._transfers.pop(transfer_id, None)
        path = rec.dir_path if rec else os.path.join(self.transfer_root, transfer_id)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass

    def cleanup_session(self, session_id: str) -> None:
        for tid in [t for t, r in self._transfers.items() if r.session_id == session_id]:
            self.cleanup_transfer(tid)

    def cleanup_all(self) -> None:
        for tid in list(self._transfers.keys()):
            self.cleanup_transfer(tid)

    async def emit_error(self, session_id: str, code: str, message: str, transfer_id: Optional[str] = None) -> None:
        if self._send_error is None:
            return
        payload: Dict[str, Any] = {"code": code, "message": message}
        if transfer_id:
            payload["transfer_id"] = transfer_id
        await self._send_error(session_id, payload)

    def _fail(self, transfer_id: Optional[str], code: str, message: str) -> TransferError:
        if transfer_id:
            self.cleanup_transfer(transfer_id)
        return TransferError(code, message)

    def handle_start(self, session_id: str, payload: Dict[str, Any]) -> TransferRecord:
        if not isinstance(payload, dict):
            raise TransferError("invalid_payload", "file.start payload 无效")
        transfer_id = payload.get("transfer_id")
        if not isinstance(transfer_id, str) or not transfer_id.strip():
            raise TransferError("invalid_transfer_id", "transfer_id 无效")
        transfer_id = transfer_id.strip()
        if transfer_id in self._transfers:
            raise self._fail(transfer_id, "duplicate_transfer", "重复的 transfer_id")
        if self.active_count() >= MAX_CONCURRENT_TRANSFERS:
            raise TransferError("too_many_transfers", "并发传输过多")

        name = sanitize_filename(payload.get("name"))
        mime_type = guess_mime(name, payload.get("mime_type") if isinstance(payload.get("mime_type"), str) else None)
        try:
            size = int(payload.get("size"))
            chunk_count = int(payload.get("chunk_count"))
        except (TypeError, ValueError) as exc:
            raise TransferError("invalid_size", "size/chunk_count 无效") from exc
        if size < 0 or size > MAX_FILE_SIZE:
            raise TransferError("file_too_large", "文件超过 20MiB 限制")
        if chunk_count < 1 or chunk_count > chunk_count_for_size(MAX_FILE_SIZE) + 8:
            raise TransferError("invalid_chunk_count", "chunk_count 无效")
        expected_min = chunk_count_for_size(size) if size > 0 else 1
        # Allow exact match or declared count consistent with size (APP may pad last chunk).
        if chunk_count < expected_min and size > 0:
            raise TransferError("invalid_chunk_count", "chunk_count 与 size 不符")
        sha256 = validate_sha256_hex(payload.get("sha256"))

        # Quota: reserved size for this transfer + existing temp.
        if self.temp_total_bytes() + size > MAX_TEMP_TOTAL:
            raise TransferError("temp_quota_exceeded", "临时目录配额不足")

        dir_path = os.path.join(self.transfer_root, transfer_id)
        if ".." in transfer_id or "/" in transfer_id or "\\" in transfer_id:
            raise TransferError("invalid_transfer_id", "transfer_id 非法")
        if is_forbidden_write_path(dir_path):
            raise TransferError("forbidden_path", "禁止写入 Web 根目录")
        ensure_dir(dir_path, 0o700)
        file_path = os.path.join(dir_path, "payload.bin")
        # Create empty file with 0600
        fd = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)

        rec = TransferRecord(
            transfer_id=transfer_id,
            session_id=session_id,
            name=name,
            mime_type=mime_type,
            size=size,
            chunk_count=chunk_count,
            sha256=sha256,
            dir_path=dir_path,
            file_path=file_path,
        )
        self._transfers[transfer_id] = rec
        log.info(
            "transfer start id=%s session=%s name=%s size=%s chunks=%s",
            transfer_id[:8],
            session_id[:8] if session_id else "",
            name,
            size,
            chunk_count,
        )
        return rec

    def handle_chunk(self, session_id: str, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise TransferError("invalid_payload", "file.chunk payload 无效")
        transfer_id = payload.get("transfer_id")
        if not isinstance(transfer_id, str):
            raise TransferError("invalid_transfer_id", "transfer_id 无效")
        rec = self._transfers.get(transfer_id)
        if rec is None:
            raise TransferError("unknown_transfer", "未知 transfer_id")
        if rec.session_id != session_id:
            raise self._fail(transfer_id, "session_mismatch", "session 与传输不匹配")
        try:
            index = int(payload.get("index"))
        except (TypeError, ValueError) as exc:
            raise self._fail(transfer_id, "invalid_index", "分片 index 无效") from exc
        if index != rec.next_index:
            raise self._fail(transfer_id, "out_of_order", "分片顺序错误")
        if index >= rec.chunk_count:
            raise self._fail(transfer_id, "extra_chunk", "多余分片")
        data_b64 = payload.get("data")
        if not isinstance(data_b64, str):
            raise self._fail(transfer_id, "invalid_chunk", "分片 data 无效")
        try:
            raw = base64.b64decode(data_b64, validate=False)
        except Exception as exc:
            raise self._fail(transfer_id, "invalid_base64", "分片 Base64 无效") from exc
        # Never log data_b64 / raw
        del data_b64
        if len(raw) > MAX_RAW_CHUNK:
            raise self._fail(transfer_id, "chunk_too_large", "分片超过 48KiB")
        if rec.bytes_written + len(raw) > MAX_FILE_SIZE:
            raise self._fail(transfer_id, "file_too_large", "文件超过 20MiB 限制")
        if rec.bytes_written + len(raw) > rec.size:
            raise self._fail(transfer_id, "size_mismatch", "写入超过声明 size")
        try:
            with open(rec.file_path, "ab") as fh:
                fh.write(raw)
                fh.flush()
            os.chmod(rec.file_path, 0o600)
        except OSError as exc:
            raise self._fail(transfer_id, "write_failed", "写入临时文件失败") from exc
        rec.hasher.update(raw)
        rec.bytes_written += len(raw)
        rec.next_index += 1

    def handle_end(self, session_id: str, payload: Dict[str, Any]) -> CompletedTransfer:
        if not isinstance(payload, dict):
            raise TransferError("invalid_payload", "file.end payload 无效")
        transfer_id = payload.get("transfer_id")
        if not isinstance(transfer_id, str):
            raise TransferError("invalid_transfer_id", "transfer_id 无效")
        rec = self._transfers.get(transfer_id)
        if rec is None:
            raise TransferError("unknown_transfer", "未知 transfer_id")
        if rec.session_id != session_id:
            raise self._fail(transfer_id, "session_mismatch", "session 与传输不匹配")
        try:
            end_count = int(payload.get("chunk_count"))
        except (TypeError, ValueError) as exc:
            raise self._fail(transfer_id, "invalid_chunk_count", "chunk_count 无效") from exc
        end_sha = validate_sha256_hex(payload.get("sha256"))
        if end_count != rec.chunk_count or end_count != rec.next_index:
            raise self._fail(transfer_id, "chunk_count_mismatch", "分片数量不匹配")
        if end_sha != rec.sha256:
            raise self._fail(transfer_id, "sha256_mismatch", "sha256 与 start 不一致")
        if rec.bytes_written != rec.size:
            raise self._fail(transfer_id, "size_mismatch", "实际大小与声明不符")
        digest = rec.hasher.hexdigest()
        if digest != rec.sha256:
            raise self._fail(transfer_id, "sha256_mismatch", "内容哈希校验失败")

        # Move into inbox/tsingpaws/
        inbox = self.inbox_dir()
        ensure_dir(inbox, 0o700)
        dest_name = rec.name
        dest = os.path.join(inbox, dest_name)
        # Avoid clobber: if exists, prefix transfer id short
        if os.path.lexists(dest):
            stem, ext = os.path.splitext(dest_name)
            dest = os.path.join(inbox, f"{stem}_{transfer_id[:8]}{ext}")
        try:
            atomic_move_no_symlink(rec.file_path, dest)
        except TransferError:
            self.cleanup_transfer(transfer_id)
            raise
        # Drop temp dir
        self._transfers.pop(transfer_id, None)
        try:
            shutil.rmtree(rec.dir_path, ignore_errors=True)
        except OSError:
            pass
        log.info(
            "transfer complete id=%s session=%s name=%s size=%s",
            transfer_id[:8],
            session_id[:8] if session_id else "",
            rec.name,
            rec.size,
        )
        return CompletedTransfer(
            transfer_id=transfer_id,
            session_id=session_id,
            name=rec.name,
            mime_type=rec.mime_type,
            size=rec.size,
            sha256=rec.sha256,
            path=dest,
        )


def build_file_frames(
    session_id: str,
    path: str,
    name: Optional[str] = None,
    mime_type: Optional[str] = None,
    transfer_id: Optional[str] = None,
    chunk_size: int = MAX_RAW_CHUNK,
) -> List[Dict[str, Any]]:
    """Build file.start/chunk/end envelopes for pushing a local file to APP."""
    if not os.path.isfile(path):
        raise TransferError("missing_file", "文件不存在")
    size = os.path.getsize(path)
    if size > MAX_FILE_SIZE:
        raise TransferError("file_too_large", "文件超过 20MiB 限制")
    safe_name = sanitize_filename(name or os.path.basename(path))
    mime = guess_mime(safe_name, mime_type)
    digest = sha256_file(path)
    tid = transfer_id or str(uuid.uuid4())
    count = chunk_count_for_size(size, chunk_size) if size > 0 else 1
    frames: List[Dict[str, Any]] = []
    frames.append(
        {
            "type": "file.start",
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "timestamp": int(time.time() * 1000),
            "payload": {
                "transfer_id": tid,
                "name": safe_name,
                "mime_type": mime,
                "size": size,
                "chunk_count": count,
                "sha256": digest,
            },
        }
    )
    index = 0
    with open(path, "rb") as fh:
        while True:
            raw = fh.read(chunk_size)
            if not raw and index > 0:
                break
            if not raw and size == 0:
                # one empty chunk for zero-byte files
                raw = b""
            frames.append(
                {
                    "type": "file.chunk",
                    "id": str(uuid.uuid4()),
                    "session_id": session_id,
                    "timestamp": int(time.time() * 1000),
                    "payload": {
                        "transfer_id": tid,
                        "index": index,
                        "data": base64.b64encode(raw).decode("ascii"),
                    },
                }
            )
            index += 1
            if size == 0 or not raw or index >= count:
                if size == 0:
                    break
                if fh.tell() >= size:
                    break
    # Fix chunk_count if last partial adjusted
    actual_count = index
    frames[0]["payload"]["chunk_count"] = actual_count
    frames.append(
        {
            "type": "file.end",
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "timestamp": int(time.time() * 1000),
            "payload": {
                "transfer_id": tid,
                "chunk_count": actual_count,
                "sha256": digest,
            },
        }
    )
    return frames


def strip_huge_binary_from_content(content: Any, limit: int = 64 * 1024) -> Any:
    """Strip oversized data: URLs from text so we don't forward megabytes to APP twice."""
    if not isinstance(content, str):
        return content
    if "data:" not in content or len(content) <= limit:
        return content

    def _repl(m: re.Match) -> str:
        return m.group(1) + "[omitted-inline-data]"

    return re.sub(r"(data:[^;]+;base64,)[A-Za-z0-9+/=\s]{1024,}", _repl, content)
