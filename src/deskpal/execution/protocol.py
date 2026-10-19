"""Bounded JSON framing for the stage-zero execution transport.

Framing validates data only. Authentication, invocation correlation and legal
message transitions belong to the supervisor; this module grants no capability.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import struct
from typing import cast


class ProtocolViolation(ValueError):
    """A fixed category, deliberately excluding untrusted message contents."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FrameLimits:
    max_frame_bytes: int = 256 * 1024
    max_stream_bytes: int = 1024 * 1024
    max_depth: int = 16
    max_string_bytes: int = 64 * 1024
    max_container_items: int = 1024
    max_nodes: int = 8192
    max_frames: int = 32

    def __post_init__(self) -> None:
        for value in (self.max_frame_bytes, self.max_stream_bytes, self.max_depth,
                      self.max_string_bytes, self.max_container_items,
                      self.max_nodes, self.max_frames):
            if type(value) is not int or value <= 0:
                raise ValueError("Protocol limits must be positive integers")
        if self.max_stream_bytes < self.max_frame_bytes + 4:
            raise ValueError("Stream budget must include a complete frame and header")
        if self.max_depth > 64 or self.max_frame_bytes > 16 * 1024 * 1024:
            raise ValueError("Protocol limit exceeds the transport safety ceiling")


DEFAULT_LIMITS = FrameLimits()


def _validate_tree(root: object, limits: FrameLimits, *, encoded_budget: int | None = None) -> None:
    pending: list[tuple[object, int]] = [(root, 0)]
    nodes = 0
    rendered_bytes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if nodes > limits.max_nodes:
            raise ProtocolViolation("node_budget")
        if type(value) is str:
            # UTF-8 uses at least one byte per code point. Reject huge host
            # strings before encoding can allocate their complete byte copy.
            if len(value) > limits.max_string_bytes:
                raise ProtocolViolation("string_budget")
            try:
                size = len(value.encode("utf-8"))
            except UnicodeError:
                raise ProtocolViolation("invalid_unicode") from None
            if size > limits.max_string_bytes:
                raise ProtocolViolation("string_budget")
            rendered_bytes += size + 2  # Quotes plus ensure_ascii=False text.
            if encoded_budget is not None:
                for char in value:
                    if char in '"\\' or char in "\b\f\n\r\t":
                        rendered_bytes += 1
                    elif ord(char) < 32:
                        rendered_bytes += 5  # One input byte becomes six.
        elif value is None or type(value) is bool:
            rendered_bytes += 4 if value is None or value is True else 5
        elif type(value) is int:
            if not -(2**63) <= value < 2**63:
                raise ProtocolViolation("integer_range")
            rendered_bytes += len(str(value))
        elif type(value) is float:
            if not math.isfinite(value):
                raise ProtocolViolation("non_finite_number")
            rendered_bytes += len(repr(value))
        elif type(value) in (dict, list):
            if depth >= limits.max_depth:
                raise ProtocolViolation("depth_budget")
            container = cast("dict[object, object] | list[object]", value)
            if len(container) > limits.max_container_items:
                raise ProtocolViolation("container_budget")
            rendered_bytes += 2 + max(0, len(container) - 1)
            if isinstance(container, dict):
                rendered_bytes += len(container)  # Key/value colons.
                for key, child in container.items():
                    if type(key) is not str:
                        raise ProtocolViolation("object_key_type")
                    pending.append((key, depth + 1))
                    pending.append((child, depth + 1))
            else:
                pending.extend((child, depth + 1) for child in container)
        else:
            raise ProtocolViolation("value_type")
        if encoded_budget is not None and rendered_bytes > encoded_budget:
            raise ProtocolViolation("frame_budget")


def _check_nesting(payload: bytes, limit: int) -> None:
    """Bound nesting before json.loads can allocate a deep tree or recurse."""
    depth = 0
    quoted = False
    escaped = False
    for byte in payload:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > limit:
                raise ProtocolViolation("depth_budget")
        elif byte in (93, 125):
            depth -= 1


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolViolation("duplicate_key")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise ProtocolViolation("non_finite_number")


