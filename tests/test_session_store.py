from __future__ import annotations

import json
from pathlib import Path

import pytest

from lancher_code.session_store import ProjectSessionStore, SessionStoreError


def _records(name: str, project: Path, *, message_count: int = 2) -> list[dict[str, object]]:
    return [
        {
            "type": "metadata",
            "version": 1,
            "name": name,
            "project_root": str(project.resolve()),
            "created_at": "2026-07-17T10:00:00+00:00",
            "updated_at": "2026-07-17T11:00:00+00:00",
            "message_count": message_count,
        }
    ]


def test_store_round_trip_list_rename_and_remove(tmp_path: Path) -> None:
    store = ProjectSessionStore(tmp_path)
    store.save("中文_session-1", _records("中文_session-1", tmp_path))

    assert store.load("中文_session-1")[0]["name"] == "中文_session-1"
    assert store.list_sessions()[0].message_count == 2

    store.rename("中文_session-1", "新名字")
    assert store.exists("新名字")
    assert store.load("新名字")[0]["name"] == "新名字"
    store.remove("新名字")
    assert store.list_sessions() == []


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "has space", "x.jsonl"])
def test_store_rejects_unsafe_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(SessionStoreError):
        ProjectSessionStore(tmp_path).exists(name)


def test_store_reports_corrupt_jsonl(tmp_path: Path) -> None:
    path = tmp_path / ".lancher" / "session" / "broken.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(SessionStoreError, match="读取会话失败"):
        ProjectSessionStore(tmp_path).load("broken")


def test_store_writes_one_json_object_per_line(tmp_path: Path) -> None:
    store = ProjectSessionStore(tmp_path)
    records = _records("demo", tmp_path) + [{"type": "state", "data": {}}]
    store.save("demo", records)

    lines = (store.session_dir / "demo.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == records
