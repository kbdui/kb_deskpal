"""Workspace policies protect trusted tools; no OS isolation is assumed."""

from pathlib import Path

import pytest

from deskpal.sandbox import PathViolation, PermissionDenied, WorkspaceSandbox
from deskpal.tools import PermissionLevel, ToolContext, ToolSpec


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
    assert not (outside / "new.txt").exists()


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
    allowed = ToolContext("task", workspace, frozenset({"sensitive"}))
    assert sandbox.check_permission(spec, allowed) is None
    with pytest.raises(PermissionDenied):
        sandbox.check_permission(spec, ToolContext("new-task", workspace))
    assert list(workspace.iterdir()) == []


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
