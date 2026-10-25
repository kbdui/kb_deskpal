"""Opt-in, isolated real managed BGE download and fresh-process offline CPU check.

No dependency installation, existing application config, personal HF cache, or cloud
chat is used. Failed runs retain logs/receipts; MaintenanceService may clean its own
safe staging directory on failure. Socket interception is not packet capture.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import socket
import stat
import struct
import subprocess
import sys
import time
import traceback
from typing import Any
import uuid


CONFIG = ('[llm]\nprovider="offline"\ncredential_ref=""\n'
          '[storage]\ndata_dir="data"\n[sandbox]\nworkspace_dir="workspace"\n'
          '[memory]\nembedding_provider="fastembed"\nmodel_cache_dir="model-cache"\n')
MARKER = "deskpal-managed-embedding-diagnostic-v1"
MODEL_MANIFEST = ".deskpal-model-cache.json"
QUERY = "用户通常什么时候学习？"
DOCUMENTS = ("用户习惯使用中文交流。", "用户喜欢晚上进行学习。", "用户希望提前收到日程提醒。")
REPO = Path(__file__).resolve().parents[1]


def write_json(path: Path, value: Any) -> None:
    # Exclusive creation keeps reruns from replacing evidence.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")


def no_links(path: Path) -> None:
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Diagnostic paths must not contain links or reparse points")
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise ValueError("Diagnostic files must not be hard links")


def validate_root(root: Path) -> None:
    no_links(root)
    for name in ("diagnostic-marker.json", "diagnostic.toml"):
        no_links(root / name)
    if json.loads((root / "diagnostic-marker.json").read_text(encoding="utf-8")) != {"kind": MARKER}:
        raise ValueError("Root is not a diagnostic root created by this script")
    if (root / "diagnostic.toml").read_text(encoding="utf-8") != CONFIG:
        raise ValueError("Generated diagnostic config changed; refusing to read it")
    if (root / ".env").exists() or (root / ".env").is_symlink():
        raise ValueError("Diagnostic root must not contain .env")
    for name in ("data", "workspace", "model-cache", "runs"):
        no_links(root / name)


def initialize_root(root: Path) -> None:
    no_links(root)
    root.mkdir(mode=0o700)  # Deliberately no exist_ok/parents: always a new explicit root.
    write_json(root / "diagnostic-marker.json", {"kind": MARKER})
    (root / "diagnostic.toml").write_text(CONFIG, encoding="utf-8")
    for name in ("data", "workspace", "runs"):
        (root / name).mkdir()


def child_environment(home: Path, *, offline: bool) -> dict[str, str]:
    # A whitelist also drops proxy credentials, custom endpoints, DESKPAL_*,
    # cloud keys, HF authentication, Python startup hooks and inherited cache paths.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
               "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "SYSTEMDRIVE"}
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "APPDATA": str(home / "appdata"), "LOCALAPPDATA": str(home / "localappdata"),
        "XDG_CACHE_HOME": str(home / "xdg-cache"), "XDG_CONFIG_HOME": str(home / "xdg-config"),
        "HF_HOME": str(home / "hf"), "HF_HUB_CACHE": str(home / "hf" / "hub"),
        "HUGGINGFACE_HUB_CACHE": str(home / "hf" / "hub"),
        "HF_ASSETS_CACHE": str(home / "hf" / "assets"),
        "HF_XET_CACHE": str(home / "hf" / "xet"),
        "HF_TOKEN_PATH": str(home / "hf" / "absent-token"),
        "TRANSFORMERS_CACHE": str(home / "transformers"),
        "FASTEMBED_CACHE_PATH": str(home / "unused-fastembed-cache"),
        "TEMP": str(home / "tmp"), "TMP": str(home / "tmp"), "TMPDIR": str(home / "tmp"),
        "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1", "ANONYMIZED_TELEMETRY": "False",
        "HF_HUB_OFFLINE": "1" if offline else "0",
        "TRANSFORMERS_OFFLINE": "1" if offline else "0",
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
    })
    return result


def install_network_guard() -> Counter[str]:
    attempts: Counter[str] = Counter()

    def deny(name: str) -> Any:
        def blocked(*args: Any, **kwargs: Any) -> Any:
            attempts[name] += 1
            raise OSError("Offline diagnostic blocked network operation: " + name)
        return blocked

    setattr(socket.socket, "connect", deny("socket.connect"))
    setattr(socket.socket, "connect_ex", deny("socket.connect_ex"))
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        setattr(socket, name, deny("socket." + name))

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
            attempts["audit:" + event] += 1
            raise OSError("Offline diagnostic audit blocked network operation: " + event)

    sys.addaudithook(audit)
    return attempts


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def asset_evidence(cache: Path) -> dict[str, Any]:
    from deskpal.maintenance.cache import _inventory
    files = _inventory(cache)  # Keep product's bounded no-link/no-hardlink rules.
    manifest = json.loads((cache / MODEL_MANIFEST).read_text(encoding="utf-8"))
    assets = manifest["assets"]
    if set(assets) != set(files) - {MODEL_MANIFEST} or not assets:
        raise ValueError("Manifest inventory differs from actual files")
    observed = {}
    for name, expected in assets.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
            raise ValueError("Invalid asset path")
        actual = {"bytes": files[name], "sha256": file_sha256(cache / name)}
        if expected != actual:
            raise ValueError("Asset size/SHA-256 mismatch: " + name)
        observed[name] = actual
    return {"revision": file_sha256(cache / MODEL_MANIFEST), "assets": observed,
            "asset_count": len(assets), "files": len(files), "bytes": sum(files.values())}


def stage_residue(root: Path) -> list[str]:
    return sorted(path.name for path in root.glob(".model-cache.stage-*") if path.is_dir())


def vector_summary(vector: tuple[float, ...]) -> dict[str, Any]:
    if len(vector) != 512 or not all(math.isfinite(value) for value in vector):
        raise ValueError("Vector must contain exactly 512 finite values")
    norm = math.sqrt(math.fsum(value * value for value in vector))
    if not math.isclose(norm, 1.0, abs_tol=1e-6):
        raise ValueError("Vector L2 norm differs from one")
    return {"dimensions": len(vector), "finite": True, "l2_norm": norm,
            "sha256_float64_le": hashlib.sha256(struct.pack("<512d", *vector)).hexdigest(),
            "min": min(vector), "max": max(vector)}


def actual_sessions(provider: Any) -> list[dict[str, Any]]:
    onnxruntime = importlib.import_module("onnxruntime")
    # Read-only observation of known SDK containers. No patches to SDK/model data.
    pending = [("provider._model", provider._model, 0)]
    seen: set[int] = set()
    sessions = []
    while pending:
        path, value, depth = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, onnxruntime.InferenceSession):
            providers = value.get_providers()
            if providers != ["CPUExecutionProvider"]:
                raise ValueError("Actual ONNX session is not CPU-only")
            sessions.append({"path": path, "type": type(value).__module__ + "." + type(value).__name__,
                             "providers": providers})
        elif depth < 6:
            fields = vars(value) if hasattr(value, "__dict__") else {}
            for name in ("model", "_model", "session", "_session", "onnx_model"):
                if name in fields:
                    pending.append((path + "." + name, fields[name], depth + 1))
    if not sessions:
        raise ValueError("No real ONNX InferenceSession observed; inspect SDK layout without mocking")
    return sessions


def versions() -> dict[str, str]:
    result = {"python": sys.version, "executable": sys.executable, "platform": platform.platform()}
    for package in ("deskpal", "fastembed", "onnxruntime", "huggingface-hub", "numpy", "tokenizers"):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = "not-installed"
    return result


def worker(root: Path, phase: str, evidence: Path, allow_download: bool) -> int:
    start = time.monotonic()
    result: dict[str, Any] = {"phase": phase, "success": False, "pid": os.getpid(),
                              "started_utc": datetime.now(timezone.utc).isoformat(),
                              "versions": versions(), "manager_may_cleanup_safe_stage_on_failure": True}
    attempts: Counter[str] | None = None
    try:
        validate_root(root)
        no_links(evidence)
        if evidence.parent != root / "runs" or not evidence.is_dir():
            raise ValueError("Invalid evidence directory")
        if any(name == "fastembed" or name.startswith("fastembed.") for name in sys.modules):
            raise ValueError("FastEmbed imported before isolation/guard")
        if phase == "offline":
            if os.environ.get("HF_HUB_OFFLINE") != "1" or os.environ.get("TRANSFORMERS_OFFLINE") != "1":
                raise ValueError("Offline flags were not set before SDK import")
            attempts = install_network_guard()
        elif not allow_download:
            raise ValueError("Download requires explicit --allow-download")
        sys.path.insert(0, str(REPO / "src"))
        from deskpal.maintenance.service import MaintenanceService
        service = MaintenanceService(root / "diagnostic.toml", provider_override="offline")
        cache = root / "model-cache"
        if phase == "download":
            result["before"] = service.execute("cache_status", {})
            if cache.exists():
                raise ValueError("Download target must not already exist")
            result["maintenance_receipt"] = service.execute("cache_prepare", {"allow_download": True})
            prepared = result["maintenance_receipt"]
            if (prepared.get("status") != "ready" or prepared.get("managed") is not True
                    or prepared.get("verification") != "previously_verified"):
                raise ValueError("Prepare did not publish a verified managed cache")
            result["assets_after"] = asset_evidence(cache)
            if result["maintenance_receipt"]["revision"] != result["assets_after"]["revision"]:
                raise ValueError("Published manifest revision differs from prepare receipt")
        else:
            result["assets_before"] = asset_evidence(cache)
            result["maintenance_receipt"] = service.execute("cache_verify", {})
            verified = result["maintenance_receipt"]
            if verified.get("status") != "ready" or verified.get("verification") != "verified_offline":
                raise ValueError("Maintenance did not confirm fresh offline verification")
            from deskpal.memory.embedding import FastEmbedProvider
            fastembed = importlib.import_module("fastembed")
            onnxruntime = importlib.import_module("onnxruntime")
            result["sdk_paths"] = {"fastembed": fastembed.__file__, "onnxruntime": onnxruntime.__file__}
            provider = FastEmbedProvider(cache_dir=cache)
            try:
                model_dir = Path(provider._model.model._model_dir).resolve()
                if not model_dir.is_relative_to(cache.resolve()):
                    raise ValueError("SDK resolved model outside the managed cache")
                result["actual_model_dir"] = str(model_dir)
                result["embedding_manifest"] = asdict(provider.manifest)
                query = provider.embed_query(QUERY)
                repeated = provider.embed_query(QUERY)
                documents = provider.embed_documents(DOCUMENTS)
                if len(documents) != len(DOCUMENTS):
                    raise ValueError("Document vector count differs from input count")
                delta = max(abs(left - right) for left, right in zip(query, repeated, strict=True))
                if delta > 1e-7:
                    raise ValueError("Repeated query is unstable")
                result["vectors"] = {"query": vector_summary(query),
                                     "repeated_query": vector_summary(repeated),
                                     "repeat_max_absolute_delta": delta,
                                     "document_count": len(documents),
                                     "documents": [vector_summary(vector) for vector in documents]}
                result["sessions"] = actual_sessions(provider)
            finally:
                provider.close()
            result["assets_after"] = asset_evidence(cache)
            revision = result["assets_before"]["revision"]
            if result["assets_before"] != result["assets_after"] or result["maintenance_receipt"]["revision"] != revision:
                raise ValueError("Manifest/assets changed during offline verification")
            if attempts:
                raise ValueError("Offline SDK attempted network access")
        result["stage_residue"] = stage_residue(root)
        if result["stage_residue"]:
            raise ValueError("Managed staging directories remain")
        result["success"] = True
        return 0
    except BaseException as error:
        result["error_type"] = type(error).__name__
        result["error_code"] = getattr(error, "code", None)
        traceback.print_exc()
        return 1
    finally:
        result["elapsed_seconds"] = time.monotonic() - start
        result["stage_residue"] = stage_residue(root)
        if attempts is not None:
            result["network_attempts"] = sum(attempts.values())
            result["network_operations"] = dict(attempts)
            result["network_evidence"] = "Python socket guard and audit hook; not physical disconnection/packet capture"
        write_json(evidence / (phase + ".receipt.json"), result)
        print(json.dumps({"phase": phase, "success": result["success"],
                          "elapsed_seconds": result["elapsed_seconds"]}), flush=True)


def run_phase(root: Path, evidence: Path, phase: str, timeout: int) -> dict[str, Any]:
    home = evidence / (phase + "-isolation")
    home.mkdir()
    (home / "tmp").mkdir()
    command = [sys.executable, "-I", str(Path(__file__).resolve()), "--root", str(root),
               "--_worker", phase, "--_evidence", str(evidence)]
    if phase == "download":
        command.append("--allow-download")
    start = time.monotonic()
    outcome: dict[str, Any] = {"phase": phase, "timeout_seconds": timeout,
                               "raw_log": str(evidence / (phase + ".raw.log"))}
    with (evidence / (phase + ".raw.log")).open("xb") as log:
        try:
            completed = subprocess.run(command, cwd=root, env=child_environment(home, offline=phase == "offline"),
                                       stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False)
            outcome["exit_code"] = completed.returncode
        except subprocess.TimeoutExpired:
            outcome.update(exit_code=None, timed_out=True)
    outcome["elapsed_seconds"] = time.monotonic() - start
    receipt = evidence / (phase + ".receipt.json")
    if receipt.is_file():
        outcome["receipt"] = json.loads(receipt.read_text(encoding="utf-8"))
    else:
        # Killed workers cannot execute finally: retain an explicit failure receipt.
        write_json(receipt, {"phase": phase, "success": False, "worker_receipt_missing": True,
                             "timed_out": outcome.get("timed_out", False),
                             "exit_code": outcome.get("exit_code"), "stage_residue": stage_residue(root)})
        outcome["receipt"] = json.loads(receipt.read_text(encoding="utf-8"))
    outcome["success"] = outcome.get("exit_code") == 0 and outcome["receipt"].get("success") is True
    return outcome


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__, epilog=(
        "First run: --root NEW_DIRECTORY --allow-download (may take up to 900 seconds to download, "
        "then 180 seconds offline). Recheck: --root EXISTING_DIAGNOSTIC_ROOT --offline. "
        "No pip install or personal cache fallback. Evidence and large model files are retained."))
    value.add_argument("--root", type=Path, required=True, help="Explicit dedicated root; must be new for download")
    mode = value.add_mutually_exclusive_group()
    mode.add_argument("--allow-download", action="store_true", help="Explicitly authorize public SDK-default model download")
    mode.add_argument("--offline", action="store_true", help="Only verify a root previously created by this script")
    value.add_argument("--download-timeout", type=int, default=900, help="Download subprocess timeout, 1..900 seconds")
    value.add_argument("--offline-timeout", type=int, default=180, help="Offline subprocess timeout, 1..180 seconds")
    value.add_argument("--_worker", choices=("download", "offline"), help=argparse.SUPPRESS)
    value.add_argument("--_evidence", type=Path, help=argparse.SUPPRESS)
    return value


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    root = arguments.root.absolute()
    if arguments._worker:
        if arguments._evidence is None:
            raise ValueError("Worker requires an evidence directory")
        return worker(root, arguments._worker, arguments._evidence.absolute(), arguments.allow_download)
    if not arguments.allow_download and not arguments.offline:
        parser().error("Choose --allow-download for a new root or --offline for an existing diagnostic root")
    if not 1 <= arguments.download_timeout <= 900 or not 1 <= arguments.offline_timeout <= 180:
        parser().error("Timeouts must be positive and at most download=900/offline=180 seconds")
    if arguments.offline:
        validate_root(root)
    else:
        initialize_root(root)
    evidence = root / "runs" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
    evidence.mkdir()
    result: dict[str, Any] = {"root": str(root), "evidence": str(evidence), "success": False,
                              "download_authorized": arguments.allow_download, "phases": []}
    start = time.monotonic()
    try:
        if arguments.allow_download:
            download = run_phase(root, evidence, "download", arguments.download_timeout)
            result["phases"].append(download)
            if not download["success"]:
                return 1
        offline = run_phase(root, evidence, "offline", arguments.offline_timeout)
        result["phases"].append(offline)
        result["success"] = offline["success"]
        if arguments.allow_download and offline["success"]:
            if result["phases"][0]["receipt"]["assets_after"] != offline["receipt"]["assets_before"]:
                result["success"] = False
                result["error"] = "Published assets/revision changed between processes"
        return 0 if result["success"] else 1
    except BaseException as error:
        result["error_type"] = type(error).__name__
        traceback.print_exc()
        return 1
    finally:
        result["elapsed_seconds"] = time.monotonic() - start
        write_json(evidence / "summary.json", result)
        print(json.dumps({"success": result["success"], "summary": str(evidence / "summary.json")}), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
