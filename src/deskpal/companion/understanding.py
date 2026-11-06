"""用统一模型解释交流要求；不使用关键词判断范围或情绪。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Understanding:
    """交流要求与临时检索线索；线索不写入人格或长期记忆。"""

    entries: tuple[dict[str, Any], ...]
    retrieval_query: str = ""
    retrieval_refs: tuple[Mapping[str, Any], ...] = ()


def parse_understanding(body: Mapping[str, Any], *, sources: Mapping[str, Mapping[str, Any]],
                        aliases: Mapping[str, str] | None = None) -> Understanding:
    """校验统一规划的上下文决定；编号仅能映射本轮已提供对象。"""
    entries = body.get("entries")
    if (not {"entries"} <= set(body) or set(body) - {"entries", "retrieval"}
            or not isinstance(entries, list) or len(entries) > 8 or any(not isinstance(row, dict) for row in entries)):
        raise ValueError("context_understanding_response_invalid")
    retrieval = body.get("retrieval")
    for row in entries:
        replaces = row.get('replaces', [])
        if not isinstance(replaces, list) or any(not isinstance(key, str) for key in replaces):
            raise ValueError('context_understanding_response_invalid')
    entries = [dict(row, expires_at=row.get('expires_at'), replaces=[(aliases or {}).get(key, key)
        for key in row.get('replaces', [])]) for row in entries]
    if retrieval is None:
        return Understanding(tuple(entries))
    if (not isinstance(retrieval, dict) or set(retrieval) != {"query", "sources"}
            or not isinstance(retrieval["query"], str) or not retrieval["query"].strip()
            or len(retrieval["query"].encode("utf-8")) > 1024 or "\x00" in retrieval["query"]
            or not isinstance(retrieval["sources"], list) or not 1 <= len(retrieval["sources"]) <= 8
            or any(not isinstance(key, str) or key not in sources for key in retrieval["sources"])):
        raise ValueError("context_retrieval_hint_invalid")
    refs: list[Mapping[str, Any]] = []
    for key in retrieval["sources"]:
        item = sources[key]
        refs.extend(item.get("refs", ()))
        if item.get("source_ref"):
            refs.append(item["source_ref"])
    return Understanding(tuple(entries), retrieval["query"].strip(), tuple(refs))
