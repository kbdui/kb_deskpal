"""规划输出校验：只兼容无歧义的排版包装，不猜动作、学习决定或工具参数。"""
from collections.abc import Mapping
import json
import re
from typing import Any

from deskpal._json import snapshot_json
from deskpal.llm.models import LLMResponse
from deskpal.tools.models import ToolCall
from .models import Plan

FIELDS = frozenset({'action', 'reply', 'organize_memory', 'memory_requests', 'context', 'tool_calls', 'call_id', 'name', 'arguments', 'response'})


class PlanningOutputError(ValueError):
    def __init__(self, field: str, rule: str, *, code: str = 'fields_invalid',
                 missing: Any = (), extra: Any = (), expected: str = '', actual: str = '',
                 line: int = 0, column: int = 0) -> None:
        self.field = field if field in FIELDS else 'unknown'
        self.rule, self.code = rule, code
        self.missing, self.extra = tuple(missing), tuple(extra)
        self.expected, self.actual = expected, actual
        self.line, self.column = line, column
        super().__init__('Planning output invalid: ' + self.field + '/' + self.rule)

    def diagnostic(self) -> dict[str, Any]:
        return dict(processing_stage='parse' if self.code == 'json_invalid' else 'validate',
            processing_code=self.code, processing_field=self.field, processing_rule=self.rule,
            processing_missing_fields=list(self.missing), processing_missing_count=len(self.missing),
            processing_extra_fields=list(self.extra), processing_extra_count=len(self.extra),
            processing_expected_type=self.expected, processing_actual_type=self.actual,
            **(dict(processing_json_line=self.line, processing_json_column=self.column) if self.line else {}))

    def correction(self) -> str:
        reasons = {'field_set': '字段不完整或含未约定字段', 'wrong_type': '字段类型不正确',
            'json_syntax': '不是完整有效的JSON对象', 'duplicate_field': 'JSON中出现重复字段',
            'invalid_choice': '动作取值不在允许范围', 'clarify_learning': 'clarify时organize_memory必须是false',
            'quiet_forbidden': '本轮普通用户请求需要answer或clarify，不能quiet',
            'clarify_forbidden': 'QQ渠道不能clarify，应通过qq_send提问后quiet',
            'context_phase': '当前阶段不接受新的context决定', 'empty_text': '需要非空的回复或澄清正文',
            'invalid_tool': '原生工具调用的结构、编号或参数不合法', 'tool_limit': '工具数量超过预算',
            'byte_limit': '输出超过本阶段长度上限'}
        description = reasons.get(self.rule, '输出不符合规划协议')
        missing = ','.join(field if field in FIELDS else 'unknown' for field in self.missing)
        return f'上次输出的问题：{self.field}，{description}。' + (f'必须补齐字段：{missing}。' if missing else '')


def value_type(value: Any) -> str:
    return ('null' if value is None else 'boolean' if type(value) is bool else 'object' if isinstance(value, dict)
            else 'array' if isinstance(value, list) else 'string' if isinstance(value, str)
            else 'number' if isinstance(value, (int, float)) else 'other')


