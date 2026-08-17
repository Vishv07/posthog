"""Writers for the live enrichment stores.

Orchestration-agnostic: plain functions any orchestrator (the real-time Temporal
workflow, a later batch Dagster asset) can call. Two of the three stores are written
here — the Postgres record read in-request and the ClickHouse group-property
projection. The at-signup person-event snapshot is a separate write-once store.

One writer per field: this owns the provider-derived registry keys. It never touches
`company_type_deterministic`, which the signup classifier owns.
"""

from typing import Any, Optional

from django.db import transaction

from posthoganalytics.client import Client

from posthog.exceptions_capture import capture_exception

from products.growth.backend.enrichment.fields import EnrichmentFields
from products.growth.backend.enrichment.score_v05 import IcpScoreResult
from products.growth.backend.models import OrganizationEnrichment, OrganizationEnrichmentFetch

ORGANIZATION_GROUP_TYPE = "organization"

# Every score-owned Postgres key. A score-less evaluation (insufficient_data / not_found)
# strips the numeric keys a previous formula version wrote, so the record never carries a
# number the current evaluation didn't produce.
_SCORE_NUMERIC_KEYS = ["icp_score", "icp_score_components", "icp_score_flags", "icp_dq_reason"]


def _merge_into_record(organization_id: str, values: dict[str, Any], remove: Optional[list[str]] = None) -> None:
    """Row-locked read/merge/save into OrganizationEnrichment.data.

    select_for_update serializes concurrent writers on the same org (the request-path
    signup write and the fire-and-forget provider write). Without the lock they read the
    same snapshot and the later save clobbers the other's keys, dropping enrichment data.

    `remove` deletes keys in the same locked write — used to strip stale score keys when a
    new evaluation supersedes them.
    """
    with transaction.atomic():
        record, _ = OrganizationEnrichment.objects.select_for_update().get_or_create(organization_id=organization_id)
        merged = {**record.data, **values}
        for key in remove or []:
            merged.pop(key, None)
        record.data = merged
        record.save(update_fields=["data", "updated_at"])


def _score_record_writes(score: IcpScoreResult) -> tuple[dict[str, Any], list[str]]:
    """The Postgres (data) writes and key removals for one evaluation."""
    values: dict[str, Any] = {"icp_score_status": score.status, "icp_score_version": score.version}
    if score.lists_version:
        values["icp_lists_version"] = score.lists_version

    if score.score is None:
        # insufficient_data / not_found: status is the result. Numeric keys from any prior
        # formula version come off so the record self-describes.
        return values, list(_SCORE_NUMERIC_KEYS)

    values["icp_score"] = score.score
    if score.components:
        values["icp_score_components"] = score.components
    flags = {
        key: value
        for key, value in {
            "quality_investor": score.quality_investor,
            "data_coverage": score.data_coverage,
            "low_confidence": score.low_confidence,
            "agency_flag": score.agency_flag,
            "nonprofit_flag": score.nonprofit_flag,
        }.items()
        if value is not None
    }
    if flags:
        values["icp_score_flags"] = flags
    if score.dq_reason:
        values["icp_dq_reason"] = score.dq_reason
        return values, []
    # Scored: a stale dq_reason from an earlier disqualification must not linger.
    return values, ["icp_dq_reason"]


def _score_group_properties(score: IcpScoreResult) -> dict[str, Any]:
    """The ClickHouse group-property projection for one evaluation.

    Group properties cannot be deleted, so a score-less evaluation writes only the status
    key: writing `icp_score_version` next to a stale numeric `icp_score` from an earlier
    formula would misattribute that number to the new version. The (score, version) pair on
    the group therefore always describes the same evaluation, and `icp_score_status` is the
    consumer's guard — a score is current only when status is scored/disqualified.
    """
    if score.score is None:
        return {"icp_score_status": score.status}
    return {"icp_score": score.score, "icp_score_version": score.version, "icp_score_status": score.status}


def write_organization_enrichment(
    *,
    organization_id: str,
    fields: Optional[EnrichmentFields],
    pha_client: Client,
    score: Optional[IcpScoreResult] = None,
    mirror_distinct_id: Optional[str] = None,
) -> None:
    """Persist enrichment to Postgres and project it onto the organization group.

    - Postgres: merge the set registry fields into OrganizationEnrichment.data, preserving
      any keys owned by other writers (e.g. company_type_deterministic).
    - ClickHouse: project `enrichment_*` group properties via group_identify.

    The V0.5 evaluation rides along on both stores when the caller computed one —
    version-stamped (`icp_score_version` for the formula, `icp_lists_version` for the
    curated lists) with `icp_score_status` marking whether a numeric score exists at all
    (insufficient_data and not_found deliberately carry none, so "no data yet" is never
    read as "evaluated and low"). Either side may be absent: a fields-only write is the
    field backfill, a score-only write (fields=None) is the score backfill and the
    miss-path status stamp.

    `mirror_distinct_id`, when the caller passes one, also sets a numeric score on that
    person's profile: that is where Clay writes its own score, and the legacy consumers
    (the ICP-threshold cohorts and their dashboards) read the person property. The caller
    decides whether mirroring is safe — this function just writes what it's told; see
    enrich_organization for the policy (never sent when it would overwrite a Clay-written
    person score). The org group property is the canonical surface consumers migrate to.

    No-op when there are no set fields and no evaluation, so a Harmonic miss with scoring
    degraded leaves the stores untouched.
    """
    values = fields.to_dict() if fields is not None else {}
    remove: list[str] = []
    if score is not None:
        score_values, remove = _score_record_writes(score)
        values = {**values, **score_values}

    if not values:
        return

    _merge_into_record(organization_id, values, remove=remove)

    properties = fields.to_group_properties() if fields is not None else {}
    if score is not None:
        properties = {**properties, **_score_group_properties(score)}

    if properties:
        pha_client.group_identify(
            ORGANIZATION_GROUP_TYPE,
            str(organization_id),
            properties=properties,
        )

    if score is not None and score.score is not None and mirror_distinct_id:
        pha_client.set(
            distinct_id=mirror_distinct_id,
            properties={"icp_score": score.score, "icp_score_version": score.version},
        )


def archive_provider_fetch(*, organization_id: str, provider: str, payload: dict[str, Any], is_recheck: bool) -> None:
    """Append one raw provider-response row to the fetch archive.

    Never raises: a raw-archive failure must not break enrichment — the live-store write and
    the caller's return still happen. One row per fetch, so signup and recheck stay distinct.
    """
    try:
        OrganizationEnrichmentFetch.objects.create(
            organization_id=organization_id,
            provider=provider,
            is_recheck=is_recheck,
            payload=payload,
        )
    except Exception as e:
        capture_exception(e)


def record_signup_work_email(*, organization_id: str, work_email: bool, signup_role: Optional[str] = None) -> None:
    """Persist the signup's work-email signal, and its role answer when one was given.

    First-party data known synchronously at signup, so it is written from the request
    path for every signup — including personal-domain ones that never get a provider
    lookup. Postgres only for v0: neither key is set by the provider transform, and
    personal domains get no provider write, so there's no group projection here.

    signup_role makes the V0.5 student disqualification replayable from the record by any
    later batch recompute — the role is otherwise only ever in flight on the workflow
    inputs.
    """
    values: dict[str, Any] = {"work_email": work_email}
    if signup_role and signup_role.strip():
        values["signup_role"] = signup_role.strip().lower()
    _merge_into_record(organization_id, values)
