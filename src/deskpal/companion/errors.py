"""Fixed, content-free diagnostics for companion callers."""


class CompanionError(Exception):
    """Base class for companion configuration, storage and lifecycle errors."""


class CompanionValidationError(CompanionError, ValueError):
    pass


class CompanionRevisionConflict(CompanionError):
    pass


class CompanionStorageError(CompanionError):
    pass


class CompanionClosedError(CompanionError, RuntimeError):
    pass
