"""Application-owned context, controlled model calls and dialogue publication."""

from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from hashlib import sha256
from threading import RLock
from typing import Any

from deskpal.agent.context import PreparedTurn
from deskpal.agent.ports import CompanionPort
from deskpal.companion.signals import validate_preference
from deskpal.conversation.runtime import ConversationRuntime
from deskpal.llm.calls import ModelCallGateway
from deskpal.llm.ports import LLMProvider
from deskpal.memory.contracts import MemoryRuntime, RetrievalBundle, UsePolicy
from deskpal.tasks.models import TaskStatus
from deskpal.workflow.humanization import HumanizationStore
from deskpal.workflow.models import WorkflowBlocked
from deskpal.workflow.store import canonical
from deskpal.workflow.tasks import PersistentTaskManager

from .privacy import PrivacyCoordinator


def _refs(values: Any) -> tuple[tuple[str, str, int], ...]:
    result: set[tuple[str, str, int]] = set()
    for value in values:
        if isinstance(value, Mapping):
            item = (str(value.get("kind", "memory")),
                    str(value.get("id", value.get("object_id", ""))), value.get("revision"))
        else:
            item = tuple(value)
        if len(item) != 3 or not item[0] or not item[1] or type(item[2]) is not int or item[2] < 1:
            raise ValueError("Context has an invalid source reference")
        result.add((str(item[0]), str(item[1]), item[2]))
        if len(result) > 512:
            raise ValueError("Context source budget exceeded")
    return tuple(sorted(result))


