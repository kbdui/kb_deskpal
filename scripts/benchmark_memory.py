"""Opt-in synthetic Chinese retrieval benchmark; strictly existing model cache.

No .env, user database or cloud client is loaded. Temporary SQLite databases
contain only the versioned fixture and generated stress records. Calibration
chooses the threshold before the held-out evaluation is executed exactly once
per system. This is a small project smoke benchmark, not a public benchmark.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import math
import platform
import re
import statistics
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from deskpal.memory.coordinator import MemoryCoordinator
from deskpal.memory.embedding import FastEmbedProvider
from deskpal.memory.repository import SQLiteMemoryRepository
from deskpal.memory.retrieval import MemoryRetriever

BENCHMARK_VERSION = "deskpal-memory-benchmark-v1"
NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _clock() -> datetime:
    return NOW


def _rss() -> dict[str, int | str | None]:
    """OS high-water working set, without requiring psutil."""
    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return {"method": "GetProcessMemoryInfo failed", "rss_bytes": None, "peak_rss_bytes": None}
        return {"method": "Windows GetProcessMemoryInfo working set", "rss_bytes": counters.WorkingSetSize,
                "peak_rss_bytes": counters.PeakWorkingSetSize, "private_bytes": counters.PrivateUsage}
    try:
        import resource
        maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {"method": "getrusage process high-water RSS", "rss_bytes": None,
                "peak_rss_bytes": int(maximum if sys.platform == "darwin" else maximum * 1024)}
    except ImportError:
        return {"method": "unavailable", "rss_bytes": None, "peak_rss_bytes": None}


def _latencies(seconds: list[float]) -> dict[str, float | int]:
    ordered = sorted(seconds)
    return {"count": len(seconds), "p50_seconds": statistics.median(seconds) if seconds else 0.0,
            "p95_seconds": ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)] if ordered else 0.0,
            "max_seconds": max(seconds, default=0.0), "total_seconds": sum(seconds)}


def _load_dataset(path: Path) -> tuple[dict[str, Any], str]:
    data = path.read_bytes()
    fixture = json.loads(data)
    if fixture.get("schema_version") != 1 or len(fixture["memories"]) < 20:
        raise ValueError("Unsupported dataset version or insufficient corpus")
    memory_ids = [item["id"] for item in fixture["memories"]]
    if len(memory_ids) != len(set(memory_ids)):
        raise ValueError("Duplicate fixture memory ids")
    if len(fixture["evaluation"]["positive"]) != 80 or len(fixture["evaluation"]["negative"]) != 20:
        raise ValueError("Evaluation must contain 80 positive and 20 negative queries")
    seen: set[str] = set()
    for split in ("calibration", "evaluation"):
        for polarity in ("positive", "negative"):
            for case in fixture[split][polarity]:
                query = case["query"].strip()
                if query in seen:
                    raise ValueError("Queries must be disjoint between splits and unique within splits")
                seen.add(query)
                if not set(case["expected"]).issubset(memory_ids):
                    raise ValueError("Expected memory id missing from fixture")
                if bool(case["expected"]) != (polarity == "positive"):
                    raise ValueError("Query polarity and labels disagree")
    return fixture, hashlib.sha256(data).hexdigest()


def _populate(repository: SQLiteMemoryRepository, fixture: dict[str, Any]) -> dict[str, str]:
    coordinator = MemoryCoordinator(repository, cloud=True, clock=_clock)
    ids = {}
    for record in fixture["memories"]:
        item = coordinator.remember(record["content"])
        if item.draft.profile_key:
            raise ValueError("Benchmark corpus must not create profile projection fields")
        ids[item.id] = record["id"]
    if coordinator.profile():
        raise ValueError("Benchmark must not benefit from unconditional profile recall")
    return ids


def _score(
    coordinator: MemoryCoordinator, cases: dict[str, list[dict[str, Any]]], ids: dict[str, str],
    *, progress_every: int = 0,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    durations: list[float] = []
    recalls: list[float] = []
    negatives = 0
    semantic_only = 0
    categories: dict[str, list[float]] = {}
    all_warnings: set[str] = set()
    for polarity in ("positive", "negative"):
        for case in cases[polarity]:
            started = time.perf_counter()
            result = coordinator.retrieve(case["query"])
            duration = time.perf_counter() - started
            durations.append(duration)
            all_warnings.update(result.warnings)
            retrieved = [ids[record.id] for record in result.records]
            reasons = {}
            for record in result.records:
                evidence = json.loads(record.content)
                selected_by = evidence["selected_by"]
                if "profile" in selected_by:
                    raise ValueError("Unconditional profile recall would invalidate the benchmark")
                reasons[ids[record.id]] = selected_by
            expected = set(case["expected"])
            if expected:
                recall = len(expected & set(retrieved[:5])) / len(expected)
                recalls.append(recall)
                categories.setdefault(case.get("category", "unspecified"), []).append(recall)
                if any(id_ in expected and "semantic" in modes and "lexical" not in modes
                       for id_, modes in reasons.items()):
                    semantic_only += 1
            elif retrieved:
                negatives += 1
            rows.append({**case, "retrieved": retrieved, "selected_by": reasons,
                         "seconds": duration, "bytes_used": result.bytes_used, "warnings": result.warnings})
            if progress_every and len(rows) % progress_every == 0:
                _progress(f"Stress: measured {len(rows)} warm queries; latest={duration:.3f}s")
    return {
        "positive_queries": len(cases["positive"]), "negative_queries": len(cases["negative"]),
        "recall_at_5": statistics.mean(recalls) if recalls else 0.0,
        "positive_hits": sum(value > 0 for value in recalls),
        "negative_acceptance": negatives / len(cases["negative"]) if cases["negative"] else 0.0,
        "negative_accepted_count": negatives, "semantic_only_positive_hits": semantic_only,
        "category_recall_at_5": {key: statistics.mean(values) for key, values in categories.items()},
        "complete_retrieval_latency": _latencies(durations), "warnings": sorted(all_warnings), "queries": rows,
    }


def _runtime(repository: SQLiteMemoryRepository, *, embedding: FastEmbedProvider | None,
             threshold: float) -> MemoryCoordinator:
    return MemoryCoordinator(repository, cloud=True, clock=_clock, retriever=MemoryRetriever(
        repository, embedding=embedding, cloud=True, clock=_clock,
        semantic_threshold=threshold, limit=5, max_bytes=8000,
    ))


def _choose(calibration: list[dict[str, Any]]) -> tuple[float, bool]:
    feasible = [entry for entry in calibration if entry["metrics"]["negative_acceptance"] <= 0.10]
    if feasible:
        selected = max(feasible, key=lambda entry: (entry["metrics"]["recall_at_5"], entry["threshold"]))
    else:
        selected = min(calibration, key=lambda entry: (entry["metrics"]["negative_acceptance"],
                       -entry["metrics"]["recall_at_5"], -entry["threshold"]))
    return selected["threshold"], bool(feasible)


def _summary(metrics: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metrics.items() if key != "queries"}


def _stress(
    directory: Path, fixture: dict[str, Any], embedding: FastEmbedProvider,
    threshold: float, size: int, query_count: int, *, resume: Path | None = None,
) -> dict[str, Any]:
    _progress(f"Stress: {'reopening' if resume else 'constructing'} {size} synthetic records")
    before = _rss()
    with SQLiteMemoryRepository(resume or directory / "stress.sqlite3", clock=_clock) as repository:
        started = time.perf_counter()
        ids: dict[str, str] = {}
        if resume:
            fixture_contents = {entry["content"]: entry["id"] for entry in fixture["memories"]}
            for item in repository.list_items():
                content = item.draft.content
                if content in fixture_contents:
                    ids[item.id] = fixture_contents[content]
                elif match := re.fullmatch(r"合成归档项目编号(\d+)的设备清单存放在青杉资料柜，标签为测试备忘条目。", content):
                    ids[item.id] = "stress-" + match[1]
                else:
                    raise ValueError("Refusing non-benchmark content in synthetic resume database")
            if len(ids) != size:
                raise ValueError("Synthetic snapshot record count does not match --stress-size")
        else:
            ids = _populate(repository, fixture)
        coordinator = MemoryCoordinator(repository, cloud=True, clock=_clock)
        for index in range(0 if resume else size - len(ids)):
            number = 100000 + index
            record = coordinator.remember(
                f"合成归档项目编号{number}的设备清单存放在青杉资料柜，标签为测试备忘条目。"
            )
            ids[record.id] = f"stress-{number}"
            if (index + 1) % 1000 == 0:
                _progress(f"Stress: populated {index + 1 + len(fixture['memories'])}/{size}")
        setup_seconds = time.perf_counter() - started
        population_seconds = 0.0 if resume else setup_seconds
        retriever = MemoryRetriever(repository, embedding=embedding, cloud=True, clock=_clock,
                                    semantic_threshold=threshold)
        _progress("Stress: checking existing index" if resume else "Stress: indexing every record before timed retrieval")
        started = time.perf_counter()
        warnings = () if resume else retriever.rebuild_index()
        index_seconds = time.perf_counter() - started
        check_started = time.perf_counter()
        index_count = len(repository.embeddings(embedding.manifest))
        index_check_seconds = time.perf_counter() - check_started
        if warnings or index_count != size:
            return {"passed": False, "record_count": size, "index_count": index_count,
                    "warnings": warnings, "population_seconds": population_seconds,
                    "index_seconds": index_seconds, "rss_before": before, "rss_after": _rss()}
        runtime = MemoryCoordinator(repository, retriever=retriever, cloud=True, clock=_clock)
        selected = fixture["evaluation"]["positive"][::4]
        queries = [selected[index % len(selected)] for index in range(query_count)]
        started = time.perf_counter()
        runtime.retrieve(queries[0]["query"])
        first_query_seconds = time.perf_counter() - started
        _progress(f"Stress: timed warm retrieval ({query_count} queries)")
        scores = _score(runtime, {"positive": queries, "negative": []}, ids, progress_every=5)
        after = _rss()
        rss_before = before.get("rss_bytes")
        peak = after.get("peak_rss_bytes")
        increment = max(0, peak - rss_before) if isinstance(peak, int) and isinstance(rss_before, int) else None
        return {"record_count": size, "index_count": index_count, "index_reused": resume is not None,
                "population_seconds": population_seconds, "index_seconds": index_seconds,
                "snapshot_validation_seconds": setup_seconds if resume else 0.0,
                "index_count_check_seconds": index_check_seconds,
                "first_complete_retrieval_after_index_seconds": first_query_seconds,
                "warm_retrieval": _summary(scores), "rss_before": before, "rss_after": after,
                "peak_over_stress_start_rss_bytes": increment,
                "latency_target_seconds": 2.0,
                "latency_target_met": scores["complete_retrieval_latency"]["p95_seconds"] <= 2.0,
                "warnings": warnings}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=ROOT / "tests/fixtures/memory-retrieval-zh.json")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "data/models/fastembed")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--stress", action="store_true", help="Also index 5,000 synthetic records and measure warm retrieval")
    parser.add_argument("--stress-size", type=int, default=5000)
    parser.add_argument("--stress-queries", type=int, default=20)
    parser.add_argument("--stress-only", action="store_true", help="Measure stress without rerunning calibration or evaluation")
    parser.add_argument("--resume-synthetic", type=Path, help="Reuse only this benchmark's synthetic SQLite snapshot")
    parser.add_argument("--threshold", type=float, help="Already calibrated threshold; required for stress-only")
    parser.add_argument("--output", type=Path, help="Write a JSON report, e.g. data/validation/v03/benchmark-memory.json")
    args = parser.parse_args()
    fixture, fixture_hash = _load_dataset(args.fixture)
    if args.stress_size < len(fixture["memories"]) or args.stress_queries < 1:
        parser.error("Stress size must cover the fixture and query count must be positive")
    if args.stress_only and (not args.resume_synthetic or args.threshold is None):
        parser.error("--stress-only requires --resume-synthetic and the previously calibrated --threshold")
    if not args.stress_only and (args.resume_synthetic or args.threshold is not None):
        parser.error("Synthetic resume and explicit threshold are only available in --stress-only mode")
    if args.resume_synthetic and not args.resume_synthetic.is_file():
        parser.error("Synthetic resume snapshot does not exist")
    report: dict[str, Any] = {
        "benchmark_version": BENCHMARK_VERSION, "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": {"id": fixture["dataset_id"], "sha256": fixture_hash,
                    "memories": len(fixture["memories"]), "profile_fields": 0},
        "environment": {"python": sys.version, "platform": platform.platform(), "dependencies": {}},
        "offline_only": True, "calibration_selection_policy": fixture["selection_policy"],
        "rss_before_model": _rss(),
        "limitations": [
            "Synthetic single-session smoke set, authored here; held-out queries share corpus topics with calibration.",
            "No claim of general Chinese retrieval quality or blinded independent annotation.",
            "RSS is OS process high-water working set; same-process phases can share an earlier peak.",
            "Local cache only was enforced by provider; the machine was not physically disconnected.",
            "Explicit temporal/correction/deletion/privacy correctness belongs to separate deterministic tests.",
        ],
    }
    for package in ("fastembed", "onnxruntime", "numpy", "tokenizers", "huggingface-hub"):
        try:
            report["environment"]["dependencies"][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            report["environment"]["dependencies"][package] = None
    _progress(f"Dataset frozen SHA256: {fixture_hash}")
    _progress("Loading existing local CPU model (downloads disabled)")
    started = time.perf_counter()
    embedding = FastEmbedProvider(cache_dir=args.cache_dir, threads=args.threads)
    report["model_load_seconds"] = time.perf_counter() - started
    report["embedding_manifest"] = asdict(embedding.manifest)
    report["model_asset_sha256"] = dict(embedding.weight_checksums)
    if args.stress_only:
        try:
            report["selected_threshold"] = args.threshold
            report["threshold_source"] = "explicit reuse of prior calibration; no quality evaluation rerun"
            report["stress"] = _stress(ROOT, fixture, embedding, args.threshold,
                                       args.stress_size, args.stress_queries, resume=args.resume_synthetic)
        finally:
            embedding.close()
        report["rss_final"] = _rss()
        rss_start = report["rss_before_model"].get("rss_bytes")
        rss_peak = report["rss_final"].get("peak_rss_bytes")
        report["model_and_retrieval_peak_rss_increment_bytes"] = (
            max(0, rss_peak - rss_start) if isinstance(rss_peak, int) and isinstance(rss_start, int) else None)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0
    try:
        started = time.perf_counter()
        embedding.embed_query(fixture["calibration"]["positive"][0]["query"])
        report["first_query_embedding_seconds"] = time.perf_counter() - started
        with tempfile.TemporaryDirectory(prefix="deskpal-synthetic-memory-") as temporary:
            directory = Path(temporary)
            with SQLiteMemoryRepository(directory / "quality.sqlite3", clock=_clock) as repository:
                started = time.perf_counter()
                ids = _populate(repository, fixture)
                report["synthetic_population_seconds"] = time.perf_counter() - started
                retriever = MemoryRetriever(repository, embedding=embedding, clock=_clock)
                started = time.perf_counter()
                warnings = retriever.ensure_index()
                report["initial_incremental_index"] = {
                    "seconds": time.perf_counter() - started, "warnings": warnings,
                    "index_count": len(repository.embeddings(embedding.manifest)),
                }
                if warnings or report["initial_incremental_index"]["index_count"] != len(ids):
                    raise RuntimeError("Synthetic indexing failed; semantic benchmark would be invalid")
                started = time.perf_counter()
                retriever.ensure_index()
                report["already_current_index_check_seconds"] = time.perf_counter() - started
                report["calibration"] = []
                for threshold in fixture["candidate_thresholds"]:
                    _progress(f"Calibration only: threshold={threshold:.2f}")
                    metrics = _score(_runtime(repository, embedding=embedding, threshold=threshold),
                                     fixture["calibration"], ids)
                    report["calibration"].append({"threshold": threshold, "metrics": metrics})
                    _progress(f"  Recall@5={metrics['recall_at_5']:.3f}; negative_acceptance={metrics['negative_acceptance']:.3f}")
                threshold, feasible = _choose(report["calibration"])
                report["selected_threshold"] = threshold
                report["calibration_negative_constraint_feasible"] = feasible
                _progress(f"Threshold frozen at {threshold:.2f}; evaluating held-out queries once per system")
                baseline = _score(_runtime(repository, embedding=None, threshold=threshold), fixture["evaluation"], ids)
                hybrid = _score(_runtime(repository, embedding=embedding, threshold=threshold), fixture["evaluation"], ids)
                baseline_by_query = {row["id"]: row for row in baseline["queries"]}
                rescued = sum(bool(set(row["expected"]) & set(row["retrieved"]))
                              and not bool(set(row["expected"]) & set(baseline_by_query[row["id"]]["retrieved"]))
                              for row in hybrid["queries"] if row["expected"])
                report["evaluation"] = {"lexical": baseline, "hybrid": hybrid,
                                        "hybrid_rescued_positive_queries": rescued,
                                        "fixed_hybrid_evaluation_runs": 1}
                report["quality_target_met"] = hybrid["recall_at_5"] >= 0.80 and hybrid["negative_acceptance"] <= 0.10
                _progress(f"Evaluation hybrid Recall@5={hybrid['recall_at_5']:.3f}; negative_acceptance={hybrid['negative_acceptance']:.3f}; lexical rescues={rescued}")
            if args.stress:
                report["stress"] = _stress(directory, fixture, embedding, threshold, args.stress_size, args.stress_queries)
    finally:
        embedding.close()
    report["rss_final"] = _rss()
    baseline_rss = report["rss_before_model"].get("rss_bytes")
    peak_rss = report["rss_final"].get("peak_rss_bytes")
    memory_increment = max(0, peak_rss - baseline_rss) if isinstance(peak_rss, int) and isinstance(baseline_rss, int) else None
    report["model_and_retrieval_peak_rss_increment_bytes"] = memory_increment
    report["memory_target_bytes"] = 1024 ** 3
    report["memory_target_met"] = memory_increment <= 1024 ** 3 if memory_increment is not None else None
    report["full_semantic_milestone_recommended"] = bool(
        report["quality_target_met"] and args.stress and report["stress"].get("latency_target_met")
        and report["memory_target_met"]
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        _progress(f"Report written: {args.output.resolve()}")
    print(json.dumps({"selected_threshold": report["selected_threshold"],
                      "lexical": _summary(report["evaluation"]["lexical"]),
                      "hybrid": _summary(report["evaluation"]["hybrid"]),
                      "stress": report.get("stress"),
                      "model_and_retrieval_peak_rss_increment_bytes": memory_increment,
                      "full_semantic_milestone_recommended": report["full_semantic_milestone_recommended"]},
                     ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
