"""Tests for overtone.storage (port of packages/core/test/storage.test.ts)."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import pytest

from overtone.errors import OvertoneError
from overtone.storage import FileStorage, MemoryStorage


@pytest.fixture(params=["memory", "file"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> MemoryStorage | FileStorage:
    if request.param == "memory":
        return MemoryStorage()
    return FileStorage(str(tmp_path / "store"))


async def assert_bad_id(awaitable: Awaitable[Any]) -> None:
    with pytest.raises(OvertoneError) as exc:
        await awaitable
    assert exc.value.status == 400
    assert exc.value.code == "bad_id"


# ── Shared behaviour (run against both MemoryStorage and FileStorage) ──


async def test_put_get_roundtrip(store: Any) -> None:
    await store.put("notebooks", {"id": "n1", "title": "Hello"})
    assert await store.get("notebooks", "n1") == {"id": "n1", "title": "Hello"}


async def test_get_missing_returns_none(store: Any) -> None:
    assert await store.get("notebooks", "does-not-exist") is None


async def test_list_on_empty_collection_returns_empty(store: Any) -> None:
    assert await store.list("empty-collection") == []


async def test_list_returns_all_items_in_a_collection(store: Any) -> None:
    await store.put("lst", {"id": "a", "title": "A"})
    await store.put("lst", {"id": "b", "title": "B"})
    await store.put("lst", {"id": "c", "title": "C"})
    items = await store.list("lst")
    assert sorted(d["id"] for d in items) == ["a", "b", "c"]


async def test_delete_removes_document_and_missing_is_noop(store: Any) -> None:
    await store.put("del", {"id": "x", "title": "X"})
    await store.delete("del", "x")
    assert await store.get("del", "x") is None
    await store.delete("del", "x")
    await store.delete("never-existed", "nope")


async def test_overwrite_replaces_existing_document(store: Any) -> None:
    await store.put("ow", {"id": "o", "title": "first"})
    await store.put("ow", {"id": "o", "title": "second", "tags": ["t"]})
    assert await store.get("ow", "o") == {"id": "o", "title": "second", "tags": ["t"]}
    assert len(await store.list("ow")) == 1


async def test_blob_roundtrip_with_binary_bytes(store: Any) -> None:
    data = bytes([0, 1, 127, 128, 254, 255, 0, 255])
    await store.put_blob("audio/bin.mp3", data)
    got = await store.get_blob("audio/bin.mp3")
    assert isinstance(got, bytes)
    assert got == data


async def test_blob_get_missing_returns_none_and_delete_missing_is_noop(store: Any) -> None:
    assert await store.get_blob("audio/none.mp3") is None
    await store.delete_blob("audio/none.mp3")


async def test_delete_blob_removes_blob(store: Any) -> None:
    await store.put_blob("sources/gone.txt", bytes([1, 2, 3]))
    await store.delete_blob("sources/gone.txt")
    assert await store.get_blob("sources/gone.txt") is None


async def test_nested_blob_keys_roundtrip(store: Any) -> None:
    await store.put_blob("a/b/c/deep.txt", "hello nested".encode())
    got = await store.get_blob("a/b/c/deep.txt")
    assert got is not None
    assert got.decode() == "hello nested"


async def test_put_without_valid_id_raises_bad_id(store: Any) -> None:
    await assert_bad_id(store.put("notebooks", {"title": "no id"}))
    await assert_bad_id(store.put("notebooks", {"id": 42, "title": "int id"}))


# ── MemoryStorage copy isolation ──


async def test_memory_mutating_returned_document_does_not_change_state() -> None:
    s = MemoryStorage()
    await s.put("notebooks", {"id": "n", "title": "orig", "tags": ["a"]})
    got = await s.get("notebooks", "n")
    assert got is not None
    got["title"] = "mutated"
    got["tags"].append("b")
    assert await s.get("notebooks", "n") == {"id": "n", "title": "orig", "tags": ["a"]}
    listed = await s.list("notebooks")
    listed[0]["title"] = "mutated-again"
    stored = await s.get("notebooks", "n")
    assert stored is not None
    assert stored["title"] == "orig"


async def test_memory_mutating_put_input_does_not_change_state() -> None:
    s = MemoryStorage()
    item: dict[str, Any] = {"id": "n", "title": "orig"}
    await s.put("notebooks", item)
    item["title"] = "changed"
    stored = await s.get("notebooks", "n")
    assert stored is not None
    assert stored["title"] == "orig"


async def test_memory_mutating_blob_bytes_does_not_change_state() -> None:
    s = MemoryStorage()
    source = bytearray([1, 2, 3])
    await s.put_blob("sources/x.txt", bytes(source))
    source[0] = 99
    out = await s.get_blob("sources/x.txt")
    assert out is not None and out[0] == 1
    out2 = await s.get_blob("sources/x.txt")
    assert out2 is not None and out2[1] == 2


# ── FileStorage path safety ──


async def test_rejects_traversal_in_blob_keys(tmp_path: Path) -> None:
    s = FileStorage(str(tmp_path))
    await assert_bad_id(s.put_blob("../x", b"\x01"))
    await assert_bad_id(s.put_blob("a/../../b", b"\x01"))
    await assert_bad_id(s.get_blob("../x"))
    await assert_bad_id(s.delete_blob("a/../b"))
    await assert_bad_id(s.put_blob("/abs/path.txt", b"\x01"))
    await assert_bad_id(s.put_blob("", b"\x01"))
    await assert_bad_id(s.put_blob("a//b", b"\x01"))
    await assert_bad_id(s.put_blob("a/./b", b"\x01"))


async def test_rejects_ids_with_slash_or_unsafe_characters(tmp_path: Path) -> None:
    s = FileStorage(str(tmp_path))
    await assert_bad_id(s.put("notebooks", {"id": "a/b", "title": "x"}))
    await assert_bad_id(s.put("notebooks", {"id": "../escape", "title": "x"}))
    await assert_bad_id(s.get("notebooks", "a/b"))
    await assert_bad_id(s.delete("notebooks", "a/b"))
    await assert_bad_id(s.put("bad/collection", {"id": "ok", "title": "x"}))
    await assert_bad_id(s.list("../etc"))


async def test_does_not_write_outside_storage_dir(tmp_path: Path) -> None:
    root = tmp_path / "store"
    s = FileStorage(str(root))
    with pytest.raises(OvertoneError):
        await s.put("notebooks", {"id": "../escape", "title": "x"})
    assert not (tmp_path / "escape.json").exists()


# ── FileStorage on disk ──


async def test_stores_documents_and_blobs_in_documented_layout(tmp_path: Path) -> None:
    s = FileStorage(str(tmp_path))
    await s.put("notebooks", {"id": "n1", "title": "T"})
    raw = json.loads((tmp_path / "notebooks" / "n1.json").read_text(encoding="utf-8"))
    assert raw == {"id": "n1", "title": "T"}
    await s.put_blob("sources/s1.txt", bytes([65]))
    assert (tmp_path / "blobs" / "sources" / "s1.txt").read_bytes() == bytes([65])


async def test_file_storage_directory_is_absolute(tmp_path: Path) -> None:
    s = FileStorage(str(tmp_path / "rel" / ".." / "store"))
    assert Path(s.directory).is_absolute()


async def test_list_skips_corrupt_json_files(tmp_path: Path) -> None:
    s = FileStorage(str(tmp_path))
    await s.put("notebooks", {"id": "good", "title": "G"})
    (tmp_path / "notebooks" / "broken.json").write_text("{not json", encoding="utf-8")
    assert [d["id"] for d in await s.list("notebooks")] == ["good"]


async def test_concurrent_puts_of_same_id_resolve_and_final_file_parses(tmp_path: Path) -> None:
    s = FileStorage(str(tmp_path))
    writes = [
        s.put("notebooks", {"id": "same", "title": f"v{i}", "tags": [str(i)]}) for i in range(50)
    ]
    await asyncio.gather(*writes)
    raw = json.loads((tmp_path / "notebooks" / "same.json").read_text(encoding="utf-8"))
    assert raw["id"] == "same"
    assert re.fullmatch(r"v\d+", raw["title"])
    # No temp files left behind.
    assert sorted(p.name for p in (tmp_path / "notebooks").iterdir()) == ["same.json"]


async def test_concurrent_get_and_put_same_id_never_return_partial_document(tmp_path: Path) -> None:
    s = FileStorage(str(tmp_path))
    writes = [
        s.put("notebooks", {"id": "race", "title": f"v{i}", "tags": [str(i)] * 200}) for i in range(30)
    ]
    reads = [s.get("notebooks", "race") for _ in range(30)]
    gathered = await asyncio.gather(*writes, *reads)
    for doc in gathered[len(writes):]:
        if doc is None:
            continue
        assert doc["id"] == "race"
        assert re.fullmatch(r"v\d+", doc["title"])
        assert len(doc["tags"]) == 200
    final = await s.get("notebooks", "race")
    assert final is not None and re.fullmatch(r"v\d+", final["title"])
