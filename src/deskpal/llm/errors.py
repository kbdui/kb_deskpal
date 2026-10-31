class LLMError(RuntimeError):
    """Sanitized provider failure; never expose raw request headers or API keys."""


class RetryableLLMError(LLMError):
    """超时或临时连接故障；只重试模型，不重放工具。"""

