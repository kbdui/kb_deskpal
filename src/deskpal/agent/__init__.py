"""Agent 核心包：提供请求协调器、公共结果模型和依赖协议。"""

from .models import AgentResult, AuthorizationRequest
from .ports import Agent
from .core import AgentCore

# 通过包级导出保持调用方可以使用 `from deskpal.agent import AgentCore`。
__all__ = ["AgentResult", "AuthorizationRequest", "Agent", "AgentCore"]
