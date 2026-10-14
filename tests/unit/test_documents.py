"""Document policy tests use temporary files, never user documents."""

import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from deskpal.sandbox import PathViolation, PermissionDenied, WorkspaceSandbox
from deskpal.tools import BuiltinToolManager, PermissionLevel, ToolCall, ToolContext
from deskpal.tools.documents import DocumentClassifyTool, DocumentCopyTool, DocumentDeleteTool, DocumentMoveTool, DocumentSearchTool


@pytest.fixture
def documents(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    sandbox = WorkspaceSandbox(root, max_file_bytes=32)
    manager = BuiltinToolManager(sandbox)
    for tool in (DocumentSearchTool(sandbox), DocumentClassifyTool(sandbox), DocumentCopyTool(sandbox), DocumentMoveTool(sandbox), DocumentDeleteTool(sandbox)):
        manager.register(tool)
    return root, sandbox, manager


def test_schemas_permissions_and_denied_operations_leave_files_intact(documents):
    root, _, manager = documents
    (root / "source.txt").write_bytes(b"source")
    levels = {spec.name: spec.permission_level for spec in manager.list_tools()}
    assert levels == {"document_search": PermissionLevel.AUTOMATIC, "document_classify": PermissionLevel.AUTOMATIC,
                      "document_copy": PermissionLevel.CONFIRM,
                      "document_move": PermissionLevel.HIGH_RISK, "document_delete": PermissionLevel.HIGH_RISK}
    for name, arguments in (("document_copy", {"source": "source.txt", "destination": "new/file.txt"}),
                            ("document_move", {"source": "source.txt", "destination": "new/file.txt"}),
                            ("document_delete", {"path": "source.txt"})):
        with pytest.raises(PermissionDenied):
            manager.execute(ToolCall(name, arguments), ToolContext("task", root))
    assert list(root.iterdir()) == [root / "source.txt"]
    assert (root / "source.txt").read_bytes() == b"source"


def test_search_defaults_directory_substring_sorting_and_truncation(documents):
    root, _, manager = documents
    (root / "nested").mkdir()
    for name in ("z.txt", "a.TXT", "nested/b.txt", "nested/private.bin"):
        (root / name).write_bytes(b"x")
    context = ToolContext("task", root)
    result = manager.execute(ToolCall("document_search", {"query": ".txt", "limit": 2}), context)
    assert result.success
    assert result.output == {"files": [{"path": "a.TXT", "bytes": 1}, {"path": "nested/b.txt", "bytes": 1}],
                             "truncated": True, "scanned_entries": 5}
    nested = manager.execute(ToolCall("document_search", {"path": "nested", "query": ".TXT"}), context)
    assert nested.output["files"] == [{"path": "nested/b.txt", "bytes": 1}]
    assert not nested.output["truncated"]


def test_search_scan_and_depth_are_bounded_without_opening_files(documents, monkeypatch):
    root, sandbox, _ = documents
    (root / "nested").mkdir()
    (root / "a.txt").write_bytes(b"x")
    (root / "nested/b.txt").write_bytes(b"x")
    def no_open(*args, **kwargs):
        raise AssertionError("Search must not read contents")
    monkeypatch.setattr(Path, "open", no_open)
    with pytest.raises(ValueError, match="traversal limit"):
        sandbox.search_files(max_entries=1)
    result = sandbox.search_files(max_depth=0)
    assert result["files"] == [{"path": "a.txt", "bytes": 1}]
    assert result["truncated"]


def test_classification_is_case_insensitive_by_extension_only_without_reading_or_moving(documents, monkeypatch):
    root, _, manager = documents
    cases = {"a.MD": "text", "b.PNG": "image", "c.mp3": "audio", "d.mp4": "video",
             "e.docx": "document", "f.tar.GZ": "archive", "g.unknown": "other", "no_suffix": "other"}
    data = b"not an image or document" * 3
    for name in cases:
        # Contents deliberately disagree with extensions and exceed the read limit.
        (root / name).write_bytes(data)
    def no_open(*args, **kwargs):
        raise AssertionError("Classification must not read or write contents")
    monkeypatch.setattr(Path, "open", no_open)
    result = manager.execute(ToolCall("document_classify", {}), ToolContext("task", root))
    assert result.success
    assert result.output["files"] == [{"path": name, "bytes": len(data), "category": cases[name]} for name in sorted(cases)]
    assert not result.output["truncated"] and result.output["scanned_entries"] == len(cases)
    assert sorted(item.name for item in root.iterdir()) == sorted(cases)


def test_classification_reuses_directory_query_and_result_limits(documents):
    root, _, manager = documents
    (root / "nested").mkdir()
    for name in ("outside.txt", "nested/a.TXT", "nested/b.txt", "nested/c.jpg"):
        (root / name).write_bytes(b"x")
    result = manager.execute(ToolCall("document_classify", {"path": "nested", "query": ".txt", "limit": 1}),
                             ToolContext("task", root))
    assert result.output == {"files": [{"path": "nested/a.TXT", "bytes": 1, "category": "text"}],
                             "truncated": True, "scanned_entries": 3}


def test_classification_mapping_is_injectable_and_isolated_from_changes(documents):
    root, sandbox, _ = documents
    (root / "custom.FOO").write_bytes(b"x")
    mapping = {".FoO": "custom"}
    tool = DocumentClassifyTool(sandbox, extension_categories=mapping)
    mapping[".FoO"] = "mutated"
    result = tool.execute({}, ToolContext("task", root))
    assert result.output["files"] == [{"path": "custom.FOO", "bytes": 1, "category": "custom"}]


@pytest.mark.parametrize("mapping", [{"txt": "text"}, {".txt": ""}, {".txt": 1}, {".tar.gz": "archive"},
                                     {".txt": "text", ".TXT": "other"}, {".bad/name": "text"}])
def test_invalid_classification_mapping_rejected(documents, mapping):
    _, sandbox, _ = documents
    with pytest.raises(ValueError):
        DocumentClassifyTool(sandbox, extension_categories=mapping)


def test_binary_copy_and_explicit_overwrite(documents):
    root, sandbox, manager = documents
    (root / "source.bin").write_bytes(b"\x00\xffbinary")
    context = ToolContext("task", root, frozenset({"document_copy"}))
    arguments = {"source": "source.bin", "destination": "nested/copy.bin"}
    result = manager.execute(ToolCall("document_copy", arguments), context)
    assert result.success
    assert result.output == {"source": "source.bin", "path": "nested/copy.bin", "bytes_written": 8}
    assert sandbox.read_bytes("nested/copy.bin") == b"\x00\xffbinary"
    (root / "source.bin").write_bytes(b"replacement")
    assert not manager.execute(ToolCall("document_copy", arguments), context).success
    assert sandbox.read_bytes("nested/copy.bin") == b"\x00\xffbinary"
    assert manager.execute(ToolCall("document_copy", {**arguments, "overwrite": True}), context).success
    assert sandbox.read_bytes("nested/copy.bin") == b"replacement"


def test_move_delete_single_file_and_overwrite(documents):
    root, sandbox, _ = documents
    (root / "source.txt").write_bytes(b"source")
    (root / "old.txt").write_bytes(b"old")
    with pytest.raises(FileExistsError):
        sandbox.move_file("source.txt", "old.txt")
    assert (root / "source.txt").exists()
    output = sandbox.move_file("source.txt", "nested/moved.txt")
    assert output == {"source": "source.txt", "path": "nested/moved.txt", "bytes_moved": 6, "completed": True}
    assert not (root / "source.txt").exists()
    sandbox.move_file("nested/moved.txt", "old.txt", overwrite=True)
    assert (root / "old.txt").read_bytes() == b"source"
    assert sandbox.delete_file("old.txt") == {"path": "old.txt", "bytes_deleted": 6}
    assert not (root / "old.txt").exists()
    with pytest.raises(PathViolation):
        sandbox.delete_file("nested")
    assert (root / "nested").is_dir()


def test_move_delete_authorization_is_independent_from_copy_confirmation(documents):
    root, _, manager = documents
    (root / "source.txt").write_bytes(b"source")
    ordinary = ToolContext("task", root, frozenset({"document_move", "document_delete"}))
    with pytest.raises(PermissionDenied):
        manager.execute(ToolCall("document_move", {"source": "source.txt", "destination": "new/file.txt"}), ordinary)
    with pytest.raises(PermissionDenied):
        manager.execute(ToolCall("document_delete", {"path": "source.txt"}), ordinary)
    assert not (root / "new").exists()
    high_risk = ToolContext("task", root, high_risk_authorized_tools=frozenset({"document_move", "document_delete"}))
    assert manager.execute(ToolCall("document_move", {"source": "source.txt", "destination": "new/file.txt"}), high_risk).success
    assert manager.execute(ToolCall("document_delete", {"path": "new/file.txt"}), high_risk).success
    assert not (root / "source.txt").exists() and not (root / "new/file.txt").exists()


@pytest.mark.parametrize("operation", ["copy_file", "move_file"])
@pytest.mark.parametrize("bad", ["../escape/file.txt", "NUL.txt", "new.txt:secret", "folder", "..\\bad.txt"])
def test_all_paths_are_validated_before_directory_creation(documents, operation, bad):
    root, sandbox, _ = documents
    (root / "source.txt").write_bytes(b"source")
    (root / "folder").mkdir()
    with pytest.raises(PathViolation):
        getattr(sandbox, operation)("source.txt", bad)
    with pytest.raises(PathViolation):
        getattr(sandbox, operation)(bad, "new/file.txt")
    assert not (root / "new").exists()
    assert (root / "source.txt").read_bytes() == b"source"


@pytest.mark.parametrize("operation", ["copy_file", "move_file"])
def test_size_limit_and_same_file_rejection_have_no_side_effect(documents, operation):
    root, sandbox, _ = documents
    (root / "large.bin").write_bytes(b"x" * 33)
    with pytest.raises(ValueError):
        getattr(sandbox, operation)("large.bin", "new/file.bin")
    assert not (root / "new").exists()
    with pytest.raises(ValueError):
        getattr(sandbox, operation)("large.bin", "large.bin", overwrite=True)
    with pytest.raises(ValueError):
        sandbox.delete_file("large.bin")
    assert (root / "large.bin").read_bytes() == b"x" * 33


@pytest.mark.parametrize("operation", ["copy_file", "move_file"])
def test_failed_overwrite_preserves_both_files_and_cleans_staging(documents, monkeypatch, operation):
    root, sandbox, _ = documents
    (root / "source.bin").write_bytes(b"new")
    (root / "destination.bin").write_bytes(b"old")
    def fail_replace(*args, **kwargs):
        raise OSError("replacement failed")
    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError):
        getattr(sandbox, operation)("source.bin", "destination.bin", overwrite=True)
    assert (root / "source.bin").read_bytes() == b"new"
    assert (root / "destination.bin").read_bytes() == b"old"
    assert sorted(item.name for item in root.iterdir()) == ["destination.bin", "source.bin"]


