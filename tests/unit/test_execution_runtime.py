"""Runtime staging safety tests; these do not certify OS isolation."""

from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

from deskpal.execution.runtime import (
    _MODULE_FILES, _MODULE_PACKAGES, ProbeProfile, RuntimeManifest, apply_readonly_dacl, readonly_sddl, stage_runtime,
    verify_manifest, worker_environment,
)
import deskpal.execution.runtime as runtime_module


def _fixture_python(root: Path) -> Path:
    root.mkdir()
    for name in ("python.exe", "python312.dll", "vcruntime140.dll"):
        (root / name).write_bytes(b"synthetic executable fixture")
    library = root / "Lib"
    library.mkdir()
    for name in _MODULE_FILES:
        (library / name).write_text("# synthetic stdlib\n", encoding="utf-8")
    for name in _MODULE_PACKAGES:
        (library / name).mkdir()
        (library / name / "__init__.py").write_text("# synthetic package\n", encoding="utf-8")
    (library / "site-packages").mkdir()
    (library / "site-packages" / "private.py").write_text("do not copy\n", encoding="utf-8")
    (root / "DLLs").mkdir()
    for name in ("_ctypes.pyd", "_struct.pyd"):
        (root / "DLLs" / name).write_bytes(b"synthetic native module")
    (root / ".env").write_text("SYNTHETIC_NOT_A_SECRET=omit\n", encoding="utf-8")
    return root / "python.exe"


def test_stage_excludes_packages_config_and_does_not_claim_acl(tmp_path: Path) -> None:
    source = _fixture_python(tmp_path / "source")
    result = stage_runtime(source, tmp_path / "runtime", worker_source="print('synthetic')\n")
    assert result.python_tag == "312"
    assert not result.acl_verified and not result.isolated_startup_verified
    assert not (tmp_path / "runtime" / ".env").exists()
    assert not (tmp_path / "runtime" / "Lib" / "site-packages").exists()
    assert (tmp_path / "runtime" / "python312._pth").read_text() == ".\nLib\n"
    assert len(result.files) > 20
    verify_manifest(result)


def test_stage_refuses_existing_destination_and_source_nested_target(tmp_path: Path) -> None:
    source = _fixture_python(tmp_path / "source")
    target = tmp_path / "existing"
    target.mkdir()
    (target / "sentinel").write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        stage_runtime(source, target, worker_source="")
    assert (target / "sentinel").read_bytes() == b"preserve"
    with pytest.raises(ValueError, match="source Python"):
        stage_runtime(source, source.parent / "runtime", worker_source="")


def test_manifest_rejects_mutation_and_unregistered_files(tmp_path: Path) -> None:
    source = _fixture_python(tmp_path / "source")
    manifest = stage_runtime(source, tmp_path / "runtime", worker_source="pass\n")
    worker = tmp_path / "runtime" / "worker.py"
    worker.write_text("different\n", encoding="utf-8")
    with pytest.raises(ValueError, match="content"):
        verify_manifest(manifest)
    worker.write_text("pass\n", encoding="utf-8")
    (tmp_path / "runtime" / "extra.py").write_bytes(b"do not load")
    with pytest.raises(ValueError, match="file set"):
        verify_manifest(manifest)


def test_missing_required_native_input_refuses_before_copy(tmp_path: Path) -> None:
    source = _fixture_python(tmp_path / "source")
    (source.parent / "DLLs" / "_ctypes.pyd").unlink()
    with pytest.raises(FileNotFoundError, match="native module"):
        stage_runtime(source, tmp_path / "runtime", worker_source="")
    assert not (tmp_path / "runtime").exists()


def test_only_explicit_native_fixture_is_manifested(tmp_path: Path) -> None:
    source = _fixture_python(tmp_path / "source")
    native = tmp_path / "fixture.exe"
    native.write_bytes(b"synthetic native fixture")
    manifest = stage_runtime(source, tmp_path / "runtime", worker_source="pass\n", native_fixture=native)
    assert (tmp_path / "runtime" / "native-fixture.exe").read_bytes() == native.read_bytes()
    assert "native-fixture.exe" in {item.path for item in manifest.files}
    verify_manifest(manifest)


