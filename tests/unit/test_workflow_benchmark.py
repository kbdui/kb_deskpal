"""The opt-in benchmark remains hermetic and distinguishes smoke from acceptance."""

import json
import sys

import pytest

from scripts import benchmark_workflow as benchmark


def test_latency_uses_nearest_rank_p95_and_counts_every_sample():
    result = benchmark.latencies([index / 1000 for index in range(1, 101)])
    assert result["samples"] == 100
    assert result["p95_ms"] == 95
    assert result["max_ms"] == 100


@pytest.mark.parametrize("flags", [
    ["--seconds", "nan"], ["--seconds", "inf"], ["--seconds", "0"],
    ["--warmup-seconds", "nan"], ["--interval-seconds", "0"],
    ["--sample-seconds", "-1"], ["--goals", "0"], ["--tasks", "-1"],
    ["--proposals", "0"], ["--latency-samples", "0"],
])
def test_invalid_arguments_fail_before_application_or_configuration(flags, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid benchmark flags must not create an application")

    monkeypatch.setattr(benchmark, "load_config", forbidden)
    monkeypatch.setattr(sys, "argv", ["benchmark_workflow.py", *flags])
    with pytest.raises(SystemExit) as error:
        benchmark.main()
    assert error.value.code == 2


def test_idle_smoke_ignores_local_config_and_cannot_claim_five_minute_acceptance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_bytes(b"SYNTHETIC_PRIVATE_MARKER=do-not-read\xff")
    (tmp_path / "config.toml").write_text("invalid TOML [", encoding="utf-8")
    output = tmp_path / "idle.json"
    monkeypatch.setattr(sys, "argv", ["benchmark_workflow.py", "--mode", "idle", "--seconds", ".2",
        "--warmup-seconds", "0", "--sample-seconds", ".05", "--output", str(output)])
    assert benchmark.main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "complete"
    assert report["acceptance_duration_met"] is False
    assert report["settings"]["proactive_enabled"] is True
    assert report["closed_within_5_seconds"] is True
    assert report["iterations"] == 0
    assert report["samples"]
    assert "SYNTHETIC_PRIVATE_MARKER" not in json.dumps(report)


def test_small_capacity_records_real_echo_fixture_and_separate_warm_latencies(tmp_path, monkeypatch):
    output = tmp_path / "capacity.json"
    monkeypatch.setattr(sys, "argv", ["benchmark_workflow.py", "--mode", "capacity", "--goals", "2",
        "--tasks", "3", "--proposals", "4", "--latency-samples", "2", "--output", str(output)])
    assert benchmark.main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["fixture_counts"] == {"goals": 2, "tasks": 3, "step_attempts": 6, "proposals": 4}
    assert "actual EchoTool.execute" in report["fixture_scope"]
    assert report["fixture_synthetic_ledger"] is True
    assert report["fixture_validates_checkpoint_before_effect"] is False
    assert report["capacity_target_met"] is False
    assert report["latency_target_met"] is False
    assert all(value["samples"] == 2 for value in report["warm_latencies"].values())
    assert report["capacity_pages"]["tasks"]["stable_repeat_order"] is True
    assert report["sqlite_integrity"] == "ok"
    assert report["closed_within_5_seconds"] is True
