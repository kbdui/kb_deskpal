"""Tool registration, validation, audit and file behavior through public APIs."""

import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from deskpal.execution_log import JsonlExecutionLog
from deskpal.sandbox import PathViolation, PermissionDenied, ToolNotFound, WorkspaceSandbox
from deskpal.tools import BuiltinToolManager, PermissionLevel, ToolCall, ToolContext, ToolResult, ToolSpec


class CountingTool:
    def __init__(self, schema=None, *, name="counted", permission=PermissionLevel.AUTOMATIC, error=None):
        self.spec = ToolSpec(name, "test trusted tool", schema or {"type": "object"}, permission)
        self.calls = []
        self.error = error

    def execute(self, arguments, context):
        self.calls.append((dict(arguments), context))
        if self.error is not None:
            raise self.error
        return ToolResult(True, dict(arguments))


@pytest.fixture
def toolkit(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    logger = JsonlExecutionLog(tmp_path / "execution.jsonl")
    manager = BuiltinToolManager(WorkspaceSandbox(workspace, max_file_bytes=12), execution_log=logger)
    manager.register_builtin()
    return manager, workspace, logger


def context(workspace, *, authorized=False):
    return ToolContext("task-1", workspace, frozenset({"write_text"}) if authorized else frozenset())


def test_builtin_discovery_echo_time_and_result_identifiers(toolkit):
    manager, workspace, _ = toolkit
    specs = {spec.name: spec for spec in manager.list_tools()}
    assert set(specs) == {"echo", "time", "read_text", "write_text"}
    assert specs["write_text"].permission_level == PermissionLevel.CONFIRM
    assert all(specs[name].permission_level == PermissionLevel.AUTOMATIC for name in ("echo", "time", "read_text"))
    echoed = manager.execute(ToolCall("echo", {"text": "中文"}, "echo-1"), context(workspace))
    assert echoed.success is True
    assert echoed.output == "中文"
    assert echoed.error is None
    assert (echoed.tool_name, echoed.call_id) == ("echo", "echo-1")
    clock = manager.execute(ToolCall("time", {}, "time-1"), context(workspace))
    assert clock.success is True
    assert datetime.fromisoformat(clock.output).utcoffset() == timezone.utc.utcoffset(None)


def test_unknown_and_duplicate_registration_are_explicit(toolkit):
    manager, workspace, _ = toolkit
    with pytest.raises(ToolNotFound):
        manager.execute(ToolCall("missing", {}), context(workspace))
    with pytest.raises(ToolNotFound):
        manager.check_permission(ToolCall("missing", {}), context(workspace))
    with pytest.raises(ValueError):
        manager.register(CountingTool(name="echo"))
    assert {spec.name for spec in manager.list_tools()} == {"echo", "time", "read_text", "write_text"}


@pytest.mark.parametrize("name,arguments", [
    ("echo", {}), ("echo", {"text": 1}), ("echo", {"text": "x", "extra": True}),
    ("time", {"extra": True}), ("read_text", {}), ("read_text", {"path": False}),
    ("write_text", {"path": "file.txt"}), ("write_text", {"path": "file.txt", "text": False}),
    ("write_text", {"path": "file.txt", "text": "x", "overwrite": 1}),
])
def test_invalid_builtin_arguments_fail_without_file_side_effects(toolkit, name, arguments):
    manager, workspace, _ = toolkit
    result = manager.execute(ToolCall(name, arguments, "invalid-1"), context(workspace, authorized=True))
    assert result.success is False
    assert result.error
    assert (result.tool_name, result.call_id) == (name, "invalid-1")
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize("kind", ["integer", "number"])
@pytest.mark.parametrize("invalid", [True, False, "1", None])
def test_numeric_schema_rejects_boolean_and_wrong_types_before_execution(toolkit, kind, invalid):
    manager, workspace, _ = toolkit
    tool = CountingTool({"type": "object", "properties": {"value": {"type": kind}},
                         "required": ["value"], "additionalProperties": False})
    manager.register(tool)
    result = manager.execute(ToolCall("counted", {"value": invalid}), context(workspace))
    assert result.success is False
    assert result.error
    assert tool.calls == []


@pytest.mark.parametrize("kind,value", [("integer", 1), ("number", 1.5)])
def test_numeric_schema_valid_input_executes_once(toolkit, kind, value):
    manager, workspace, _ = toolkit
    tool = CountingTool({"type": "object", "properties": {"value": {"type": kind}}, "required": ["value"]})
    manager.register(tool)
    result = manager.execute(ToolCall("counted", {"value": value}), context(workspace))
    assert result.success is True
    assert result.output == {"value": value}
    assert len(tool.calls) == 1


def test_registered_schema_and_permission_are_isolated_from_external_changes(toolkit):
    manager, workspace, _ = toolkit
    schema = {"type": "object", "properties": {"value": {"type": "integer"}},
              "required": ["value"], "additionalProperties": False}
    tool = CountingTool(schema, permission=PermissionLevel.CONFIRM)
    manager.register(tool)
    schema["properties"]["value"]["type"] = "boolean"
    tool.spec = ToolSpec("counted", "changed", {"type": "object"}, PermissionLevel.AUTOMATIC)
    exposed = next(spec for spec in manager.list_tools() if spec.name == "counted")
    exposed.parameters["properties"]["value"]["type"] = "boolean"
    current = next(spec for spec in manager.list_tools() if spec.name == "counted")
    assert current.parameters["properties"]["value"]["type"] == "integer"
    assert current.permission_level == PermissionLevel.CONFIRM
    with pytest.raises(PermissionDenied):
        manager.execute(ToolCall("counted", {"value": 1}), context(workspace))
    allowed = ToolContext("task-1", workspace, frozenset({"counted"}))
    assert manager.execute(ToolCall("counted", {"value": True}), allowed).success is False
    assert tool.calls == []
    assert manager.execute(ToolCall("counted", {"value": 1}), allowed).success is True


def test_permission_preflight_and_execution_do_not_create_files_without_authorization(toolkit):
    manager, workspace, logger = toolkit
    call = ToolCall("write_text", {"path": "nested/note.txt", "text": "中文"}, "write-1")
    with pytest.raises(PermissionDenied):
        manager.check_permission(call, context(workspace))
    assert manager.check_permission(call, context(workspace, authorized=True)) is None
    assert list(workspace.iterdir()) == []
    with pytest.raises(PermissionDenied):
        manager.execute(call, context(workspace))
    assert list(workspace.iterdir()) == []
    assert "permission_checked" in logger.path.read_text(encoding="utf-8")


def test_authorized_utf8_write_read_and_explicit_overwrite(toolkit):
    manager, workspace, _ = toolkit
    write = ToolCall("write_text", {"path": "nested/note.txt", "text": "中文🙂"}, "write-1")
    result = manager.execute(write, context(workspace, authorized=True))
    assert result.success is True
    assert result.output == {"path": "nested/note.txt", "bytes_written": 10}
    assert (workspace / "nested" / "note.txt").read_bytes() == "中文🙂".encode("utf-8")
    read = manager.execute(ToolCall("read_text", {"path": "nested/note.txt"}, "read-1"), context(workspace))
    assert read.success is True
    assert read.output == "中文🙂"
    duplicate = manager.execute(write, context(workspace, authorized=True))
    assert duplicate.success is False
    assert (workspace / "nested" / "note.txt").read_text(encoding="utf-8") == "中文🙂"
    overwritten = manager.execute(ToolCall("write_text", {"path": "nested/note.txt", "text": "", "overwrite": True}),
                                  context(workspace, authorized=True))
    assert overwritten.success is True
    assert (workspace / "nested" / "note.txt").read_bytes() == b""


def test_file_size_limit_counts_utf8_bytes_and_preserves_existing_content(toolkit):
    manager, workspace, _ = toolkit
    limit = "中" * 4
    allowed = manager.execute(ToolCall("write_text", {"path": "limit.txt", "text": limit}),
                             context(workspace, authorized=True))
    assert allowed.success is True
    assert allowed.output["bytes_written"] == 12
    assert manager.execute(ToolCall("read_text", {"path": "limit.txt"}), context(workspace)).output == limit
    rejected = manager.execute(ToolCall("write_text", {"path": "limit.txt", "text": limit + "x", "overwrite": True}),
                              context(workspace, authorized=True))
    assert rejected.success is False
    assert (workspace / "limit.txt").read_text(encoding="utf-8") == limit
    new = manager.execute(ToolCall("write_text", {"path": "new/file.txt", "text": limit + "x"}),
                         context(workspace, authorized=True))
    assert new.success is False
    assert not (workspace / "new" / "file.txt").exists()


def test_read_rejects_oversized_missing_and_non_utf8_files(toolkit):
    manager, workspace, _ = toolkit
    (workspace / "large.txt").write_bytes(b"x" * 13)
    (workspace / "invalid.txt").write_bytes(b"\xff")
    for path in ("large.txt", "invalid.txt", "missing.txt"):
        result = manager.execute(ToolCall("read_text", {"path": path}), context(workspace))
        assert result.success is False
        assert result.error


def test_read_remains_bounded_if_file_is_larger_than_stat_report(toolkit, monkeypatch):
    manager, workspace, _ = toolkit
    target = workspace / "growing.txt"
    target.write_bytes(b"x" * 30)
    original_stat = Path.stat
    original_open = Path.open
    read_sizes = []

    def small_stat(path, *args, **kwargs):
        actual = original_stat(path, *args, **kwargs)
        if path == target:
            values = list(actual)
            values[6] = 1
            return os.stat_result(values)
        return actual

    class RecordingBytes(io.BytesIO):
        def read(self, size=-1):
            read_sizes.append(size)
            return super().read(size)

    def recording_open(path, mode="r", *args, **kwargs):
        if path == target and mode == "rb":
            return RecordingBytes(b"x" * 30)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", small_stat)
    monkeypatch.setattr(Path, "open", recording_open)
    result = manager.execute(ToolCall("read_text", {"path": "growing.txt"}), context(workspace))
    assert result.success is False
    assert read_sizes
    assert all(0 <= size <= 13 for size in read_sizes)


def test_tool_path_and_context_violations_are_visible_and_do_not_escape(toolkit, tmp_path):
    manager, workspace, _ = toolkit
    with pytest.raises(PathViolation):
        manager.execute(ToolCall("write_text", {"path": "../escape/file.txt", "text": "x"}),
                        context(workspace, authorized=True))
    assert not (tmp_path / "escape").exists()
    with pytest.raises(PathViolation):
        manager.execute(ToolCall("echo", {"text": "x"}), context(tmp_path))


def test_success_validation_and_execution_failure_are_audited(toolkit):
    manager, workspace, logger = toolkit
    manager.execute(ToolCall("echo", {"text": "ok"}, "call-ok"), context(workspace))
    manager.execute(ToolCall("echo", {}, "call-invalid"), context(workspace))
    tool = CountingTool(error=RuntimeError("internal-stack-detail"))
    manager.register(tool)
    failed = manager.execute(ToolCall("counted", {}, "call-failed"), context(workspace))
    assert failed.success is False
    assert failed.error
    assert "Traceback" not in failed.error
    assert (failed.tool_name, failed.call_id) == ("counted", "call-failed")
    entries = [json.loads(line) for line in logger.path.read_text(encoding="utf-8").splitlines()]
    events = {entry["event"] for entry in entries}
    assert {"permission_checked", "tool_started", "tool_completed", "tool_failed"} <= events
    assert all(entry["task_id"] == "task-1" for entry in entries if entry["event"] != "tool_registered")
    assert "Traceback" not in logger.path.read_text(encoding="utf-8")


def test_audit_failure_propagates_before_tool_side_effect(toolkit):
    _, workspace, _ = toolkit

    class BrokenLog:
        def record(self, event, task_id=None, data=None):
            if event != "tool_registered":
                raise OSError("disk unavailable")

    manager = BuiltinToolManager(WorkspaceSandbox(workspace), execution_log=BrokenLog())
    tool = CountingTool()
    manager.register(tool)
    with pytest.raises(OSError):
        manager.execute(ToolCall("counted", {}), context(workspace))
    assert tool.calls == []


def test_registration_audit_failure_propagates_without_registering_tool(toolkit):
    _, workspace, _ = toolkit

    class BrokenLog:
        def record(self, event, task_id=None, data=None):
            raise OSError("disk unavailable")

    manager = BuiltinToolManager(WorkspaceSandbox(workspace), execution_log=BrokenLog())
    with pytest.raises(OSError):
        manager.register(CountingTool())
    assert manager.list_tools() == ()


def test_failed_atomic_overwrite_preserves_previous_bytes_and_cleans_temp(toolkit, monkeypatch):
    manager, workspace, _ = toolkit
    target = workspace / "note.txt"
    target.write_text("旧内容", encoding="utf-8")
    original = target.read_bytes()
    original_replace = Path.replace

    def fail_replace(path, destination):
        if Path(destination) == target:
            raise OSError("replacement failed")
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "replace", fail_replace)
    result = manager.execute(ToolCall("write_text", {"path": "note.txt", "text": "新内容", "overwrite": True}),
                             context(workspace, authorized=True))
    assert result.success is False
    assert result.error
    assert target.read_bytes() == original
    assert list(workspace.iterdir()) == [target]


