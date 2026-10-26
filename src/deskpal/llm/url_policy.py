"""Pure shared endpoint policy for setup and Responses providers."""

import ipaddress
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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


def validate_connection_url(value: Any, location: str) -> str:
    """Cloud uses HTTPS; local connects directly to a numeric loopback address.

    Normalize localhost without consulting DNS, preventing hosts-file/DNS changes
    from turning a local-only connection into a remote request.
    """
    normalized = validate_base_url(value)
    parsed = urlsplit(normalized)
    if location == "cloud" and parsed.scheme == "https":
        return normalized
    if location == "local":
        host = parsed.hostname or ""
        if host.lower() == "localhost":
            host = "127.0.0.1"
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise LLMError("Local endpoint must use a loopback address") from None
        if address.is_loopback:
            authority = f"[{host}]" if address.version == 6 else host
            if parsed.port is not None:
                authority += f":{parsed.port}"
            return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
    raise LLMError("Endpoint does not match its execution location")


__all__ = ["validate_base_url", "validate_connection_url"]