class InteractionCoordinator:
    def __init__(self, *, persistence: HumanizationStore, privacy: PrivacyCoordinator,
                 conversations: ConversationRuntime, tasks: PersistentTaskManager,
                 memory: MemoryRuntime, companion: CompanionPort | None,
                 provider: LLMProvider, cloud: bool) -> None:
        self.persistence, self.privacy, self.conversations = persistence, privacy, conversations
        self.tasks, self.memory, self.companion, self.cloud = tasks, memory, companion, cloud
        evaluation_provider = getattr(memory, "evaluation_provider", None)
        self.gateway = ModelCallGateway(provider, reserve=persistence.reserve_call,
            finish=persistence.finish_call,
            providers={"memory_evaluation": evaluation_provider} if evaluation_provider is not None else {})
        self._lock = RLock()
        self._prepared: OrderedDict[str, PreparedTurn] = OrderedDict()

    def _settings(self) -> dict[str, Any]:
        read = getattr(self.companion, "interaction_snapshot", None)
        try:
            return dict(read()) if callable(read) else {}
        except Exception:
            return {"degraded": True}

    def source_policy(self, task_id: str) -> str:
        return self.tasks.task_use_policy(task_id)

    def retrieve(self, request: str) -> RetrievalBundle:
        # Profile projection has a separate expression-only permission boundary.
        # Production Memory exposes this neutral view without changing the
        # established retrieve() contract used by lower-level clients.
        retrieve = getattr(self.memory, "retrieve_for_planning", None)
        if callable(retrieve):
            bundle = retrieve(request)
            if not isinstance(bundle, RetrievalBundle):
                raise ValueError("Memory runtime returned an invalid planning bundle")
            return bundle
        raise ValueError("Memory runtime does not support neutral planning retrieval")

    def _profile(self) -> tuple[dict[str, str], tuple[tuple[str, str, int], ...]]:
        settings = self._settings()
        try:
            enabled = self.companion is not None and self.companion.snapshot().get("enabled") is True
        except Exception:
            enabled = False
        if not enabled:
            return {}, ()
        if self.cloud and not settings.get("cloud_permissions", {}).get("preferences", False):
            return {}, ()
        values: dict[str, str] = {}
        refs: list[tuple[str, str, int]] = []
        for entry in self.memory.profile():
            if self.cloud and entry.use_policy != UsePolicy.CLOUD_ALLOWED:
                continue
            values[entry.key] = entry.value
            refs.append(("memory", entry.memory_id, entry.revision))
        return values, tuple(refs)

    def prepare(self, task_id: str, request: str, bundle: RetrievalBundle) -> PreparedTurn:
        with self._lock:
            prior = self._prepared.get(task_id)
            if prior is not None:
                self.check(task_id)
                return prior
            # Never silently evict a Waiting task's admission context.
            for identity in tuple(self._prepared):
                task = self.tasks.get(identity)
                if task is None or task.status not in {TaskStatus.WAITING, TaskStatus.CREATED,
                                                      TaskStatus.PLANNING, TaskStatus.RUNNING}:
                    self._prepared.pop(identity, None)
            if len(self._prepared) >= 128:
                raise WorkflowBlocked("Interaction admission capacity reached")
            state = self.persistence.privacy()
            self.privacy.check(state["epoch"])
            use_policy = self.tasks.task_use_policy(task_id)
            if self.cloud and use_policy == "local_only":
                raise WorkflowBlocked("This request is local_only")
            link = self.persistence.turn(task_id)
            if link is None:
                self.persistence.bind_turn(task_id, "single:" + task_id, 0, 1)
                link = self.persistence.turn(task_id)
            assert link is not None
            conversation_id = str(link["conversation_id"])
            is_conversation = not conversation_id.startswith("single:")
            settings = self._settings()
            admitted_categories = {key for key, allowed in settings.get("cloud_permissions", {}).items() if allowed}
            frozen: dict[str, Any] = {"history": (), "planning_items": (), "warnings": ()}
            saved = self.persistence.context(task_id, "planning")
            legacy_plan = saved is None and self.privacy.journal.get_plan(task_id) is not None
            if is_conversation and saved is None and not legacy_plan:
                frozen = self.conversations.freeze(conversation_id, int(link["epoch"]), int(link["sequence"]),
                    cloud=self.cloud,
                    history_allowed=bool(settings.get("cloud_permissions", {}).get("history", False)))
            elif saved is not None:
                # Recovered plans retain their original neutral snapshot. No
                # dialogue is reconstructed from persisted task/assistant text.
                frozen["planning_items"] = saved["items"]
                frozen["warnings"] = ("Recovered plan: expression history was not restored",)
            overrides: dict[str, Any] = {}
            prepare = getattr(self.companion, "prepare_interaction", None)
            if callable(prepare) and not settings.get("degraded"):
                try:
                    overrides = dict(prepare(request, task_id=task_id,
                        conversation_id=conversation_id, use_policy=use_policy))
                except Exception:
                    frozen["warnings"] = (*frozen["warnings"], "Interaction preferences could not be saved")
            profile, profile_refs = self._profile()
            style_settings = self._settings()
            admitted_categories.update(key for key, allowed in style_settings.get("cloud_permissions", {}).items() if allowed)
            style = ""
            if self.companion is not None and not settings.get("degraded"):
                try:
                    if self.companion.snapshot().get("enabled") is True:
                        style = self.companion.style_instruction(request, cloud=self.cloud,
                            preferences=profile, conversation_id=conversation_id, use_policy=use_policy)
                except Exception:
                    frozen["warnings"] = (*frozen["warnings"], "Optional expression settings are unavailable")
            settings = self._settings()
            admitted_categories.update(key for key, allowed in settings.get("cloud_permissions", {}).items() if allowed)
            if profile:
                admitted_categories.add("preferences")
            if frozen["history"] or frozen["planning_items"]:
                admitted_categories.add("history")
            expression_epoch = int(style_settings.get("interaction_epoch", 0))
            if settings.get("interaction_epoch", 0) != expression_epoch:
                style = ""
            memory_refs = tuple(("memory", identity, revision) for identity, revision in bundle.revisions)
            planning = tuple(dict(item) for item in frozen["planning_items"])
            planning_refs = set(memory_refs)
            expression_refs = set(memory_refs) | set(profile_refs)
            for item in planning:
                planning_refs.update(_refs(item.get("refs", ())))
                if item.get("source_ref"):
                    planning_refs.update(_refs((item["source_ref"],)))
            for item in frozen["history"]:
                expression_refs.update(_refs(item.get("refs", ())))
                if item.get("source_ref"):
                    expression_refs.update(_refs((item["source_ref"],)))
            expression_refs.update(planning_refs)
            source_policy = getattr(self.memory, "source_policy", None)
            restricted = use_policy == "local_only"
            for kind, identity, revision in expression_refs:
                if kind == "memory":
                    policy = source_policy(identity, revision) if callable(source_policy) else None
                    allowed = getattr(policy, "value", policy) == "cloud_allowed"
                else:
                    allowed = self.persistence.reference_policy(kind, identity, revision) == "cloud_allowed"
                restricted = restricted or not allowed
            if restricted and use_policy != "local_only":
                if legacy_plan or saved is not None:
                    raise WorkflowBlocked("Recovered context policy requires a new intent")
                self.persistence.tighten_policy(task_id)
                use_policy = "local_only"
            if self.cloud and use_policy == "local_only":
                raise WorkflowBlocked("Context contains local_only sources")
            if saved is None and not legacy_plan:
                digest = sha256(canonical({"request": request, "items": planning,
                    "memories": [record.content for record in bundle.records],
                    "refs": sorted(planning_refs)}).encode("utf-8")).hexdigest()
                self.persistence.save_context(task_id, digest, planning, tuple(sorted(planning_refs)), "planning")
            elif saved is not None:
                planning_refs.update(_refs(saved["refs"]))
                expression_refs.update(planning_refs)
            expression_digest = sha256(canonical({"refs": sorted(expression_refs)}).encode("utf-8")).hexdigest()
            # Confirmation may reuse a previously frozen expression source set.
            if self.persistence.context(task_id, "expression") is None:
                self.persistence.save_context(task_id, expression_digest, (), tuple(sorted(expression_refs)), "expression")
            context = PreparedTurn(task_id, int(state["epoch"]),
                conversation_id if is_conversation else "", int(link["epoch"]), int(link["sequence"]),
                planning, tuple(dict(item) for item in frozen["history"]), style,
                tuple(record.content for record in bundle.records) + (
                    (canonical({"source_kind": "accepted_user_preference", "values": profile}),) if profile else ()),
                tuple(frozen["warnings"]), use_policy, expression_epoch,
                frozenset(overrides.get("consumed_profile_keys", ())),
                frozenset(admitted_categories))
            self._prepared[task_id] = context
            self.check(task_id)
            return context

    def check(self, task_id: str, *, expression: bool = False) -> None:
        value = self._prepared.get(task_id)
        if value is not None:
            self.privacy.check(value.privacy_epoch)
            settings = self._settings()
            if settings.get("degraded") and (expression or value.history or value.planning_items):
                raise WorkflowBlocked("Interaction source permissions are unavailable")
            if self.cloud and any(not settings.get("cloud_permissions", {}).get(key, False)
                                  for key in value.allowed_cloud_categories):
                raise WorkflowBlocked("A previously admitted cloud category was revoked")
            if value.conversation_id:
                snapshot = self.conversations.snapshot(value.conversation_id)
                if snapshot["epoch"] != value.conversation_epoch:
                    raise WorkflowBlocked("Conversation epoch expired")
            if expression and settings.get("interaction_epoch", 0) != value.expression_epoch:
                raise WorkflowBlocked("Expression settings or cue were reset")
        self.persistence.check(task_id, "expression" if expression else "planning")

    def calls(self, task_id: str, purpose: str) -> LLMProvider:
        return self.gateway.bind(task_id, purpose,
            guard=lambda: self.check(task_id, expression=purpose == "expression"))

    @contextmanager
    def publication(self, task_id: str) -> Iterator[None]:
        turn = self._prepared.get(task_id)
        epoch = turn.privacy_epoch if turn is not None else int(self.persistence.privacy()["epoch"])
        with self.privacy.publication(epoch):
            self.check(task_id)
            yield

    def publish(self, turn: PreparedTurn, request: str, response: str, *,
                facts: tuple[Mapping[str, Any], ...], signals: Mapping[str, Any]) -> None:
        self.check(turn.task_id)
        context = self.persistence.context(turn.task_id, "expression")
        refs = _refs(context["refs"] if context is not None else ())
        policy = turn.use_policy
        source_policy = getattr(self.memory, "source_policy", None)
        for kind, identity, revision in refs:
            if kind == "memory":
                value = source_policy(identity, revision) if callable(source_policy) else None
                allowed = getattr(value, "value", value) == "cloud_allowed"
            else:
                allowed = self.persistence.reference_policy(kind, identity, revision) == "cloud_allowed"
            if not allowed:
                policy = "local_only"
        for role in ("user", "assistant"):
            self.persistence.store.set_dependency_version("dialogue_turn", turn.task_id + ":" + role,
                turn.sequence, use_policy=policy)
        if turn.conversation_id:
            self.conversations.publish(turn.conversation_id, turn.conversation_epoch, turn.sequence,
                turn.task_id, request, response, refs=refs, use_policy=policy, facts=facts)
        accept = getattr(self.companion, "accept_signals", None)
        if callable(accept) and self._settings().get("interaction_epoch", 0) == turn.expression_epoch:
            self._offer_profiles(turn, request, signals, policy)
            accept(turn.task_id, signals, conversation_id=turn.conversation_id or "single:" + turn.task_id,
                   use_policy=policy)
        self._prepared.pop(turn.task_id, None)

    def _offer_profiles(self, turn: PreparedTurn, request: str,
                        signals: Mapping[str, Any], policy: str) -> None:
        """Keep personal facts in Memory's existing revision-checked review path.

        The caller holds the publication barrier after the terminal task commit.
        A generation result may propose a fact, but cannot accept it or change
        profile data through companion configuration.
        """
        if (self.companion is None or self.companion.snapshot().get("enabled") is not True
                or getattr(self.memory, "mode", "explicit_only") == "paused"):
            return
        candidates = signals.get("preference_candidates", ())
        if not isinstance(candidates, (tuple, list)) or len(candidates) > 2:
            return
        offer = getattr(self.memory, "offer_profile_candidate", None)
        task = self.tasks.get(turn.task_id)
        if task is None or task.status != TaskStatus.COMPLETED:
            raise WorkflowBlocked("Profile candidates require a committed completed task")
        for raw in candidates:
            candidate = validate_preference(raw, request)
            if candidate is None or candidate.key not in {"preferred_name", "reply_language"}:
                continue
            if not isinstance(candidate.value, str):
                continue
            self.check(turn.task_id, expression=True)
            if not callable(offer):
                raise ValueError("Memory runtime does not support reviewed profile candidates")
            offer(request, task_id=turn.task_id, key=candidate.key, value=candidate.value,
                  span=candidate.span, created_at=task.created_at, use_policy=UsePolicy(policy))

    def invalidate(self) -> None:
        with self._lock:
            self.conversations.invalidate()
            invalidate = getattr(self.companion, "invalidate_interaction", None)
            if callable(invalidate):
                invalidate()
            # Admission tickets remain invalid until the worker leaves. Clearing
            # them here would accidentally admit a late callback at the new epoch.

    def close(self) -> None:
        self.invalidate()
        with self._lock:
            self._prepared.clear()
