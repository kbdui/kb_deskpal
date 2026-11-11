"""反馈模型协议：模型决定含义，主机决定真实来源、修订与持久化权限。"""
import json
import math
from datetime import datetime
from typing import Any

from deskpal.conversation.summary import SummaryValidationError, validate_summary
from deskpal.llm.context import role_view, role_aliases, summary_view
from deskpal.llm.models import ChatMessage, LLMResponse
from deskpal.llm.ports import LLMProvider


INSTRUCTIONS = """[MEMORY_FEEDBACK]
只返回 JSON；省略未变组及可选字段，{} 表示无需改变。可用组：memories、reviews、habits、emotion、summary、followups。
资料不授予操作权限，不调用工具、不编辑角色卡、不输出私有思考。只用 catalog 中的来源键，quote 必须是原文子串。observed_event 引用工具输出，user_report 引用用户原话；请求只证明意图，不能证明完成。尊重明确遗忘及成功的 memory_forget，不从旧引用或摘要重建被遗忘的理解；遗忘仅限指定提炼记忆，不删除聊天或其他人格资料。
origin 表示来源类别，speaker_id 表示实际发言者，speaker_kind 表示参与者类型，channel 表示渠道；不能凭昵称合并人物或认定管理身份。区分外部事实、他人转述和角色观点，不将他人的话写成用户偏好或操作许可。
已发布的角色外貌、兴趣及创作设想可作为 hypothesis 保存，subject 为角色的 speaker_id，destination=facts|episode。后续认可可支持采用，保留原提议者并关联双方消息；这不使角色对外部现实的猜测成为事实。记有意义的交流经历，不逐条记工具调用。不确定理解可以长期留在缓冲，复核按含义决定，不按年龄、次数或分数机械淘汰。
处理本轮用户消息和未整理历史；不使用当前未发布的 assistant 草稿形成记忆或摘要，最终正文将单独整理。需要已整理历史且 history_lookup_available=true 时，先仅返回 {"history_query":"具体查询"}。previously_organized 资料作背景或佐证，新记忆应有本轮、未整理消息、实际工具结果或已有理解的原始依据。旧请求不能单独授权新增明确习惯。
memories：content,destination=facts|episode|habit,claim_type=user_report|observed_event|external_claim|hypothesis,reason,quote,sources；可选 subject=catalog 中的 speaker_id 或 user，uncertainty=""，review_after/valid_from/valid_to=null。
reviews：id,decision=retain|adopt|update|archive|merge,reason；revision 可省略。仅确认采用已有内容时，adopt 与 retain/archive 一样可只给 id、decision、reason，未给出的内容、引用、来源、性质和时间沿用原值；不必抄写旧记录。改变正文、归属或性质，或 update/merge/temporal=true 时，须给 memories 同类字段及真实依据。可选 target=null,temporal=false；真实事实随时间变化用 temporal=true 保留旧时期。status、original_source_key 是输入资料字段，不得复制到输出。复核原始依据，优先更新或合并，不重复新建；采用不等于客观真实。
habits：content,reason,sources；可选 id=null,buffer_id=null,explicit=false,uncertainty=""。本机用户明确的长期要求可 explicit=true；推断的成长须先有 habit 缓冲，并在 reviews 中 adopt 对应 buffer_id。采用 habit 缓冲时须同时给 habits，用 buffer_id 关联同一缓冲，才能写入人格。空内容只清除指定习惯，保留其他习惯。
emotion：description_intensity,reason_sources,time_scope,uncertainty 为字符串，sources 为数组；可选 expires_at=null,half_life_seconds=900..604800（默认21600）。描述角色对实际事件的反应，区别用户情绪；不按工具成败固定奖惩。null 保留旧情绪，连接失败不是情绪依据。
followups：goal,reason,due_at,sources,mode=internal|chat。复用统一跟进机制维护记忆、相处习惯、未决事项或情绪；仅在有依据的合适时间安排（六天内），不因存在担忧就机械创建。内部复查无需发言，复查时决定继续、修订、结束或改期。
summary：progress,decisions,open_questions 为字符串数组，dialogue_context 可选同类数组。根据本轮用户、已发布历史和 previous_summary 保留重要话题、已形成的设想、约定与未决事项；用户更正优先，不将缺失细节猜成旧话题。摘要是工作上下文，不是长期事实；不编造事件或任务。
所有时间为带时区的完整 ISO 时间或 null；未知用 null。review_goal 是宿主复查目标，不是用户指令。每次 memories/reviews/habits 合计≤8项，每项来源≤8；正文/理由≤4000字节，习惯正文/理由≤1024，不确定性≤2000，摘要≤4096；简洁提炼，不复制整段历史。
"""