def test_move_partial_source_removal_failure_reports_created_destination(documents, monkeypatch):
    root, sandbox, _ = documents
    (root / "source.bin").write_bytes(b"new")
    original = Path.unlink
    def fail_source(path, *args, **kwargs):
        if path == root / "source.bin":
            raise OSError("cannot remove source")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_source)
    tool = DocumentMoveTool(sandbox)
    result = tool.execute({"source": "source.bin", "destination": "destination.bin"}, ToolContext("task", root))
    assert not result.success
    assert result.output["destination_created"] and not result.output["source_removed"]
    assert (root / "source.bin").read_bytes() == (root / "destination.bin").read_bytes() == b"new"


def test_move_path_violation_after_publication_reports_destination_residue(documents, monkeypatch):
    root, sandbox, _ = documents
    (root / "source.bin").write_bytes(b"new")
    original = sandbox.resolve_path
    def changed_source(path, *args, **kwargs):
        if path == "source.bin" and (root / "destination.bin").exists():
            raise PathViolation("Source path changed after destination publication")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(sandbox, "resolve_path", changed_source)
    result = DocumentMoveTool(sandbox).execute({"source": "source.bin", "destination": "destination.bin"},
                                             ToolContext("task", root))
    assert not result.success
    assert result.output["completed"] is False and result.output["destination_created"] is True
    assert result.output["source_removed"] is False
    assert (root / "source.bin").read_bytes() == (root / "destination.bin").read_bytes() == b"new"


