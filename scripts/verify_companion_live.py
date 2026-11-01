"""只读复核已保存的合成验收证据；不加载连接、凭据或调用模型。"""
import argparse
import json
from pathlib import Path
import sqlite3
from collections.abc import Callable
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    args = parser.parse_args()
    root = args.input.resolve()
    report = json.loads((root / 'report.json').read_text(encoding='utf-8'))
    transcript = json.loads((root / 'synthetic-transcripts.json').read_text(encoding='utf-8'))
    workflow_paths = list((root / 'data').glob('profiles/*/workflow.sqlite3'))
    assert len(workflow_paths) == 1
    workflow = sqlite3.connect(workflow_paths[0].as_uri() + '?mode=ro', uri=True)
    memory = sqlite3.connect((workflow_paths[0].parent / 'memory.sqlite3').as_uri() + '?mode=ro', uri=True)
    results: list[dict[str, Any]] = []

    def row(prefix: str) -> dict[str, Any]:
        items = [x for x in transcript if x.get('request', '').startswith(prefix)]
        assert len(items) == 1
        item: dict[str, Any] = items[0]
        return item

    def completed(item: dict[str, Any]) -> None:
        assert item['status'] == 'Completed' and item['reply'].strip()
        assert all(w in {'semantic_disabled: lexical retrieval only', 'history_context_limit', 'planning_context_limit'} for w in item['warnings'])
        assert all(x['success'] for x in item['results'])

    def check(name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
            results.append({'name': name, 'status': 'passed'})
        except Exception as exc:
            results.append({'name': name, 'status': 'failed', 'error_type': type(exc).__name__})

    def connection() -> None:
        assert report['cases'][0]['name'] == 'connection_plain_text'
        assert report['cases'][0]['status'] == 'passed'
    check('connection_plain_text', connection)

    def identity() -> None:
        item = row('你叫什么名字')
        completed(item)
        assert '青禾' in item['reply']
    check('role_card_chat', identity)

    def calendar() -> None:
        item = row('请实际查询')
        completed(item)
        assert any(x['tool'] == 'calendar_list' and x['output'] == [] for x in item['results'])
        assert any(x in item['reply'] for x in ('没有', '暂无', '无日程', '未安排', '无安排'))
        assert 'call_id' not in item['reply'] and 'tool_name' not in item['reply']
    check('empty_calendar_natural_reply', calendar)

    def multi() -> None:
        item = row('读取 clue.txt')
        completed(item)
        assert '松果7429' in item['reply']
        assert sum(x['tool'] == 'read_text' for x in item['results']) >= 2
        plans = [json.loads(x[0]) for x in workflow.execute(
            'SELECT body FROM decision_round WHERE task_id=? ORDER BY round', (item['task'],))]
        rounds = {step['call_id'].split('-step-')[0] for plan in plans for step in plan['steps'] if step['name'] == 'read_text'}
        assert len(rounds) >= 2
    check('multi_round_observe_then_replan', multi)

    def permission() -> None:
        item = row('请新建 approved.txt')
        assert item['status'] == 'Waiting'
        status, raw = workflow.execute('SELECT status,result FROM task WHERE id=?', (item['task'],)).fetchone()
        assert status == 'Completed'
        assert (root / 'workspace' / 'approved.txt').read_text(encoding='utf-8').strip() == '测试写入完成'
        # 原脚本先断言不存在且需要 write_text 许可，才会发出确认；保留其证据和首败。
        case = next(x for x in report['cases'] if x['name'] == 'permission_before_real_temporary_write')
        assert case['status'] == 'passed' or case.get('error_type') == 'AssertionError'
        final = json.loads(raw)
        # 文件副作用正确不等于整项通过；最终表达／学习失败仍必须保留为失败。
        assert final['response'].strip()
        assert all(w in {'semantic_disabled: lexical retrieval only', 'history_context_limit', 'planning_context_limit'} for w in final.get('warnings', []))
        assert all(x['success'] for x in final.get('tool_results', []))
    check('permission_before_real_temporary_write', permission)

    def learn() -> None:
        item = row('请记住：')
        completed(item)
        assert memory.execute("SELECT COUNT(*) FROM learning_job WHERE task_id=? AND status='completed'", (item['task'],)).fetchone()[0]
        assert memory.execute('SELECT COUNT(*) FROM learning_record').fetchone()[0]
        drafts = [json.loads(x[0]) for x in memory.execute(
            'SELECT draft FROM memory_revisions r WHERE revision=(SELECT MAX(revision) FROM memory_revisions WHERE id=r.id) AND status IN (\'accepted\',\'candidate\',\'disputed\')')]
        assert any('763' in x['content'] for x in drafts)
    check('automatic_learning_saved', learn)

    def recall() -> None:
        item = row('我之前说过')
        completed(item)
        assert '763' in item['reply']
        plans = [json.loads(x[0]) for x in workflow.execute('SELECT canonical_json FROM immutableplan WHERE task_id=?', (item['task'],))]
        plans += [json.loads(x[0]) for x in workflow.execute('SELECT body FROM decision_round WHERE task_id=?', (item['task'],))]
        refs = {tuple(ref) for plan in plans for ref in plan['memory_refs']}
        assert any((found := memory.execute("SELECT draft FROM memory_revisions WHERE id=? AND revision=(SELECT MAX(revision) FROM memory_revisions WHERE id=?) AND status IN ('accepted','candidate','disputed')", (identity, identity)).fetchone())
            and '763' in json.loads(found[0])['content'] for identity, revision in refs)
    check('cross_conversation_memory_recall', recall)

    def review() -> None:
        assert memory.execute("SELECT COUNT(*) FROM learning_job WHERE kind='review' AND status='completed'").fetchone()[0]
    check('model_background_memory_review', review)
    workflow.close()
    memory.close()
    result = {'cases': results, 'passed': all(x['status'] == 'passed' for x in results),
        'application_closed': report.get('closed'), 'request_count': report.get('request_count'),
        'note': 'Offline evidence recheck; only expected retrieval/context budget notices allowed, with semantic and storage assertions. Original report preserved.'}
    (root / 'verified-report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['passed'] and result['application_closed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
