import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..core.errors import AppError


@dataclass(frozen=True)
class StoredObject:
    object_key: str
    sha256: str
    byte_size: int


class LocalAttachmentStorage:
    """MVP object store with opaque keys and atomic local writes."""

    def __init__(self, root: Path, max_bytes: int = 10 * 1024 * 1024):
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes

    def store(self, *, user_id: str, target_type: str, target_id: str, attachment_id: str, data: bytes) -> StoredObject:
        if not data:
            raise AppError("附件不能为空", code="ATTACHMENT_EMPTY")
        if len(data) > self.max_bytes:
            raise AppError("附件超过大小限制", code="ATTACHMENT_TOO_LARGE", status=413)
        safe_user = self._segment(user_id)
        safe_type = self._segment(target_type)
        safe_target = self._segment(target_id)
        safe_id = self._segment(attachment_id)
        object_key = f"{safe_user}/{safe_type}/{safe_target}/{safe_id}"
        destination = self.resolve(object_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return StoredObject(object_key=object_key, sha256=hashlib.sha256(data).hexdigest(), byte_size=len(data))

    def resolve(self, object_key: str) -> Path:
        path = (self.root / object_key).resolve()
        if path == self.root or self.root not in path.parents:
            raise AppError("附件对象地址无效", code="ATTACHMENT_KEY_INVALID", status=500)
        return path

    @staticmethod
    def _segment(value: str) -> str:
        if not value or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise AppError("附件对象标识无效", code="ATTACHMENT_KEY_INVALID")
        return value