def test_environment_is_explicit_and_has_no_ambient_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", "synthetic-ambient-localappdata-must-not-cross")
    monkeypatch.setenv("SYNTHETIC_API_KEY", "must-not-cross-boundary")
    explicit_local = tmp_path / "local-appdata"
    result = worker_environment(
        tmp_path / "Windows", tmp_path / "readonly", local_appdata=explicit_local,
    )
    assert set(result) == {"SystemRoot", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA"}
    assert result["TEMP"] == str(tmp_path / "readonly")
    assert result["LOCALAPPDATA"] == str(explicit_local)
    assert result["LOCALAPPDATA"] != "synthetic-ambient-localappdata-must-not-cross"
    assert "SYNTHETIC_API_KEY" not in result
    with pytest.raises(ValueError, match="absolute"):
        worker_environment(Path("relative"), tmp_path, local_appdata=explicit_local)


@pytest.mark.parametrize("local_appdata", [Path("relative"), Path(""), Path("nul\0path")])
def test_worker_environment_rejects_invalid_local_appdata(
    tmp_path: Path, local_appdata: Path,
) -> None:
    with pytest.raises(ValueError):
        worker_environment(
            tmp_path / "Windows", tmp_path / "readonly", local_appdata=local_appdata,
        )


@pytest.mark.parametrize("owner,package", [
    ("S-1-5-21-1-2-3-4)(A;;FA;;;WD", "S-1-15-2-1"),
    ("S-1-5-21-1-2-3-4", "S-1-1-0"),
    ("S-1-15-2-1", "S-1-15-2-1"),
])
def test_acl_proposal_refuses_sid_injection_and_non_package_identity(owner: str, package: str) -> None:
    with pytest.raises(ValueError):
        readonly_sddl(owner, package)


def test_acl_proposal_denies_package_mutation_and_retains_host_owner_rights() -> None:
    value = readonly_sddl("S-1-5-21-1-2-3-4", "S-1-15-2-1-2-3")
    assert value.startswith("D:P(D;OICI;0x000d0156;;;S-1-15-2-1-2-3)")
    assert "(A;OICI;RC;;;S-1-3-4)" in value
    assert "(A;OICI;FA;;;S-1-5-21-1-2-3-4)" in value
    assert "S-1-15-2-1)" not in value  # no blanket ALL APPLICATION PACKAGES grant


def test_acl_provisioning_refuses_fabricated_ownership_dtos(tmp_path: Path) -> None:
    owner, package = "S-1-5-21-1-2-3-4", "S-1-15-2-1-2-3"
    fake_runtime = RuntimeManifest(str(tmp_path), str(tmp_path / "python.exe"), "312", "source", ())
    fake_profile = ProbeProfile("deskpal.v06.12345678", package, str(tmp_path))
    for value in (fake_runtime, fake_profile):
        with pytest.raises(ValueError, match="provisioning session"):
            apply_readonly_dacl(value, owner_sid=owner, package_sid=package)


def test_equal_runtime_copy_cannot_authorize_acl_provisioning(tmp_path: Path) -> None:
    source = _fixture_python(tmp_path / "source")
    manifest = stage_runtime(source, tmp_path / "runtime", worker_source="pass\n")
    copied = replace(manifest)
    assert copied == manifest and copied is not manifest
    with pytest.raises(ValueError, match="provisioning session"):
        apply_readonly_dacl(copied, owner_sid="S-1-5-21-1-2-3-4", package_sid="S-1-15-2-1-2-3")


def test_equal_profile_copy_cannot_authorize_native_operations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = ProbeProfile("deskpal.v06.12345678", "S-1-15-2-1-2-3", str(tmp_path / "AC"))
    monkeypatch.setitem(runtime_module._CREATED_PROFILES, profile.name, profile)
    copied = replace(profile)
    for operation in (runtime_module.profile_package_root, runtime_module.delete_probe_profile):
        with pytest.raises(ValueError, match="provisioning session"):
            operation(copied)
    with pytest.raises(ValueError, match="provisioning session"):
        apply_readonly_dacl(copied, owner_sid="S-1-5-21-1-2-3-4", package_sid=profile.sid)


def _load_probe() -> ModuleType:
    script = Path(__file__).resolve().parents[2] / "scripts" / "probe_worker_filesystem.py"
    spec = importlib.util.spec_from_file_location("deskpal_filesystem_probe_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("delete_fails", [False, True])
def test_profile_is_cleaned_when_first_report_write_fails(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delete_fails: bool) -> None:
    """No OS calls: inject failure immediately after simulated profile ownership."""
    probe = _load_probe()
    output = tmp_path / "data" / "validation" / "v06" / "case"
    manifest = RuntimeManifest(str(output / "runtime"), str(output / "runtime" / "python.exe"),
                               "312", str(tmp_path / "source"), ())
    profile = ProbeProfile("deskpal.v06.12345678", "S-1-15-2-1-2-3", str(tmp_path / "profile" / "AC"))
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    monkeypatch.setattr(probe, "stage_runtime", lambda *args, **kwargs: manifest)
    monkeypatch.setattr(probe, "verify_manifest", lambda item: None)
    monkeypatch.setattr(probe, "_windows_directory", lambda: tmp_path / "Windows")
    monkeypatch.setattr(probe, "_owner_sid", lambda item: "S-1-5-21-1-2-3-4")
    monkeypatch.setattr(probe, "create_probe_profile", lambda name: profile)
    deleted: list[ProbeProfile] = []

    def fake_delete(item: ProbeProfile) -> None:
        deleted.append(item)
        if delete_fails:
            raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(probe, "delete_probe_profile", fake_delete)
    monkeypatch.setattr(sys, "argv", ["probe", "--python", str(tmp_path / "source"),
                                     "--root", str(output), "--experiment"])
    original_write = Path.write_text
    writes = 0

    def injected_write(path: Path, data: str, *args: object, **kwargs: object) -> int:
        nonlocal writes
        if path.name == "filesystem-report.json":
            writes += 1
            if writes == 1:
                raise OSError("synthetic first report write failure")
        return original_write(path, data, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", injected_write)
    assert probe.main() == 2
    assert deleted == [profile]
    body = json.loads((output / "filesystem-report.json").read_text(encoding="utf-8"))
    assert body["status"] == "blocked"
    if delete_fails:
        assert body["cleanup"]["status"] == "failed"
    else:
        assert body["cleanup"] == "profile_deleted"