def normalize(text: str) -> tuple[str, bool]:
    original = text
    text = text.strip().removeprefix('\ufeff').strip()
    fence = re.fullmatch(r'```(?:json)?\s*\n?(.*?)\n?```', text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence[1].strip()
    return text, text != original


def parse(response: Any, *, initial: bool, origin: str, max_steps: int) -> tuple[Plan, bool]:
    if not isinstance(response, LLMResponse) or not isinstance(response.content, str) or not isinstance(response.tool_calls, (tuple, list)):
        raise PlanningOutputError('response', 'wrong_type', expected='object', actual=value_type(response))
    try:
        size = len(response.content.encode('utf-8'))
    except UnicodeError:
        raise PlanningOutputError('response', 'invalid_text') from None
    if size > 65536:
        raise PlanningOutputError('response', 'byte_limit')
    calls, context, identities = [], None, set()
    for call in response.tool_calls:
        if (not isinstance(call, ToolCall) or not isinstance(call.name, str) or not call.name.strip()
                or not isinstance(call.call_id, str) or not isinstance(call.arguments, Mapping)):
            raise PlanningOutputError('tool_calls', 'invalid_tool')
        if call.call_id and call.call_id in identities:
            raise PlanningOutputError('call_id', 'invalid_tool')
        identities.add(call.call_id)
        try:
            arguments = snapshot_json(call.arguments)
            json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (ValueError, TypeError, RecursionError):
            raise PlanningOutputError('arguments', 'invalid_tool') from None
        if call.name == 'turn_context':
            if not initial or context is not None:
                raise PlanningOutputError('context', 'context_phase')
            context = arguments
            if len(json.dumps(context, ensure_ascii=False).encode('utf-8')) > 16384:
                raise PlanningOutputError('context', 'byte_limit')
        else:
            calls.append(ToolCall(call.name, arguments, call.call_id))
    if len(calls) > max_steps:
        raise PlanningOutputError('tool_calls', 'tool_limit')
    if calls or context is not None and not response.content.strip():
        return Plan(response.content, tuple(calls), context=context), False
    text, normalized = normalize(response.content)
    if not text:
        raise PlanningOutputError('reply', 'empty_text')

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise PlanningOutputError(key, 'duplicate_field', code='json_invalid')
            result[key] = value
        return result

    def invalid_constant(value: str) -> Any:
        raise PlanningOutputError('response', 'json_syntax', code='json_invalid')

    try:
        decision = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except json.JSONDecodeError as error:
        raise PlanningOutputError('response', 'json_syntax', code='json_invalid', line=error.lineno, column=error.colno) from None
    except PlanningOutputError:
        raise
    except (ValueError, RecursionError):
        raise PlanningOutputError('response', 'json_syntax', code='json_invalid') from None
    if not isinstance(decision, dict):
        raise PlanningOutputError('response', 'wrong_type', expected='object', actual=value_type(decision))
    from deskpal.memory.management import requests
    try:
        memory_requests = requests(decision.get('memory_requests', []))
    except (ValueError, TypeError, UnicodeError):
        raise PlanningOutputError('memory_requests', 'invalid_choice') from None
    if memory_requests and origin != 'user':
        raise PlanningOutputError('memory_requests', 'invalid_choice')
    required = {'action', 'reply', 'organize_memory'}
    missing, extra = required - decision.keys(), decision.keys() - required - {'context', 'memory_requests'}
    if missing or extra:
        raise PlanningOutputError('response', 'field_set', missing=sorted(missing), extra=sorted(extra))
    for field, expected, valid in [('action', 'string', isinstance(decision['action'], str)),
                                  ('reply', 'string', isinstance(decision['reply'], str)),
                                  ('organize_memory', 'boolean', type(decision['organize_memory']) is bool)]:
        if not valid:
            raise PlanningOutputError(field, 'wrong_type', expected=expected, actual=value_type(decision[field]))
    action = decision['action']
    try:
        decision['reply'].encode('utf-8')
    except UnicodeError:
        raise PlanningOutputError('reply', 'invalid_text') from None
    if action not in {'answer', 'clarify', 'quiet'}:
        raise PlanningOutputError('action', 'invalid_choice')
    if action == 'quiet' and origin == 'user':
        raise PlanningOutputError('action', 'quiet_forbidden')
    if action == 'clarify' and origin == 'plugin':
        raise PlanningOutputError('action', 'clarify_forbidden')
    if action == 'clarify' and (decision['organize_memory'] or memory_requests):
        raise PlanningOutputError('organize_memory', 'clarify_learning')
    if not decision['reply'].strip() and (action == 'clarify' or action == 'answer' and decision.get('context') is None and context is None):
        raise PlanningOutputError('reply', 'empty_text')
    text_context = decision.get('context')
    if text_context is not None:
        if not initial or context is not None or not isinstance(text_context, dict):
            raise PlanningOutputError('context', 'context_phase')
        context = text_context
        try:
            size = len(json.dumps(context, ensure_ascii=False).encode('utf-8'))
        except (UnicodeError, RecursionError):
            raise PlanningOutputError('context', 'invalid_text') from None
        if size > 16384:
            raise PlanningOutputError('context', 'byte_limit')
    return Plan(decision['reply'], (), action, context, decision['organize_memory'] or bool(memory_requests), memory_requests), normalized
