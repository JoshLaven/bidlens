from __future__ import annotations

import os
import re
import uuid
from abc import ABC, abstractmethod
from pathlib import Path, PurePosixPath

from ... import config


class SourceMaterialStorageError(RuntimeError):
    pass


class SourceMaterialStorage(ABC):
    @abstractmethod
    def put(self, key: str, content: bytes) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...


def generate_storage_key(*, organization_id: int, workspace_id: int, draft_id: int) -> str:
    for name, value in (
        ("organization_id", organization_id),
        ("workspace_id", workspace_id),
        ("draft_id", draft_id),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    return f"org-{organization_id}/workspace-{workspace_id}/draft-{draft_id}/{uuid.uuid4().hex}"


def sanitize_original_filename(filename: str | None) -> str:
    value = str(filename or "").replace("\\", "/").split("/")[-1]
    value = re.sub(r"[\x00-\x1f\x7f]", "", value).strip()
    value = re.sub(r"\s+", " ", value)
    if value in {"", ".", ".."}:
        return "upload"
    return value[:180]


class LocalSourceMaterialStorage(SourceMaterialStorage):
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()

    @staticmethod
    def _validate_key(key: str) -> PurePosixPath:
        value = PurePosixPath(str(key or ""))
        if not str(key or "") or value.is_absolute() or any(part in {"", ".", ".."} for part in value.parts):
            raise SourceMaterialStorageError("Invalid source-material storage key")
        return value

    def _path(self, key: str) -> Path:
        relative = self._validate_key(key)
        target = (self.root / Path(*relative.parts)).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise SourceMaterialStorageError("Storage key escapes configured root") from exc
        return target

    def put(self, key: str, content: bytes) -> None:
        if not isinstance(content, bytes):
            raise TypeError("Source material content must be bytes")
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise SourceMaterialStorageError("Storage key already exists") from exc
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        target = self._path(key)
        try:
            target.unlink()
        except FileNotFoundError:
            return

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


def configured_source_material_storage() -> SourceMaterialStorage:
    backend = config.SOURCE_MATERIAL_STORAGE_BACKEND
    if backend != "local":
        raise SourceMaterialStorageError(
            f"Unsupported SOURCE_MATERIAL_STORAGE_BACKEND: {backend or '<empty>'}"
        )
    if config.deployment_validation_enabled():
        raise SourceMaterialStorageError(
            "Local source-material storage is not durable in hosted deployments. "
            "Configure a production object-storage backend before enabling uploads."
        )
    return LocalSourceMaterialStorage(config.SOURCE_MATERIAL_LOCAL_ROOT)
