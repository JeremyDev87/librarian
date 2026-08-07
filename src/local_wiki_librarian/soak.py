"""Pure helpers for fixed-rate Wiki Librarian soak scheduling."""
from __future__ import annotations

import math


def validate_query_contract(query: dict) -> None:
    has_paths = bool(query.get("expected_paths"))
    no_answer = query.get("expected_no_answer") is True
    if has_paths == no_answer:
        raise ValueError("query must declare expected_paths xor expected_no_answer")


def validate_query_result(query: dict, paths: list[str]) -> None:
    validate_query_contract(query)
    if query.get("expected_no_answer") is True:
        if paths:
            raise ValueError("no-answer query returned retrieval candidates")
        return
    expected = {str(value) for value in query.get("expected_paths", [])}
    if not expected.intersection(paths):
        raise ValueError("query missed expected authority path")


def fixed_rate_offsets(duration_seconds: float, interval_seconds: float) -> list[float]:
    """Return start-anchored deadlines, including t=0 and the exact end."""
    if duration_seconds < 0:
        raise ValueError("duration_seconds must be non-negative")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    count = math.floor(duration_seconds / interval_seconds) + 1
    offsets = [index * interval_seconds for index in range(count)]
    if not offsets or offsets[-1] < duration_seconds:
        offsets.append(duration_seconds)
    return offsets
