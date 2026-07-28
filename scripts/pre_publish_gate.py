#!/usr/bin/env python3
"""
Pre-publish safety gate — fail closed.

Runs immediately before YouTube upload:
  1) null/placeholder metadata check
  2) named-entity hard gate (title / trend)
  3) catalog similarity hard gate (title / opener / CTA)

On failure: log + alert webhook, raise PrePublishBlocked (do not upload).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from similarity_guard import (
    SimilarityRejectError,
    assert_below_similarity_threshold,
    extract_cta_from_description,
)
from topic_validation import (
    EntityValidationError,
    assert_publishable_metadata,
    log_entity_rejection,
    normalize_entity,
    passes_named_entity_gate,
    require_named_entity,
)

ROOT = Path(__file__).resolve().parent.parent
BLOCK_LOG_PATH = ROOT / "output" / "pre_publish_blocks.json"


class PrePublishBlocked(RuntimeError):
    """Raised when the pre-publish gate refuses to upload."""

    def __init__(self, message: str, *, reason: str = "", details: dict | None = None) -> None:
        super().__init__(message)
        self.reason = reason or "pre_publish_blocked"
        self.details = details or {}


def _alert(source: str, title: Any, payload: dict, *, reason: str) -> None:
    log_entity_rejection(source, title, payload, reason=reason)


def _record_block(payload: dict) -> None:
    try:
        BLOCK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        rows: list = []
        if BLOCK_LOG_PATH.exists():
            data = json.loads(BLOCK_LOG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                rows = list(data.get("blocks", []))
            elif isinstance(data, list):
                rows = data
        rows.append(payload)
        BLOCK_LOG_PATH.write_text(
            json.dumps({"blocks": rows[-100:]}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"  [WARN] Could not write pre-publish block log: {exc}")


def run_pre_publish_gate(
    *,
    title: str,
    description: str,
    opener: str = "",
    trend: str = "",
    source: str = "pre_publish",
) -> dict[str, Any]:
    """Validate final metadata. Returns check results on success; raises on failure."""
    title = normalize_entity(title)
    description = str(description or "").strip()
    opener = normalize_entity(opener)
    trend = normalize_entity(trend)
    results: dict[str, Any] = {
        "ok": False,
        "source": source,
        "utc": datetime.now(timezone.utc).isoformat(),
        "title": title,
        "checks": {},
    }

    # 1) Null / placeholder metadata
    try:
        assert_publishable_metadata(title, description, source=f"{source}.metadata")
        results["checks"]["metadata"] = {"ok": True}
    except EntityValidationError as exc:
        details = {"error": str(exc), "description": description}
        _alert(f"{source}.metadata", title, details, reason="invalid_publish_metadata")
        _record_block({**results, "reason": "invalid_publish_metadata", "details": details})
        raise PrePublishBlocked(
            f"Pre-publish blocked (metadata): {exc}",
            reason="invalid_publish_metadata",
            details=details,
        ) from exc

    # 2) Named-entity hard gate — prefer trend seed, else title must resolve to an entity
    entity_probe = trend or title
    try:
        if trend:
            require_named_entity(trend, source=f"{source}.named_entity.trend")
            results["checks"]["named_entity"] = {"ok": True, "value": trend, "via": "trend"}
        elif passes_named_entity_gate(title):
            results["checks"]["named_entity"] = {"ok": True, "value": title, "via": "title"}
        else:
            require_named_entity(title, source=f"{source}.named_entity.title")
    except EntityValidationError as exc:
        details = {"error": str(exc), "trend": trend, "title": title}
        _alert(f"{source}.named_entity", entity_probe, details, reason="no_named_entity")
        _record_block({**results, "reason": "no_named_entity", "details": details})
        raise PrePublishBlocked(
            f"Pre-publish blocked (named entity): {exc}",
            reason="no_named_entity",
            details=details,
        ) from exc

    # 3) Similarity hard gate vs published catalog (exclude this title / self)
    cta = extract_cta_from_description(description)
    try:
        sim = assert_below_similarity_threshold(
            title=title,
            opener=opener,
            cta=cta,
            entity=trend or title,
            source=f"{source}.similarity",
            exclude_titles={title},
        )
        results["checks"]["similarity"] = {"ok": True, **sim}
    except SimilarityRejectError as exc:
        details = {"scores": exc.scores, "opener": opener, "cta": cta, "trend": trend}
        _alert(f"{source}.similarity", title, details, reason="similarity_reject")
        _record_block({**results, "reason": "similarity_reject", "details": details})
        raise PrePublishBlocked(
            f"Pre-publish blocked (similarity): {exc}",
            reason="similarity_reject",
            details=details,
        ) from exc

    results["ok"] = True
    print(
        f"  [OK] Pre-publish gate passed at {source} "
        f"(entity ok, similarity title={sim['title']['score']:.3f} "
        f"opener={sim['opener']['score']:.3f} cta={sim['cta']['score']:.3f})"
    )
    return results


def gate_kit(kit: dict, *, source: str = "pre_publish.kit") -> dict[str, Any]:
    """Convenience wrapper for kit.json-shaped payloads."""
    return run_pre_publish_gate(
        title=str(kit.get("title") or ""),
        description=str(kit.get("description") or ""),
        opener=str(kit.get("opener") or kit.get("hook") or ""),
        trend=str(kit.get("trend_topic") or kit.get("trend") or kit.get("topic") or ""),
        source=source,
    )
