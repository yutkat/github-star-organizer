"""Bind an AI categorization plan to its immutable prepared input."""

from __future__ import annotations

import hashlib
import json
from typing import Any

DIGEST_FIELD = "source_sha256"


def source_digest(source: dict[str, Any]) -> str:
    canonical = json.dumps(
        source, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def create_input(source: dict[str, Any]) -> dict[str, Any]:
    return {**source, DIGEST_FIELD: source_digest(source)}


def verified_source(
    prepared_input: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    input_digest = prepared_input.get(DIGEST_FIELD)
    plan_digest = plan.get(DIGEST_FIELD)
    source = {
        key: value for key, value in prepared_input.items() if key != DIGEST_FIELD
    }
    if not isinstance(input_digest, str) or not isinstance(plan_digest, str):
        raise TypeError("input and plan source_sha256 values are required")
    if source_digest(source) != input_digest:
        raise ValueError("categorization input was modified after preparation")
    if plan_digest != input_digest:
        raise ValueError("plan does not match the categorization input")
    return source