@pytest.mark.parametrize("arguments", [{"value": float("nan")}, {"value": float("inf")},
                                       {"value": object()}, {1: "non-string-key"}])
def test_non_json_arguments_fail_without_execution(toolkit, arguments):
    manager, workspace, _ = toolkit
    tool = CountingTool()
    manager.register(tool)
    result = manager.execute(ToolCall("counted", arguments), context(workspace))
    assert result.success is False
    assert tool.calls == []


@pytest.mark.parametrize("arguments", [
    {"values": [True]}, {"values": [0]}, {"values": [1, 2, 3]}, {"values": []},
    {"values": [1], "mode": "forbidden"},
])
def test_supported_nested_array_constraints_are_enforced(toolkit, arguments):
    manager, workspace, _ = toolkit
    tool = CountingTool({"type": "object", "properties": {
        "values": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1, "maxItems": 2},
        "mode": {"type": "string", "enum": ["allowed"]}},
        "required": ["values"], "additionalProperties": False})
    manager.register(tool)
    assert manager.execute(ToolCall("counted", arguments), context(workspace)).success is False
    assert tool.calls == []


def test_supported_array_constraints_accept_boundary_values(toolkit):
    manager, workspace, _ = toolkit
    tool = CountingTool({"type": "object", "properties": {
        "values": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1, "maxItems": 2}},
        "required": ["values"], "additionalProperties": False})
    manager.register(tool)
    assert manager.execute(ToolCall("counted", {"values": [1, 2]}), context(workspace)).success is True
    assert len(tool.calls) == 1


def test_manager_audit_redacts_sensitive_keys_without_changing_executed_arguments(toolkit):
    manager, workspace, logger = toolkit
    tool = CountingTool()
    manager.register(tool)
    arguments = {"password": "fake-private-password", "nested": {"api_key": "fake-private-key"}}
    result = manager.execute(ToolCall("counted", arguments, "secret-call"), context(workspace))
    assert result.success is True
    assert tool.calls[0][0] == arguments
    assert result.output == arguments
    raw = logger.path.read_text(encoding="utf-8")
    assert "fake-private-password" not in raw
    assert "fake-private-key" not in raw