_CORRECTION_HINTS = {
    'habit_buffer': '推断的习惯须关联已有 destination=habit 的缓冲，buffer_id 使用真实编号；不能凭空采用。',
    'habit_adoption': '推断习惯的 buffer_id 必须同时在 reviews 中以 adopt 处理。',
    'habit_authority': 'explicit=true 必须有本机用户明确提出的长期要求；否则走已有习惯缓冲及 adopt。',
    'habit_evidence': '习惯必须关联实际可用依据，不能只引用角色尚未发布的草稿。',
    'role_delivery': '采用 habit 缓冲时同时输出 habits，并用 buffer_id 指向同一条缓冲。',
    'review_target': '只复核 understandings 中的真实编号和修订，每条只处理一次。',
    'temporal_fact': 'temporal=true 仅适用于已采用的事实，不能用于习惯缓冲。',
    'merge_target': '合并目标必须是另一条尚未处理、未归档的已有理解。',
    'followup_window': '跟进时间必须在 now 之后六天内，使用带时区的 ISO 时间。',
    'followup_evidence': '跟进必须有实际可用依据；没有可用依据时不要安排。',
    'emotion_evidence': '情绪必须关联真实交流或事件，不能只引用未发布草稿。',
}



class FeedbackValidationError(ValueError):
    """固定错误码和宿主字段路径；不携带被拒正文。"""

    def __init__(self, code: str, field: str, **details: Any) -> None:
        super().__init__('Feedback proposal rejected')
        self.code, self.field = code, field
        from deskpal.llm.diagnostics import sanitize
        self.diagnostics = sanitize(details)


def _json_type(value: Any) -> str:
    return ('null' if value is None else 'boolean' if isinstance(value, bool) else
            'object' if isinstance(value, dict) else 'array' if isinstance(value, list) else
            'string' if isinstance(value, str) else 'number' if isinstance(value, (int, float)) else 'other')


def _fields_error(value: Any, required: set[str], allowed: set[str], field: str,
                  *, stage: str = 'validate') -> FeedbackValidationError:
    """记录补全后仍缺失/多余的字段，不保留值或任意异常正文。"""
    keys = set(value) if isinstance(value, dict) else set()
    missing, extra = required - keys, keys - allowed
    return FeedbackValidationError('fields_invalid', field, processing_stage=stage,
        processing_rule='field_set' if isinstance(value, dict) else 'wrong_type',
        processing_expected_type='object', processing_actual_type=_json_type(value),
        processing_missing_count=len(missing), processing_extra_count=len(extra),
        processing_missing_fields=sorted(missing), processing_extra_fields=sorted(extra),
        **({'processing_decision': value.get('decision', 'unknown')} if isinstance(value, dict) else {}))


def feedback_view(payload: dict[str, Any]) -> dict[str, Any]:
    catalog = payload.get('catalog', {})
    role = payload.get('role', {})
    return dict(role=role_view(role), previous_summary=summary_view(payload.get('previous_summary')),
        now=payload.get('now'),
        catalog={key: {field: row[field] for field in ('content', 'role', 'origin', 'available', 'created_at', 'previously_organized',
                 'speaker_id', 'speaker_name', 'speaker_kind', 'channel', 'published') if field in row}
                 for key, row in catalog.items()},
        understandings=[{key: value for key, value in row.items() if key not in {'source_refs', 'quote'}}
                        for row in payload.get('understandings', [])],
        current_exchange={'user_source': 'user' if 'user' in catalog else None,
                          'assistant_source': 'assistant' if 'assistant' in catalog else None,
                          'assistant_is_proposal': True},
        published_history=[{'source': key} for key in catalog if key.startswith('history')],
        **{key: payload[key] for key in ('history_lookup_available', 'history_lookup_result') if key in payload},
        **({'review_goal': payload['review_goal']} if 'review_goal' in payload else {}))


