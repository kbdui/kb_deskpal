"""Bounded planning through LLMProvider, without execution or memory evaluation."""

import json

import pytest

from deskpal.llm.errors import LLMError
from deskpal.llm.models import LLMResponse
from deskpal.memory import MemoryRecord
from deskpal.planner import LLMPlanner
from deskpal.tools import ToolCall, ToolSpec


class ControlledLLM:
    def __init__(self, response=None, *, error=None, mutate_schema=False):
        self.response = response if response is not None else LLMResponse(content="普通回复")
        self.error = error
        self.mutate_schema = mutate_schema
        self.calls = []

    def chat(self, messages, *, tools=()):
        self.calls.append((tuple(messages), tuple(tools)))
        if self.mutate_schema:
            tools[0].parameters["properties"]["value"]["type"] = "boolean"
        if self.error:
            raise self.error
        return self.response


def test_planner_uses_request_memories_and_supplied_tools_in_one_chat_call():
    llm = ControlledLLM()
    spec = ToolSpec("echo", "回显", {"type": "object"})
    plan = LLMPlanner(llm).plan("原始目标", memories=(MemoryRecord("用户喜欢中文"),), tools=(spec,))
    assert plan.reply == "普通回复" and plan.steps == ()
    assert len(llm.calls) == 1
    messages, tools = llm.calls[0]
    assert messages[-1].role == "user" and messages[-1].content == "原始目标"
    assert all("用户喜欢中文" not in message.content for message in messages if message.role == "system")
    memory_data = messages[-2]
    assert memory_data.role == "user"
    assert memory_data.content.startswith("[UNTRUSTED MEMORY DATA]\n")
    assert json.loads(memory_data.content.split("\n", 1)[1]) == ["用户喜欢中文"]
    assert tools == (spec,)


def test_planner_copies_schema_and_tool_arguments_across_provider_boundary():
    arguments = {"nested": ["original"]}
    call = ToolCall("echo", arguments, "call-1")
    llm = ControlledLLM(LLMResponse(content="计划", tool_calls=(call,)), mutate_schema=True)
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}}
    plan = LLMPlanner(llm).plan("请求", tools=(ToolSpec("echo", "回显", schema),))
    assert schema["properties"]["value"]["type"] == "integer"
    arguments["nested"].append("provider-changed")
    assert plan.steps[0].arguments == {"nested": ["original"]}
    assert plan.steps[0].call_id == "call-1"


@pytest.mark.parametrize("request_text", ["", " ", "\n\t", None])
def test_blank_or_invalid_request_fails_before_llm(request_text):
    llm = ControlledLLM()
    with pytest.raises(ValueError):
        LLMPlanner(llm).plan(request_text)
    assert not llm.calls


@pytest.mark.parametrize("response", [
    LLMResponse(), LLMResponse(content=" "),
    LLMResponse(tool_calls=(ToolCall("echo", {}, "same"), ToolCall("echo", {}, "same"))),
    LLMResponse(tool_calls=(ToolCall("", {}, "id"),)),
    LLMResponse(tool_calls=(ToolCall("echo", {"value": float("nan")}, "id"),)),
])
def test_invalid_planning_output_is_rejected(response):
    with pytest.raises(ValueError):
        LLMPlanner(ControlledLLM(response)).plan("request")


def test_plan_length_exceeding_budget_is_rejected():
    response = LLMResponse(tool_calls=(ToolCall("echo", {}, "one"), ToolCall("echo", {}, "two")))
    with pytest.raises(ValueError):
        LLMPlanner(ControlledLLM(response), max_tool_steps=1).plan("request")


def test_llm_failure_is_preserved():
    with pytest.raises(LLMError, match="provider unavailable"):
        LLMPlanner(ControlledLLM(error=LLMError("provider unavailable"))).plan("request")
