"""Immutable plans and conservative, durable execution checkpoints."""

from collections.abc import Callable, Iterator
from hashlib import sha256
import json
from typing import Any
from uuid import uuid4

from deskpal.planner.models import Plan
from deskpal.tasks.models import Task, TaskStatus, utc_now
from deskpal.tools.models import PermissionLevel, ToolCall, ToolResult, ToolSpec

from .models import (DeletionJob, DependencyRef, PlanSnapshot, StepAttempt,
                     TaskCancellationRequested, WorkflowBlocked)
from .store import WorkflowStore, canonical
from .tasks import PersistentTaskManager, dependency_ref


def _result_dict(result: ToolResult) -> dict[str, Any]:
    return {"success": result.success, "output": result.output, "error": result.error,
            "tool_name": result.tool_name, "call_id": result.call_id}


class ExecutionJournal:
    def __init__(self, store: WorkflowStore) -> None:
        self.store = store
        self.tasks = PersistentTaskManager(store)

    def add_validator(self, callback: Callable[[str, tuple[DependencyRef, ...]], None]) -> None:
        self.store.add_validator(callback)

    def set_dependencies(self, task_id: str, refs: tuple[Any, ...]) -> None:
        with self.store.transaction() as conn:
            if self.tasks.get(task_id) is None:
                raise KeyError(task_id)
            for value in refs:
                ref = dependency_ref(value)
                prior = conn.execute("SELECT revision FROM dependency WHERE task_id=? AND kind=? AND object_id=?", (task_id, ref.kind, ref.object_id)).fetchone()
                if prior is not None and prior[0] != ref.revision:
                    raise WorkflowBlocked("Existing task dependency cannot be replaced")
                conn.execute("INSERT OR IGNORE INTO dependency VALUES(?,?,?,?)", (task_id, ref.kind, ref.object_id, ref.revision))

    def get_dependencies(self, task_id: str) -> tuple[DependencyRef, ...]:
        return self.store.dependencies(task_id)

    def check_task(self, task_id: str) -> Task:
        # Cross-database validators run without the workflow write lock.
        self.store.validate_external(task_id)
        with self.store.transaction() as conn:
            task = self.tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.cancel_requested or task.status == TaskStatus.CANCELLED:
                raise TaskCancellationRequested("Task cancellation requested")
            if task.status in {TaskStatus.UNCERTAIN, TaskStatus.NEEDS_REVIEW}:
                raise WorkflowBlocked("Task requires explicit recovery review")
            self.store.check_dependencies(conn, task_id)
            return task

    def save_plan(self, task_id: str, plan: Plan, tool_specs: tuple[ToolSpec, ...],
                  workspace_dir: str, memory_refs: tuple[tuple[str, int], ...] = ()) -> PlanSnapshot:
        self.check_task(task_id)
        calls = [{"name": step.name, "arguments": dict(step.arguments), "call_id": step.call_id or f"step-{index}"}
                 for index, step in enumerate(plan.steps)]
        if len({call["call_id"] for call in calls}) != len(calls):
            raise ValueError("Plan call IDs must be unique")
        specs = [{"name": spec.name, "description": spec.description, "parameters": dict(spec.parameters),
                  "permission_level": int(spec.permission_level), "contract_version": getattr(spec, "contract_version", "1")}
                 for spec in tool_specs]
        value = {"task_id": task_id, "reply": plan.reply, "steps": calls, "tool_specs": specs,
                 "workspace_dir": str(workspace_dir), "memory_refs": list(memory_refs), "policy_version": "1"}
        with self.store.transaction() as conn:
            self.set_dependencies(task_id, memory_refs)
            self.store.check_dependencies(conn, task_id)
            task = self.tasks.get(task_id)
            assert task is not None
            if task.cancel_requested:
                raise TaskCancellationRequested("Task cancellation requested")
            value["dependencies"] = [(r.kind, r.object_id, r.revision) for r in self.get_dependencies(task_id)]
            value["use_policy"] = conn.execute("SELECT use_policy FROM task WHERE id=?", (task_id,)).fetchone()[0]
            encoded = canonical(value)
            if len(encoded.encode("utf-8")) > self.store.plan_max_bytes:
                raise ValueError("Plan exceeds byte limit; no executable snapshot saved")
            digest = sha256(encoded.encode("utf-8")).hexdigest()
            prior = conn.execute("SELECT hash,scrubbed FROM immutableplan WHERE task_id=?", (task_id,)).fetchone()
            if prior:
                if prior[0] != digest or prior[1]:
                    raise WorkflowBlocked("An immutable plan already exists for this task")
            else:
                conn.execute("INSERT INTO immutableplan(id,task_id,hash,canonical_json) VALUES(?,?,?,?)", (str(uuid4()), task_id, digest, encoded))
                self.store.record_event("plan_saved", task_id, {"hash": digest, "step_count": len(calls)})
            snapshot = self.get_plan(task_id)
            assert snapshot is not None
            return snapshot

    def get_plan(self, task_id: str) -> PlanSnapshot | None:
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM immutableplan WHERE task_id=?", (task_id,)).fetchone()
            if row is None or row["scrubbed"]:
                return None
            if sha256(row["canonical_json"].encode("utf-8")).hexdigest() != row["hash"]:
                raise WorkflowBlocked("Persisted plan integrity failed")
            data = json.loads(row["canonical_json"])
            plan = Plan(data["reply"], tuple(ToolCall(call["name"], call["arguments"], call["call_id"]) for call in data["steps"]))
            specs = tuple(ToolSpec(spec["name"], spec["description"], spec["parameters"],
                                   PermissionLevel(spec["permission_level"]), spec["contract_version"])
                          for spec in data["tool_specs"])
            return PlanSnapshot(row["id"], task_id, row["hash"], row["canonical_json"], plan, specs,
                                data["workspace_dir"], tuple((ref[0], ref[1]) for ref in data["memory_refs"]))

    def review(self, task_id: str) -> dict[str, Any]:
        """Read the task, immutable plan and attempts in one workflow transaction."""
        with self.store.transaction():
            task = self.tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            return {"task": task, "plan": self.get_plan(task_id), "attempts": self.attempts(task_id)}

    def start_step(self, task_id: str, index: int) -> StepAttempt:
        self.check_task(task_id)
        with self.store.transaction() as conn:
            task = self.tasks.get(task_id)
            assert task is not None
            if task.cancel_requested:
                raise TaskCancellationRequested("Task cancellation requested")
            if task.status != TaskStatus.RUNNING:
                raise WorkflowBlocked("Steps may start only while task is Running")
            self.store.check_dependencies(conn, task_id)
            snapshot = self.get_plan(task_id)
            if snapshot is None or not 0 <= index < len(snapshot.plan.steps):
                raise WorkflowBlocked("No matching immutable plan step")
            prior = conn.execute("SELECT step_index,status FROM attempt WHERE task_id=? ORDER BY step_index", (task_id,)).fetchall()
            if len(prior) != index or any(row["step_index"] != i or row["status"] != "Succeeded" for i, row in enumerate(prior)):
                raise WorkflowBlocked("A previous step is incomplete, failed, uncertain, or already executed")
            attempt_id = str(uuid4())
            conn.execute("INSERT INTO attempt(id,task_id,step_index,status) VALUES(?,?,?,'Started')", (attempt_id, task_id, index))
            self.store.record_event("step_started", task_id, {"attempt_id": attempt_id, "index": index})
            return StepAttempt(attempt_id, task_id, index, "Started")

    def finish_step(self, attempt_id: str, result: ToolResult) -> None:
        if not isinstance(result, ToolResult) or type(result.success) is not bool:
            raise ValueError("Step result must contain a boolean outcome")
        with self.store.transaction() as conn:
            row = conn.execute("SELECT a.*,t.scrubbed FROM attempt a JOIN task t ON t.id=a.task_id WHERE a.id=?", (attempt_id,)).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            if row["status"] != "Started":
                raise WorkflowBlocked("Attempt already has an outcome; it cannot be overwritten")
            snapshot = self.get_plan(row["task_id"])
            body = _result_dict(result)
            if snapshot is not None:
                call = snapshot.plan.steps[row["step_index"]]
                if result.tool_name and result.tool_name != call.name or result.call_id and result.call_id != call.call_id:
                    raise WorkflowBlocked("Step result does not match its immutable tool call")
                body["tool_name"], body["call_id"] = call.name, call.call_id
            omitted = bool(row["scrubbed"])
            try:
                encoded = canonical(body)
                omitted = omitted or len(encoded.encode("utf-8")) > self.store.result_max_bytes
            except (ValueError, TypeError):
                omitted, encoded = True, ""
            if omitted:
                # Outcome survives oversized, unsupported or forgotten output bodies.
                encoded = canonical({"success": result.success, "output": {"output_omitted": True},
                                     "error": None, "tool_name": "" if row["scrubbed"] else body["tool_name"],
                                     "call_id": "" if row["scrubbed"] else body["call_id"]})
            status = "Succeeded" if result.success else "Failed"
            conn.execute("UPDATE attempt SET status=?,result=? WHERE id=?", (status, encoded, attempt_id))
            self.store.record_event("step_finished", row["task_id"], {"attempt_id": attempt_id, "status": status})

    def attempts(self, task_id: str) -> tuple[StepAttempt, ...]:
        with self.store.transaction() as conn:
            rows = conn.execute("SELECT * FROM attempt WHERE task_id=? ORDER BY step_index", (task_id,)).fetchall()
            result: list[StepAttempt] = []
            for row in rows:
                outcome = None
                if row["result"]:
                    try:
                        outcome = ToolResult(**json.loads(row["result"]))
                    except (TypeError, ValueError):
                        pass  # Recovery classifies missing reliable outcome as Uncertain.
                result.append(StepAttempt(row["id"], task_id, row["step_index"], row["status"], outcome))
            return tuple(result)

    def mark_uncertain(self, task_id: str, error: str) -> Task:
        with self.store.transaction() as conn:
            conn.execute("UPDATE attempt SET status='Uncertain' WHERE task_id=? AND status='Started'", (task_id,))
            conn.execute("UPDATE task SET status=?,error=?,revision=revision+1,updated_at=? WHERE id=?",
                         (TaskStatus.UNCERTAIN.value, "Execution outcome requires review", utc_now().isoformat(), task_id))
            conn.execute("UPDATE submission SET status='Blocked' WHERE task_id=?", (task_id,))
            self.store.record_event("task_uncertain", task_id)
            task = self.tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            return task

    def recover(self) -> tuple[Task, ...]:
        recovered: list[Task] = []
        # Each external authority is consulted outside a workflow transaction.
        for initial in self._recovery_tasks():
            external_valid = True
            try:
                self.store.validate_external(initial.id)
            except (ValueError, RuntimeError):
                external_valid = False
            corrupt_plan = False
            try:
                plan = self.get_plan(initial.id)
            except (WorkflowBlocked, ValueError, KeyError, TypeError):
                corrupt_plan, plan = True, None
            with self.store.transaction() as conn:
                task = self.tasks.get(initial.id)
                assert task is not None
                attempts = self.attempts(task.id)
                scrubbed = bool(conn.execute("SELECT scrubbed FROM task WHERE id=?", (task.id,)).fetchone()[0])
                if scrubbed:
                    # Erasing parameters is not evidence that a previously known
                    # effect became unknown. Preserve the opaque outcome ledger.
                    if any(a.status in {"Started", "Uncertain"} for a in attempts):
                        recovered.append(self.mark_uncertain(task.id, "Unresolved forgotten attempt"))
                    elif task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.UNCERTAIN}:
                        conn.execute("UPDATE task SET status=?,revision=revision+1 WHERE id=?", (TaskStatus.NEEDS_REVIEW.value, task.id))
                        self.tasks.finish_submission(task.id, "Blocked")
                        updated = self.tasks.get(task.id)
                        assert updated is not None
                        recovered.append(updated)
                    continue
                consistent = not corrupt_plan
                for index, attempt in enumerate(attempts):
                    if attempt.index != index or plan is None or index >= len(plan.plan.steps):
                        consistent = False
                        break
                    if attempt.status in {"Succeeded", "Failed"}:
                        result = attempt.result
                        call = plan.plan.steps[index]
                        if result is None or type(result.success) is not bool or result.success != (attempt.status == "Succeeded") or result.tool_name != call.name or result.call_id != call.call_id:
                            consistent = False
                            break
                    elif attempt.status not in {"Started", "Uncertain"}:
                        consistent = False
                        break
                    if index < len(attempts) - 1 and attempt.status != "Succeeded":
                        consistent = False
                        break
                if not consistent or any(attempt.status in {"Started", "Uncertain"} for attempt in attempts):
                    recovered.append(self.mark_uncertain(task.id, "Unresolved started attempt"))
                    continue
                if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.UNCERTAIN}:
                    continue
                all_succeeded = plan is not None and len(attempts) == len(plan.plan.steps) and all(a.status == "Succeeded" for a in attempts)
                valid = external_valid
                try:
                    self.store.check_dependencies(conn, task.id)
                except WorkflowBlocked:
                    valid = False
                if all_succeeded and valid:
                    status = TaskStatus.CANCELLED if task.cancel_requested else TaskStatus.COMPLETED
                    recovered_result = {"reply": plan.plan.reply if plan else "", "tool_results": [_result_dict(a.result) for a in attempts if a.result],
                              "warning": "Recovered from committed tool outcomes; reflection was not replayed"}
                    encoded_result = canonical(recovered_result)
                    if len(encoded_result.encode("utf-8")) > self.store.result_max_bytes:
                        encoded_result = canonical({"output_omitted": True, "warning": "Recovered committed outcomes; aggregate output omitted"})
                    conn.execute("UPDATE task SET status=?,result=?,error=NULL,revision=revision+1 WHERE id=?", (status.value, encoded_result, task.id))
                    self.tasks.finish_submission(task.id)
                    self.store.record_event("task_recovered_finalized", task.id, {"status": status.value})
                elif task.cancel_requested and not attempts:
                    conn.execute("UPDATE task SET status=?,revision=revision+1 WHERE id=?", (TaskStatus.CANCELLED.value, task.id))
                    self.tasks.finish_submission(task.id)
                else:
                    conn.execute("UPDATE task SET status=?,revision=revision+1 WHERE id=?", (TaskStatus.NEEDS_REVIEW.value, task.id))
                    conn.execute("UPDATE submission SET status='Blocked' WHERE task_id=?", (task.id,))
                    self.store.record_event("task_recovery_review_required", task.id, {"completed_steps": sum(a.status == "Succeeded" for a in attempts)})
                updated = self.tasks.get(task.id)
                assert updated is not None
                recovered.append(updated)
        return tuple(recovered)

    def _recovery_tasks(self) -> Iterator[Task]:
        offset = 0
        while True:
            page = self.tasks.list_tasks(limit=100, offset=offset)
            yield from page
            if len(page) < 100:
                return
            offset += len(page)

    def resume_unstarted(self, task_id: str) -> Task:
        self.store.validate_external(task_id)
        return self.tasks.resume(task_id)

    def review_uncertain(self, task_id: str, verdict: str, evidence: str) -> Task:
        if verdict not in {"occurred", "not_occurred", "unknown"} or not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("Review needs occurred/not_occurred/unknown and non-empty evidence")
        if len(evidence.encode("utf-8")) > self.store.result_max_bytes:
            raise ValueError("Review evidence exceeds byte limit")
        with self.store.transaction() as conn:
            task = self.tasks.get(task_id)
            if task is None or task.status != TaskStatus.UNCERTAIN:
                raise WorkflowBlocked("Only uncertain tasks can be reviewed")
            scrubbed = conn.execute("SELECT scrubbed FROM task WHERE id=?", (task_id,)).fetchone()[0]
            conn.execute("UPDATE attempt SET review_verdict=?,review_evidence=? WHERE task_id=? AND status='Uncertain'", (verdict, None if scrubbed else evidence, task_id))
            self.store.record_event("uncertain_reviewed", task_id, {"verdict": verdict})
            return task

    def begin_deletion(self, memory_id: str, expected_revision: int) -> str:
        if not memory_id or expected_revision < 1:
            raise ValueError("Deletion requires memory ID and positive revision")
        with self.store.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO memory_block VALUES(?)", (memory_id,))
            affected = conn.execute("SELECT t.id,t.status FROM task t JOIN dependency d ON d.task_id=t.id WHERE d.kind='memory' AND d.object_id=?", (memory_id,)).fetchall()
            for task in affected:
                conn.execute("UPDATE audit_outbox SET data='{}' WHERE task_id=?", (task["id"],))
                if task["status"] in {TaskStatus.CREATED.value, TaskStatus.WAITING.value, TaskStatus.NEEDS_REVIEW.value}:
                    conn.execute("UPDATE task SET status=?,revision=revision+1 WHERE id=?", (TaskStatus.NEEDS_REVIEW.value, task["id"]))
                conn.execute("UPDATE submission SET status='Blocked' WHERE task_id=? AND status IN ('Queued','Claimed')", (task["id"],))
            row = conn.execute("SELECT id FROM deletionjob WHERE memory_id=? AND expected_revision=? AND status!='Completed'", (memory_id, expected_revision)).fetchone()
            if row:
                return str(row[0])
            job_id = str(uuid4())
            conn.execute("INSERT INTO deletionjob(id,memory_id,expected_revision) VALUES(?,?,?)", (job_id, memory_id, expected_revision))
            self.store.record_event("memory_deletion_started", data={"job_id": job_id, "memory_id": memory_id})
            return job_id

    def pending_deletions(self) -> tuple[DeletionJob, ...]:
        with self.store.transaction() as conn:
            return tuple(DeletionJob(*row) for row in conn.execute("SELECT id,memory_id,expected_revision,status FROM deletionjob WHERE status!='Completed' ORDER BY rowid"))

    def scrub_memory(self, memory_id: str) -> tuple[str, ...]:
        with self.store.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO memory_block VALUES(?)", (memory_id,))
            ids = tuple(row[0] for row in conn.execute("SELECT task_id FROM dependency WHERE kind='memory' AND object_id=?", (memory_id,)))
            for task_id in ids:
                self.tasks.scrub(task_id)
            return ids

    def complete_deletion(self, job_id: str) -> None:
        with self.store.transaction() as conn:
            row = conn.execute("SELECT memory_id FROM deletionjob WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            self.scrub_memory(row[0])
            conn.execute("UPDATE deletionjob SET status='Completed' WHERE id=?", (job_id,))
            self.store.record_event("memory_deletion_completed", data={"job_id": job_id})
