"""只允许固定类别和计量进入账本；不序列化异常正文。"""

import re
from typing import Any

from .errors import LLMError

NUMBERS = frozenset({'elapsed_ms', 'request_bytes', 'response_bytes', 'input_tokens',
    'cached_tokens', 'output_tokens', 'reasoning_tokens', 'http_status', 'retry_delay_ms'})
CODES = frozenset({'provider_error', 'timeout', 'network', 'rate_limit', 'http_transient',
    'http_error', 'output_limit', 'invalid_response', 'source_invalidated', 'interrupted',
    'json_invalid', 'fields_invalid', 'time_invalid', 'source_invalid', 'quote_invalid',
    'revision_conflict', 'proposal_invalid', 'commit_failed', 'input_limit', 'ok'})


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
    return clean


def failure(error: BaseException) -> dict[str, Any]:
    if isinstance(error, LLMError):
        return sanitize(dict(error_code=error.code, http_status=error.http_status))
    return {'error_code': 'interrupted' if isinstance(error, (KeyboardInterrupt, SystemExit)) else 'source_invalidated'}
