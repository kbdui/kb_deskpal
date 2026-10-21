"""One passive native/installed 160s run of the frozen V0.7 preview probe.

This diagnoses ordering; it is not another soak or a performance acceptance run.
No production source, probe assertion, timer interval, poll or Core state changes.
Trace rows contain bounded scalar metadata and never retain a Qt object.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sys
from time import monotonic, perf_counter_ns
from typing import Any, Callable
import weakref

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "data" / "validation" / "v07"
TARGET = Path(__file__).with_name("check_v07_pet.py").resolve()
FROZEN_SHA256 = "5f23b888c922e4aa4e67fad53581f259f9afb3f6e5c10bc27a36e94e64a9c988"
TRACE_LIMIT = 20_000
CALLBACKS = {
    "main.<locals>.PreviewObserver.eventFilter.<locals>.painted": 0,
    "main.<locals>.preview.<locals>.restore_authority": 250,
}


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--installed-package-root", required=True, type=Path,
                        help="Exact prepared installed deskpal package directory, not its parent")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This single native diagnostic requires Windows")
    output, expected_package = args.output_dir.resolve(), args.installed_package_root.resolve()
    if output == EVIDENCE.resolve() or not output.is_relative_to(EVIDENCE.resolve()):
        parser.error("output-dir must be a NEW directory beneath data/validation/v07")
    if not expected_package.is_dir() or not expected_package.is_relative_to(EVIDENCE.resolve()):
        parser.error("installed-package-root must be an explicit prepared package beneath data/validation/v07")
    if digest(TARGET) != FROZEN_SHA256:
        parser.error("target probe differs from frozen 5F23; do not run")
    # Exactly the installed target is made importable; no source-tree insertion.
    sys.path.insert(0, str(expected_package.parent))
    os.environ["QT_QPA_PLATFORM"] = "windows"
    from PySide6.QtCore import QTimer
    import deskpal.gui.pet_controller as controller_module
    import deskpal.gui.pet_window as window_module

    actual_package = Path(controller_module.__file__).resolve().parents[1]
    if actual_package != expected_package:
        parser.error("deskpal did not resolve from installed-package-root")
    spec = importlib.util.spec_from_file_location("v07_frozen_preview_trace_target", TARGET)
    if spec is None or spec.loader is None:
        parser.error("cannot load explicit frozen target")
    target = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(target)
    output.mkdir(parents=True, exist_ok=False)
    tracked = (Path(__file__).resolve(), TARGET, Path(controller_module.__file__).resolve(),
               Path(window_module.__file__).resolve())
    before = {str(path): digest(path) for path in tracked}
    probe_argv = [str(TARGET), "--mode", "cycles", "--seconds", "160", "--native", "--installed",
                  "--output-dir", str(output / "probe-output")]
    metadata: dict[str, Any] = {"schema_version": 1, "status": "prepared", "helper_argv": sys.argv,
        "original_probe_argv": probe_argv, "executable": sys.executable,
        "installed_package_root": str(actual_package), "fingerprints_before": before,
        "trace_limit": TRACE_LIMIT, "callbacks": CALLBACKS,
        "scope": "passive method/callback ordering only; original 160s/20-cycle assertions and exit code retained; no performance/soak acceptance claim",
        "timers": "only exact target-file/qualname/0-or-250 callbacks wrapped; parameters unchanged; all other singleShot calls forwarded unchanged",
        "retention": "Qt objects only as temporary call arguments or weak references; recorded pixmaps are integer identities, not objects",
        "writes": "metadata prepared before Qt; trace/final metadata written after original main returns or raises",
        "original_main_entered": False, "original_exit_code": None,
        "original_exception_type": None, "setup_exception_type": None}
    (output / "trace-metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    origin = perf_counter_ns()
    rows: list[dict[str, Any]] = []
    dropped = 0
    trace_errors: set[str] = set()
    controller_ref: weakref.ReferenceType[Any] | None = None
    window_tokens: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()
    next_window = 0
    preview_number = 0
    active_preview: dict[str, Any] = {}
    context: list[str] = []

    def window_token(window: Any) -> int | None:
        nonlocal next_window
        if window is None:
            return None
        token = window_tokens.get(window)
        if token is None:
            next_window += 1
            token = next_window
            window_tokens[window] = token
        return token

    def identify(pixmap: Any, controller: Any) -> str | None:
        if pixmap is None or controller is None:
            return None
        return next((name for name, cached in controller._cache.items() if cached is pixmap), None)

    def remember_controller(instance: Any) -> None:
        nonlocal controller_ref
        try:
            controller_ref = weakref.ref(instance)
        except Exception as exc:
            trace_errors.add(type(exc).__name__)

    def emit(kind: str, *, window: Any = None, **fields: Any) -> None:
        nonlocal dropped
        if len(rows) >= TRACE_LIMIT:
            dropped += 1
            return
        try:
            controller = controller_ref() if controller_ref is not None else None
            current_window = controller.window if controller is not None else None
            observed_window = window if window is not None else current_window
            pixmap = getattr(observed_window, "_pixmap", None)
            row: dict[str, Any] = {"sequence": len(rows) + 1, "kind": kind,
                "elapsed_ns": perf_counter_ns() - origin, "monotonic_seconds": monotonic(),
                "context": "/".join(context), "window_token": window_token(observed_window),
                "controller_window_token": window_token(current_window),
                "pixmap_id": id(pixmap) if pixmap is not None else None,
                "cached_id": identify(pixmap, controller),
                "controller_status": getattr(controller, "status", None),
                "controller_expression": getattr(controller, "expression", None),
                "render_broken": getattr(controller, "_render_broken", None),
                "window_paint_failed": getattr(observed_window, "_paint_failed", None),
                "cache_bytes": getattr(controller, "cache_bytes", None)}
            row.update(active_preview)
            for name, value in fields.items():
                if value is not None and type(value) not in {str, int, float, bool}:
                    raise TypeError("trace field is not scalar")
                row[name] = value
            rows.append(row)
        except Exception as exc:
            # Instrumentation failure must not replace the original call/result.
            trace_errors.add(type(exc).__name__)

    controller_class, window_class = controller_module.PetController, window_module.PetWindow
    originals: dict[str, Callable[..., Any]] = {
        "reply": controller_class._reply, "render": controller_class._render,
        "present": window_class.present, "paint": window_class.paintEvent}
    shot_descriptor = vars(QTimer)["singleShot"]
    original_shot = QTimer.singleShot
    target_filename = os.path.normcase(str(TARGET))

    def invoke(label: str, original: Callable[..., Any], instance: Any, *values: Any,
               **fields: Any) -> Any:
        context.append(label)
        emit(label + ".enter", window=instance if label in {"present", "paint"} else None, **fields)
        try:
            return original(instance, *values)
        except BaseException as exc:
            emit(label + ".exception", exception_type=type(exc).__name__)
            raise
        finally:
            emit(label + ".exit", window=instance if label in {"present", "paint"} else None)
            context.pop()

    def reply(instance: Any, envelope: Any) -> Any:
        remember_controller(instance)
        if type(envelope) is not dict or envelope.get("operation") != "presentation":
            return originals["reply"](instance, envelope)
        data = envelope.get("data")
        fields = {"request_matches": envelope.get("request_id") == instance._request,
                  "reply_ok": envelope.get("ok") is True,
                  "reply_status": data.get("status") if type(data) is dict else None}
        return invoke("reply", originals["reply"], instance, envelope, **fields)

    def render(instance: Any) -> Any:
        remember_controller(instance)
        return invoke("render", originals["render"], instance,
                      caller=sys._getframe(1).f_code.co_qualname)

    def present(instance: Any, pixmap: Any, status: str, diagnostic: str = "") -> Any:
        nonlocal preview_number, active_preview
        controller = controller_ref() if controller_ref is not None else None
        name = diagnostic.removeprefix("probe-only:") if diagnostic.startswith("probe-only:") else None
        if name in {"neutral", "thinking", "focused", "pleased", "alert"}:
            preview_number += 1
            try:
                active_preview = {"preview_ordinal": preview_number - 1, "expected_cached_id": name,
                    "expected_pixmap_id": id(pixmap), "expected_window_token": window_token(instance)}
            except Exception as exc:
                trace_errors.add(type(exc).__name__)
        return invoke("present", originals["present"], instance, pixmap, status, diagnostic,
            caller=sys._getframe(1).f_code.co_qualname, incoming_pixmap_id=id(pixmap) if pixmap is not None else None,
            incoming_cached_id=identify(pixmap, controller), is_probe_preview=name is not None)

    def paint(instance: Any, event: Any) -> Any:
        return invoke("paint", originals["paint"], instance, event)

    def single_shot(*values: Any, **keywords: Any) -> Any:
        if len(values) != 2 or keywords:
            return original_shot(*values, **keywords)
        interval, callback = values
        code = getattr(callback, "__code__", None)
        qualname = getattr(code, "co_qualname", "")
        if (code is None or os.path.normcase(code.co_filename) != target_filename
                or qualname not in CALLBACKS or type(interval) is not int or interval != CALLBACKS[qualname]):
            return original_shot(*values, **keywords)
        captured = dict(active_preview)  # Scalars only; do not capture a window/controller/pixmap.
        emit("callback.scheduled", callback=qualname, interval_ms=interval, **captured)

        def observed() -> Any:
            context.append(qualname)
            emit("callback.enter", callback=qualname, **captured)
            try:
                return callback()
            except BaseException as exc:
                emit("callback.exception", callback=qualname, exception_type=type(exc).__name__, **captured)
                raise
            finally:
                emit("callback.exit", callback=qualname, **captured)
                context.pop()

        # This callback owns the original functor only until its original timer
        # fires; no trace row/global history retains either callable afterwards.
        return original_shot(interval, observed)

    previous_argv = sys.argv
    try:
        setattr(controller_class, "_reply", reply)
        setattr(controller_class, "_render", render)
        setattr(window_class, "present", present)
        setattr(window_class, "paintEvent", paint)
        setattr(QTimer, "singleShot", staticmethod(single_shot))
        sys.argv = probe_argv
        metadata["status"] = "running"
        emit("original_main.enter")
        metadata["original_main_entered"] = True
        result: int = target.main()
        metadata["original_exit_code"] = result
        return result
    except BaseException as exc:
        category = "original_exception_type" if metadata["original_main_entered"] else "setup_exception_type"
        metadata[category] = type(exc).__name__
        raise
    finally:
        emit("original_main.exit")
        sys.argv = previous_argv
        setattr(QTimer, "singleShot", shot_descriptor)
        setattr(window_class, "paintEvent", originals["paint"])
        setattr(window_class, "present", originals["present"])
        setattr(controller_class, "_render", originals["render"])
        setattr(controller_class, "_reply", originals["reply"])
        controller_ref = None
        after = {str(path): digest(path) for path in tracked}
        metadata.update(status="returned" if metadata["original_exit_code"] is not None else "raised",
            fingerprints_after=after, fingerprints_unchanged=before == after,
            trace_rows=len(rows), trace_dropped=dropped, trace_errors=sorted(trace_errors),
            previews_observed=preview_number, patches_restored=True,
            trace_valid=metadata["original_main_entered"] and dropped == 0 and not trace_errors and before == after,
            interpretation="ordered Python method/callback entry/exit, not screen-pixel capture or native compositor proof; callback wrappers add diagnostic overhead; original failures remain authoritative")
        with (output / "trace.jsonl").open("x", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        (output / "trace-metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
