"""Where the weekly snapshot goes: the private Vercel Blob store, or a local folder.

The Blob store is private: the site reads it through its own function, behind Vercel
Authentication. The token (BLOB_READ_WRITE_TOKEN, from .env) only allows this one store.
The SDK (`vercel-blob`) is imported lazily, so scoring and reports work without it.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Protocol

SNAPSHOT_PATH = "snapshot/latest.json"


class StoreError(RuntimeError):
    """The snapshot could not be stored (missing token, network, refused)."""


class SnapshotStore(Protocol):
    where: str

    def put(self, path: str, data: bytes, content_type: str) -> None: ...


class LocalStore:
    """A folder on disk, for dry runs and tests (`publish --out <folder>`)."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.where = str(self.folder)

    def put(self, path: str, data: bytes, content_type: str) -> None:
        target = (self.folder / path).resolve()
        if self.folder.resolve() not in target.parents:
            raise StoreError(f"refusing to write outside {self.folder}")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)


class VercelBlobStore:
    """The project's private Vercel Blob store."""

    where = "Vercel Blob (private)"

    def __init__(self, token: Optional[str] = None):
        token = (token if token is not None else os.environ.get("BLOB_READ_WRITE_TOKEN", "")).strip()
        if not token:
            raise StoreError("BLOB_READ_WRITE_TOKEN is not set: copy it from Vercel → Storage into .env.")
        if not token.startswith("vercel_blob_rw_"):
            raise StoreError("BLOB_READ_WRITE_TOKEN looks malformed (it starts with 'vercel_blob_rw_').")
        self._token = token

    def put(self, path: str, data: bytes, content_type: str) -> None:
        try:
            from vercel.blob.sync import put_blob  # noqa: PLC0415
        except ImportError as exc:
            raise StoreError("The vercel-blob package is not installed: pip install -r requirements.txt") from exc
        os.environ["BLOB_READ_WRITE_TOKEN"] = self._token  # the SDK reads its credentials from here
        try:
            put_blob(path, data, access="private", content_type=content_type,
                     allow_overwrite=True, cache_control_max_age=60)
        except Exception as exc:  # network, refused token, quota: never echo the token
            raise StoreError(f"Upload to Vercel Blob failed ({exc.__class__.__name__}).") from exc
