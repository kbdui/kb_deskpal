"""Workspace policies protect trusted tools; no OS isolation is assumed."""

from pathlib import Path
import os
import stat
import subprocess
import sys

import pytest

from deskpal.sandbox import PathViolation, PermissionDenied, WorkspaceSandbox
from deskpal.tools import BuiltinToolManager, PermissionLevel, ToolCall, ToolContext, ToolSpec


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def test_relative_paths_resolve_inside_workspace_without_creating_parents(workspace):
    sandbox = WorkspaceSandbox(workspace)
    assert sandbox.resolve_path("nested/note.txt", for_write=True) == workspace / "nested" / "note.txt"
    assert list(workspace.iterdir()) == []
    (workspace / "note.txt").write_text("中文", encoding="utf-8")
    assert sandbox.resolve_path("note.txt") == workspace / "note.txt"


@pytest.mark.parametrize("path", ["../outside.txt", "nested/../../outside.txt", "..\\outside.txt",
                                  "", ".", "CON", "NUL.txt", "nested/COM1.log", "LPT9",
                                  "note.txt:stream", "nested/file.txt:secret"])
def test_invalid_paths_fail_without_filesystem_side_effects(workspace, path):
    with pytest.raises(PathViolation):
        WorkspaceSandbox(workspace).resolve_path(path, for_write=True)
    assert list(workspace.iterdir()) == []


def test_absolute_and_adjacent_prefix_paths_are_rejected(workspace):
    sandbox = WorkspaceSandbox(workspace)
    sibling = workspace.parent / (workspace.name + "-neighbor")
    sibling.mkdir()
    for path in (workspace / "inside.txt", sibling / "outside.txt"):
        with pytest.raises(PathViolation):
            sandbox.resolve_path(str(path), for_write=True)
    assert not (sibling / "outside.txt").exists()


def test_directory_target_is_rejected(workspace):
    (workspace / "nested").mkdir()
    for for_write in (False, True):
        with pytest.raises(PathViolation):
            WorkspaceSandbox(workspace).resolve_path("nested", for_write=for_write)


def test_symlink_escape_is_rejected_if_platform_allows_links(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "existing.txt").write_text("private", encoding="utf-8")
    link = workspace / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"Cannot create symlink: {type(error).__name__}, "
                    f"winerror={getattr(error, 'winerror', None)}, errno={getattr(error, 'errno', None)}")
    sandbox = WorkspaceSandbox(workspace)
    with pytest.raises(PathViolation):
        sandbox.resolve_path("escape/existing.txt")
    with pytest.raises(PathViolation):
        sandbox.resolve_path("escape/new.txt", for_write=True)
    _check_linked_file_tools(sandbox, workspace, outside)
    assert not (outside / "new.txt").exists()


def _check_linked_file_tools(sandbox, workspace, outside):
    """Exercise real manager permission and I/O paths through a real link."""
    manager = BuiltinToolManager(sandbox)
    manager.register_builtin()
    normal = ToolContext("link-test", workspace)
    write = ToolCall("write_text", {"path": "escape/new.txt", "text": "must-not-write"}, "write")
    with pytest.raises(PathViolation):
        manager.execute(ToolCall("read_text", {"path": "escape/existing.txt"}, "read"), normal)
    with pytest.raises(PermissionDenied):
        manager.execute(write, normal)
    authorized = ToolContext("link-test", workspace, frozenset({"write_text"}))
    with pytest.raises(PathViolation):
        manager.execute(write, authorized)
    assert not (outside / "new.txt").exists()
    assert (outside / "existing.txt").read_text(encoding="utf-8") == "private"


@pytest.mark.skipif(sys.platform != "win32", reason="Real junction creation requires Windows")
def test_windows_junction_escape_rejects_actual_read_and_authorized_write(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "existing.txt").write_text("private", encoding="utf-8")
    junction = workspace / "escape"
    # cmd's built-in is required for mklink. Pass a fixed argv list and only
    # controlled temporary paths; reject metacharacters instead of interpolating.
    if any(char in str(path) for path in (junction, outside) for char in '%!&|<>\r\n'):
        pytest.skip("Temporary paths contain unsupported cmd metacharacters")
    try:
        try:
            created = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                                     capture_output=True, text=True, errors="replace", timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            pytest.skip(f"Cannot create real junction: {type(error).__name__}, "
                        f"winerror={getattr(error, 'winerror', None)}, errno={getattr(error, 'errno', None)}")
        if created.returncode != 0:
            detail = (created.stderr or created.stdout).strip()
            pytest.skip(f"mklink /J failed with exit {created.returncode}: {detail}")
        assert junction.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        assert junction.resolve() == outside.resolve()
        sandbox = WorkspaceSandbox(workspace)
        with pytest.raises(PathViolation):
            sandbox.resolve_path("escape/existing.txt")
        with pytest.raises(PathViolation):
            sandbox.resolve_path("escape/new.txt", for_write=True)
        _check_linked_file_tools(sandbox, workspace, outside)
    finally:
        # Nonrecursive rmdir removes only the junction itself on Windows. Never
        # pass this link to recursive cleanup that could traverse its target.
        if os.path.lexists(junction):
            os.rmdir(junction)
    assert outside.is_dir()
    assert (outside / "existing.txt").read_text(encoding="utf-8") == "private"


