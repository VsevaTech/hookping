"""Flatten a JSON payload into dotted paths for the template editor."""

from __future__ import annotations

from typing import Any

MAX_PATHS = 200
MAX_DEPTH = 6


def extract_paths(payload: Any) -> list[str]:
    """Return dotted paths such as ``customer.name`` or ``items[0].sku``.

    Only paths that can be used directly in a template are returned;
    non-identifier keys are skipped at the top level because they cannot
    be referenced as bare variables.
    """
    paths: list[str] = []

    def walk(value: Any, prefix: str, depth: int) -> None:
        if len(paths) >= MAX_PATHS or depth > MAX_DEPTH:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    continue
                if prefix:
                    segment = f"{prefix}.{key}" if key.isidentifier() else f'{prefix}["{key}"]'
                else:
                    if not key.isidentifier():
                        continue
                    segment = key
                walk(child, segment, depth + 1)
        elif isinstance(value, list):
            if value:
                walk(value[0], f"{prefix}[0]", depth + 1)
        elif prefix:
            paths.append(prefix)

    # A top-level array has no bare-variable names; expose it through ``payload``.
    walk(payload, "payload" if isinstance(payload, list) else "", 0)
    return paths
