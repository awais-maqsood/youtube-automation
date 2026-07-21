#!/usr/bin/env python3
"""
Shared validation for trending-topic entities and pre-publish title/description guards.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any


INVALID_LITERALS = frozenset(
    {"none", "null", "undefined", "n/a", "na", "unknown", "...", "nan", "nil"}
)

PLACEHOLDER_PATTERNS = (
    re.compile(r"^placeholder$", re.I),
    re.compile(r"^todo$", re.I),
    re.compile(r"^tbd$", re.I),
    re.compile(r"^example$", re.I),
    re.compile(r"^test$", re.I),
    re.compile(r"^xxx+$", re.I),
    re.compile(r"^unknown topic$", re.I),
    re.compile(r"^this trend$", re.I),
)

# Word-boundary scan for leaked Python/JSON nulls in rendered publish strings.
INVALID_PUBLISH_SUBSTRINGS = re.compile(
    r"\b(none|null|undefined)\b",
    re.I,
)


class EntityValidationError(ValueError):
    """Raised when a trending-topic entity or publish string fails validation."""

    def __init__(
        self,
        message: str,
        *,
        source: str = "",
        value: Any = None,
        raw_upstream: Any = None,
    ) -> None:
        super().__init__(message)
        self.source = source
        self.value = value
        self.raw_upstream = raw_upstream


def normalize_entity(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def is_valid_entity(value: Any, *, min_length: int = 2) -> bool:
    text = normalize_entity(value)
    if len(text) < min_length:
        return False
    if text.lower() in INVALID_LITERALS:
        return False
    if not re.search(r"[A-Za-z0-9]", text):
        return False
    if any(pattern.match(text) for pattern in PLACEHOLDER_PATTERNS):
        return False
    return True


def require_valid_entity(
    value: Any,
    *,
    source: str,
    raw_upstream: Any = None,
) -> str:
    text = normalize_entity(value)
    if not is_valid_entity(text):
        raise EntityValidationError(
            f"Invalid entity from {source}: {value!r}",
            source=source,
            value=value,
            raw_upstream=raw_upstream,
        )
    return text


def log_entity_rejection(
    source: str,
    value: Any,
    raw_upstream: Any = None,
    *,
    reason: str = "invalid_entity",
) -> None:
    payload = {
        "reason": reason,
        "source": source,
        "value": value,
        "raw_upstream": raw_upstream,
    }
    print(f"  [REJECT] Entity validation failed: {json.dumps(payload, ensure_ascii=False, default=str)}")

    webhook = (
        os.environ.get("MANUAL_REVIEW_WEBHOOK", "").strip()
        or os.environ.get("TOPIC_REVIEW_WEBHOOK", "").strip()
    )
    if not webhook:
        return
    try:
        body = json.dumps(
            {
                "event": "topic_validation_rejected",
                **payload,
            },
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        req = urllib.request.Request(
            webhook,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"  [WARN] Manual review webhook failed: {exc}")


def contains_invalid_publish_text(text: str) -> bool:
    return bool(INVALID_PUBLISH_SUBSTRINGS.search(normalize_entity(text)))


def assert_publishable_title(title: str, *, source: str = "pre_publish") -> None:
    if not normalize_entity(title):
        raise EntityValidationError(
            f"Empty title blocked at {source}",
            source=source,
            value=title,
        )
    if contains_invalid_publish_text(title):
        raise EntityValidationError(
            f"Blocked title containing placeholder/null text at {source}: {title!r}",
            source=source,
            value=title,
        )


def assert_publishable_metadata(
    title: str,
    description: str,
    *,
    source: str = "pre_publish",
) -> None:
    assert_publishable_title(title, source=source)
    if not normalize_entity(description):
        raise EntityValidationError(
            f"Empty description blocked at {source}",
            source=source,
            value=description,
        )
    if contains_invalid_publish_text(description):
        raise EntityValidationError(
            f"Blocked description containing placeholder/null text at {source}: {description!r}",
            source=source,
            value=description,
        )
