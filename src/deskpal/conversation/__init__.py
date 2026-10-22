"""Bounded dialogue context, intentionally independent of Qt and persistence."""

from .models import ConversationRef, ConversationTurn, ConversationWindow
from .runtime import ConversationCapacity, ConversationConflict, ConversationRuntime

__all__ = ["ConversationCapacity", "ConversationConflict", "ConversationRef",
           "ConversationRuntime", "ConversationTurn", "ConversationWindow"]
