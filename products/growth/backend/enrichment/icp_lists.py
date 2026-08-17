"""Loader for the RevOps-curated ICP scoring lists (IcpScoringConfig rows).

Turns the active DB row into the frozen CuratedLists the scorer consumes, normalized once
at load. Cached with a short TTL so the signup path doesn't re-read the row per score,
while a newly-activated row still takes effect within a minute, without a deploy.

No active row means V0.5 scoring is skipped (the caller degrades to writing firmographics
without a score, and the fetch archive backstops a later recompute) — never a crash and
never a silently empty-list score, which would floor Capital/AIPilled/SoftwareRelevance
for every org and look like a real evaluation.
"""

import time
import threading
from typing import Any, Optional

from products.growth.backend.enrichment.score_v05 import CuratedLists, norm
from products.growth.backend.models import IcpScoringConfig

# Buckets a tag row's recommendation may name; anything else (e.g. "ignore") is skipped.
TAG_BUCKETS = frozenset({"dq", "capital_quality", "software_positive", "software_negative", "ai_positive"})

_CACHE_TTL_SECONDS = 60

_cache_lock = threading.Lock()
_cached: Optional[tuple[float, Optional[CuratedLists]]] = None


def build_curated_lists(config: IcpScoringConfig) -> CuratedLists:
    """Parse one config row's JSON into pre-normalized frozensets.

    Malformed rows are skipped rather than raising: a partially-bad sheet export should
    degrade to a slightly smaller list, not take down scoring.
    """
    buckets: dict[str, set[str]] = {bucket: set() for bucket in TAG_BUCKETS}
    for row in config.tags if isinstance(config.tags, list) else []:
        if not isinstance(row, dict):
            continue
        tag = norm(row.get("tag"))
        if not tag:
            continue
        for bucket in str(row.get("recommendation") or "").split("+"):
            if bucket in buckets:
                buckets[bucket].add(tag)

    investors: set[str] = set()
    for row in config.quality_investors if isinstance(config.quality_investors, list) else []:
        if not isinstance(row, dict):
            continue
        name = norm(row.get("investor"))
        if name:
            investors.add(name)
        aliases = row.get("aliases")
        for alias in aliases if isinstance(aliases, list) else []:
            alias = norm(alias if isinstance(alias, str) else None)
            if alias:
                investors.add(alias)

    return CuratedLists(
        version=config.version,
        capital_quality=frozenset(buckets["capital_quality"]),
        ai_positive=frozenset(buckets["ai_positive"]),
        software_positive=frozenset(buckets["software_positive"]),
        software_negative=frozenset(buckets["software_negative"]),
        dq=frozenset(buckets["dq"]),
        quality_investors=frozenset(investors),
    )


def load_active_lists() -> Optional[CuratedLists]:
    """The active config row as CuratedLists, or None when no row is active."""
    global _cached
    now = time.monotonic()
    with _cache_lock:
        if _cached is not None and now - _cached[0] < _CACHE_TTL_SECONDS:
            return _cached[1]

    config = IcpScoringConfig.objects.filter(is_active=True).first()
    lists = build_curated_lists(config) if config is not None else None

    with _cache_lock:
        _cached = (now, lists)
    return lists


def clear_lists_cache() -> None:
    """Test hook; also useful right after activating a new row from a shell."""
    global _cached
    with _cache_lock:
        _cached = None


def parse_tags_csv_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize RevOps' tag-sheet export rows into the stored shape.

    Applies the one standing import-time decision (Mine, 2026-08-13): AI Grant batch tags
    count as BOTH quality capital and AI signal, whatever single bucket the sheet assigned —
    stored rows then carry the effective assignment visibly instead of hiding it in loader
    code.
    """
    stored = []
    for row in rows:
        tag = (row.get("tag") or "").strip()
        if not tag:
            continue
        recommendation = (row.get("recommendation") or "").strip()
        if norm(tag).startswith("ai grant batch"):
            effective = {bucket for bucket in recommendation.split("+") if bucket in TAG_BUCKETS}
            effective.update({"capital_quality", "ai_positive"})
            recommendation = "+".join(sorted(effective))
        stored.append(
            {
                "tag": tag,
                "type": (row.get("type") or "").strip(),
                "recommendation": recommendation,
                "reason": (row.get("reason") or "").strip(),
                "note": (row.get("note") or "").strip(),
            }
        )
    return stored


def parse_investors_csv_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize RevOps' investor-sheet export rows ("aliases" pipe-separated) into the stored shape."""
    stored = []
    for row in rows:
        investor = (row.get("investor") or "").strip()
        if not investor:
            continue
        aliases = [alias.strip() for alias in (row.get("aliases") or "").split("|") if alias.strip()]
        stored.append({"investor": investor, "aliases": aliases, "notes": (row.get("notes") or "").strip()})
    return stored
