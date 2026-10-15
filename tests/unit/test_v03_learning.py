"""Learning authority/privacy regressions against temporary repositories only."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import pytest

from deskpal.llm.models import ChatMessage, LLMResponse
from deskpal.memory.contracts import (
    ClaimType, MemoryDraft, MemoryError, MemoryRuntime, MemorySource,
    MemoryStatus, RevisionConflict, UsePolicy,
)
from deskpal.memory.coordinator import MemoryCoordinator
from deskpal.memory.evaluation import LLMMemoryEvaluator, profile_hint
from deskpal.memory.models import MemoryRecord
from deskpal.memory.repository import SQLiteMemoryRepository
from deskpal.tools.models import ToolCall, ToolSpec


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class LLM:
    def __init__(self, content: str = '{"candidates": []}') -> None:
        self.response = LLMResponse(content)
        self.calls: list[tuple[tuple[ChatMessage, ...], tuple[ToolSpec, ...]]] = []
        self.error: Exception | None = None

    def chat(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] = ()) -> LLMResponse:
        self.calls.append((tuple(messages), tuple(tools)))
        if self.error:
            raise self.error
        return self.response

    def generate(self, prompt: str) -> str:
        raise AssertionError("Independent extraction must use chat")


def candidate(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "content": "我喜欢茶", "quote": "我喜欢茶", "subject": "user",
        "predicate": "statement", "value": "", "scope": "global",
        "profile_key": "", "importance": .5, "extraction_confidence": .7,
    }
    result.update(changes)
    return result


def evaluator_for(*items: dict[str, object]) -> tuple[LLMMemoryEvaluator, LLM]:
    llm = LLM(json.dumps({"candidates": items}, ensure_ascii=False))
    return LLMMemoryEvaluator(llm), llm


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def repo(tmp_path: Path, clock: Clock):
    repository = SQLiteMemoryRepository(tmp_path / "memory.sqlite", clock=clock)
    yield repository
    repository.close()


def test_explicit_remember_retains_exact_source_without_evaluation(repo, clock: Clock) -> None:
    evaluator, llm = evaluator_for(candidate())
    runtime: MemoryRuntime = MemoryCoordinator(repo, evaluator=evaluator, clock=clock)
    item = runtime.remember("  我喜欢茶。 ", task_id="task")
    assert item.status == MemoryStatus.ACCEPTED
    assert item.source.origin == "user_message"
    assert item.source.content == item.draft.quote == "  我喜欢茶。 "
    assert item.draft.claim_type == ClaimType.USER_REPORT
    assert item.draft.profile_key == ""
    assert llm.calls == []


@pytest.mark.parametrize("text,key,value", [
    ("请叫我小明", "preferred_name", "小明"),
    ("请用中文回答。", "reply_language", "zh"),
    ("以后用英文回答", "reply_language", "en"),
    ("我喜欢简洁的回复", "verbosity", "brief"),
    ("请详细回答", "verbosity", "detailed"),
])
def test_narrow_profile_grammar(text: str, key: str, value: str, repo, clock: Clock) -> None:
    item = MemoryCoordinator(repo, clock=clock).remember(text)
    assert (item.draft.profile_key, item.draft.value) == (key, value)


@pytest.mark.parametrize("text", [
    "朋友说请叫我小明", "如果我说请用中文回答", '“请用中文回答”',
    "小说里的我喜欢简短回答", "请用中文回答，但朋友用英文", "我住在上海", "我很高兴",
])
def test_ambiguous_statements_do_not_become_profile(text: str) -> None:
    assert profile_hint(text) is None


def test_explicit_profile_metadata_requires_exact_original_evidence(repo, clock: Clock) -> None:
    runtime = MemoryCoordinator(repo, clock=clock)
    with pytest.raises(MemoryError):
        runtime.remember("朋友说请用中文回答", profile_key="reply_language", profile_value="zh")
    assert repo.list_items() == ()


def test_conflicting_profile_requires_cas_correction_and_preserves_policy(repo, clock: Clock) -> None:
    runtime = MemoryCoordinator(repo, clock=clock)
    first = runtime.remember("请用中文回答", use_policy=UsePolicy.LOCAL_ONLY)
    with pytest.raises(MemoryError, match="correct"):
        runtime.remember("请用英文回答")
    with pytest.raises(RevisionConflict):
        runtime.correct(first.id, first.revision + 1, "请用英文回答")
    corrected = runtime.correct(first.id, first.revision, "请用英文回答")
    assert corrected.source.use_policy == UsePolicy.LOCAL_ONLY
    assert corrected.draft.value == "en"
    assert not runtime.valid(((first.id, first.revision),))
    assert runtime.profile()[0].value == "en"  # management sees local-only
    assert runtime.retrieve("回答").records == ()  # cloud context does not


@pytest.mark.parametrize("mode", ["explicit_only", "paused"])
def test_learning_is_opt_in(mode: str, repo, clock: Clock) -> None:
    evaluator, llm = evaluator_for(candidate())
    assert MemoryCoordinator(repo, evaluator=evaluator, mode=mode, clock=clock).learn("我喜欢茶", "task") == ()
    assert llm.calls == []
    assert repo.list_items() == ()


def test_candidates_are_not_accepted_and_ttl_is_removed_on_accept(repo, clock: Clock) -> None:
    evaluator, llm = evaluator_for(candidate())
    runtime = MemoryCoordinator(repo, evaluator=evaluator, mode="candidate_review", clock=clock)
    assert runtime.learn("我喜欢茶", "task") == ()
    item, = runtime.list_items()
    assert item.status == MemoryStatus.CANDIDATE
    assert item.draft.expires_at == clock.value + timedelta(days=7)
    assert item.source.content == item.draft.quote == "我喜欢茶"
    assert runtime.retrieve("茶").records == ()
    accepted = runtime.accept(item.id, item.revision)
    assert accepted.status == MemoryStatus.ACCEPTED
    assert accepted.draft.expires_at is None
    assert len(llm.calls) == 1
    messages, tools = llm.calls[0]
    assert tuple(message.role for message in messages) == ("system", "user")
    assert json.loads(messages[1].content) == {"evidence": "我喜欢茶"}
    assert tools == ()


def test_learning_consumed_across_time_delete_and_reopen(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "restart.sqlite"
    evaluator, llm = evaluator_for(candidate())
    with SQLiteMemoryRepository(path, clock=clock) as repository:
        runtime = MemoryCoordinator(repository, evaluator=evaluator, mode="candidate_review", clock=clock)
        assert runtime.learn("我喜欢茶", "task") == ()
        item, = runtime.list_items()
        runtime.forget(item.id, item.revision)
    clock.value += timedelta(days=1)
    with SQLiteMemoryRepository(path, clock=clock) as repository:
        runtime = MemoryCoordinator(repository, evaluator=evaluator, mode="candidate_review", clock=clock)
        assert runtime.learn("换一种请求也不能重用任务", "task") == ()
        assert len(llm.calls) == 1
        assert all(item.status == MemoryStatus.DELETED for item in runtime.list_items())


def test_failed_evaluation_is_consumed_without_leaking_input(repo, clock: Clock) -> None:
    llm = LLM()
    llm.error = RuntimeError("SECRET user private text")
    runtime = MemoryCoordinator(repo, evaluator=LLMMemoryEvaluator(llm), mode="candidate_review", clock=clock)
    warnings = runtime.learn("SECRET user private text", "task")
    assert warnings and "SECRET" not in str(warnings)
    assert runtime.learn("SECRET user private text", "task") == ()
    assert len(llm.calls) == 1
    assert repo.list_items() == ()


def test_empty_evaluation_is_consumed(repo, clock: Clock) -> None:
    evaluator, llm = evaluator_for()
    runtime = MemoryCoordinator(repo, evaluator=evaluator, mode="candidate_review", clock=clock)
    assert runtime.learn("我喜欢茶", "task") == ()
    assert runtime.learn("我喜欢茶", "task") == ()
    assert len(llm.calls) == 1


def test_local_only_never_reaches_evaluator(repo, clock: Clock) -> None:
    evaluator, llm = evaluator_for(candidate())
    runtime = MemoryCoordinator(repo, evaluator=evaluator, mode="candidate_review", clock=clock)
    assert runtime.learn("我喜欢茶", "task", use_policy=UsePolicy.LOCAL_ONLY)
    with pytest.raises(MemoryError):
        evaluator.evaluate(MemorySource("我喜欢茶", use_policy=UsePolicy.LOCAL_ONLY))
    assert llm.calls == []
    assert repo.list_items() == ()


def test_execution_idempotence_after_clock_advance_correction_and_deletion(repo, clock: Clock) -> None:
    runtime = MemoryCoordinator(repo, clock=clock)
    item = runtime.record_execution("task", '{"success":true}')
    assert item.draft.claim_type == ClaimType.OBSERVED_EVENT
    assert item.source.origin == "tool_result"
    clock.value += timedelta(seconds=5)
    assert runtime.record_execution("task", '{"success":true}').id == item.id
    with pytest.raises(MemoryError):
        runtime.record_execution("task", '{"success":false}')
    runtime.correct(item.id, item.revision, "结果已更正")
    # Corrections create a new assertion; replay still identifies the original
    # event, without creating another record or overwriting the correction.
    assert runtime.record_execution("task", '{"success":true}').id == item.id
    runtime.forget(item.id, repo.get(item.id).revision)
    assert runtime.record_execution("task", '{"success":true}').status == MemoryStatus.DELETED


def test_export_deletion_and_revision_validity(repo, clock: Clock) -> None:
    runtime = MemoryCoordinator(repo, cloud=False, clock=clock)
    item = runtime.remember("SECRET", use_policy=UsePolicy.LOCAL_ONLY)
    assert runtime.valid(((item.id, item.revision),))
    assert not MemoryCoordinator(repo, cloud=True, clock=clock).valid(((item.id, item.revision),))
    runtime.forget(item.id, item.revision)
    assert not runtime.valid(((item.id, item.revision),))
    assert "SECRET" not in runtime.export()
    assert "SECRET" not in runtime.export(include_history=True)
    assert json.loads(runtime.export())["items"] == []


def test_validity_is_half_open(repo, clock: Clock) -> None:
    runtime = MemoryCoordinator(repo, clock=clock)
    item = repo.ingest(MemorySource("期限", created_at=clock.value),
                       MemoryDraft("期限", "期限", valid_to=clock.value + timedelta(seconds=1)), accepted=True)
    assert runtime.valid(((item.id, item.revision),))
    clock.value += timedelta(seconds=1)
    assert not runtime.valid(((item.id, item.revision),))


def test_legacy_save_cannot_overwrite_accepted(repo, clock: Clock) -> None:
    runtime = MemoryCoordinator(repo, clock=clock)
    accepted = runtime.remember("我喜欢茶")
    with pytest.raises(MemoryError):
        runtime.save(MemoryRecord("不应覆盖", id=accepted.id))
    assert repo.get(accepted.id).draft.content == "我喜欢茶"
    legacy = runtime.save(MemoryRecord("旧接口"))
    assert repo.get(legacy.id).status == MemoryStatus.CANDIDATE
    assert repo.get(legacy.id).draft.claim_type == ClaimType.LEGACY


@pytest.mark.parametrize("payload", [
    'not-json', '{"candidates":[],"candidates":[]}', '{"candidates":[],"accepted":true}',
    '{"candidates":{}}', '{"candidates":[{}]}',
])
def test_invalid_top_level_json_rejected(payload: str) -> None:
    evaluator = LLMMemoryEvaluator(LLM(payload))
    with pytest.raises(MemoryError):
        evaluator.evaluate(MemorySource("我喜欢茶"))


@pytest.mark.parametrize("changes", [
    {"accepted": True}, {"source": "user"}, {"quote": "不存在"}, {"importance": True},
    {"importance": float("nan")}, {"importance": float("inf")}, {"importance": 2},
    {"extraction_confidence": -1}, {"content": []}, {"subject": ""},
    {"profile_key": "admin", "value": "true"},
])
def test_candidate_schema_and_evidence_rejected(changes: dict[str, object]) -> None:
    evaluator, _ = evaluator_for(candidate(**changes))
    with pytest.raises(MemoryError):
        evaluator.evaluate(MemorySource("我喜欢茶"))


def test_nested_duplicate_key_and_exponent_infinity_rejected() -> None:
    text = json.dumps({"candidates": [candidate()]}, ensure_ascii=False)
    for altered in (text.replace('"importance": 0.5', '"importance": 0.5, "importance": 0.2'),
                    text.replace('"importance": 0.5', '"importance": 1e999')):
        with pytest.raises(MemoryError):
            LLMMemoryEvaluator(LLM(altered)).evaluate(MemorySource("我喜欢茶"))


def test_profile_candidate_cannot_promote_quoted_speech() -> None:
    item = candidate(content="用中文", quote="请用中文回答", predicate="reply_language",
                     profile_key="reply_language", value="zh")
    evaluator, _ = evaluator_for(item)
    with pytest.raises(MemoryError):
        evaluator.evaluate(MemorySource("朋友说：请用中文回答"))
    assert evaluator.evaluate(MemorySource("请用中文回答"))[0].profile_key == "reply_language"


def test_tools_and_oversized_input_rejected_before_persistence() -> None:
    llm = LLM()
    llm.response = LLMResponse("{}", (ToolCall("echo", {"text": "oops"}),))
    with pytest.raises(MemoryError):
        LLMMemoryEvaluator(llm).evaluate(MemorySource("我喜欢茶"))
    llm.calls.clear()
    with pytest.raises(MemoryError):
        LLMMemoryEvaluator(llm, max_input_bytes=5).evaluate(MemorySource("我喜欢茶"))
    assert llm.calls == []


def test_candidate_limit_configured_above_five_is_respected(repo, clock: Clock) -> None:
    evaluator, llm = evaluator_for(*(candidate(content=f"茶{i}") for i in range(6)))
    evaluator.max_candidates = 6
    runtime = MemoryCoordinator(repo, evaluator=evaluator, mode="candidate_review", clock=clock)
    assert runtime.learn("我喜欢茶", "task") == ()
    assert len(runtime.list_items()) == 6
    assert len(llm.calls) == 1
    evaluator.max_candidates = 5
    with pytest.raises(MemoryError):
        evaluator.evaluate(MemorySource("我喜欢茶"))


def test_invalid_custom_evaluator_batch_is_validated_before_any_write(repo, clock: Clock) -> None:
    class Evaluator:
        def evaluate(self, source: MemorySource) -> tuple[MemoryDraft, ...]:
            return (MemoryDraft("有效", "我喜欢茶"), MemoryDraft("无效", "不存在"))

    runtime = MemoryCoordinator(repo, evaluator=Evaluator(), mode="candidate_review", clock=clock)
    assert runtime.learn("我喜欢茶", "task")
    assert repo.list_items() == ()


def test_candidate_cleanup_does_not_delete_accepted(repo, clock: Clock) -> None:
    runtime = MemoryCoordinator(repo, clock=clock)
    accepted = runtime.remember("必须保留")
    candidate_item = repo.ingest(MemorySource("候选", created_at=clock.value), MemoryDraft("候选", "候选"))
    clock.value += timedelta(days=8)
    runtime.cleanup_candidates()
    assert repo.get(accepted.id).status == MemoryStatus.ACCEPTED
    assert repo.get(candidate_item.id).status == MemoryStatus.DELETED