def test_link_components_rejected_and_search_does_not_follow(documents, tmp_path):
    root, sandbox, _ = documents
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_bytes(b"private")
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Platform cannot create symlinks")
    try:
        _assert_unsafe_link(sandbox, root, outside)
    finally:
        link.unlink()


def _assert_unsafe_link(sandbox, root, outside):
    assert sandbox.search_files()["files"] == []
    for action in (lambda: sandbox.copy_file("linked/private.txt", "new/file.txt"),
                   lambda: sandbox.move_file("linked/private.txt", "new/file.txt"),
                   lambda: sandbox.delete_file("linked/private.txt"),
                   lambda: sandbox.resolve_directory("linked")):
        with pytest.raises(PathViolation):
            action()
    assert not (root / "new").exists()
    assert (outside / "private.txt").read_bytes() == b"private"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction policy")
def test_real_windows_junction_is_skipped_and_never_mutated(documents, tmp_path):
    root, sandbox, _ = documents
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_bytes(b"private")
    junction = root / "linked"
    if any(char in str(path) for path in (junction, outside) for char in '%!&|<>\r\n'):
        pytest.skip("Temporary paths have shell metacharacters")
    try:
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                                capture_output=True, timeout=10, check=False)
        if result.returncode:
            pytest.skip("Cannot create junction")
        assert junction.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        _assert_unsafe_link(sandbox, root, outside)
    finally:
        if os.path.lexists(junction):
            os.rmdir(junction)
