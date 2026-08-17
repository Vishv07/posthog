"""Orchestration-agnostic enrichment core.

Wraps a provider lookup and the live-store write into one coroutine that any
orchestrator can await — the real-time Temporal workflow (fire-and-forget from
signup) today, a batch Dagster asset later. No orchestration concerns leak in here.
"""

import dataclasses
from typing import Any, Optional

from django.conf import settings

from asgiref.sync import sync_to_async
from posthoganalytics.client import Client

from posthog.exceptions_capture import capture_exception
from posthog.models.person.util import get_person_by_distinct_id

from products.growth.backend.enrichment.fields import EnrichmentFields
from products.growth.backend.enrichment.harmonic_adapter import normalize_graphql_company
from products.growth.backend.enrichment.icp_lists import load_active_lists
from products.growth.backend.enrichment.providers import EnrichmentProvider
from products.growth.backend.enrichment.score_v05 import IcpScoreResult, score_company
from products.growth.backend.enrichment.writer import archive_provider_fetch, write_organization_enrichment
from products.growth.backend.models import OrganizationEnrichment, OrganizationEnrichmentFetch

# Placeholder archived for a not-found when the provider hands back no response body — records
# the miss as a distinct observation, since absence at fetch time is evidence too.
_MISS_PAYLOAD = {"companyFound": False}

# How many archived fetches to walk back looking for the last matched payload on a miss.
_MATCHED_PAYLOAD_LOOKBACK = 10


@dataclasses.dataclass(frozen=True)
class EnrichmentOutcome:
    """What one enrichment attempt produced.

    provider_fields tracks the provider lookup itself (None on a miss, even when an
    archived-payload fallback still scored the org) — that is what the workflow's
    matched/upgraded reporting reads. score is the V0.5 evaluation, present whenever
    scoring ran (its status distinguishes scored/insufficient_data/not_found/disqualified);
    None only when scoring degraded (no active curated-lists row, or an unexpected error).
    """

    provider_fields: Optional[EnrichmentFields] = None
    score: Optional[IcpScoreResult] = None


def _reconstruct_fields_from_record(organization_id: str) -> Optional[EnrichmentFields]:
    """Rebuild EnrichmentFields from the last-written record when a provider lookup misses.

    Registry keys are exactly the dataclass field names (see fields.py), so a prior write can be
    replayed back into the same shape. Returns None when there is no prior record, or it carries
    no provider-derived fields — same as a fresh miss. work_email is excluded: it is first-party
    data recorded for every signup, so it neither proves a prior provider match nor belongs in
    the group projection this replay feeds.
    """
    record = OrganizationEnrichment.objects.filter(organization_id=organization_id).first()
    if record is None:
        return None
    fields = EnrichmentFields(
        **{f.name: record.data.get(f.name) for f in dataclasses.fields(EnrichmentFields) if f.name != "work_email"}
    )
    return fields if fields.to_dict() else None


def _latest_matched_payload(organization_id: str) -> Optional[dict[str, Any]]:
    """The org's most recent archived payload that was an actual match, or None.

    The scorer consumes the raw payload (description, per-tag types, traction series — all
    deliberately absent from EnrichmentFields), so on a provider miss the archive, not the
    field record, is the scoring fallback. Bounded walk: an org has a handful of fetches
    (signup + recheck + occasional backfills), and a sentinel-only history is a real
    never-matched org.
    """
    payloads = (
        OrganizationEnrichmentFetch.objects.filter(organization_id=organization_id)
        .order_by("-fetched_at", "-id")
        .values_list("payload", flat=True)[:_MATCHED_PAYLOAD_LOOKBACK]
    )
    for payload in payloads:
        if isinstance(payload, dict) and payload and payload.get("companyFound") is not False:
            return payload
    return None


def _person_mirror_allowed(distinct_id: str) -> bool:
    """Whether mirroring the score onto the signer's person profile would be safe.

    Clay's own writes never stamp icp_score_version; ours always do, so an unversioned
    icp_score on the person is Clay's — never clobber it. A lookup failure or a malformed
    person record (unreadable properties) degrades to no mirror rather than raising out of
    the scoring path, preferring a missed mirror over a possible clobber.
    """
    try:
        person = get_person_by_distinct_id(team_id=settings.GROWTH_ENRICHMENT_INTERNAL_TEAM_ID, distinct_id=distinct_id)
        if person is None:
            return True
        properties = person.properties or {}
        clay_owned = properties.get("icp_score") is not None and properties.get("icp_score_version") is None
    except Exception as e:
        capture_exception(e)
        return False
    return not clay_owned