def _integer(value: str) -> int:
    if len(value.lstrip("-")) > 19:
        raise ProtocolViolation("integer_range")
    result = int(value)
    if not -(2**63) <= result < 2**63:
        raise ProtocolViolation("integer_range")
    return result


def decode_payload(payload: bytes, limits: FrameLimits = DEFAULT_LIMITS) -> dict[str, object]:
    """Decode one strictly UTF-8 JSON object within fixed limits."""
    if not payload or len(payload) > limits.max_frame_bytes:
        raise ProtocolViolation("frame_budget")
    _check_nesting(payload, limits.max_depth)
    try:
        text = payload.decode("utf-8")
        value: object = json.loads(text, object_pairs_hook=_object,
                                   parse_constant=_constant, parse_int=_integer)
    except ProtocolViolation:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolViolation("invalid_json") from None
    if type(value) is not dict:
        raise ProtocolViolation("root_object_required")
    _validate_tree(value, limits)
    return cast(dict[str, object], value)


def encode_frame(message: Mapping[str, object], limits: FrameLimits = DEFAULT_LIMITS) -> bytes:
    """Create a four-byte big-endian length header and canonical JSON body."""
    if len(message) > limits.max_container_items:
        raise ProtocolViolation("container_budget")
    value = dict(message)
    _validate_tree(value, limits, encoded_budget=limits.max_frame_bytes)
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > limits.max_frame_bytes:
        raise ProtocolViolation("frame_budget")
    return struct.pack("!I", len(payload)) + payload


class FrameDecoder:
    """Incremental bounded decoder; any malformed input poisons the channel.

    The stream byte budget includes headers. Call finish() on EOF, including
    shutdown, so a truncated message cannot be silently accepted.
    """

    def __init__(self, limits: FrameLimits = DEFAULT_LIMITS) -> None:
        self.limits = limits
        self._buffer = bytearray()
        self._expected: int | None = None
        self._received = 0
        self._frames = 0
        self._failed = False
        self._finished = False

    def feed(self, chunk: bytes) -> list[dict[str, object]]:
        if self._failed:
            raise ProtocolViolation("decoder_failed")
        if self._finished:
            raise ProtocolViolation("decoder_finished")
        try:
            return self._feed(chunk)
        except ProtocolViolation:
            self._failed = True
            self._buffer.clear()
            raise

    def _feed(self, chunk: bytes) -> list[dict[str, object]]:
        if type(chunk) is not bytes:
            raise ProtocolViolation("chunk_type")
        if self._received + len(chunk) > self.limits.max_stream_bytes:
            raise ProtocolViolation("stream_budget")
        self._received += len(chunk)
        messages: list[dict[str, object]] = []
        position = 0
        while position < len(chunk):
            needed = (4 if self._expected is None else self._expected) - len(self._buffer)
            count = min(needed, len(chunk) - position)
            self._buffer.extend(chunk[position:position + count])
            position += count
            if self._expected is None and len(self._buffer) == 4:
                self._expected = struct.unpack("!I", self._buffer)[0]
                self._buffer.clear()
                if not 0 < self._expected <= self.limits.max_frame_bytes:
                    raise ProtocolViolation("frame_budget")
            elif self._expected is not None and len(self._buffer) == self._expected:
                self._frames += 1
                if self._frames > self.limits.max_frames:
                    raise ProtocolViolation("frame_count_budget")
                messages.append(decode_payload(bytes(self._buffer), self.limits))
                self._buffer.clear()
                self._expected = None
        return messages

    def finish(self) -> None:
        if self._failed:
            raise ProtocolViolation("decoder_failed")
        if self._buffer or self._expected is not None:
            self._failed = True
            self._buffer.clear()
            raise ProtocolViolation("truncated_frame")
        self._finished = True
