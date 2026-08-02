import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .password import normalize_username


class PasswordEscrowStore:
    """Explicit development-only plaintext escrow for administrator handoff."""

    SCHEMA_VERSION = 1

    def __init__(self, path: Path, *, enabled: bool, app_mode: str):
        if enabled and app_mode == "production":
            raise RuntimeError("生产环境禁止启用密码明文托管")
        self.path = path
        self.enabled = enabled

    def record(self, *, username: str, password: str) -> None:
        self._require_enabled()
        payload = self._load_or_empty()
        payload["accounts"][normalize_username(username)] = {
            "password": password,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        self._save(payload)

    def reveal(self, *, username: str) -> str:
        self._require_enabled()
        account = self._load_or_empty()["accounts"].get(
            normalize_username(username)
        )
        if not account:
            raise ValueError("没有该账号的托管密码；请重置密码后再查看")
        return account["password"]

    def purge(self) -> bool:
        if not self.path.exists():
            return False
        self.path.unlink()
        return True

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise RuntimeError(
                "密码托管未启用；仅可在非生产环境显式设置 "
                "PASSWORD_ESCROW_ENABLED=true"
            )

    def _load_or_empty(self) -> dict:
        if not self.path.exists():
            return {
                "schemaVersion": self.SCHEMA_VERSION,
                "classification": "development-password-escrow",
                "accounts": {},
            }
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("密码托管文件损坏，拒绝继续操作") from error
        if (
            payload.get("schemaVersion") != self.SCHEMA_VERSION
            or payload.get("classification")
            != "development-password-escrow"
            or not isinstance(payload.get("accounts"), dict)
        ):
            raise RuntimeError("密码托管文件格式无效，拒绝继续操作")
        for account in payload["accounts"].values():
            if not isinstance(account, dict) or not isinstance(
                account.get("password"), str
            ):
                raise RuntimeError("密码托管记录格式无效，拒绝继续操作")
        os.chmod(self.path, 0o600)
        return payload

    def _save(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".password-escrow-",
            dir=self.path.parent,
            text=True,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise
