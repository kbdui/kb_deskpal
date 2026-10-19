"""Source and mocked builder checks only; never compile or launch native code."""

from __future__ import annotations

import ctypes
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import probe_windows_launch as builder

SOURCE = Path(__file__).resolve().parents[1] / "fixtures/windows/native_memory_probe_v3.c"


def _forbid(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("Compilation, launch, profile and native API calls forbidden")


@pytest.fixture(autouse=True)
def _tripwires(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctypes, "WinDLL", _forbid)
    monkeypatch.setattr(subprocess, "run", _forbid)
    monkeypatch.setattr(builder, "WindowsLauncher", _forbid)
    monkeypatch.setattr(builder, "_native_profile_diagnostic", _forbid)
    monkeypatch.setattr(builder, "windows_directory", _forbid)
    monkeypatch.setenv("SystemRoot", "C:/synthetic-windows")


def test_v3_source_has_two_modes_and_an_absolute_commit_ceiling() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert re.findall(r'wcscmp\(argv\[3\], L"([^"]+)"\)', source) == ["smoke", "bounded-memory"]
    assert "if (argc != 4) return 90;" in source
    assert "#define ALLOCATION_BYTES (16UL * 1024UL * 1024UL)" in source
    assert "#define MAX_ALLOCATION_COUNT 32UL" in source
    assert "#define ALLOCATION_CEILING_BYTES (ALLOCATION_BYTES * MAX_ALLOCATION_COUNT)" in source
    assert "while (allocation_count < MAX_ALLOCATION_COUNT)" in source
    assert "MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE" in source
    assert source.count("VirtualAlloc(") == 1
    assert not any(term in source for term in ("CreateProcess", "LoadLibrary", "GetEnvironmentVariable", "argv[4]"))
    assert 32 * 16 * 1024 * 1024 == 512 * 1024 * 1024


def test_v3_captures_first_error_and_frees_all_blocks_before_emit() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert re.search(r"if \(block == NULL\)\s*\{\s*allocation_error = GetLastError\(\);\s*break;", source)
    assert "for (index = allocation_count; index > 0; --index)" in source
    assert "VirtualFree(allocations[index - 1], 0, MEM_RELEASE)" in source
    assert "DWORD observed_error = GetLastError();" in source
    assert "if (!free_failed) free_error = observed_error;" in source
    assert source.index("VirtualFree(") < source.index("sprintf_s(") < source.index("emitted = emit(")
    assert "return freed_count == allocation_count ? 0 : 99;" in source
    assert "if (emitted != 0) return emitted;" in source


def test_v3_only_excluded_handle_query_catches_exact_exception() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert source.count("__try") == source.count("__except") == 1
    protected = source.split("__try {", 1)[1].split("} __except", 1)[0]
    assert protected.strip() == "inherited = GetHandleInformation(excluded, &flags);"
    assert "GetExceptionCode() == 0xC0000008UL" in source
    assert "EXCEPTION_EXECUTE_HANDLER : EXCEPTION_CONTINUE_SEARCH" in source
    assert "if (excluded_exception == 0) excluded_error = GetLastError();" in source


def test_v3_emits_one_distinct_fixed_schema() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    formatting = source.split("sprintf_s(body, sizeof(body),", 1)[1].split('bounded_memory ? "bounded-memory"', 1)[0]
    literals = re.findall(r'"(?:\\.|[^"\\])*"', formatting)
    template = "".join(json.loads(token) for token in literals)
    template = template.replace("%s", "smoke", 1).replace("%s", "false").replace("%lu", "0")
    body = json.loads(template)
    assert body["probe"] == "native_memory_v3" and body["fixture_version"] == 3
    assert set(body) == {"probe", "fixture_version", "mode", "excluded_handle_valid", "excluded_handle_error",
                         "excluded_handle_exception", "allocation_attempted", "allocation_count", "allocation_bytes",
                         "allocation_unit_bytes", "allocation_ceiling_bytes", "allocation_error",
                         "allocation_ceiling_reached", "freed_count", "freed_bytes", "free_error", "all_allocations_freed"}
    assert source.count("emitted = emit(") == 1


def _workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path, Path, str]:
    workspace = tmp_path / "repository"
    source_root = workspace / "tests/fixtures/windows"
    source_root.mkdir(parents=True)
    (source_root / "native_memory_probe_v3.c").write_bytes(SOURCE.read_bytes())
    (source_root / "native_probe_v2.c").write_bytes(b"synthetic preserved v2 source")
    monkeypatch.setattr(builder, "__file__", str(workspace / "scripts/probe_windows_launch.py"))
    compiler = tmp_path / "toolchain/14/bin/Hostx64/x64/cl.exe"
    compiler.parent.mkdir(parents=True)
    compiler.write_bytes(b"synthetic compiler identity")
    sdk, version = tmp_path / "sdk", "10.0.synthetic"
    for relative in (f"Include/{version}/um/windows.h", f"Lib/{version}/um/x64/kernel32.lib"):
        path = sdk / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode("ascii"))
    return workspace, compiler, sdk, version


def _mock_compile(monkeypatch: pytest.MonkeyPatch, calls: list[list[str]], *, returncode: int = 0,
                  mutate_compiler: Path | None = None) -> None:
    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        assert kwargs["timeout"] == 60 and kwargs["capture_output"] is True and kwargs["check"] is False
        assert kwargs["env"]["SystemRoot"] == "C:/synthetic-windows"
        assert kwargs["env"]["TEMP"] == kwargs["env"]["TMP"] == str(kwargs["cwd"])
        executable = Path(next(part[3:] for part in command if part.startswith("/Fe")))
        executable.write_bytes(b"synthetic executable; never run")
        if mutate_compiler is not None:
            mutate_compiler.write_bytes(b"changed toolchain input")
        return subprocess.CompletedProcess(command, returncode, b"synthetic compiler log\n", b"")

    monkeypatch.setattr(subprocess, "run", run)


def test_v3_builder_selects_distinct_source_and_records_provenance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workspace, compiler, sdk, version = _workspace(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    _mock_compile(monkeypatch, calls)
    destination = workspace / "new-v3-build"
    receipt = builder.build_native_fixture(destination, compiler=compiler, sdk_root=sdk,
                                           sdk_version=version, fixture_version=3)
    assert (destination / "fixture.c").read_bytes() == SOURCE.read_bytes()
    assert receipt["repository_source"] == str(workspace / "tests/fixtures/windows/native_memory_probe_v3.c")
    assert receipt["repository_source_sha256"] == receipt["source_sha256"] == sha256(SOURCE.read_bytes()).hexdigest()
    assert receipt["compiler_sha256"] == sha256(compiler.read_bytes()).hexdigest()
    assert receipt["executable_sha256"] == receipt["sha256"] == sha256(b"synthetic executable; never run").hexdigest()
    assert receipt["sdk_root"] == str(sdk) and receipt["sdk_version"] == version
    assert receipt["allocation_ceiling_bytes"] == 512 * 1024 * 1024
    assert receipt["supported_modes"] == ["smoke", "bounded-memory"]
    assert receipt["build_command"] == calls[0]
    assert {"/W4", "/WX", "/MT"} <= set(calls[0])
    assert "/MD" not in calls[0] and len(calls) == 1
    assert json.loads((destination / "build-receipt.json").read_text()) == receipt
    assert (destination / "build.log").read_bytes() == b"synthetic compiler log\n"


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_source_and_default_receipt_remain_preserved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: int) -> None:
    workspace, compiler, sdk, sdk_version = _workspace(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    _mock_compile(monkeypatch, calls)
    options: dict[str, Any] = {} if version == 1 else {"fixture_version": 2}
    receipt = builder.build_native_fixture(workspace / "legacy-build", compiler=compiler,
                                           sdk_root=sdk, sdk_version=sdk_version, **options)
    assert receipt["fixture_version"] == version
    expected = builder.NATIVE_FIXTURE_SOURCE if version == 1 else "synthetic preserved v2 source"
    assert (workspace / "legacy-build/fixture.c").read_text(encoding="utf-8") == expected
    assert "compiler_sha256" not in receipt and "supported_modes" not in receipt


def test_builder_refuses_existing_directory_without_overwrite(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workspace, compiler, sdk, version = _workspace(monkeypatch, tmp_path)
    destination = workspace / "old-build"
    destination.mkdir()
    marker = destination / "fixture.exe"
    marker.write_bytes(b"preserve old artifact")
    with pytest.raises(ValueError, match="NEW directory"):
        builder.build_native_fixture(destination, compiler=compiler, sdk_root=sdk, sdk_version=version, fixture_version=3)
    assert marker.read_bytes() == b"preserve old artifact"


def test_v3_source_reparse_is_rejected_before_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workspace, compiler, sdk, version = _workspace(monkeypatch, tmp_path)
    repository_source = workspace / "tests/fixtures/windows/native_memory_probe_v3.c"
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == repository_source or original(path))
    with pytest.raises(ValueError, match="source ancestors cannot be reparse"):
        builder.build_native_fixture(workspace / "unused", compiler=compiler, sdk_root=sdk,
                                     sdk_version=version, fixture_version=3)
    assert not (workspace / "unused").exists()


@pytest.mark.parametrize("kind", ["destination_ancestor", "source", "compiler"])
def test_junction_attribute_is_checked_without_following_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str,
) -> None:
    workspace, compiler, sdk, version = _workspace(monkeypatch, tmp_path)
    target = {"destination_ancestor": workspace, "source": workspace / "tests/fixtures/windows/native_memory_probe_v3.c",
              "compiler": compiler}[kind]
    original_lstat = Path.lstat
    original_is_symlink = Path.is_symlink

    def lstat(path: Path) -> Any:
        # The target can look like an ordinary directory/file when followed,
        # while its own Windows metadata exposes FILE_ATTRIBUTE_REPARSE_POINT.
        return SimpleNamespace(st_file_attributes=0x400) if path == target else original_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(Path, "is_symlink", lambda path: False if path == target else original_is_symlink(path))
    with pytest.raises(ValueError, match="reparse"):
        builder.build_native_fixture(workspace / "unused", compiler=compiler, sdk_root=sdk,
                                     sdk_version=version, fixture_version=3)
    assert not (workspace / "unused").exists()


