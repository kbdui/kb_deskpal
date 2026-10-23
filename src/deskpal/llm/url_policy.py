"""Pure shared endpoint policy for setup and Responses providers."""

import ipaddress
from typing import Any
from urllib.parse import urlsplit

from .errors import LLMError


def validate_base_url(value: Any) -> str:
    """Return a normalized safe base URL without DNS, requests or credentials."""
    try:
        if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.query or parsed.fragment or "%" in parsed.netloc or "\\" in value:
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        if parsed.scheme == "http":
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                loopback = parsed.hostname.lower() == "localhost"
            if not loopback:
                raise ValueError
        elif parsed.scheme != "https":
            raise ValueError
        return value.rstrip("/")
    except (ValueError, TypeError):
        raise LLMError("base_url must be HTTPS (or loopback HTTP), without credentials, query or fragment") from None


__all__ = ["validate_base_url"]
