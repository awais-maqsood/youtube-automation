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


# ── Named-entity hard gate (distribution anti-template) ───────────────────────
# YouTube suppresses clusters of generic category topics ("streaming platform new
# release"). Require at least one concrete proper noun: person, org, product,
# event, or work of art.

ALLOWED_NER_LABELS = frozenset(
    {"PERSON", "ORG", "PRODUCT", "EVENT", "WORK_OF_ART", "GPE", "FAC"}
)

# Case-insensitive lexicon for brands/products/people spaCy often misses on
# lowercase Trends strings. Score = NER_LEXICON_SCORE when matched.
_NAMED_ENTITY_LEXICON = frozenset(
    {
        # Tech / products
        "apple", "iphone", "ipad", "macbook", "airpods", "vision pro",
        "google", "pixel", "gemini", "android", "youtube",
        "microsoft", "windows", "copilot", "xbox", "surface",
        "amazon", "alexa", "kindle", "aws",
        "meta", "facebook", "instagram", "whatsapp", "threads",
        "openai", "chatgpt", "gpt", "claude", "anthropic",
        "nvidia", "geforce", "rtx", "blackwell",
        "samsung", "galaxy", "sony", "xperia", "playstation", "ps5",
        "nintendo", "switch", "tesla", "spacex", "starlink",
        "netflix", "disney", "disney+", "hulu", "hbo", "max", "spotify",
        "tiktok", "snapchat", "reddit", "twitter", "x.com",
        "costco", "walmart", "target", "starbucks", "mcdonald",
        "uber", "lyft", "airbnb", "stripe", "shopify", "notion",
        "adobe", "photoshop", "figma", "slack", "zoom",
        "gta", "gta 6", "grand theft auto", "rockstar",
        # People / orgs often in trends
        "trump", "biden", "harris", "musk", "elon", "zuckerberg",
        "taylor swift", "beyonce", "rihanna", "drake", "kanye",
        "fifa", "uefa", "nba", "nfl", "mlb", "nhl", "ufc", "wwe",
        "olympics", "world cup", "super bowl", "oscars", "grammys",
        "nasa", "fcc", "sec", "federal reserve", "fed",
    }
)

# Patterns that imply a concrete product/release/event even without spaCy.
_NAMED_ENTITY_PATTERNS = (
    re.compile(r"\biphone\s*\d+", re.I),
    re.compile(r"\bpixel\s*\d+", re.I),
    re.compile(r"\bgalaxy\s*[sz]?\s*\d+", re.I),
    re.compile(r"\bxperia\s*\d+", re.I),
    re.compile(r"\bps\s*[45]\b", re.I),
    re.compile(r"\bgpt[\s-]?\d+", re.I),
    re.compile(r"\brtx\s*\d+", re.I),
    re.compile(r"\bgta\s*\d+", re.I),
    re.compile(r"\bworld\s+cup\s*\d{4}\b", re.I),
    re.compile(r"\bsuper\s+bowl\b", re.I),
    # "France vs Spain", "Lakers vs Celtics"
    re.compile(
        r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)\s+vs\.?\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)\b"
    ),
)

_GENERIC_TOPIC_PATTERNS = (
    re.compile(r"\b(going viral|public reaction|new release|feature rollout)\b", re.I),
    re.compile(r"\b(streaming platform|internet debate|social media challenge)\b", re.I),
    re.compile(r"\b(breaking entertainment headline|celebrity interview controversy)\b", re.I),
    re.compile(r"\b(policy change public reaction|app feature rollout)\b", re.I),
    re.compile(r"\b(new smartphone launch reaction|viral social media)\b", re.I),
)

NER_LEXICON_SCORE = 0.9
NER_PATTERN_SCORE = 0.85
NER_SPACY_SCORE = 1.0
_SPACY_NLP = None
_SPACY_LOAD_ATTEMPTED = False


def _ner_min_confidence() -> float:
    raw = (
        os.environ.get("NER_MIN_CONFIDENCE", "").strip()
        or os.environ.get("NAMED_ENTITY_MIN_CONFIDENCE", "").strip()
        or "0.7"
    )
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.7


def _get_spacy_nlp():
    """Lazy-load spaCy English model; returns None if unavailable."""
    global _SPACY_NLP, _SPACY_LOAD_ATTEMPTED
    if _SPACY_LOAD_ATTEMPTED:
        return _SPACY_NLP
    _SPACY_LOAD_ATTEMPTED = True
    try:
        import spacy  # type: ignore

        model = os.environ.get("SPACY_MODEL", "en_core_web_sm").strip() or "en_core_web_sm"
        try:
            _SPACY_NLP = spacy.load(model)
        except OSError:
            # Model not downloaded — try blank + disable rather than fail open.
            print(f"  [WARN] spaCy model {model!r} not installed; using lexicon/pattern NER only")
            _SPACY_NLP = None
    except ImportError:
        print("  [WARN] spaCy not installed; using lexicon/pattern NER only")
        _SPACY_NLP = None
    return _SPACY_NLP