@pytest.mark.parametrize("value", [0, 4, True, "3"])
def test_unknown_version_fails_before_compiler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: Any) -> None:
    workspace, compiler, sdk, version = _workspace(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="Unknown native fixture version"):
        builder.build_native_fixture(workspace / "unused", compiler=compiler, sdk_root=sdk,
                                     sdk_version=version, fixture_version=value)
    assert not (workspace / "unused").exists()


def test_failed_build_keeps_log_but_has_no_success_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workspace, compiler, sdk, version = _workspace(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    _mock_compile(monkeypatch, calls, returncode=2)
    destination = workspace / "failed-v3"
    with pytest.raises(RuntimeError, match="build failed"):
        builder.build_native_fixture(destination, compiler=compiler, sdk_root=sdk, sdk_version=version, fixture_version=3)
    assert (destination / "build.log").is_file() and not (destination / "build-receipt.json").exists()


def test_changed_compiler_prevents_frozen_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workspace, compiler, sdk, version = _workspace(monkeypatch, tmp_path)
    calls: list[list[str]] = []
    _mock_compile(monkeypatch, calls, mutate_compiler=compiler)
    destination = workspace / "changed-v3"
    with pytest.raises(RuntimeError, match="input changed during build"):
        builder.build_native_fixture(destination, compiler=compiler, sdk_root=sdk, sdk_version=version, fixture_version=3)
    assert not (destination / "build-receipt.json").exists()


def test_v3_cli_only_dispatches_builder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[int] = []

    def build(destination: Path, *, compiler: Path, sdk_root: Path, sdk_version: str,
              fixture_version: int = 1) -> dict[str, object]:
        calls.append(fixture_version)
        return {"fixture_version": fixture_version}

    monkeypatch.setattr(builder, "build_native_fixture", build)
    monkeypatch.setattr(sys, "argv", ["probe_windows_launch.py", "--build-native-fixture", str(tmp_path),
                                     "--fixture-version", "3"])
    assert builder.main() == 0 and calls == [3]


def test_v3_cli_does_not_enable_generic_memory_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["probe_windows_launch.py", "--fixture-version", "3", "--mode", "memory"])
    with pytest.raises(SystemExit) as failure:
        builder.main()
    assert failure.value.code == 2
