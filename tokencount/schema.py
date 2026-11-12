"""Validate records against the JSON Schema, without third-party packages.

Supports the subset of JSON Schema our schema uses: type, const, enum,
required, properties, additionalProperties, items, minItems, minimum,
maxLength and pattern.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schemas" / "task_record.v0.1.json"

_PY_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def load_schema(path: Path | None = None) -> dict:
    return json.loads(Path(path or SCHEMA_PATH).read_text(encoding="utf-8"))


def _is_type(value: object, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _PY_TYPES[name])


def validate(value: object, schema: dict, path: str = "$") -> list[str]:
    """Return a list of human-readable errors; empty means valid."""
    errors: list[str] = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, t) for t in types):
            errors.append(f"{path}: expected {' or '.join(types)}, got {type(value).__name__}")
            return errors
    if _is_type(value, "number") and "minimum" in schema and value < schema["minimum"]:
        errors.append(f"{path}: {value} is below the minimum {schema['minimum']}")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match {schema['pattern']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing field {key!r}")
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                errors += validate(item, properties[key], f"{path}.{key}")
            elif extra is False:
                errors.append(f"{path}: unexpected field {key!r}")
            elif isinstance(extra, dict):
                errors += validate(item, extra, f"{path}.{key}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} item(s)")
        if "items" in schema:
            for i, item in enumerate(value):
                errors += validate(item, schema["items"], f"{path}[{i}]")
    return errors


def validate_record(record: object, schema: dict | None = None) -> list[str]:
    return validate(record, schema or load_schema())
