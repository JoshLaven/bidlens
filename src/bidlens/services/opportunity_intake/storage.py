from __future__ import annotations

import os
import logging
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from time import perf_counter
from typing import Iterator

from ... import config


class SourceMaterialStorageError(RuntimeError):
    pass


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StorageObjectMetadata:
    content_length: int
    etag: str | None = None


class SourceMaterialStorage(ABC):
    @abstractmethod
    def put(self, key: str, content: bytes) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    def metadata(self, key: str) -> StorageObjectMetadata:
        return StorageObjectMetadata(content_length=len(self.get(key)))

    def iter_bytes(self, key: str, *, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        content = self.get(key)
        for offset in range(0, len(content), chunk_size):
            yield content[offset : offset + chunk_size]

    def list_keys(self, prefix: str = "") -> Iterator[str]:
        raise SourceMaterialStorageError("This storage backend does not support object listing")


def _validate_storage_key(key: str) -> PurePosixPath:
    value = PurePosixPath(str(key or ""))
    if not str(key or "") or value.is_absolute() or any(part in {"", ".", ".."} for part in value.parts):
        raise SourceMaterialStorageError("Invalid source-material storage key")
    return value


def _storage_log(*, backend: str, operation: str, started: float, success: bool, byte_count: int | None = None) -> None:
    logger.info(
        "source_material_storage_operation backend=%s operation=%s success=%s duration_ms=%.2f bytes=%s",
        backend,
        operation,
        str(success).lower(),
        (perf_counter() - started) * 1000,
        byte_count if byte_count is not None else "unknown",
    )


def cleanup_uploaded_objects(storage: SourceMaterialStorage, keys: list[str] | tuple[str, ...]) -> int:
    failures = 0
    for key in keys:
        try:
            storage.delete(key)
        except Exception:
            failures += 1
    logger.log(
        logging.ERROR if failures else logging.INFO,
        "source_material_orphan_cleanup attempted=%s failures=%s",
        len(keys),
        failures,
    )
    return failures


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
        return _validate_storage_key(key)

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
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
        except Exception:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            raise

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

    def metadata(self, key: str) -> StorageObjectMetadata:
        path = self._path(key)
        try:
            return StorageObjectMetadata(content_length=path.stat().st_size)
        except FileNotFoundError as exc:
            raise SourceMaterialStorageError("Source material object was not found") from exc

    def iter_bytes(self, key: str, *, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        try:
            with self._path(key).open("rb") as handle:
                while chunk := handle.read(chunk_size):
                    yield chunk
        except FileNotFoundError as exc:
            raise SourceMaterialStorageError("Source material object was not found") from exc

    def list_keys(self, prefix: str = "") -> Iterator[str]:
        normalized_prefix = str(prefix or "").strip("/")
        if normalized_prefix:
            _validate_storage_key(normalized_prefix)
        if not self.root.exists():
            return
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            key = path.relative_to(self.root).as_posix()
            if not normalized_prefix or key.startswith(normalized_prefix):
                yield key


class S3SourceMaterialStorage(SourceMaterialStorage):
    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        path_prefix: str = "",
        use_ssl: bool = True,
        client=None,
    ):
        self.bucket = str(bucket or "").strip()
        self.path_prefix = str(path_prefix or "").strip("/")
        if not self.bucket:
            raise SourceMaterialStorageError("S3 source-material bucket is required")
        if self.path_prefix:
            _validate_storage_key(self.path_prefix)
        if client is None:
            if not str(access_key_id or "").strip() or not str(secret_access_key or "").strip():
                raise SourceMaterialStorageError("S3 source-material credentials are incomplete")
            try:
                import boto3
                from botocore.config import Config

                client = boto3.client(
                    "s3",
                    endpoint_url=endpoint_url or None,
                    region_name=region or "us-east-1",
                    aws_access_key_id=access_key_id,
                    aws_secret_access_key=secret_access_key,
                    use_ssl=bool(use_ssl),
                    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
                )
            except Exception:
                raise SourceMaterialStorageError("S3 source-material storage could not be initialized") from None
        self.client = client

    def _object_key(self, key: str) -> str:
        normalized = _validate_storage_key(key).as_posix()
        return f"{self.path_prefix}/{normalized}" if self.path_prefix else normalized

    def _logical_key(self, object_key: str) -> str | None:
        if not self.path_prefix:
            return object_key
        prefix = f"{self.path_prefix}/"
        return object_key[len(prefix) :] if object_key.startswith(prefix) else None

    def put(self, key: str, content: bytes) -> None:
        if not isinstance(content, bytes):
            raise TypeError("Source material content must be bytes")
        started = perf_counter()
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._object_key(key),
                Body=content,
                ContentLength=len(content),
            )
        except Exception:
            _storage_log(backend="s3", operation="put", started=started, success=False, byte_count=len(content))
            raise SourceMaterialStorageError("S3 source-material upload failed") from None
        _storage_log(backend="s3", operation="put", started=started, success=True, byte_count=len(content))

    def get(self, key: str) -> bytes:
        started = perf_counter()
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._object_key(key))
            body = response["Body"]
            content = body.read()
            close = getattr(body, "close", None)
            if close:
                close()
        except Exception:
            _storage_log(backend="s3", operation="get", started=started, success=False)
            raise SourceMaterialStorageError("S3 source-material download failed") from None
        _storage_log(backend="s3", operation="get", started=started, success=True, byte_count=len(content))
        return content

    def delete(self, key: str) -> None:
        started = perf_counter()
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._object_key(key))
        except Exception:
            _storage_log(backend="s3", operation="delete", started=started, success=False)
            raise SourceMaterialStorageError("S3 source-material delete failed") from None
        _storage_log(backend="s3", operation="delete", started=started, success=True)

    def exists(self, key: str) -> bool:
        started = perf_counter()
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._object_key(key))
            result = True
        except Exception as exc:
            response = getattr(exc, "response", {}) or {}
            code = str((response.get("Error") or {}).get("Code") or "")
            status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
            if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
                result = False
            else:
                _storage_log(backend="s3", operation="exists", started=started, success=False)
                raise SourceMaterialStorageError("S3 source-material existence check failed") from None
        _storage_log(backend="s3", operation="exists", started=started, success=True)
        return result

    def metadata(self, key: str) -> StorageObjectMetadata:
        started = perf_counter()
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=self._object_key(key))
            metadata = StorageObjectMetadata(
                content_length=int(response.get("ContentLength") or 0),
                etag=str(response.get("ETag") or "").strip('"') or None,
            )
        except Exception:
            _storage_log(backend="s3", operation="metadata", started=started, success=False)
            raise SourceMaterialStorageError("S3 source-material metadata lookup failed") from None
        _storage_log(backend="s3", operation="metadata", started=started, success=True, byte_count=metadata.content_length)
        return metadata

    def iter_bytes(self, key: str, *, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        started = perf_counter()
        body = None
        byte_count = 0
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._object_key(key))
            body = response["Body"]
            for chunk in body.iter_chunks(chunk_size=chunk_size):
                if chunk:
                    byte_count += len(chunk)
                    yield chunk
        except Exception:
            _storage_log(backend="s3", operation="stream", started=started, success=False, byte_count=byte_count)
            raise SourceMaterialStorageError("S3 source-material streaming download failed") from None
        finally:
            close = getattr(body, "close", None)
            if close:
                close()
        _storage_log(backend="s3", operation="stream", started=started, success=True, byte_count=byte_count)

    def list_keys(self, prefix: str = "") -> Iterator[str]:
        raw_prefix = str(prefix or "")
        logical_prefix = raw_prefix.strip("/")
        if logical_prefix:
            _validate_storage_key(logical_prefix)
        object_prefix = self._object_key(logical_prefix) if logical_prefix else (
            f"{self.path_prefix}/" if self.path_prefix else ""
        )
        if logical_prefix and raw_prefix.endswith("/"):
            object_prefix += "/"
        continuation = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": object_prefix}
            if continuation:
                kwargs["ContinuationToken"] = continuation
            try:
                response = self.client.list_objects_v2(**kwargs)
            except Exception:
                raise SourceMaterialStorageError("S3 source-material object listing failed") from None
            for item in response.get("Contents") or []:
                logical = self._logical_key(str(item.get("Key") or ""))
                if logical:
                    yield logical
            if not response.get("IsTruncated"):
                break
            continuation = response.get("NextContinuationToken")
            if not continuation:
                break


