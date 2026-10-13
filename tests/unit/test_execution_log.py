"""Audit records remain usable, redact credentials, and expose write failures."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from deskpal.execution_log import JsonlExecutionLog, NullExecutionLog


def test_recursive_credential_redaction_preserves_business_data_and_input(tmp_path):
    path = tmp_path / "logs" / "events.jsonl"
    data = {
        "api_key": "fake-key", "Authorization": "fake-auth", "password": "fake-password",
        "nested": [{"secret": "fake-secret", "access_token": "fake-access",
                    "refreshToken": "fake-refresh", "credential": "fake-credential",
                    "token": "fake-token", "token_count": 7, "text": "中文\n第二行"}],
        "sequence": ({"API-KEY": "fake-tuple-key"}, "plain-value"),
        "success": True, "bytes_written": 8,
    }
    original = copy.deepcopy(data)
    JsonlExecutionLog(path).record("tool_completed", "task-1", data)
    raw = path.read_bytes().decode("utf-8")
    assert raw.endswith("\n")
    assert len(raw.splitlines()) == 1
    assert "fake-" not in raw
    entry = json.loads(raw)
    assert set(entry) == {"timestamp", "event", "task_id", "data"}
    assert entry["event"] == "tool_completed"
    assert entry["task_id"] == "task-1"
    assert entry["data"]["nested"][0]["text"] == "中文\n第二行"
    assert entry["data"]["nested"][0]["token_count"] == 7
    assert entry["data"]["sequence"][1] == "plain-value"
    assert entry["data"]["success"] is True
    assert entry["data"]["bytes_written"] == 8
    assert datetime.fromisoformat(entry["timestamp"]).utcoffset() == timezone.utc.utcoffset(None)
    assert data == original


def test_repeated_records_append_and_missing_data_is_an_object(tmp_path):
    path = tmp_path / "events.jsonl"
    logger = JsonlExecutionLog(path)
    logger.record("first")
    logger.record("second", "task-2", {"message": "结果"})
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [entry["event"] for entry in entries] == ["first", "second"]
    assert entries[0]["task_id"] is None
    assert entries[0]["data"] == {}
    assert entries[1]["data"] == {"message": "结果"}


def test_shared_instance_threads_write_complete_unique_jsonl_records(tmp_path):
    path = tmp_path / "events.jsonl"
    logger = JsonlExecutionLog(path)

    def write(index):
        logger.record("tool_started", f"task-{index}", {"index": index, "text": "中文" * 1000})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(80)))
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 80
    assert {entry["task_id"] for entry in entries} == {f"task-{i}" for i in range(80)}
    assert {entry["data"]["index"] for entry in entries} == set(range(80))
    assert all(entry["data"]["text"] == "中文" * 1000 for entry in entries)


@pytest.mark.parametrize("event", ["", "  ", None, 3])
def test_invalid_event_does_not_create_a_file(tmp_path, event):
    path = tmp_path / "events.jsonl"
    with pytest.raises(ValueError):
        JsonlExecutionLog(path).record(event)
    assert not path.exists()


@pytest.mark.parametrize("value,error", [(object(), TypeError), (float("nan"), ValueError),
                                         (float("inf"), ValueError), (float("-inf"), ValueError)])
def test_non_json_data_is_reported_without_partial_record(tmp_path, value, error):
    path = tmp_path / "events.jsonl"
    logger = JsonlExecutionLog(path)
    logger.record("valid", data={"ok": True})
    previous = path.read_bytes()
    with pytest.raises(error):
        logger.record("invalid", data={"value": value})
    assert path.read_bytes() == previous


def test_filesystem_failure_propagates_to_caller(tmp_path):
    parent = tmp_path / "blocked"
    parent.write_text("a file cannot become a directory", encoding="utf-8")
    with pytest.raises(OSError):
        JsonlExecutionLog(parent / "events.jsonl").record("tool_started")
    assert parent.read_text(encoding="utf-8") == "a file cannot become a directory"


def test_null_logger_accepts_business_event():
    assert NullExecutionLog().record("tool_completed", "task-1", {"result": "ok"}) is None
