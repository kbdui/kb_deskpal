class LLMError(RuntimeError):
    """Sanitized provider failure; never expose raw request headers or API keys."""

