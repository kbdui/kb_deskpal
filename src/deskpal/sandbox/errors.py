class PermissionDenied(PermissionError):
    """The current request does not authorize this tool."""


class PathViolation(PermissionError):
    """A path resolves outside the configured workspace."""


class ToolNotFound(LookupError):
    """No trusted tool with this name is registered."""

