from dataclasses import dataclass
from typing import Any

from deskpal.tools.models import ToolCall


@dataclass(frozen=True)
class Plan:
    reply: str = ""
    steps: tuple[ToolCall, ...] = ()
    action: str = "answer"
    context: dict[str, Any] | None = None
    # 兼容既有确定性规划器的构造；真实 LLM 结束协议必须显式提供布尔值，解析器不猜默认值。
    organize_memory: bool = True

