"""Stage a narrow Windows Python probe runtime; never imply ACL verification.

Provisioning functions change only newly created project-owned probe objects.
Callers must obtain the required OS experiment approval before invoking them.
This is a stage-0 experiment, not a production sandbox or runtime distributor.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat


_MODULE_FILES = (
    "_collections_abc.py", "abc.py", "codecs.py", "copyreg.py", "enum.py",
    "functools.py", "genericpath.py", "io.py", "keyword.py", "ntpath.py",
    "operator.py", "os.py", "reprlib.py", "stat.py", "struct.py", "types.py",
    "warnings.py", "zipimport.py",
)
_MODULE_PACKAGES = ("collections", "ctypes", "encodings", "json", "re")
_SID = re.compile(r"S-1-(?:[0-9]+-)*[0-9]+\Z", re.ASCII)
_PROFILE_NAME = re.compile(r"deskpal\.v06\.[a-z0-9]{8,40}\Z", re.ASCII)


@dataclass(frozen=True)
class RuntimeFile:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class RuntimeManifest:
    root: str
    executable: str
    python_tag: str
    source_executable: str
    files: tuple[RuntimeFile, ...]
    acl_verified: bool = False
    isolated_startup_verified: bool = False


@dataclass(frozen=True)
class ProbeProfile:
    name: str
    sid: str
    folder: str


_STAGED_ROOTS: dict[str, RuntimeManifest] = {}
_CREATED_PROFILES: dict[str, ProbeProfile] = {}


def _plain_file(path: Path) -> None:
    """Do not stage links/reparse objects or special files."""
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or
            getattr(metadata, "st_file_attributes", 0) & 0x400):
        raise ValueError("Runtime input must be a plain file")
    for parent in path.parents:
        if getattr(parent.lstat(), "st_file_attributes", 0) & 0x400 or parent.is_symlink():
            raise ValueError("Runtime source ancestors must not be links or reparse points")


def stage_runtime(source_executable: Path, destination: Path, *, worker_source: str,
                  native_fixture: Path | None = None) -> RuntimeManifest:
    """Copy explicit standard-library/DLL inputs into a NEW dedicated directory.

    Does not change source ACLs, import source packages, create profiles, download
    software or grant the worker any write permission. Existing roots are refused.
    Missing dependencies fail closed and may leave an incomplete NEW staging root.
    """
    source = Path(os.path.abspath(source_executable))
    _plain_file(source)
    if source.name.casefold() != "python.exe":
        raise ValueError("An explicit Windows python.exe is required")
    origin = source.parent
    candidates = [tag for tag in ("311", "312") if (origin / f"python{tag}.dll").is_file()]
    if len(candidates) != 1:
        raise ValueError("Exactly one supported Python DLL is required")
    tag = candidates[0]
    target = Path(os.path.abspath(destination))
    if target.exists() or target.is_symlink():
        raise FileExistsError("Runtime destination must not exist")
    if target.is_relative_to(origin):
        raise ValueError("Do not stage inside the source Python installation")
    for parent in target.parents:
        if parent.exists() and (parent.is_symlink() or getattr(parent.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Runtime destination ancestors must not be reparse points")
    inputs: dict[str, Path] = {"python.exe": source, f"python{tag}.dll": origin / f"python{tag}.dll"}
    if native_fixture is not None:
        fixture = Path(os.path.abspath(native_fixture))
        _plain_file(fixture)
        if fixture.suffix.casefold() != ".exe" or fixture.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("The explicit synthetic native fixture must be an exe below 2MiB")
        inputs["native-fixture.exe"] = fixture
    for name in ("python3.dll", "vcruntime140.dll", "vcruntime140_1.dll", "zlib.dll"):
        if (origin / name).is_file():
            inputs[name] = origin / name
    for name in _MODULE_FILES:
        inputs[f"Lib/{name}"] = origin / "Lib" / name
    for package in _MODULE_PACKAGES:
        package_root = origin / "Lib" / package
        if not package_root.is_dir():
            raise FileNotFoundError(f"Missing standard-library package: {package}")
        # Only immediate .py sources; do not copy __pycache__, tests or metadata.
        for item in sorted(package_root.glob("*.py")):
            inputs[f"Lib/{package}/{item.name}"] = item
    for name in ("_ctypes.pyd", "_struct.pyd", "_json.pyd", "_codecs_cn.pyd", "_multibytecodec.pyd"):
        item = origin / "DLLs" / name
        if item.is_file():
            inputs[name] = item
        elif name == "_ctypes.pyd":
            raise FileNotFoundError(f"Missing required native module: {name}")
    # Conda's _ctypes uses libffi; explicitly select known local runtime DLLs.
    # This is not a dependency-closure claim; actual startup must prove the copy.
    for name in ("ffi-8.dll", "ffi-7.dll", "ffi.dll", "libffi-8.dll"):
        item = origin / "Library" / "bin" / name
        if item.is_file():
            inputs[name] = item
    for item in inputs.values():
        _plain_file(item)
    target.mkdir(parents=True, exist_ok=False)
    for relative, item in inputs.items():
        output = target / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, output)
    (target / f"python{tag}._pth").write_text(".\nLib\n", encoding="ascii")
    (target / "worker.py").write_text(worker_source, encoding="utf-8")
    files = tuple(RuntimeFile(path.relative_to(target).as_posix(), path.stat().st_size,
                              sha256(path.read_bytes()).hexdigest())
                  for path in sorted(target.rglob("*")) if path.is_file())
    manifest = RuntimeManifest(str(target), str(target / "python.exe"), tag, str(source), files)
    (target / "runtime-manifest.json").write_text(
        json.dumps(asdict(manifest), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _STAGED_ROOTS[manifest.root] = manifest
    return manifest


def verify_manifest(manifest: RuntimeManifest) -> None:
    """Reject content drift, extra files, or links before ACL provisioning."""
    root = Path(manifest.root)
    expected = {item.path: item for item in manifest.files}
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual != set(expected) | {"runtime-manifest.json"}:
        raise ValueError("Runtime file set changed")
    for relative, item in expected.items():
        path = root / relative
        _plain_file(path)
        if path.stat().st_size != item.bytes or sha256(path.read_bytes()).hexdigest() != item.sha256:
            raise ValueError("Runtime content changed")


def worker_environment(windows_directory: Path, readonly_temp: Path, *,
                       local_appdata: Path) -> dict[str, str]:
    """Construct five explicit keys; the caller supplies its native profile path.

    LOCALAPPDATA must come from this invocation's freshly queried profile, never
    the ambient host environment. This helper does not authorize profile access.
    """
    windows = Path(windows_directory)
    temporary = Path(readonly_temp)
    local = Path(local_appdata)
    paths = (windows, temporary, local)
    if not all(path.is_absolute() for path in paths):
        raise ValueError("Worker environment paths must be absolute")
    if any("\x00" in str(path) for path in paths):
        raise ValueError("Invalid worker environment path")
    return {"SystemRoot": str(windows), "WINDIR": str(windows),
            "TEMP": str(temporary), "TMP": str(temporary),
            "LOCALAPPDATA": str(local)}


def readonly_sddl(owner_sid: str, package_sid: str) -> str:
    """Protected inheritable DACL proposal; still requires real access probes.

    No write/delete/change-owner/change-DACL bits for the package. OWNER RIGHTS
    ACE replaces implicit owner WRITE_DAC; host's explicit full ACE is retained.
    This deliberately does not lower the integrity label or grant all-packages.
    """
    if not _SID.fullmatch(owner_sid) or not _SID.fullmatch(package_sid) or not package_sid.startswith("S-1-15-2-"):
        raise ValueError("Explicit owner and AppContainer package SIDs are required")
    if owner_sid == package_sid:
        raise ValueError("Host owner and package identities must differ")
    return (f"D:P(D;OICI;0x000d0156;;;{package_sid})"
            f"(A;OICI;FA;;;SY)(A;OICI;FA;;;{owner_sid})"
            f"(A;OICI;0x001200a9;;;{package_sid})(A;OICI;RC;;;S-1-3-4)")


def create_probe_profile(name: str) -> ProbeProfile:
    """Create a fresh project profile; NEVER adopt or modify an existing one."""
    if os.name != "nt":
        raise OSError("AppContainer profiles require Windows")
    if not _PROFILE_NAME.fullmatch(name):
        raise ValueError("A fresh deskpal.v06.<random> profile name is required")
    import ctypes
    from ctypes import wintypes

    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    userenv.CreateAppContainerProfile.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    userenv.GetAppContainerFolderPath.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    userenv.GetAppContainerFolderPath.restype = ctypes.c_long
    userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    userenv.DeleteAppContainerProfile.restype = ctypes.c_long
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.FreeSid.argtypes = [ctypes.c_void_p]
    advapi.FreeSid.restype = ctypes.c_void_p
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    sid, sid_text, folder = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    hr = userenv.CreateAppContainerProfile(name, name, "Deskpal stage-0 synthetic probe", None, 0, ctypes.byref(sid))
    if hr != 0:
        raise OSError(f"CreateAppContainerProfile refused fresh profile: HRESULT 0x{hr & 0xffffffff:08x}")
    try:
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        value = ctypes.wstring_at(sid_text)
        hr = userenv.GetAppContainerFolderPath(value, ctypes.byref(folder))
        if hr != 0:
            raise OSError(f"GetAppContainerFolderPath failed: HRESULT 0x{hr & 0xffffffff:08x}")
        profile = ProbeProfile(name, value, ctypes.wstring_at(folder))
        native_path = Path(profile.folder)
        if native_path.name.casefold() != "ac" or native_path.parent.name.casefold() != name.casefold():
            raise ValueError("Native profile path is not the expected dedicated moniker/AC tree")
        _CREATED_PROFILES[name] = profile
        return profile
    except BaseException as error:
        cleanup_hr = userenv.DeleteAppContainerProfile(name)
        if cleanup_hr != 0:
            raise OSError(f"Profile {name} setup failed ({type(error).__name__}); cleanup FAILED "
                          f"HRESULT 0x{cleanup_hr & 0xffffffff:08x}; dedicated profile may remain") from error
        raise
    finally:
        if folder.value:
            ole.CoTaskMemFree(folder)
        if sid_text.value:
            kernel.LocalFree(sid_text)
        if sid.value:
            advapi.FreeSid(sid)


def delete_probe_profile(profile: ProbeProfile) -> None:
    """Delete only a profile returned for the project prefix (after workers stop)."""
    if not _PROFILE_NAME.fullmatch(profile.name) or _CREATED_PROFILES.get(profile.name) is not profile:
        raise ValueError("Not a fresh profile created by this provisioning session")
    import ctypes
    from ctypes import wintypes

    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    userenv.DeleteAppContainerProfile.restype = ctypes.c_long
    hr = userenv.DeleteAppContainerProfile(profile.name)
    if hr != 0:
        raise OSError(f"Profile cleanup failed: HRESULT 0x{hr & 0xffffffff:08x}")
    del _CREATED_PROFILES[profile.name]


def profile_package_root(profile: ProbeProfile) -> Path:
    """Bind the whole new moniker tree, including siblings of its native AC dir."""
    if _CREATED_PROFILES.get(profile.name) is not profile:
        raise ValueError("Profile was not created in this provisioning session")
    native_path = Path(profile.folder)
    if native_path.name.casefold() != "ac" or native_path.parent.name.casefold() != profile.name.casefold():
        raise ValueError("Native profile is not inside its dedicated moniker")
    return native_path.parent


def apply_readonly_dacl(target: RuntimeManifest | ProbeProfile, *, owner_sid: str, package_sid: str) -> None:
    """Provision ONLY an explicit dedicated tree; not a general ACL utility.

    Accept only objects staged/created by THIS host provisioning session, and
    verify their manifest/native profile path again. An arbitrary caller path,
    fabricated DTO or manifest from disk cannot authorize changing its DACL.
    """
    if isinstance(target, RuntimeManifest):
        if _STAGED_ROOTS.get(target.root) is not target:
            raise ValueError("Runtime was not staged in this provisioning session")
        verify_manifest(target)
        directory = Path(target.root)
    elif isinstance(target, ProbeProfile):
        if _CREATED_PROFILES.get(target.name) is not target or package_sid != target.sid:
            raise ValueError("Profile was not created in this provisioning session")
        directory = profile_package_root(target)
    else:
        raise ValueError("A session-owned runtime manifest or profile is required")
    if os.name != "nt":
        raise OSError("DACL provisioning requires Windows")
    if not directory.is_absolute() or not directory.is_dir():
        raise ValueError("An existing dedicated ownership root is required")
    # The root and all ancestors must be ordinary directories, not junctions.
    for parent in (directory, *directory.parents):
        metadata = parent.lstat()
        if parent.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError("ACL target cannot contain reparse ancestors")
    entries = [directory, *sorted(directory.rglob("*"))]
    for path in entries:
        metadata = path.lstat()
        if path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError("ACL tree cannot contain reparse points")
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise ValueError("Unsupported ACL target")
    import ctypes
    from ctypes import wintypes

    if isinstance(target, ProbeProfile):
        userenv = ctypes.WinDLL("userenv", use_last_error=True)
        ole = ctypes.WinDLL("ole32", use_last_error=True)
        userenv.GetAppContainerFolderPath.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
        userenv.GetAppContainerFolderPath.restype = ctypes.c_long
        ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole.CoTaskMemFree.restype = None
        native_folder = ctypes.c_void_p()
        hr = userenv.GetAppContainerFolderPath(target.sid, ctypes.byref(native_folder))
        try:
            if hr != 0 or not native_folder.value or Path(ctypes.wstring_at(native_folder)) != Path(target.folder):
                raise ValueError("Native profile folder no longer matches its session-owned object")
        finally:
            if native_folder.value:
                ole.CoTaskMemFree(native_folder)

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor, dacl = ctypes.c_void_p(), ctypes.c_void_p()
    present, defaulted = wintypes.BOOL(), wintypes.BOOL()
    sddl = readonly_sddl(owner_sid, package_sid)
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)) or not present.value:
            raise OSError("No DACL in generated security descriptor")
        for path in entries:
            result = advapi.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None)
            if result:
                raise ctypes.WinError(result)
    finally:
        kernel.LocalFree(descriptor)