def test_resolved_path_membership_rejects_neighbor_with_same_prefix(workspace, monkeypatch):
    sandbox = WorkspaceSandbox(workspace)
    neighbor = workspace.parent / (workspace.name + "-neighbor")
    neighbor.mkdir()
    actual = neighbor / "private.txt"
    actual.write_text("private", encoding="utf-8")
    requested = workspace / "linked.txt"
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        return actual if path == requested else original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(PathViolation):
        sandbox.resolve_path("linked.txt")


@pytest.mark.parametrize("level", [PermissionLevel.CONFIRM, PermissionLevel.HIGH_RISK])
def test_permission_requires_specific_tool_in_current_context(workspace, level):
    sandbox = WorkspaceSandbox(workspace)
    spec = ToolSpec("sensitive", "demo", {"type": "object"}, level)
    denied = ToolContext("task", workspace, frozenset({"other_tool"}))
    with pytest.raises(PermissionDenied):
        sandbox.check_permission(spec, denied)
    allowed = (ToolContext("task", workspace, high_risk_authorized_tools=frozenset({"sensitive"}))
               if level == PermissionLevel.HIGH_RISK else ToolContext("task", workspace, frozenset({"sensitive"})))
    assert sandbox.check_permission(spec, allowed) is None
    with pytest.raises(PermissionDenied):
        sandbox.check_permission(spec, ToolContext("new-task", workspace))
    assert list(workspace.iterdir()) == []


def test_level_one_and_high_risk_authorizations_are_independent(workspace):
    sandbox = WorkspaceSandbox(workspace)
    low = ToolSpec("low", "test", {"type": "object"}, PermissionLevel.CONFIRM)
    high = ToolSpec("high", "test", {"type": "object"}, PermissionLevel.HIGH_RISK)
    with pytest.raises(PermissionDenied):
        sandbox.check_permission(high, ToolContext("task", workspace, frozenset({"high"})))
    with pytest.raises(PermissionDenied):
        sandbox.check_permission(low, ToolContext("task", workspace,
                                                high_risk_authorized_tools=frozenset({"low"})))
    sandbox.check_permission(high, ToolContext("task", workspace, high_risk_authorized_tools=frozenset({"high"})))


@pytest.mark.parametrize("field", ["authorized_tools", "high_risk_authorized_tools"])
def test_invalid_grant_containers_cannot_authorize_high_risk_tools(workspace, field):
    sandbox = WorkspaceSandbox(workspace)
    high = ToolSpec("danger", "test", {"type": "object"}, PermissionLevel.HIGH_RISK)
    for invalid in ("danger", ("danger",), {"danger"}, ["danger"], frozenset({""}), frozenset({1})):
        grants = {"authorized_tools": frozenset(), "high_risk_authorized_tools": frozenset({"danger"})}
        grants[field] = invalid
        with pytest.raises(PermissionDenied):
            sandbox.check_permission(high, ToolContext("task", workspace, **grants))


def test_automatic_tool_is_allowed_and_context_cannot_change_workspace(workspace, tmp_path):
    sandbox = WorkspaceSandbox(workspace)
    spec = ToolSpec("echo", "demo", {"type": "object"})
    assert sandbox.check_permission(spec, ToolContext("task", workspace)) is None
    with pytest.raises(PathViolation):
        sandbox.check_permission(spec, ToolContext("task", tmp_path))


def test_equivalent_workspace_context_resolves_to_same_root(workspace):
    spec = ToolSpec("echo", "demo", {"type": "object"})
    assert WorkspaceSandbox(workspace).check_permission(
        spec, ToolContext("task", workspace / "nested" / "..")) is None


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_file_size_limit_is_rejected(workspace, limit):
    with pytest.raises(ValueError):
        WorkspaceSandbox(workspace, max_file_bytes=limit)