def normalize_feedback(data: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """只补技术默认值/已有原值，不猜测含义、时间、用户意图或许可。"""
    if not isinstance(data, dict):
        raise _fields_error(data, set(), set(), 'feedback', stage='normalize')
    result = {'memories': [], 'reviews': [], 'habits': [], 'emotion': None, 'summary': None, **data}
    if isinstance(result['summary'], dict):
        # 单段文字与单元素数组语义相同，宿主规范化即可，无需重发模型。
        result['summary'] = {key: [value] if isinstance(value, str) and value.strip() else value
                             for key, value in result['summary'].items()}
    existing = {row['id']: row for row in payload.get('understandings', [])}
    aliases = role_aliases(payload.get('role', {}))
    for kind in ('memories', 'reviews', 'habits'):
        if not isinstance(result[kind], list):
            raise FeedbackValidationError('fields_invalid', kind, processing_stage='normalize',
                processing_rule='wrong_type', processing_expected_type='array', processing_actual_type=_json_type(result[kind]))
        normalized = []
        for index, raw in enumerate(result[kind]):
            if not isinstance(raw, dict):
                raise _fields_error(raw, set(), set(), f'{kind}[{index}]', stage='normalize')
            row = dict(raw)
            if kind in {'memories', 'reviews'}:
                row.setdefault('subject', '')
                for field in ('review_after', 'valid_from', 'valid_to'):
                    row.setdefault(field, None)
            row.setdefault('uncertainty', '')
            if kind == 'reviews':
                previous = existing.get(row.get('id')) if isinstance(row.get('id'), str) else None
                if previous is None:
                    raise FeedbackValidationError('source_invalid', f'reviews[{index}].id')
                row.setdefault('revision', previous['revision'])
                row.setdefault('target', None)
                row.setdefault('temporal', False)
                # 纯采用只改变采用状态；正文/人物/性质改变仍必须提供完整修改及依据。
                unchanged_adoption = (row.get('decision') == 'adopt' and not row['temporal'] and
                    all(key not in raw or raw[key] == previous.get(key, default)
                        for key, default in (('content', ''), ('destination', ''), ('claim_type', ''),
                                             ('subject', ''), ('valid_from', None), ('valid_to', None))))
                if row.get('decision') in {'retain', 'archive'} or unchanged_adoption:
                    for field in ('content', 'destination', 'claim_type', 'uncertainty'):
                        if field not in raw:
                            row[field] = previous[field]
                    for field in ('review_after', 'valid_from', 'valid_to'):
                        if field not in raw:
                            row[field] = previous.get(field)
                    row['subject'] = raw.get('subject', previous.get('subject', ''))
                    key = previous['original_source_key']
                    row.setdefault('sources', [key])
                    row.setdefault('quote', previous.get('quote', payload['catalog'][key]['content']))
            elif kind == 'habits':
                row.setdefault('id', None)
                row.setdefault('buffer_id', None)
                row.setdefault('explicit', False)
                if isinstance(row['id'], str):
                    row['id'] = aliases.get(row['id'], row['id'])
            normalized.append(row)
        result[kind] = normalized
    if isinstance(result['emotion'], dict):
        result['emotion'] = {'expires_at': None, **result['emotion']}
    return result


def text(value: Any, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or '\x00' in value or len(value.encode('utf-8')) > limit:
        raise ValueError('Invalid feedback text')
    return value


def moment(value: Any) -> datetime | None:
    if value is None:
        return None
    result = datetime.fromisoformat(text(value, 100))
    if result.utcoffset() is None:
        raise ValueError('Feedback time requires timezone')
    return result


def evaluate(provider: LLMProvider, payload: dict[str, Any], *, correction: bool = False, correction_code: str = "",
             correction_field: str = "", correction_diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
    encoded = json.dumps(feedback_view(payload), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode('utf-8')) > 96000:
        raise FeedbackValidationError('input_limit', 'feedback')
    from deskpal.llm.diagnostics import sanitize
    correction_details = sanitize({**(correction_diagnostics or {}),
        'processing_code': correction_code, 'processing_field': correction_field})
    hint = _CORRECTION_HINTS.get(correction_details.get('processing_rule', ''), '')
    instruction = INSTRUCTIONS + ('\n[CORRECTION] 上次提议未通过校验，仅修正以下宿主诊断：'
        + json.dumps(correction_details, separators=(',', ':'))
        + '。missing_fields 对应的字段需补齐，extra_fields 对应的不支持字段需删除；核对预期类型和字节限制。'
        + hint + '核对原始依据，确实无需变更或依据不足可返回空结果。' if correction else '')
    response = provider.chat((ChatMessage('system', instruction), ChatMessage('user', encoded)), tools=())
    if (not isinstance(response, LLMResponse) or not isinstance(response.content, str)
            or response.tool_calls or len(response.content.encode('utf-8')) > 48000):
        raise FeedbackValidationError('fields_invalid', 'feedback')
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate feedback field')
            result[key] = value
        return result
    def reject(value: str) -> Any:
        raise ValueError('Nonfinite feedback value')
    try:
        data = json.loads(response.content, object_pairs_hook=unique, parse_constant=reject)
    except json.JSONDecodeError as error:
        raise FeedbackValidationError('json_invalid', 'feedback', processing_stage='parse',
            processing_json_line=error.lineno, processing_json_column=error.colno) from None
    except ValueError:
        raise FeedbackValidationError('json_invalid', 'feedback', processing_stage='parse') from None
    if isinstance(data, dict) and 'history_query' in data:
        if set(data) != {'history_query'} or not payload.get('history_lookup_available'):
            raise FeedbackValidationError('fields_invalid', 'history_query')
        try:
            return {'history_query': text(data['history_query'], 256).strip()}
        except ValueError:
            raise FeedbackValidationError('fields_invalid', 'history_query') from None
    data = normalize_feedback(data, payload)
    required = {'memories', 'reviews', 'habits', 'emotion', 'summary'}
    if not isinstance(data, dict) or not required <= set(data) or set(data) - required - {'followups'}:
        raise _fields_error(data, required, required | {'followups'}, 'feedback')
    for key in ('memories', 'reviews', 'habits'):
        if not isinstance(data[key], list):
            raise ValueError('Invalid feedback actions')
    if sum(len(data[key]) for key in ('memories', 'reviews', 'habits')) > 8:
        raise ValueError('Feedback action budget exceeded')
    common = {'content', 'reason', 'uncertainty', 'sources'}
    fields = {'memories': common | {'destination', 'claim_type', 'quote', 'subject', 'review_after', 'valid_from', 'valid_to'},
              'reviews': common | {'id', 'revision', 'decision', 'destination', 'claim_type', 'quote', 'subject', 'review_after', 'valid_from', 'valid_to', 'target', 'temporal'},
              'habits': common | {'id', 'buffer_id', 'explicit'}}
    for kind in fields:
        for index, row in enumerate(data[kind]):
            if not isinstance(row, dict) or set(row) != fields[kind]:
                raise _fields_error(row, fields[kind], fields[kind], f'{kind}[{index}]')
            for field, limit, empty in (('content', 1024 if kind == 'habits' else 4000, kind == 'habits'),
                                        ('reason', 1024 if kind == 'habits' else 4000, False),
                                        ('uncertainty', 2000, True)):
                try:
                    text(row[field], limit, empty=empty)
                except ValueError:
                    value = row[field]
                    size = len(value.encode('utf-8')) if isinstance(value, str) else 0
                    rule = ('wrong_type' if not isinstance(value, str) else 'byte_limit' if size > limit else
                            'empty_text' if not empty and not value.strip() else 'invalid_text')
                    raise FeedbackValidationError('fields_invalid', f'{kind}[{index}].{field}',
                        processing_rule=rule, processing_expected_type='string', processing_actual_type=_json_type(value),
                        processing_actual_bytes=size, processing_limit_bytes=limit) from None
            sources = row['sources']
            if not isinstance(sources, list) or not 1 <= len(sources) <= 8 or any(not isinstance(key, str) or key not in payload['catalog'] for key in sources):
                raise FeedbackValidationError('source_invalid', f'{kind}[{index}].sources')
            if kind != 'habits':
                subject = row['subject']
                allowed_subjects = {'', 'user'} | {entry.get('speaker_id', '') for entry in payload['catalog'].values()}
                if not isinstance(subject, str) or subject not in allowed_subjects:
                    raise FeedbackValidationError('source_invalid', f'{kind}[{index}].subject')
                text(row['quote'], 8000)
                if row['destination'] not in ('facts', 'episode', 'habit'):
                    raise ValueError('Unknown feedback destination')
                if row['claim_type'] not in ('user_report', 'observed_event', 'external_claim', 'hypothesis'):
                    raise ValueError('Unknown feedback attribution')
                for time_field in ('review_after', 'valid_from', 'valid_to'):
                    try:
                        moment(row[time_field])
                    except ValueError:
                        raise FeedbackValidationError('time_invalid', f'{kind}[{index}].{time_field}') from None
            if kind == 'reviews':
                text(row['id'], 256)
                if type(row['revision']) is not int or row['revision'] < 1 or type(row['temporal']) is not bool:
                    raise ValueError('Invalid feedback revision')
                if row['decision'] not in ('retain', 'adopt', 'update', 'archive', 'merge'):
                    raise ValueError('Unknown review decision')
                if row['target'] is not None:
                    text(row['target'], 256)
            if kind == 'habits':
                if type(row['explicit']) is not bool:
                    raise ValueError('Invalid habit authority')
                for key in ('id', 'buffer_id'):
                    if row[key] is not None:
                        text(row[key], 256)
    emotion = data['emotion']
    if emotion is not None:
        fields_emotion = {'description_intensity', 'reason_sources', 'time_scope', 'uncertainty', 'sources', 'expires_at'}
        if not isinstance(emotion, dict) or not fields_emotion <= set(emotion) or set(emotion) - fields_emotion - {'half_life_seconds'}:
            raise _fields_error(emotion, fields_emotion, fields_emotion | {'half_life_seconds'}, 'emotion')
        for key in ('description_intensity', 'reason_sources', 'time_scope', 'uncertainty'):
            text(emotion[key], 2048)
        if not isinstance(emotion['sources'], list) or not 1 <= len(emotion['sources']) <= 8 or any(not isinstance(key, str) or key not in payload['catalog'] for key in emotion['sources']):
            raise FeedbackValidationError('source_invalid', 'emotion.sources')
        try:
            moment(emotion['expires_at'])
        except ValueError:
            raise FeedbackValidationError('time_invalid', 'emotion.expires_at') from None
        half_life = emotion.get('half_life_seconds', 21600)
        if type(half_life) not in (int, float) or not math.isfinite(half_life) or not 900 <= half_life <= 604800:
            raise ValueError('Invalid emotion weakening time')
    followups = data.get('followups', [])
    if not isinstance(followups, list) or len(followups) > 4:
        raise ValueError('Follow-up budget exceeded')
    for index, row in enumerate(followups):
        if not isinstance(row, dict) or set(row) != {'goal', 'reason', 'due_at', 'sources', 'mode'}:
            required_followup = {'goal', 'reason', 'due_at', 'sources', 'mode'}
            raise _fields_error(row, required_followup, required_followup, f'followups[{index}]')
        text(row['goal'], 4000); text(row['reason'], 4000)
        if row['mode'] not in ('internal', 'chat'):
            raise FeedbackValidationError('fields_invalid', f'followups[{index}].mode')
        try:
            if moment(row['due_at']) is None:
                raise ValueError('Missing follow-up time')
        except ValueError:
            raise FeedbackValidationError('time_invalid', f'followups[{index}].due_at') from None
        if not isinstance(row['sources'], list) or not 1 <= len(row['sources']) <= 8 or any(not isinstance(key, str) or key not in payload['catalog'] for key in row['sources']):
            raise FeedbackValidationError('source_invalid', f'followups[{index}].sources')
    summary = data['summary']
    if summary is not None:
        try:
            validate_summary(summary)
        except SummaryValidationError as error:
            raise FeedbackValidationError('fields_invalid', error.field) from error
    return data
