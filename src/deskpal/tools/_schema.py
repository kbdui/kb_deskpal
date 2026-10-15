"""A deliberately small, explicit JSON Schema subset for trusted tools."""

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


class ArgumentError(ValueError):
    """A safe, user-visible validation error."""


def snapshot_json(value: Any, active: set[int] | None = None) -> Any:
    """Copy only JSON values, rejecting cycles and non-finite numbers."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ArgumentError("JSON numbers must be finite")
        return value
    if not isinstance(value, (Mapping, list)):
        raise ArgumentError("Arguments and outputs must contain only JSON values")
    seen = set() if active is None else active
    if id(value) in seen:
        raise ArgumentError("JSON values must not contain cycles")
    seen.add(id(value))
    try:
        if isinstance(value, Mapping):
            if any(not isinstance(key, str) for key in value):
                raise ArgumentError("JSON object keys must be strings")
            return {key: snapshot_json(item, seen) for key, item in value.items()}
        return [snapshot_json(item, seen) for item in value]
    finally:
        seen.remove(id(value))


def freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze_json(item) for item in value)
    return value


def thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
_COMMON = {"type", "description", "title", "enum", "default"}
_BY_TYPE = {
    "object": {"properties", "required", "additionalProperties"},
    "array": {"items", "minItems", "maxItems"},
    "string": {"minLength", "maxLength"},
    "integer": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"},
    "number": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"},
    "boolean": set(),
    "null": set(),
}


def validate_schema(schema: Mapping[str, Any], *, root: bool = True) -> None:
    """Reject unsupported or malformed schemas at registration, never at execution."""
    kind = schema.get("type")
    if not isinstance(kind, str) or kind not in _TYPES or (root and kind != "object"):
        raise ValueError("Schema must declare a supported type; root must be object")
    unknown = set(schema) - _COMMON - _BY_TYPE[kind]
    if unknown:
        raise ValueError("Unsupported schema keyword: " + ", ".join(sorted(unknown)))
    for annotation in ("title", "description"):
        if annotation in schema and not isinstance(schema[annotation], str):
            raise ValueError(f"Schema {annotation} must be a string")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError("Schema enum must be a non-empty array")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ValueError(f"Schema {key} must be a non-negative integer")
    for low, high in (("minLength", "maxLength"), ("minItems", "maxItems")):
        if low in schema and high in schema and schema[low] > schema[high]:
            raise ValueError("Schema minimum cannot exceed maximum")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema and (type(schema[key]) not in (int, float)):
            raise ValueError(f"Schema {key} must be a number")
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        additional = schema.get("additionalProperties", True)
        if not isinstance(properties, Mapping):
            raise ValueError("Schema properties must be an object")
        if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
            raise ValueError("Schema required must be an array of strings")
        if len(set(required)) != len(required):
            raise ValueError("Schema required must not repeat properties")
        if not isinstance(additional, (bool, Mapping)):
            raise ValueError("Schema additionalProperties must be boolean or schema")
        if additional is False and any(key not in properties for key in required):
            raise ValueError("Required properties must be declared when extras are forbidden")
        for child in properties.values():
            if not isinstance(child, Mapping):
                raise ValueError("Property schemas must be objects")
            validate_schema(child, root=False)
        if isinstance(additional, Mapping):
            validate_schema(additional, root=False)
    if kind == "array" and "items" in schema:
        if not isinstance(schema["items"], Mapping):
            raise ValueError("Schema items must be a schema object")
        validate_schema(schema["items"], root=False)


def _equal_json(left: Any, right: Any) -> bool:
    # Python considers True == 1; JSON boolean and numeric enum values differ.
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return set(left) == set(right) and all(_equal_json(left[k], right[k]) for k in left)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(_equal_json(a, b) for a, b in zip(left, right))
    return bool(left == right)


def validate_arguments(value: Any, schema: Mapping[str, Any], location: str = "arguments") -> None:
    kind = schema["type"]
    matches = {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "boolean": type(value) is bool,
        "null": value is None,
    }
    if not matches[kind]:
        raise ArgumentError(f"{location} must have type {kind}")
    if "enum" in schema and not any(_equal_json(value, choice) for choice in schema["enum"]):
        raise ArgumentError(f"{location} is not an allowed value")
    if kind == "object":
        properties = schema.get("properties", {})
        for key in schema.get("required", ()):
            if key not in value:
                raise ArgumentError(f"{location} is missing required property {key}")
        for key, item in value.items():
            child = properties.get(key)
            if child is None:
                additional = schema.get("additionalProperties", True)
                if additional is False:
                    raise ArgumentError(f"{location} contains unexpected property {key}")
                if isinstance(additional, Mapping):
                    validate_arguments(item, additional, f"{location}.{key}")
            else:
                validate_arguments(item, child, f"{location}.{key}")
    if kind in {"string", "array"}:
        low, high = ("minLength", "maxLength") if kind == "string" else ("minItems", "maxItems")
        if low in schema and len(value) < schema[low]:
            raise ArgumentError(f"{location} is shorter than allowed")
        if high in schema and len(value) > schema[high]:
            raise ArgumentError(f"{location} is longer than allowed")
        if kind == "array" and "items" in schema:
            for index, item in enumerate(value):
                validate_arguments(item, schema["items"], f"{location}[{index}]")
    if kind in {"number", "integer"}:
        for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
            if key not in schema:
                continue
            bound = schema[key]
            valid = ((key == "minimum" and value >= bound) or (key == "maximum" and value <= bound)
                     or (key == "exclusiveMinimum" and value > bound)
                     or (key == "exclusiveMaximum" and value < bound))
            if not valid:
                raise ArgumentError(f"{location} is outside the allowed numeric range")
