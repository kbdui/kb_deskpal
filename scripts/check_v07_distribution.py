"""Read-only offline wheel/resources and isolated no-Qt CLI --help evidence.

Does not build/install packages or run GUI code. Use explicit prepared artifacts.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "data" / "validation" / "v07"
RESOURCE = Path("deskpal/gui/assets/pet")
FILES = ("neutral.png", "thinking.png", "focused.png", "pleased.png", "alert.png", "provenance.json")
MARKER = "V07_DISTRIBUTION_CLI_JSON="
CLI_PROBE = r'''
import importlib.util, json, os, pathlib, runpy, sys, traceback
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.dont_write_bytecode = True
target = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(target))
writes = []
def audit(event, args):
    if event == "open":
        mode, flags = args[1], args[2]
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
            writes.append("open_write")
    elif event in {"sqlite3.connect", "os.mkdir", "os.remove", "os.rename", "os.rmdir"}:
        writes.append(event)
sys.addaudithook(audit)
qt = importlib.util.find_spec("PySide6")
package = importlib.util.find_spec("deskpal")
origin = package.origin if package is not None else None
result = {"executable": sys.executable, "cwd": str(pathlib.Path.cwd()),
    "isolated": sys.flags.isolated, "dont_write_bytecode": sys.dont_write_bytecode,
    "pyside6_spec_found": qt is not None, "pyside6_spec": None if qt is None else qt.origin,
    "deskpal_origin": origin,
    "package_from_target": origin is not None and pathlib.Path(origin).resolve().is_relative_to(target),
    "cli_argv": ["deskpal", "--help"], "missing_module": None}
sys.argv = result["cli_argv"]
code = 1
try:
    if not result["package_from_target"]:
        raise RuntimeError("deskpal did not resolve from explicit installed-dir")
    runpy.run_module("deskpal", run_name="__main__")
    code = 0
except SystemExit as exc:
    code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
except ModuleNotFoundError as exc:
    result["missing_module"] = exc.name
    traceback.print_exc()
except Exception:
    traceback.print_exc()
result.update(cli_exit_code=code, file_write_or_db_events=writes,
    qt_modules_loaded=sorted(name for name in sys.modules if name.startswith(("PySide", "PyQt", "shiboken"))))
print("V07_DISTRIBUTION_CLI_JSON=" + json.dumps(result, ensure_ascii=False), flush=True)
sys.exit(code)
'''


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--installed-dir", required=True, type=Path)
    parser.add_argument("--cli-python", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    wheel, installed, interpreter = args.wheel.resolve(), args.installed_dir.resolve(), args.cli_python.resolve()
    if not wheel.is_file() or wheel.suffix != ".whl" or not wheel.is_relative_to(EVIDENCE.resolve()):
        parser.error("wheel must be an explicit .whl beneath data/validation/v07")
    if not installed.is_dir() or (installed.is_relative_to(ROOT) and not installed.is_relative_to(EVIDENCE)):
        parser.error("installed-dir must be a prepared isolated target, outside source/worktree code")
    if not interpreter.is_file():
        parser.error("cli-python must be an explicit existing base interpreter")
    output = (args.output_dir or EVIDENCE / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
              + "-distribution-" + uuid4().hex[:8])).resolve()
    if not output.is_relative_to(EVIDENCE.resolve()) or output == EVIDENCE.resolve():
        parser.error("output-dir must be a NEW directory beneath data/validation/v07")
    output.mkdir(parents=True, exist_ok=False)
    cwd = Path(tempfile.mkdtemp(prefix="deskpal-v07-cli-")).resolve()
    if cwd.is_relative_to(ROOT):
        parser.error("system temporary directory must be outside the repository")
    report: dict[str, Any] = {"schema_version": 1, "started_at": datetime.now(timezone.utc).isoformat(),
        "probe_sha256": digest(Path(__file__)), "argv": sys.argv, "python": sys.executable,
        "wheel": str(wheel), "wheel_sha256": digest(wheel), "installed_dir": str(installed),
        "resources": [], "failures": [], "assessment": "Resource/CLI gate only; GUI/native acceptance is separate"}
    try:
        with ZipFile(wheel) as archive:
            names = archive.namelist()
            for name in FILES:
                member = (RESOURCE / name).as_posix()
                source, target = ROOT / "src" / RESOURCE / name, installed / RESOURCE / name
                source_hash = digest(source)
                wheel_hash = sha256(archive.read(member)).hexdigest() if names.count(member) == 1 else None
                installed_hash = (digest(target) if target.is_file() and not target.is_symlink()
                                  and target.resolve().is_relative_to(installed) else None)
                equal = wheel_hash == source_hash == installed_hash
                report["resources"].append({"file": name, "wheel_member_count": names.count(member),
                    "source_sha256": source_hash, "wheel_sha256": wheel_hash,
                    "installed_sha256": installed_hash, "all_equal": equal})
                if not equal:
                    report["failures"].append("resource_missing_duplicate_or_hash_mismatch:" + name)
    except (OSError, BadZipFile, KeyError) as exc:
        report["failures"].append("resource_inspection_error:" + str(exc))
    argv = [str(interpreter), "-I", "-B", "-c", CLI_PROBE, str(installed)]
    report["subprocess"] = {"argv": argv, "cwd": str(cwd), "timeout_seconds": 60,
        "cwd_policy": "fresh OS temporary directory outside repo; retained as evidence"}
    stdout = stderr = ""
    try:
        completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=60, check=False)
        stdout, stderr = completed.stdout, completed.stderr
        report["subprocess"]["exit_code"] = completed.returncode
        markers = [line[len(MARKER):] for line in stdout.splitlines() if line.startswith(MARKER)]
        child = json.loads(markers[-1]) if len(markers) == 1 else None
        report["cli"] = child
        if (not isinstance(child, dict) or completed.returncode != 0 or child.get("cli_exit_code") != 0
                or child.get("pyside6_spec_found") is not False or child.get("qt_modules_loaded") != []
                or child.get("file_write_or_db_events") != [] or child.get("isolated") != 1
                or not child.get("package_from_target")):
            report["failures"].append("no_qt_cli_help_gate_failed")
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        report["failures"].append("cli_help_timeout_60_seconds")
    except (OSError, ValueError) as exc:
        report["failures"].append("cli_probe_error:" + str(exc))
    report["cwd_created_files"] = sorted(str(path.relative_to(cwd)) for path in cwd.rglob("*"))
    if report["cwd_created_files"]:
        report["failures"].append("cli_help_created_files_in_empty_cwd")
    (output / "cli-stdout.log").write_text(stdout, encoding="utf-8")
    (output / "cli-stderr.log").write_text(stderr, encoding="utf-8")
    report["subprocess"].update(stdout=stdout, stderr=stderr)
    report["passed"] = not report["failures"] and len(report["resources"]) == len(FILES)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