def _score_v05_and_mirror(
    *,
    organization_id: str,
    raw_payload: Optional[dict[str, Any]],
    role: Optional[str],
    domain: str,
    is_recheck: bool,
    distinct_id: Optional[str],
) -> tuple[Optional[IcpScoreResult], Optional[str]]:
    """Evaluate one org under V0.5; on the recheck, also clear the person-mirror write.

    Scores from the raw provider payload — falling back to the org's last matched archived
    payload on a miss, so one flaky lookup can't leave an org permanently score-less. No
    active curated-lists row degrades to no evaluation at all (captured, never raised):
    scoring against empty lists would floor three components and look like a real answer.

    The mirror stays a recheck-only coverage write (the person surface is Clay's legacy
    home; the org group property is canonical), and is withheld whenever the person carries
    a Clay-owned unversioned score — see _person_mirror_allowed.
    """
    try:
        lists = load_active_lists()
        if lists is None:
            capture_exception(RuntimeError("icp_v05_no_active_lists: IcpScoringConfig has no active row"))
            return None, None

        payload = normalize_graphql_company(raw_payload)
        if payload is None:
            payload = normalize_graphql_company(_latest_matched_payload(organization_id))

        result = score_company(payload, lists=lists, role=role, domain=domain)
    except Exception as e:
        capture_exception(e)
        return None, None

    mirror_ok = False
    if is_recheck and distinct_id and result.score is not None:
        mirror_ok = _person_mirror_allowed(distinct_id)

    return result, (distinct_id if mirror_ok else None)


async def enrich_organization(
    *,
    organization_id: str,
    domain: str,
    provider: EnrichmentProvider,
    pha_client: Client,
    is_recheck: bool = False,
    role_at_organization: Optional[str] = None,
    geoip_country_code: Optional[str] = None,
    distinct_id: Optional[str] = None,
) -> EnrichmentOutcome:
    """Look up enrichment for a domain, archive the raw response, and persist the live stores.

    Every fetch is archived verbatim — including a not-found — before the live-store write.
    The Postgres writes run via sync_to_async to bridge the async provider.

    Every attempt is also ICP-scored under V0.5, from the raw payload plus the signup's own
    role answer and email domain — see `_score_v05_and_mirror` for the scoring and
    person-mirror policy. A scoring failure degrades to writing firmographics with no
    score rather than a wrong one; the delayed recheck gets a second chance, and the fetch
    archive backstops a later batch recompute.

    On a miss, a prior `OrganizationEnrichment` record (if any) is reconstructed into fields
    and the last matched archived payload is scored anyway — first attempt or recheck alike —
    so an org can't end up permanently score-less because of one flaky lookup. A genuine
    never-matched org gets its `icp_score_status` stamped not_found, which is what the
    re-enrichment sweep selects on.
    """
    lookup = await provider.enrich_by_domain(domain)

    await sync_to_async(archive_provider_fetch)(
        organization_id=organization_id,
        provider=provider.name,
        payload=lookup.raw_payload if lookup.raw_payload is not None else _MISS_PAYLOAD,
        is_recheck=is_recheck,
    )

    fields = lookup.fields
    if fields is None:
        fields = await sync_to_async(_reconstruct_fields_from_record)(organization_id)

    if fields is not None and fields.country is None and geoip_country_code:
        # The incumbent icp_country was a merge — provider country first, signup GeoIP as
        # fallback — so all three stores see the merged value here. replace() keeps the
        # returned provider_fields verbatim for the at-signup snapshot.
        fields = dataclasses.replace(fields, country=geoip_country_code)

    score, mirror_distinct_id = await sync_to_async(_score_v05_and_mirror)(
        organization_id=organization_id,
        raw_payload=lookup.raw_payload,
        role=role_at_organization,
        domain=domain,
        is_recheck=is_recheck,
        distinct_id=distinct_id,
    )

    if fields is None and score is None:
        return EnrichmentOutcome(provider_fields=None, score=None)

    await sync_to_async(write_organization_enrichment)(
        organization_id=organization_id,
        fields=fields,
        pha_client=pha_client,
        score=score,
        mirror_distinct_id=mirror_distinct_id,
    )
    return EnrichmentOutcome(provider_fields=lookup.fields, score=score)
