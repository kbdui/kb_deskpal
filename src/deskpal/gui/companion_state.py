"""Small allow-listed companion metadata, independent of task presentation DTOs."""

from dataclasses import dataclass
from math import isfinite
from typing import Any


PRESETS = {"balanced": "均衡", "warm": "温和", "concise": "简洁"}
STYLE_AXES = {"formality": "正式程度", "warmth": "温和程度", "humor": "幽默程度",
              "verbosity": "详略程度", "expressiveness": "表达程度", "curiosity": "好奇程度"}
EXPRESSIONS = frozenset({"neutral", "thinking", "focused", "pleased", "alert"})


def _integer(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("Invalid companion counter")
    return value


def _number(value: Any, low: float, high: float) -> float:
    if type(value) not in {int, float} or not low <= value <= high:
        raise ValueError("Invalid simulated affect")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError("Invalid simulated affect") from None
    if not isfinite(number):
        raise ValueError("Invalid simulated affect")
    return number


@dataclass(frozen=True)
class CompanionSnapshot:
    """Only bounded local metadata is retained; role cards and text are discarded."""

    session_id: str
    memory_epoch: int
    revision: int
    reset_epoch: int
    enabled: bool
    emotions_enabled: bool
    persona_version: int
    preset: str
    style: tuple[tuple[str, float], ...]
    persona_versions: tuple[int, ...]
    valence: float
    arousal: float
    mood: float
    mode: str
    expression: str
    has_warnings: bool
    storage_available: bool

    @classmethod
    def from_payload(cls, value: Any) -> "CompanionSnapshot":
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise ValueError("Unsupported companion snapshot")
        session = value.get("session_id")
        if not isinstance(session, str) or not session or len(session) > 256:
            raise ValueError("Invalid companion session")
        persona, affect = value.get("persona"), value.get("affect")
        if not isinstance(persona, dict) or not isinstance(affect, dict):
            raise ValueError("Missing companion state")
        enabled, emotions = value.get("enabled"), value.get("emotions_enabled")
        if type(enabled) is not bool or type(emotions) is not bool:
            raise ValueError("Invalid companion toggles")
        preset = persona.get("preset")
        if not isinstance(preset, str) or preset not in PRESETS:
            raise ValueError("Invalid companion preset")
        expression = affect.get("expression")
        if not isinstance(expression, str) or expression not in EXPRESSIONS:
            raise ValueError("Invalid companion expression")
        mode = affect.get("mode")
        if not isinstance(mode, str) or mode not in {"neutral", "supportive", "focused"}:
            raise ValueError("Invalid companion state code")
        warnings = value.get("warnings")
        if not isinstance(warnings, (list, tuple)) or len(warnings) > 32:
            raise ValueError("Invalid companion warning list")
        style = persona.get("style")
        versions = value.get("persona_versions")
        if not isinstance(style, dict) or set(style) != set(STYLE_AXES):
            raise ValueError("Invalid companion style")
        if not isinstance(versions, list) or len(versions) > 32:
            raise ValueError("Invalid companion version list")
        return cls(session, _integer(value.get("memory_epoch")), _integer(value.get("revision")),
                   _integer(value.get("reset_epoch")), enabled, emotions,
                   _integer(persona.get("version")), preset,
                   tuple((key, _number(style[key], 0.0, 1.0)) for key in STYLE_AXES),
                   tuple(_integer(version) for version in versions),
                   _number(affect.get("valence"), -1.0, 1.0),
                   _number(affect.get("arousal"), 0.0, 1.0),
                   _number(affect.get("mood"), -0.25, 0.25), mode, expression, bool(warnings),
                   "companion_storage_unavailable" not in warnings)

    def follows(self, previous: "CompanionSnapshot") -> bool:
        forward = (self.session_id == previous.session_id
                and self.memory_epoch >= previous.memory_epoch
                and self.revision >= previous.revision
                and self.reset_epoch >= previous.reset_epoch)
        if not forward:
            return False
        if self.revision == previous.revision and self.memory_epoch == previous.memory_epoch:
            # Decay, cue expiry and warning metadata may change without a write;
            # persisted configuration cannot conflict at the same CAS revision.
            return (self.reset_epoch, self.enabled, self.emotions_enabled,
                    self.persona_version, self.preset, self.style, self.persona_versions) == (
                        previous.reset_epoch, previous.enabled, previous.emotions_enabled,
                        previous.persona_version, previous.preset, previous.style, previous.persona_versions)
        return True