def configured_source_material_storage() -> SourceMaterialStorage:
    backend = config.SOURCE_MATERIAL_STORAGE_BACKEND
    if backend == "local":
        if config.deployment_validation_enabled():
            raise SourceMaterialStorageError(
                "Local source-material storage is not durable in hosted deployments. "
                "Configure the S3 source-material backend before enabling uploads."
            )
        return LocalSourceMaterialStorage(config.SOURCE_MATERIAL_LOCAL_ROOT)
    if backend == "s3":
        return S3SourceMaterialStorage(
            bucket=config.SOURCE_MATERIAL_S3_BUCKET,
            endpoint_url=config.SOURCE_MATERIAL_S3_ENDPOINT_URL,
            region=config.SOURCE_MATERIAL_S3_REGION,
            access_key_id=config.SOURCE_MATERIAL_S3_ACCESS_KEY_ID,
            secret_access_key=config.SOURCE_MATERIAL_S3_SECRET_ACCESS_KEY,
            path_prefix=config.SOURCE_MATERIAL_S3_PATH_PREFIX,
            use_ssl=config.SOURCE_MATERIAL_S3_USE_SSL,
        )
    raise SourceMaterialStorageError(
        f"Unsupported SOURCE_MATERIAL_STORAGE_BACKEND: {backend or '<empty>'}"
    )
