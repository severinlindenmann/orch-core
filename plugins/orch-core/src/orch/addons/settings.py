"""Addon settings forms (spec A1 §5.6): flat fields text|select|bool|map, saved per workspace by a human POST."""
from __future__ import annotations

from collections.abc import Mapping

MAX_TEXT = 500
MAX_MAP = 50


def form_value(field, saved: dict):
    value = saved.get(field.key, field.default)
    if field.type == "map":
        return "\n".join(f"{k} = {v}" for k, v in value.items()) if isinstance(value, dict) else ""
    if field.type == "bool":
        return value is True
    return "" if value is None else str(value)


def parse_settings(manifest, form: Mapping) -> tuple[dict, list[str]]:
    values: dict = {}
    errors: list[str] = []
    for f in manifest.settings_schema:
        raw = form.get(f.key)
        if f.type == "bool":
            values[f.key] = raw in ("1", "on", "true")
            continue
        raw = "" if raw is None else str(raw)
        if f.type == "text":
            if "\n" in raw or "\r" in raw or len(raw) > MAX_TEXT:
                errors.append(f"{f.label}: one line, at most {MAX_TEXT} characters")
                continue
            values[f.key] = raw.strip()
        elif f.type == "select":
            if raw not in f.options:
                errors.append(f"{f.label}: pick one of {', '.join(f.options)}")
                continue
            values[f.key] = raw
        elif f.type == "map":
            out: dict = {}
            for n, line in enumerate(raw.splitlines(), 1):
                if not line.strip():
                    continue
                key, sep, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if not sep or not key or len(key) > 100 or len(val) > MAX_TEXT:
                    errors.append(f"{f.label} line {n}: use 'key = value'")
                    break
                out[key] = val
            if len(out) > MAX_MAP:
                errors.append(f"{f.label}: at most {MAX_MAP} entries")
            values[f.key] = out
    return values, errors
