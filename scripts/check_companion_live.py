"""显式真实模型验收：仅连接设置复用真实配置，业务数据完全隔离。"""
from __future__ import annotations

import argparse
from hashlib import sha256
from dataclasses import replace
import json
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

from deskpal.application import factory
from deskpal.agent.models import AgentResult
from deskpal.config import load_config
from deskpal.llm.models import ChatMessage
from deskpal.llm.providers import OpenAILLMProvider
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolSpec
from deskpal.llm.models import LLMResponse
from deskpal.tools.screenshot import Monitor


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Budget:
    def __init__(self, maximum: int, token_limit: int = 3072) -> None:
        self.maximum = maximum
        self.events: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.deadline = time.monotonic() + 1200
        self.token_limit = token_limit
        self.reserved_tokens = 0
        self.total_token_limit = 48 * 3072

    def transport(self, request: urllib.request.Request, *, timeout: float) -> Any:
        assert isinstance(request.data, bytes)
        payload = json.loads(request.data)
        limit = min(payload.get('max_output_tokens', 3072), self.token_limit)
        with self.lock:
            if (len(self.events) >= self.maximum or time.monotonic() >= self.deadline
                    or self.reserved_tokens + limit > self.total_token_limit):
                raise RuntimeError('Live budget exhausted')
            self.reserved_tokens += limit
            event: dict[str, Any] = {'index': len(self.events) + 1, 'outcome': 'started'}
            self.events.append(event)
        payload['max_output_tokens'] = limit
        limited = urllib.request.Request(request.full_url,
            data=json.dumps(payload).encode(), headers=dict(request.header_items()), method='POST')
        started = time.monotonic()
        try:
            result = urllib.request.build_opener(NoRedirect()).open(limited, timeout=min(timeout, 40))
            event['outcome'] = 'http_success'
            return result
        except urllib.error.HTTPError as exc:
            event['outcome'] = f'HTTP_{exc.code}'
            raise
        except Exception as exc:
            event['outcome'] = type(exc).__name__
            raise
        finally:
            event['seconds'] = round(time.monotonic() - started, 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--config', type=Path, default=Path('config.toml'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-calls', type=int, choices=range(1, 65), default=48)
    parser.add_argument('--max-output-tokens', type=int, choices=(3072, 8192), default=3072)
    parser.add_argument('--focus', choices=('all', 'memory', 'review'), default='all')
    parser.add_argument('--probe-json', action='store_true', help='One-call native JSON capability probe; never writes connection settings')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required; no connection or credentials have been read')
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).read_bytes()
    (root / 'runner-source.py').write_bytes(source)
    budget = Budget(args.max_calls, args.max_output_tokens)
    report: dict[str, Any] = {'runner_sha256': sha256(source).hexdigest(), 'cases': [], 'calls': budget.events, 'limits': {'calls': args.max_calls,
        'tokens_per_call': args.max_output_tokens, 'reserved_output_tokens_total': budget.total_token_limit,
        'seconds_per_http': 40, 'total_seconds': 1200}}
    transcripts: list[dict[str, Any]] = []
    original = getattr(factory, 'OpenAILLMProvider')
    app: Any = None

    def save() -> None:
        (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        (root / 'synthetic-transcripts.json').write_text(json.dumps(transcripts, ensure_ascii=False, indent=2), encoding='utf-8')

    def case(name: str, action: Callable[[], None]) -> bool:
        if args.focus == 'memory' and name not in {'connection_plain_text', 'automatic_learning_saved', 'cross_conversation_memory_recall', 'model_background_memory_review'}:
            return True
        if args.focus == 'review' and name not in {'connection_plain_text', 'model_background_memory_review'}:
            return True
        start, calls = time.monotonic(), len(budget.events)
        row: dict[str, Any] = {'name': name}
        report['cases'].append(row)
        try:
            action()
            row['status'] = 'passed'
        except Exception as exc:
            row.update(status='failed', error_type=type(exc).__name__)
            # 不打印异常正文，第三方响应／配置错误可能含秘密。
        row.update(seconds=round(time.monotonic() - start, 2), calls=len(budget.events) - calls)
        save()
        print(json.dumps(row, ensure_ascii=False), flush=True)
        return bool(row['status'] == 'passed')

    class ObservedProvider(OpenAILLMProvider):
        def __init__(self, *a: Any, **kw: Any) -> None:
            kw['transport'] = budget.transport
            super().__init__(*a, **kw)

        def chat(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] = ()) -> LLMResponse:
            stage = messages[0].content[:45] if messages else ''
            self._stage = stage
            try:
                response = super().chat(messages, tools=tools)
            except Exception as exc:
                report.setdefault('provider_errors', []).append(dict(stage=stage, error_type=type(exc).__name__))
                raise
            transcripts.append({'stage': stage, 'text': response.content,
                'tools': [{'name': x.name, 'arguments': dict(x.arguments)} for x in response.tool_calls]})
            return response

        def chat_json(self, messages: Sequence[ChatMessage]) -> LLMResponse:
            self._stage = messages[0].content[:45] if messages else ''
            try:
                response = super().chat_json(messages)
            except Exception as exc:
                report.setdefault('provider_errors', []).append(dict(stage=self._stage, error_type=type(exc).__name__))
                raise
            transcripts.append({'stage': self._stage, 'text': response.content, 'tools': [], 'native_json': True})
            return response

        def _parse_response(self, raw: Any) -> LLMResponse:
            if isinstance(raw, dict):
                details = raw.get('incomplete_details')
                report.setdefault('response_metadata', []).append(dict(stage=getattr(self, '_stage', ''),
                    status=raw.get('status'), incomplete_reason=details.get('reason') if isinstance(details, dict) else None,
                    output_types=[item.get('type') for item in raw.get('output', []) if isinstance(item, dict)],
                    usage={k: v for k, v in (raw['usage'] if isinstance(raw.get('usage'), dict) else {}).items()
                        if k in {'input_tokens', 'output_tokens', 'total_tokens'} and type(v) is int}))
            return super()._parse_response(raw)

    try:
        connection = load_config(args.config).llm
        if not connection.ready or connection.provider != 'openai':
            raise RuntimeError('Live connection unavailable')
        report['connection'] = {'model': connection.model, 'host': urlsplit(connection.base_url).hostname,
            'protocol': connection.protocol, 'credential_source': connection.credential_source}
        if args.probe_json:
            connection = replace(connection, expression_output_format='json_object')
        provider = ObservedProvider(connection, timeout_seconds=40)
        def plain() -> None:
            if args.probe_json:
                response = provider.chat_json((ChatMessage('user', 'Return exactly a JSON object {"reply_text":"连接成功"}. No tools.'),))
                parsed = json.loads(response.content)
                assert isinstance(parsed, dict) and set(parsed) == {'reply_text'}
                assert isinstance(parsed['reply_text'], str) and parsed['reply_text'].strip()
            else:
                response = provider.chat((ChatMessage('user', '只回复“连接成功”，不要调用工具。'),))
            assert response.content.strip() and not response.tool_calls
        if not case('native_json_connection' if args.probe_json else 'connection_plain_text', plain):
            report['stopped'] = 'Connection failed; application scenarios not executed'
            return 1
        if args.probe_json:
            report['probe_only'] = 'native_json'
            report['passed'] = True
            return 0
        # 真实配置仅取连接。其余从无环境的默认配置构造，不触碰真实档案／工作区。
        clean = load_config(environ={})
        settings = replace(clean, llm=replace(connection, timeout_seconds=40),
            storage=replace(clean.storage, data_dir=root / 'data'),
            sandbox=replace(clean.sandbox, workspace_dir=root / 'workspace'),
            memory=replace(clean.memory, embedding_provider='disabled', model_cache_dir=root / 'unused-cache',
                evaluation_timeout_seconds=40, evaluation_max_output_tokens=args.max_output_tokens),
            proactive=replace(clean.proactive, enabled=False))
        setattr(factory, 'OpenAILLMProvider', ObservedProvider)
        class DisabledCapture:
            def monitors(self) -> Sequence[Monitor]:
                raise RuntimeError('Desktop capture is outside live acceptance scope')

            def capture(self, region: Monitor) -> bytes:
                raise RuntimeError('Desktop capture is outside live acceptance scope')
        app = factory.create_application(settings, screenshot_backend=DisabledCapture())
        role = app.core.companion_runtime
        role.command('character_card', expected_revision=role.snapshot()['revision'], command_id=uuid4().hex,
            text='你叫青禾，是沉稳好奇、重视实际证据的桌宠。遇到不确定的事先核实。用简洁中文交流。')
        daily = app.conversation_ensure_default()

        def turn(prompt: str, conversation: dict[str, Any] = daily) -> AgentResult:
            task = app.submit(prompt, conversation_id=conversation['id'], expected_context_epoch=conversation['context_epoch'])
            result = cast(AgentResult, app.wait(task.id, timeout=240))
            transcripts.append({'task': task.id, 'request': prompt, 'status': result.status.value,
                'reply': result.response, 'warnings': list(result.warnings),
                'results': [{'tool': x.tool_name, 'success': x.success, 'output': x.output} for x in result.tool_results]})
            return result

        def completed(result: AgentResult) -> None:
            assert result.status == TaskStatus.COMPLETED
            assert result.response.strip()
            # 预期检索/上下文预算提示单独保留；实际回复、工具和存储仍逐项验证。
            assert all(w in {'semantic_disabled: lexical retrieval only', 'history_context_limit', 'planning_context_limit'} for w in result.warnings)
            assert all(x.success for x in result.tool_results)

        def identity() -> None:
            result = turn('你叫什么名字？用一句话介绍自己。')
            completed(result)
            assert '青禾' in result.response
        case('role_card_chat', identity)

        def empty_calendar() -> None:
            result = turn('请实际查询我现在的日程，告诉我有没有安排。')
            completed(result)
            assert any(x.tool_name == 'calendar_list' for x in result.tool_results)
            assert any(x in result.response for x in ('没有', '暂无', '无日程', '未安排', '无安排'))
            assert 'call_id' not in result.response and 'tool_name' not in result.response
        case('empty_calendar_natural_reply', empty_calendar)

        workspace = settings.sandbox.workspace_dir
        answer = 'answer-' + uuid4().hex[:8] + '.txt'
        (workspace / 'clue.txt').write_text(f'下一步请读取 {answer}，答案在那个文件中。', encoding='utf-8')
        (workspace / answer).write_text('合成项目的口令是：松果7429。', encoding='utf-8')
        def multi() -> None:
            result = turn('读取 clue.txt，按里面指出的文件继续读取，告诉我最终口令。不要猜测。')
            completed(result)
            assert '松果7429' in result.response
            assert sum(x.tool_name == 'read_text' for x in result.tool_results) >= 2
            assert app.journal.round_count(result.task_id) >= 2
            with app.store.read_connection() as db:
                rounds = [json.loads(row[0]) for row in db.execute(
                    'SELECT body FROM decision_round WHERE task_id=? ORDER BY round', (result.task_id,))]
            read_rounds = {step['call_id'].split('-step-')[0] for data in rounds for step in data['steps']
                if step['name'] == 'read_text'}
            assert len(read_rounds) >= 2
        case('multi_round_observe_then_replan', multi)

        def permission() -> None:
            result = turn('请新建 approved.txt，内容必须恰好为“测试写入完成”。如果需要许可请等我确认。')
            assert result.status == TaskStatus.WAITING
            assert not (workspace / 'approved.txt').exists()
            assert result.required_authorizations and set(result.required_authorizations) <= {'write_text'}
            app.enqueue_confirmation(result.task_id, authorized_tools=result.required_authorizations,
                plan_hash=app.journal.get_plan(result.task_id).hash, command_id=uuid4().hex)
            final = app.wait(result.task_id, timeout=240)
            completed(final)
            assert (workspace / 'approved.txt').read_text(encoding='utf-8').strip() == '测试写入完成'
        case('permission_before_real_temporary_write', permission)

        def learn() -> None:
            result = turn('请记住：我的合成项目叫白榆，固定预算为763元。这是确定信息，以后也会用到。')
            completed(result)
            assert app.learning.store.job('turn:' + result.task_id)['status'] == 'completed'
            assert app.learning_records()['items']
            assert any('763' in x.draft.content for x in app.memory.list_items())
        case('automatic_learning_saved', learn)

        other = app.conversation_create('合成专项会话', uuid4().hex)
        def recall() -> None:
            result = turn('我之前说过白榆项目的固定预算是多少？请根据记忆回答。', other)
            completed(result)
            assert '763' in result.response
            with app.store.read_connection() as db:
                plans = [json.loads(row[0]) for row in db.execute('SELECT canonical_json FROM immutableplan WHERE task_id=?', (result.task_id,))]
                plans += [json.loads(row[0]) for row in db.execute('SELECT body FROM decision_round WHERE task_id=?', (result.task_id,))]
            refs = {tuple(ref) for plan in plans for ref in plan['memory_refs']}
            assert any((item := app.memory.repository.get(identity)) is not None
                and item.status.value in {'accepted', 'candidate', 'disputed'} and '763' in item.draft.content for identity, revision in refs)
        case('cross_conversation_memory_recall', recall)

        def review() -> None:
            if args.focus == 'review':
                app.memory.remember('合成项目白榆的预算为763元，这是供模型后台复核的合成事实。')
            identity = 'live-review-' + uuid4().hex
            app.enqueue_learning_review(identity)
            end = time.monotonic() + 180
            while time.monotonic() < end:
                job = app.learning.store.job('review:' + identity)
                idle = not app._dispatcher.diagnostics()['internal_active']
                if idle and job and job['status'] in ('completed', 'failed', 'cancelled'):
                    assert job['status'] == 'completed'
                    return
                time.sleep(.1)
            raise TimeoutError('Learning review did not finish')
        case('model_background_memory_review', review)
        report['passed'] = all(row['status'] == 'passed' for row in report['cases'])
        return 0 if report['passed'] else 1
    except Exception as exc:
        report['fatal_error_type'] = type(exc).__name__
        return 1
    finally:
        if app is not None:
            report['closed'] = app.close(timeout=60)
        setattr(factory, 'OpenAILLMProvider', original)
        report['request_count'] = len(budget.events)
        save()


if __name__ == '__main__':
    raise SystemExit(main())
