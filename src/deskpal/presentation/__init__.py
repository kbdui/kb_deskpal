"""Qt-free, metadata-only presentation values and fixed state mappings."""

from .models import PresentationSnapshot
from .state import expression_for_status, status_label

__all__ = ["PresentationSnapshot", "expression_for_status", "status_label"]
