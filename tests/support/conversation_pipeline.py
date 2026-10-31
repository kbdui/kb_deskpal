"""HM02-HM05 production-entry acceptance, using only temporary synthetic data.

No network provider is constructed. Barriers cap waits at ten seconds. Private
store reads below inspect durable evidence; concurrency always enters through
ApplicationService, including the public request_forget privacy barrier.
"""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from threading import Event
from time import monotonic
from uuid import UUID

import pytest

from deskpal.application.desktop import DesktopFacade
from deskpal.application.factory import create_application
from deskpal.companion import CompanionStorageError
from deskpal.config import LLMConfig, StorageConfig, load_config
from deskpal.memory.contracts import MemoryError, MemoryStatus
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolCall
from deskpal.llm.models import LLMResponse
from deskpal.workflow.models import WorkflowBlocked


WAIT = 10
DRAFT = "HM_NEUTRAL_DRAFT"
REPLY = "HM_UNVERIFIED_ASSISTANT_REPLY"
PRIVATE = "HM_PRIVATE_SOURCE_1849"


def envelope(reply=REPLY, *, cues=(), preferences=()):
    return json.dumps({"reply_text": reply, "cues": list(cues),
                       "preference_candidates": list(preferences)}, ensure_ascii=False)


class HumanizationRecordingProvider:
    """Record all provider exits, rejecting generate and hidden repair traffic."""

    def __init__(self):
        self.calls = []
        self.plans = {}
        self.replies = {}
        self.block = None
        self.entered = Event()
        self.release = Event()

    def chat(self, messages, *, tools=()):
        system = messages[0].content
        stage = ("expression" if system.startswith("[EXPRESSION]") else
                 "memory_evaluation" if system.startswith("Extract optional long-term memory") else
                 "planning")
        entry = {"stage": stage, "messages": [asdict(item) for item in messages],
                 "tools": [asdict(item) for item in tools]}
        self.calls.append(deepcopy(entry))
        if stage == self.block:
            self.entered.set()
            if not self.release.wait(WAIT):
                raise TimeoutError("synthetic model barrier expired")
        if stage == "planning":
            return LLMResponse(DRAFT, self.plans.get(messages[-1].content, ()))
        if stage == "memory_evaluation":
            return LLMResponse('{"candidates":[]}')
        request = json.loads(messages[-1].content)["request"]
        selected = self.replies.get(request)
        if isinstance(selected, Exception):
            raise selected
        if isinstance(selected, LLMResponse):
            return selected
        structured = "OUTPUT PROTOCOL" in system
        return LLMResponse(selected if selected is not None else envelope() if structured else REPLY)

    def generate(self, prompt):
        self.calls.append({"stage": "generate", "prompt": prompt})
        raise AssertionError("unexpected generate exit")


@pytest.fixture
def hm_config(tmp_path):
    return load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
        "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "DESKPAL_DATA_DIR": str(tmp_path / "data")})


@pytest.fixture
def wire(monkeypatch):
    from deskpal.application import factory

    provider = HumanizationRecordingProvider()
    monkeypatch.setattr(factory, "OfflineLLMProvider", lambda: provider)
    monkeypatch.setattr(factory, "OpenAILLMProvider", lambda *args, **kwargs: provider)
    return provider


def eventually(check, message):
    deadline = monotonic() + WAIT
    tick = Event()
    while not check():
        remaining = deadline - monotonic()
        if remaining <= 0:
            pytest.fail(message)
        tick.wait(min(0.01, remaining))


def idle(app):
    eventually(lambda: app.diagnostics()["dispatcher"]["active_task_id"] is None,
               "application did not leave its active task")


def run_turn(app, request, *, conversation=None, **parameters):
    if conversation is not None:
        parameters.update(conversation_id=conversation["conversation_id"],
                          expected_conversation_epoch=conversation["epoch"])
    task = app.submit(request, **parameters)
    result = app.wait(task.id, WAIT)
    idle(app)
    return result


def configure(app, **parameters):
    return app.companion_command("configure", expected_revision=app.companion_snapshot()["revision"],
                                 **parameters)


def interact(app, action, **parameters):
    return app.interaction_command(action, expected_revision=app.interaction_snapshot()["revision"],
                                   **parameters)


def companion_command(app, action, **parameters):
    return app.companion_command(action, expected_revision=app.companion_snapshot()["revision"],
                                 **parameters)


def stages(wire):
    return [call["stage"] for call in wire.calls]


def last_expression(wire):
    return next(call for call in reversed(wire.calls) if call["stage"] == "expression")


def cloud(config):
    return replace(config, llm=LLMConfig(provider="openai", model="synthetic", api_key="synthetic"))


