"""JSON documents and blobs on the local filesystem, plus an in-memory variant.

Port of packages/core/src/storage.ts. The on-disk layout is unchanged so existing
data directories stay readable: documents at <dir>/<collection>/<id>.json and blobs
at <dir>/blobs/<key>.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import random
import re
import time
import uuid
from typing import Any

from .errors import OvertoneError

_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.-]+")
# Windows can refuse a replace or read while another handle has the file open.
_RENAME_TRIES = 6
_READ_TRIES = 6


def _bad_id(what: str, value: object) -> OvertoneError:
    """Build the 400 bad_id error used for every rejected name, id, key or path."""
    shown = json.dumps(value, ensure_ascii=False, default=str)
    return OvertoneError(f"Invalid {what}: {shown}", status=400, code="bad_id")


def _assert_name(what: str, value: object) -> None:
    """Collections and document ids must be a plain [A-Za-z0-9_-]+ token."""
    if not isinstance(value, str) or _NAME_RE.fullmatch(value) is None:
        raise _bad_id(what, value)


def _blob_segments(key: object) -> list[str]:
    """Validate a blob key such as "sources/<id>.txt" and return its path segments."""
    if not isinstance(key, str) or key == "" or key.startswith("/") or os.path.isabs(key):
        raise _bad_id("blob key", key)
    segments = key.split("/")
    for seg in segments:
        # Reject empty, "." and ".." (and dot-only names, which Windows normalises oddly).
        if seg.strip(".") == "" or _SEGMENT_RE.fullmatch(seg) is None:
            raise _bad_id("blob key", key)
    return segments


def _inside(root: str, *parts: str) -> str:
    """Join parts under root and verify the resolved path stays strictly inside root."""
    full = os.path.abspath(os.path.join(root, *parts))
    try:
        rel = os.path.relpath(full, root)
    except ValueError:  # different drive on Windows
        raise _bad_id("path", "/".join(parts)) from None
    if rel == os.curdir or os.path.isabs(rel) or rel.split(os.sep)[0] == os.pardir:
        raise _bad_id("path", "/".join(parts))
    return full


def _backoff() -> float:
    return random.uniform(0.02, 0.1)


def _read_once(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _read_bytes(path: str) -> bytes | None:
    """Read a file, or None if missing. Retries PermissionError while a replace is in flight."""
    for _ in range(_READ_TRIES - 1):
        try:
            return _read_once(path)
        except PermissionError:
            time.sleep(_backoff())
    return _read_once(path)


def _replace_with_retry(src: str, dst: str) -> None:
    """os.replace with retries: on Windows it can fail while another handle has dst open."""
    for attempt in range(1, _RENAME_TRIES + 1):
        try:
            os.replace(src, dst)
            return
        except OSError:
            if attempt >= _RENAME_TRIES:
                raise
            time.sleep(_backoff())


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _atomic_write(target: str, data: bytes) -> None:
    """Write data to a unique temp file beside target, then replace target with it."""
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = f"{target}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        _replace_with_retry(tmp, target)
    except Exception:
        _unlink_quiet(tmp)
        raise


def _delete_file(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _list_docs(dir_path: str) -> list[dict[str, Any]]:
    """Parse every *.json file in dir_path, skipping unreadable or unparsable ones."""
    try:
        names = sorted(os.listdir(dir_path))
    except FileNotFoundError:
        return []
    out: list[dict[str, Any]] = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            raw = _read_bytes(os.path.join(dir_path, name))
            if raw is None:
                continue
            out.append(json.loads(raw.decode("utf-8")))
        except (OSError, ValueError):
            continue  # unreadable or mid-write: skip
    return out


class FileStorage:
    """JSON documents and blobs on the local filesystem under `directory`."""

    def __init__(self, directory: str) -> None:
        self.directory = os.path.abspath(directory)

    def _doc_path(self, collection: str, id: str) -> str:
        _assert_name("collection", collection)
        _assert_name("id", id)
        return _inside(self.directory, collection, f"{id}.json")

    def _blob_path(self, key: str) -> str:
        return _inside(self.directory, "blobs", *_blob_segments(key))

    async def get(self, collection: str, id: str) -> dict[str, Any] | None:
        """Return the document, or None if it does not exist."""
        path = self._doc_path(collection, id)
        raw = await asyncio.to_thread(_read_bytes, path)
        if raw is None:
            return None
        return json.loads(raw.decode("utf-8"))

    async def list(self, collection: str) -> list[dict[str, Any]]:
        """Return all documents in a collection; corrupt files are skipped."""
        _assert_name("collection", collection)
        dir_path = _inside(self.directory, collection)
        return await asyncio.to_thread(_list_docs, dir_path)

    async def put(self, collection: str, item: dict[str, Any]) -> None:
        """Create or replace the document whose id is item["id"]."""
        path = self._doc_path(collection, item.get("id"))  # type: ignore[arg-type]
        data = json.dumps(item, ensure_ascii=False, indent=2).encode("utf-8")
        await asyncio.to_thread(_atomic_write, path, data)

    async def delete(self, collection: str, id: str) -> None:
        """Delete a document. Deleting a missing document is a no-op."""
        path = self._doc_path(collection, id)
        await asyncio.to_thread(_delete_file, path)

    async def put_blob(self, key: str, data: bytes) -> None:
        """Store raw bytes under key."""
        path = self._blob_path(key)
        await asyncio.to_thread(_atomic_write, path, bytes(data))

    async def get_blob(self, key: str) -> bytes | None:
        """Return the blob bytes, or None if missing."""
        path = self._blob_path(key)
        return await asyncio.to_thread(_read_bytes, path)

    async def delete_blob(self, key: str) -> None:
        """Delete a blob. Deleting a missing blob is a no-op."""
        path = self._blob_path(key)
        await asyncio.to_thread(_delete_file, path)


class MemoryStorage:
    """In-process storage for tests and ephemeral use. Returns and stores deep copies."""

    def __init__(self) -> None:
        self._docs: dict[str, dict[str, dict[str, Any]]] = {}
        self._blobs: dict[str, bytes] = {}

    async def get(self, collection: str, id: str) -> dict[str, Any] | None:
        """Return a copy of the document, or None if it does not exist."""
        _assert_name("collection", collection)
        _assert_name("id", id)
        item = self._docs.get(collection, {}).get(id)
        return None if item is None else copy.deepcopy(item)

    async def list(self, collection: str) -> list[dict[str, Any]]:
        """Return copies of all documents in a collection."""
        _assert_name("collection", collection)
        return [copy.deepcopy(v) for v in self._docs.get(collection, {}).values()]

    async def put(self, collection: str, item: dict[str, Any]) -> None:
        """Store a deep copy of item under its item["id"]."""
        _assert_name("collection", collection)
        doc_id = item.get("id")
        _assert_name("id", doc_id)
        self._docs.setdefault(collection, {})[doc_id] = copy.deepcopy(item)

    async def delete(self, collection: str, id: str) -> None:
        """Delete a document. Deleting a missing document is a no-op."""
        _assert_name("collection", collection)
        _assert_name("id", id)
        self._docs.get(collection, {}).pop(id, None)

    async def put_blob(self, key: str, data: bytes) -> None:
        """Store a copy of the bytes under key."""
        _blob_segments(key)
        self._blobs[key] = bytes(data)

    async def get_blob(self, key: str) -> bytes | None:
        """Return the blob bytes, or None if missing (bytes are immutable, so no copy is needed)."""
        _blob_segments(key)
        return self._blobs.get(key)

    async def delete_blob(self, key: str) -> None:
        """Delete a blob. Deleting a missing blob is a no-op."""
        _blob_segments(key)
        self._blobs.pop(key, None)
