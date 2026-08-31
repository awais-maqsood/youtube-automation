#!/usr/bin/env python3
"""
Cross-run similarity guard for Shorts metadata.

Scores new title / opener / CTA text against a rolling published catalog
(default: last 300 entries). Hard-rejects candidates above per-channel
thresholds so near-duplicate rhetorical structures cannot ship.

Embedding backends:
  1. Local character/word n-gram TF-IDF cosine (always on, no extra deps)
  2. Optional OpenAI text-embedding-3-small when SIMILARITY_USE_OPENAI=1
     and OPENAI_API_KEY is set (blended with local score via max())
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "data" / "publish_catalog.json"
TITLE_HISTORY_PATH = ROOT / "output" / "title_history.json"
CATALOG_MAX_DEFAULT = 300

# Soft structural stop-words blanked when building skeletons.
_SKELETON_STOP = frozenset(
    {
        "a", "an", "the", "of", "and", "or", "to", "in", "on", "for", "with",
        "is", "are", "was", "be", "this", "that", "it", "just", "now", "vs",
    }
)


class SimilarityRejectError(ValueError):
    """Raised when a candidate cannot be diversified below the hard threshold."""

    def __init__(self, message: str, *, scores: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.scores = scores or {}


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _catalog_max() -> int:
    try:
        return max(50, int(_env("SIMILARITY_CATALOG_MAX", str(CATALOG_MAX_DEFAULT)) or CATALOG_MAX_DEFAULT))
    except (TypeError, ValueError):
        return CATALOG_MAX_DEFAULT


def _threshold(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def title_threshold() -> float:
    return _threshold("SIMILARITY_TITLE_MAX", 0.72)


def opener_threshold() -> float:
    return _threshold("SIMILARITY_OPENER_MAX", 0.75)


def cta_threshold() -> float:
    return _threshold("SIMILARITY_CTA_MAX", 0.85)


def _compact(text: Any) -> str:
    return " ".join(str(text or "").split()).strip()


def normalize_for_embed(text: Any) -> str:
    t = _compact(text).lower()
    t = re.sub(r"[^\w\s'?&+-]", " ", t)
    return " ".join(t.split())


def structural_skeleton(text: Any, entity: str = "") -> str:
    """Blank named entities so structure (not topic) dominates.

    Only the entity phrase is replaced — other words stay so
    '{ENT} under pressure' does not falsely match 'most takes on {ENT}'.
    """
    t = normalize_for_embed(text)
    e = normalize_for_embed(entity)
    if e and e in t:
        t = t.replace(e, " {ENT} ")
    else:
        # Best-effort: blank likely product/model tokens (iphone 18, gpt-5, etc.)
        t = re.sub(
            r"\b([a-z]+(?:\s+[a-z0-9]+){0,3}\s+\d+[a-z0-9]*)\b",
            " {ENT} ",
            t,
            count=1,
        )
    return " ".join(t.split())


def _char_ngrams(text: str, n: int = 3) -> list[str]:
    s = f"  {text}  "
    if len(s) < n:
        return [s]
    return [s[i : i + n] for i in range(len(s) - n + 1)]


def _tf_vector(text: str) -> dict[str, float]:
    """Sparse TF vector over word tokens + char trigrams."""
    norm = normalize_for_embed(text)
    if not norm:
        return {}
    counts: dict[str, float] = {}
    for tok in norm.split():
        counts[f"w:{tok}"] = counts.get(f"w:{tok}", 0.0) + 1.0
    for ng in _char_ngrams(norm, 3):
        counts[f"c:{ng}"] = counts.get(f"c:{ng}", 0.0) + 1.0
    skel = structural_skeleton(norm)
    for tok in skel.split():
        counts[f"s:{tok}"] = counts.get(f"s:{tok}", 0.0) + 1.5  # structure weighted up
    # L2 normalize
    norm_sq = sum(v * v for v in counts.values())
    if norm_sq <= 0:
        return counts
    inv = 1.0 / math.sqrt(norm_sq)
    return {k: v * inv for k, v in counts.items()}


def cosine_sparse(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return float(sum(v * b.get(k, 0.0) for k, v in a.items()))


def local_similarity(a: str, b: str) -> float:
    """0..1 similarity blending lexical n-grams and structural skeleton overlap."""
    a_n, b_n = normalize_for_embed(a), normalize_for_embed(b)
    if not a_n or not b_n:
        return 0.0
    if a_n == b_n:
        return 1.0
    lex = cosine_sparse(_tf_vector(a_n), _tf_vector(b_n))
    sa, sb = set(structural_skeleton(a_n).split()), set(structural_skeleton(b_n).split())
    if not sa or not sb:
        struct = 0.0
    else:
        struct = len(sa & sb) / float(len(sa | sb))
    # Hard structural twins (same skeleton, different entity) still score high.
    return round(max(lex, 0.55 * lex + 0.45 * struct), 4)


def _openai_embed(texts: list[str]) -> list[list[float]] | None:
    if _env("SIMILARITY_USE_OPENAI", "0") not in {"1", "true", "yes", "on"}:
        return None
    api_key = _env("OPENAI_API_KEY", "")
    if not api_key or not texts:
        return None
    model = _env("SIMILARITY_OPENAI_MODEL", "text-embedding-3-small") or "text-embedding-3-small"
    payload = json.dumps({"model": model, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        items = sorted(data.get("data", []), key=lambda x: int(x.get("index", 0)))
        return [list(it.get("embedding") or []) for it in items]
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"  [WARN] OpenAI embedding failed; using local similarity only: {exc}")
        return None


def _cosine_dense(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return float(dot / (na * nb))


def similarity(a: str, b: str, *, a_emb: list[float] | None = None, b_emb: list[float] | None = None) -> float:
    local = local_similarity(a, b)
    if a_emb is not None and b_emb is not None:
        return round(max(local, _cosine_dense(a_emb, b_emb)), 4)
    return local


def _load_json_list(path: Path, key: str | None = None) -> list[dict]:
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and key:
            rows = data.get(key, [])
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        return [r for r in rows if isinstance(r, dict)]
    except Exception as exc:
        print(f"  [WARN] Failed reading {path}: {exc}")
        return []


def load_catalog(limit: int | None = None) -> list[dict]:
    """Load rolling catalog (durable data/ + optional output title history)."""
    max_n = limit if limit is not None else _catalog_max()
    catalog = _load_json_list(CATALOG_PATH, key="entries")
    # Supplement with local title history (ephemeral CI / local runs).
    hist = _load_json_list(TITLE_HISTORY_PATH, key="titles")
    seen = {
        (
            _compact(e.get("title")).lower(),
            _compact(e.get("opener") or e.get("hook")).lower(),
            _compact(e.get("cta")).lower(),
        )
        for e in catalog
    }
    for row in hist:
        title = _compact(row.get("title"))
        opener = _compact(row.get("hook") or row.get("opener"))
        cta = _compact(row.get("cta"))
        key = (title.lower(), opener.lower(), cta.lower())
        if not title or key in seen:
            continue
        seen.add(key)
        catalog.append(
            {
                "title": title,
                "opener": opener,
                "cta": cta,
                "trend": _compact(row.get("trend")),
                "utc": row.get("utc") or "",
                "source": "title_history",
            }
        )
    return catalog[-max_n:]


def is_postiz_duplicate(
    *,
    youtube_id: str = "",
    app_id: str = "",
    catalog: list[dict[str, Any]] | None = None,
) -> bool:
    """True when this YouTube video or app was already posted to Postiz."""
    if catalog is None:
        catalog = load_catalog()
    yt = _compact(youtube_id)
    aid = _compact(app_id)
    for entry in catalog:
        source = str(entry.get("source") or "").strip().lower()
        if not source.startswith("upload.postiz"):
            continue
        if yt and _compact(entry.get("youtube_id")) == yt:
            return True
        if aid:
            from app_safety import catalog_app_id

            if catalog_app_id(entry) == aid:
                return True
    return False


def save_catalog(entries: list[dict]) -> None:
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        "entries": entries[-_catalog_max():],
    }
    CATALOG_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def extract_cta_from_description(description: str) -> str:
    """Best-effort CTA line from a multi-line YouTube description."""
    lines = [_compact(x) for x in str(description or "").splitlines() if _compact(x)]
    # Prefer comment/reply invite lines; skip hashtag rows.
    for line in lines:
        low = line.lower()
        if line.startswith("#"):
            continue
        if any(k in low for k in ("comment", "reply", "below", "tell me", "vote", "verdict", "take")):
            return line
    # Fallback: last non-hashtag prose line.
    for line in reversed(lines):
        if not line.startswith("#") and len(line) > 12:
            return line
    return ""


def extract_cta_from_content(content: dict) -> str:
    closes = content.get("close_lines") or []
    for line in reversed(list(closes)):
        text = _compact(line)
        low = text.lower()
        if any(k in low for k in ("comment", "reply", "below", "tell me", "vote", "verdict", "take")):
            return text
    if closes:
        return _compact(closes[-1])
    return ""


def structural_similarity(a: str, b: str, *, entity_a: str = "", entity_b: str = "") -> float:
    """Compare rhetorical skeletons after blanking entities (ignores shared product names)."""
    sa = structural_skeleton(a, entity_a).split()
    sb = structural_skeleton(b, entity_b).split()
    if not sa or not sb:
        return 0.0
    if sa == sb:
        return 1.0

    set_a, set_b = set(sa), set(sb)
    jaccard = len(set_a & set_b) / float(len(set_a | set_b))

    def _bigrams(toks: list[str]) -> set[tuple[str, str]]:
        if len(toks) < 2:
            return set()
        return set(zip(toks, toks[1:]))

    ba, bb = _bigrams(sa), _bigrams(sb)
    if not ba and not bb:
        bigram_j = 0.0
    elif not ba or not bb:
        bigram_j = 0.0
    else:
        bigram_j = len(ba & bb) / float(len(ba | bb))

    # Prefer ordered pattern overlap so "{ENT} {X} {X}" ≠ "most {X} {ENT} {X}".
    return round(0.35 * jaccard + 0.65 * bigram_j, 4)


def channel_similarity(
    a: str,
    b: str,
    *,
    entity_a: str = "",
    entity_b: str = "",
    mode: str = "structural",
) -> float:
    """Similarity for one metadata channel.

    structural — blank entities and compare rhetorical skeleton (title/opener).
    lexical — full-text n-gram cosine (CTA phrasing).
    """
    if mode == "lexical":
        return local_similarity(a, b)
    return structural_similarity(a, b, entity_a=entity_a, entity_b=entity_b)


def score_candidate_against_catalog(
    *,
    title: str,
    opener: str,
    cta: str,
    catalog: list[dict] | None = None,
    entity: str = "",
    exclude_titles: set[str] | None = None,
) -> dict[str, Any]:
    """Return max similarity per channel + nearest neighbors."""
    catalog = catalog if catalog is not None else load_catalog()
    title, opener, cta = _compact(title), _compact(opener), _compact(cta)
    exclude = {t.lower() for t in (exclude_titles or set()) if str(t).strip()}
    if exclude:
        catalog = [
            e for e in catalog
            if _compact(e.get("title")).lower() not in exclude
        ]

    channels = {
        "title": (title, "title", title_threshold(), "structural"),
        "opener": (opener, "opener", opener_threshold(), "structural"),
        "cta": (cta, "cta", cta_threshold(), "lexical"),
    }

    use_openai = _env("SIMILARITY_USE_OPENAI", "0") in {"1", "true", "yes", "on"}
    result: dict[str, Any] = {
        "title": {"score": 0.0, "threshold": title_threshold(), "match": None, "reject": False},
        "opener": {"score": 0.0, "threshold": opener_threshold(), "match": None, "reject": False},
        "cta": {"score": 0.0, "threshold": cta_threshold(), "match": None, "reject": False},
        "reject": False,
        "catalog_size": len(catalog),
    }
    if not catalog:
        return result

    cand_embs: dict[str, list[float] | None] = {"title": None, "opener": None, "cta": None}
    if use_openai:
        # Embed structural skeletons for title/opener so OpenAI also judges structure.
        texts = [
            structural_skeleton(title, entity) if title else "",
            structural_skeleton(opener, entity) if opener else "",
            cta,
        ]
        embs = _openai_embed([t for t in texts if t])
        if embs is not None:
            idx = 0
            for key, raw in (("title", texts[0]), ("opener", texts[1]), ("cta", texts[2])):
                if raw:
                    cand_embs[key] = embs[idx]
                    idx += 1

    for channel, (cand, field, thr, mode) in channels.items():
        if not cand:
            continue
        best = 0.0
        best_row = None
        scored: list[tuple[float, dict]] = []
        for row in catalog:
            other = _compact(row.get(field) or (row.get("hook") if field == "opener" else "") or "")
            if not other:
                continue
            row_entity = _compact(row.get("trend") or "")
            s = channel_similarity(
                cand,
                other,
                entity_a=entity,
                entity_b=row_entity,
                mode=mode,
            )
            scored.append((s, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        best = scored[0][0] if scored else 0.0
        best_row = scored[0][1] if scored else None

        if use_openai and cand_embs.get(channel) and scored:
            top = scored[:8]
            others = []
            for _, r in top:
                other = _compact(r.get(field) or (r.get("hook") if field == "opener" else "") or "")
                if mode == "structural":
                    other = structural_skeleton(other, _compact(r.get("trend") or ""))
                others.append(other)
            other_embs = _openai_embed([o for o in others if o])
            if other_embs is not None:
                emb_i = 0
                for s_local, row in top:
                    other = others[emb_i] if emb_i < len(others) else ""
                    if not other:
                        continue
                    s = max(s_local, _cosine_dense(cand_embs[channel], other_embs[emb_i]))
                    emb_i += 1
                    if s > best:
                        best = s
                        best_row = row

        reject = best >= thr
        result[channel] = {
            "score": round(best, 4),
            "threshold": thr,
            "match": _compact((best_row or {}).get(field) or (best_row or {}).get("title")) if best_row else None,
            "reject": reject,
        }
        if reject:
            result["reject"] = True
    return result


def assert_below_similarity_threshold(
    *,
    title: str,
    opener: str,
    cta: str,
    catalog: list[dict] | None = None,
    entity: str = "",
    source: str = "similarity_guard",
    exclude_titles: set[str] | None = None,
) -> dict[str, Any]:
    """Fail closed if any channel exceeds its hard threshold."""
    scores = score_candidate_against_catalog(
        title=title,
        opener=opener,
        cta=cta,
        catalog=catalog,
        entity=entity,
        exclude_titles=exclude_titles,
    )
    if scores.get("reject"):
        offenders = [
            f"{ch}={scores[ch]['score']:.3f}>={scores[ch]['threshold']}"
            for ch in ("title", "opener", "cta")
            if scores.get(ch, {}).get("reject")
        ]
        print(
            f"  [REJECT] Similarity gate failed at {source}: "
            f"{', '.join(offenders)} catalog={scores.get('catalog_size')}"
        )
        raise SimilarityRejectError(
            f"Similarity reject at {source}: {', '.join(offenders)}",
            scores=scores,
        )
    return scores


def record_catalog_entry(
    *,
    title: str,
    opener: str = "",
    cta: str = "",
    trend: str = "",
    app_id: str = "",
    description: str = "",
    source: str = "generate",
    youtube_id: str = "",
) -> None:
    """Append a published/generated entry to the durable rolling catalog."""
    title = _compact(title)
    if not title:
        return
    opener = _compact(opener)
    cta = _compact(cta) or extract_cta_from_description(description)
    row: dict[str, Any] = {
        "title": title,
        "opener": opener,
        "cta": cta,
        "trend": _compact(trend),
        "utc": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    if _compact(app_id):
        row["app_id"] = _compact(app_id)
    if _compact(youtube_id):
        row["youtube_id"] = _compact(youtube_id)
    entries = load_catalog(limit=_catalog_max() * 2)
    entries.append(row)
    # Dedupe exact triples keeping latest.
    dedup: dict[tuple[str, str, str], dict] = {}
    for e in entries:
        key = (
            _compact(e.get("title")).lower(),
            _compact(e.get("opener")).lower(),
            _compact(e.get("cta")).lower(),
        )
        dedup[key] = e
    save_catalog(list(dedup.values())[-_catalog_max():])


def diversify_content_against_catalog(
    content: dict,
    *,
    regenerate_title,
    regenerate_opener,
    regenerate_cta,
    max_attempts: int = 6,
    samples_per_attempt: int = 8,
) -> tuple[dict, dict[str, Any]]:
    """Hard-reject loop: regenerate offending channels until under threshold or raise."""
    catalog = load_catalog()
    entity = _compact(content.get("trend_topic") or content.get("title") or "")
    last_scores: dict[str, Any] = {}
    attempts = max(1, int(max_attempts))
    samples = max(1, int(samples_per_attempt))

    def _best_regen(channel: str, factory, current_content: dict) -> Any:
        """Try several regenerations; keep the lowest similarity vs catalog."""
        best_val = None
        best_score = 1.0
        thr = {"title": title_threshold(), "opener": opener_threshold(), "cta": cta_threshold()}[channel]
        for _ in range(samples):
            cand = factory(entity, current_content)
            if channel == "title":
                title, opener, cta = _compact(cand), _compact(current_content.get("hook")), extract_cta_from_content(current_content)
            elif channel == "opener":
                title, opener, cta = _compact(current_content.get("title")), _compact(cand), extract_cta_from_content(current_content)
            else:
                tmp = dict(current_content)
                tmp["close_lines"] = cand
                title = _compact(current_content.get("title"))
                opener = _compact(current_content.get("hook"))
                cta = extract_cta_from_content(tmp)
            scores = score_candidate_against_catalog(
                title=title, opener=opener, cta=cta, catalog=catalog, entity=entity
            )
            sc = float(scores[channel]["score"])
            if sc < best_score:
                best_score = sc
                best_val = cand
            if sc < thr:
                break
        return best_val if best_val is not None else factory(entity, current_content)

    for attempt in range(1, attempts + 1):
        title = _compact(content.get("title"))
        opener = _compact(content.get("hook"))
        cta = extract_cta_from_content(content)
        last_scores = score_candidate_against_catalog(
            title=title, opener=opener, cta=cta, catalog=catalog, entity=entity
        )
        if not last_scores.get("reject"):
            content["_similarity"] = last_scores
            if attempt > 1:
                print(f"  Similarity guard cleared after {attempt} attempt(s)")
            return content, last_scores

        print(
            f"  [SIM] attempt {attempt}/{attempts} reject — "
            f"title={last_scores['title']['score']:.3f} "
            f"opener={last_scores['opener']['score']:.3f} "
            f"cta={last_scores['cta']['score']:.3f}"
        )
        if last_scores["title"].get("reject"):
            content["title"] = _best_regen("title", regenerate_title, content)
        if last_scores["opener"].get("reject"):
            content["hook"] = _best_regen("opener", regenerate_opener, content)
        if last_scores["cta"].get("reject"):
            content["close_lines"] = _best_regen("cta", regenerate_cta, content)

    raise SimilarityRejectError(
        f"Could not diversify below similarity thresholds after {attempts} attempts",
        scores=last_scores,
    )