def _title_case_for_ner(text: str) -> str:
    """Trends RSS is often lowercase; capitalization helps spaCy NER."""
    words = text.split()
    out: list[str] = []
    small = {"a", "an", "the", "of", "and", "or", "vs", "vs.", "to", "in", "on", "for"}
    for i, w in enumerate(words):
        low = w.lower()
        if i > 0 and low in small:
            out.append(low)
        else:
            out.append(w[:1].upper() + w[1:] if w else w)
    return " ".join(out)


def _lexicon_hits(text: str) -> list[dict[str, Any]]:
    low = f" {text.lower()} "
    product_terms = {
        "iphone", "ipad", "chatgpt", "pixel", "galaxy", "xperia", "ps5",
        "macbook", "airpods", "vision pro", "geforce", "rtx", "kindle",
    }
    hits: list[dict[str, Any]] = []
    # Longer phrases first so "taylor swift" / "world cup" win over singles.
    for term in sorted(_NAMED_ENTITY_LEXICON, key=len, reverse=True):
        if f" {term} " not in low:
            continue
        hits.append(
            {
                "text": term,
                "label": "PRODUCT" if term in product_terms else "ORG",
                "score": NER_LEXICON_SCORE,
                "source": "lexicon",
            }
        )
        break  # one strong lexicon hit is enough for scoring
    return hits


def _pattern_hits(text: str) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    titled = _title_case_for_ner(text)
    for pat in _NAMED_ENTITY_PATTERNS:
        m = pat.search(text) or pat.search(titled)
        if m:
            hits.append(
                {
                    "text": m.group(0),
                    "label": "EVENT" if "vs" in m.group(0).lower() or "cup" in m.group(0).lower() or "bowl" in m.group(0).lower() else "PRODUCT",
                    "score": NER_PATTERN_SCORE,
                    "source": "pattern",
                }
            )
    return hits


def _spacy_hits(text: str) -> list[dict[str, Any]]:
    nlp = _get_spacy_nlp()
    if nlp is None:
        return []
    doc = nlp(_title_case_for_ner(text))
    hits: list[dict[str, Any]] = []
    for ent in doc.ents:
        label = ent.label_
        if label not in ALLOWED_NER_LABELS:
            continue
        # spaCy small model has no per-ent probability; treat allowed labels as full confidence.
        hits.append(
            {
                "text": ent.text,
                "label": label,
                "score": NER_SPACY_SCORE,
                "source": "spacy",
            }
        )
    return hits


def extract_named_entities(value: Any) -> list[dict[str, Any]]:
    """Extract PERSON/ORG/PRODUCT/EVENT/WORK_OF_ART (etc.) from a topic string."""
    text = normalize_entity(value)
    if not text:
        return []
    hits = _spacy_hits(text) + _lexicon_hits(text) + _pattern_hits(text)
    # Dedupe by lowercase text+label
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for h in hits:
        key = f"{h['label']}:{str(h['text']).lower()}"
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def named_entity_score(value: Any) -> tuple[float, list[dict[str, Any]]]:
    """Return (best_confidence, entities). 0.0 if none."""
    ents = extract_named_entities(value)
    if not ents:
        return 0.0, []
    return max(float(e["score"]) for e in ents), ents


def is_generic_category_topic(value: Any) -> bool:
    """True for known non-entity template seeds that suppress distribution."""
    text = normalize_entity(value)
    if not text:
        return True
    return any(p.search(text) for p in _GENERIC_TOPIC_PATTERNS)


def passes_named_entity_gate(
    value: Any,
    *,
    min_confidence: float | None = None,
) -> bool:
    """Hard gate: topic must resolve to a named proper noun above threshold."""
    text = normalize_entity(value)
    if not is_valid_entity(text, min_length=4):
        return False
    if is_generic_category_topic(text):
        return False
    thr = _ner_min_confidence() if min_confidence is None else min_confidence
    score, _ents = named_entity_score(text)
    return score >= thr


def require_named_entity(
    value: Any,
    *,
    source: str,
    raw_upstream: Any = None,
    min_confidence: float | None = None,
) -> str:
    """Raise EntityValidationError unless topic clears the named-entity hard gate."""
    text = normalize_entity(value)
    thr = _ner_min_confidence() if min_confidence is None else min_confidence

    if not is_valid_entity(text, min_length=4):
        log_entity_rejection(source, value, raw_upstream, reason="invalid_entity")
        raise EntityValidationError(
            f"Invalid entity from {source}: {value!r}",
            source=source,
            value=value,
            raw_upstream=raw_upstream,
        )

    if is_generic_category_topic(text):
        log_entity_rejection(
            source,
            value,
            {"raw_upstream": raw_upstream, "threshold": thr},
            reason="generic_category_topic",
        )
        raise EntityValidationError(
            f"Generic category topic rejected at {source}: {value!r}",
            source=source,
            value=value,
            raw_upstream=raw_upstream,
        )

    score, ents = named_entity_score(text)
    if score < thr:
        log_entity_rejection(
            source,
            value,
            {
                "raw_upstream": raw_upstream,
                "score": score,
                "threshold": thr,
                "entities": ents,
            },
            reason="no_named_entity",
        )
        raise EntityValidationError(
            f"No named entity (PERSON/ORG/PRODUCT/EVENT/WORK_OF_ART) above "
            f"confidence {thr} at {source}: {value!r} (score={score})",
            source=source,
            value=value,
            raw_upstream=raw_upstream,
        )
    return text
