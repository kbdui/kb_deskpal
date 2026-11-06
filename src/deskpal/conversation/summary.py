"""任务进展与聊天背景共用的有界摘要正文；旧摘要仍然可读。"""

import json
from collections.abc import Mapping


class SummaryValidationError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def validate_summary(content: object) -> None:
    required = {"progress", "decisions", "open_questions"}
    if (not isinstance(content, Mapping) or not required <= set(content)
            or set(content) - required - {"dialogue_context"}):
        raise SummaryValidationError("summary.fields", "Missing or unsupported summary fields")
    for field, values in content.items():
        if (not isinstance(values, list) or len(values) > 16
                or any(not isinstance(value, str) or not value.strip() or "\x00" in value
                       or len(value.encode("utf-8")) > 2000 for value in values)):
            raise SummaryValidationError("summary." + field, "Expected up to 16 non-empty strings, at most 2000 UTF-8 bytes each")
    if len(json.dumps(dict(content), ensure_ascii=False).encode("utf-8")) > 4096:
        raise SummaryValidationError("summary.bytes", "Summary exceeds 4096 UTF-8 bytes")
