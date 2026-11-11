"""模型视图：保留业务含义，完整修订、用途和删除索引仍由宿主保存。"""

import json
from collections.abc import Mapping, Sequence
from typing import Any


def role_view(role: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {'character_card': {'text': role.get('character_card', {}).get('text', '')}}
    for layer, prefix in (('learned_habits', 'h'), ('user_understanding', 'u'), ('interaction_approach', 'a')):
        result[layer] = [{**{k: row[k] for k in ('content', 'reason', 'nature', 'scope') if k in row},
                          'id': f'{prefix}{index + 1}'} for index, row in enumerate(role.get(layer, []))]
    emotion = role.get('current_emotion')
    result['current_emotion'] = ({k: emotion[k] for k in (
        'description_intensity', 'reason_sources', 'time_scope', 'uncertainty', 'influence_weight') if k in emotion}
        if isinstance(emotion, Mapping) else None)
    return result


def role_aliases(role: Mapping[str, Any]) -> dict[str, str]:
    return {f'{prefix}{index + 1}': row['id']
        for layer, prefix in (('learned_habits', 'h'), ('user_understanding', 'u'), ('interaction_approach', 'a'))
        for index, row in enumerate(role.get(layer, []))}


def summary_view(summary: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if summary is None:
        return None
    return {'kind': 'conversation_summary', 'content': summary.get('content'),
            'instruction': False, 'externally_verified': False, 'source': 'summary'}


def history_view(history: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, item in enumerate(history):
        row: dict[str, Any] = {'role': item['role'], 'content': item['content'], 'source': f'history:{index}'}
        # 固定资料边界在系统指令中说明一次，模型视图不逐消息重复；宿主原记录保留。
        if item['role'] == 'assistant':
            row['confirmed'] = False
            if 'draft_version' in item:
                row['draft_version'] = item['draft_version']
            if item.get('tool_facts'):
                row['tool_facts'] = [{'kind': 'tool_fact', 'verified': True,
                    'external_content_verified': False, 'fact': fact['fact']}
                    for fact in item['tool_facts']]
        result.append(row)
    return result


def references_view(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    history_index = 0
    for item in items:
        kind = item.get('kind')
        if kind == 'role_context':
            result.append({'kind': kind, 'data': role_view(item['data'])})
        elif kind == 'conversation_summary':
            result.append(summary_view(item) or {})
        elif kind in {'user_reference', 'assistant_reference'}:
            result.append({'kind': kind, 'content': item['content'], 'source': f'history:{history_index}'})
            history_index += 1
        elif kind == 'task_origin':
            data = item.get('data', {})
            result.append({'kind': kind, 'data': {key: data[key] for key in ('kind', 'basis') if key in data},
                           'grants_permission': False})
        elif kind == 'tool_fact':
            result.append({'kind': kind, 'verified': item.get('verified', False), 'fact': item.get('fact')})
        else:
            # 执行结果和追加要求不可语义截断；只移除宿主自己的重复来源索引。
            result.append({key: value for key, value in item.items() if key not in {'refs', 'source_ref'}})
    return result


def memory_view(content: str) -> Any:
    try:
        value = json.loads(content)
    except ValueError:
        return content
    if not isinstance(value, dict) or value.get('type') != 'memory_evidence':
        return content
    return {key: value[key] for key in ('type', 'memory_id', 'revision', 'claim_type', 'source_origin',
        'source_time', 'valid_from', 'valid_to', 'expires_at', 'content', 'status', 'uncertainty', 'destination',
        'subject', 'speaker_id', 'speaker_name', 'speaker_kind', 'channel') if key in value}
