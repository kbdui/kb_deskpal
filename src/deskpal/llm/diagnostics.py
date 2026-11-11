"""只允许固定类别和计量进入账本；不序列化异常正文。"""

import re
from typing import Any

from .errors import LLMError

NUMBERS = frozenset({'elapsed_ms', 'request_bytes', 'response_bytes', 'input_tokens',
    'cached_tokens', 'output_tokens', 'reasoning_tokens', 'http_status', 'retry_delay_ms',
    'processing_missing_count', 'processing_extra_count', 'processing_actual_bytes', 'processing_limit_bytes',
    'learning_attempt', 'learning_attempt_limit', 'learning_catalog_count', 'learning_understanding_count',
    'learning_correction', 'processing_json_line', 'processing_json_column'})
CODES = frozenset({'provider_error', 'timeout', 'network', 'rate_limit', 'http_transient',
    'http_error', 'output_limit', 'invalid_response', 'source_invalidated', 'interrupted',
    'json_invalid', 'fields_invalid', 'time_invalid', 'source_invalid', 'quote_invalid',
    'revision_conflict', 'proposal_invalid', 'commit_failed', 'input_limit', 'ok'})

# 仅记录协议字段名和固定类别；未知字段统一为 unknown，不写任意模型文本。
FIELD_NAMES = frozenset(('memories reviews habits emotion summary followups history_query content reason uncertainty '
    'sources destination claim_type quote subject review_after valid_from valid_to id revision decision target temporal '
    'buffer_id explicit description_intensity reason_sources time_scope expires_at half_life_seconds goal due_at mode '
    'progress decisions open_questions dialogue_context status original_source_key source_refs type action confidence '
    'speaker_id speaker_name speaker_kind channel unknown').split())
ENUMS = {
    'processing_stage': {'model', 'parse', 'normalize', 'validate', 'commit'},
    'processing_rule': {'field_set', 'wrong_type', 'empty_text', 'invalid_text', 'byte_limit', 'unknown',
        'followup_window', 'followup_evidence', 'habit_buffer', 'habit_adoption', 'habit_authority',
        'habit_evidence', 'review_target', 'role_delivery', 'temporal_fact', 'merge_target', 'emotion_evidence'},
    'processing_expected_type': {'object', 'array', 'string', 'number', 'boolean', 'null', 'other'},
    'processing_actual_type': {'object', 'array', 'string', 'number', 'boolean', 'null', 'other'},
    'processing_decision': {'retain', 'adopt', 'update', 'archive', 'merge', 'unknown'},
    'learning_kind': {'turn', 'review'},
    'learning_outcome': {'saved', 'history_lookup', 'invalid_proposal', 'provider_failure', 'source_invalidated',
        'commit_failed', 'delivery_pending', 'revision_conflict', 'input_limit'},
}


def sanitize(values: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key in NUMBERS:
        value = values.get(key)
        if type(value) is int and 0 <= value <= 2**53:
            clean[key] = value
    for key in ('error_code', 'processing_code'):
        if isinstance(values.get(key), str) and values[key] in CODES:
            clean[key] = values[key]
    for key in ('processing_field', 'request_id'):
        value = values.get(key)
        if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:\[\]-]{1,160}', value):
            clean[key] = value
    for key, allowed in ENUMS.items():
        value = values.get(key)
        if isinstance(value, str) and value in allowed:
            clean[key] = value
    for key in ('processing_missing_fields', 'processing_extra_fields'):
        value = values.get(key)
        if isinstance(value, list):
            clean[key] = sorted({item if isinstance(item, str) and item in FIELD_NAMES else 'unknown'
                                 for item in value[:32]})
    return clean


def failure(error: BaseException) -> dict[str, Any]:
    if isinstance(error, LLMError):
        return sanitize(dict(error_code=error.code, http_status=error.http_status))
    return {'error_code': 'interrupted' if isinstance(error, (KeyboardInterrupt, SystemExit)) else 'source_invalidated'}
