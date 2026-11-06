class LLMError(RuntimeError):
    """Sanitized provider failure; never expose raw request headers or API keys."""

    def __init__(self, message: str, *, code: str = "provider_error", http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class RetryableLLMError(LLMError):
    """超时或临时连接故障；只重试模型，不重放工具。"""

