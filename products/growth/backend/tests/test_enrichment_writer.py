from posthog.test.base import BaseTest
from unittest.mock import MagicMock

from products.growth.backend.enrichment.fields import EnrichmentFields
from products.growth.backend.enrichment.score_v05 import IcpScoreResult
from products.growth.backend.enrichment.writer import record_signup_work_email, write_organization_enrichment
from products.growth.backend.models import OrganizationEnrichment


def _scored(score=72, **overrides):
    kwargs = {
        "status": "scored",
        "score": score,
        "components": {"traction": 25, "capital": 30, "ai_pilled": 15, "headcount_growth": 0, "software_relevance": 2},
        "quality_investor": True,
        "data_coverage": 3,
        "low_confidence": False,
        "agency_flag": False,
        "nonprofit_flag": False,
        "lists_version": "lists-1",
    }
    kwargs.update(overrides)
    return IcpScoreResult(**kwargs)


class TestEnrichmentWriter(BaseTest):
    def test_merges_into_existing_record_without_clobbering_other_writers(self):
        OrganizationEnrichment.objects.create(organization=self.organization, data={"company_type_deterministic": "yc"})
        pha_client = MagicMock()

        write_organization_enrichment(
            organization_id=str(self.organization.id),
            fields=EnrichmentFields(company_type="STARTUP", headcount=130),
            pha_client=pha_client,
        )

        record = OrganizationEnrichment.objects.get(organization=self.organization)
        assert record.data == {
            "company_type_deterministic": "yc",
            "company_type": "STARTUP",
            "headcount": 130,
        }

    def test_creates_record_when_missing(self):
        write_organization_enrichment(
            organization_id=str(self.organization.id),
            fields=EnrichmentFields(industry="Fintech"),
            pha_client=MagicMock(),
        )
        record = OrganizationEnrichment.objects.get(organization=self.organization)
        assert record.data == {"industry": "Fintech"}

    def test_projects_enrichment_group_properties(self):
        pha_client = MagicMock()
        write_organization_enrichment(
            organization_id=str(self.organization.id),
            fields=EnrichmentFields(company_type="STARTUP", founded_year=2019),
            pha_client=pha_client,
        )
        pha_client.group_identify.assert_called_once_with(
            "organization",
            str(self.organization.id),
            properties={"enrichment_company_type": "STARTUP", "enrichment_founded_year": 2019},
        )

    def test_record_signup_work_email_merges_without_clobbering_provider_data(self):
        record_signup_work_email(organization_id=str(self.organization.id), work_email=False)
        assert OrganizationEnrichment.objects.get(organization=self.organization).data == {"work_email": False}

        write_organization_enrichment(
            organization_id=str(self.organization.id),
            fields=EnrichmentFields(headcount=9),
            pha_client=MagicMock(),
        )
        record_signup_work_email(organization_id=str(self.organization.id), work_email=True)
        assert OrganizationEnrichment.objects.get(organization=self.organization).data == {
            "work_email": True,
            "headcount": 9,
        }

    def test_no_op_when_no_fields_set_and_no_score(self):
        pha_client = MagicMock()
        write_organization_enrichment(
            organization_id=str(self.organization.id),
            fields=EnrichmentFields(),
            pha_client=pha_client,
        )
        assert not OrganizationEnrichment.objects.filter(organization=self.organization).exists()
        pha_client.group_identify.assert_not_called()

    def test_numeric_score_writes_record_group_and_stays_off_the_person_without_mirror(self):
        pha_client = MagicMock()
        write_organization_enrichment(
            organization_id=str(self.organization.id),
            fields=EnrichmentFields(company_type="STARTUP"),
            pha_client=pha_client,
            score=_scored(),
        )

        record = OrganizationEnrichment.objects.get(organization=self.organization)
        assert record.data["icp_score"] == 72
        assert record.data["icp_score_version"] == "v0.5"
        assert record.data["icp_score_status"] == "scored"
        assert record.data["icp_lists_version"] == "lists-1"
        assert record.data["icp_score_components"]["capital"] == 30
        assert record.data["icp_score_flags"] == {
            "quality_investor": True,
            "data_coverage": 3,
            "low_confidence": False,
            "agency_flag": False,
            "nonprofit_flag": False,
        }
        properties = pha_client.group_identify.call_args.kwargs["properties"]
        assert properties["icp_score"] == 72
        assert properties["icp_score_version"] == "v0.5"
        assert properties["icp_score_status"] == "scored"
        pha_client.set.assert_not_called()

    def test_score_only_write_carries_no_field_keys(self):
        # The score backfill passes fields=None: only the icp_* keys may move.
        pha_client = MagicMock()
        write_organization_enrichment(
            organization_id=str(self.organization.id),
            fields=None,
            pha_client=pha_client,
            score=_scored(score=41),
        )

        record = OrganizationEnrichment.objects.get(organization=self.organization)
        assert record.data["icp_score"] == 41
        assert "company_type" not in record.data
        properties = pha_client.group_identify.call_args.kwargs["properties"]
        assert set(properties) == {"icp_score", "icp_score_version", "icp_score_status"}

    def test_scoreless_evaluation_strips_stale_numeric_keys_from_the_record(self):
        OrganizationEnrichment.objects.create(
            organization=self.organization,
            data={
                "icp_score": 6,
                "icp_score_version": "clay-parity-2",
                "icp_score_components": {"traction": 6},
                "work_email": True,
            },
        )
        pha_client = MagicMock()
        write_organization_enrichment(
            organization_id=str(self.organization.id),
            fields=None,
            pha_client=pha_client,
            score=IcpScoreResult(status="insufficient_data", lists_version="lists-1"),
        )

        record = OrganizationEnrichment.objects.get(organization=self.organization)
        assert record.data == {
            "work_email": True,
            "icp_score_status": "insufficient_data",
            "icp_score_version": "v0.5",
            "icp_lists_version": "lists-1",
        }
        # Group properties cannot be deleted, so only the status key is projected: pairing
        # the new version with the group's stale numeric score would misattribute it.
        properties = pha_client.group_identify.call_args.kwargs["properties"]
        assert properties == {"icp_score_status": "insufficient_data"}

    def test_disqualification_writes_zero_with_reason_and_a_later_scored_pass_clears_it(self):
        pha_client = MagicMock()
        write_organization_enrichment(
            organization_id=str(self.organization.id),
            fields=None,
            pha_client=pha_client,
            score=IcpScoreResult(status="disqualified", score=0, dq_reason="company_type=SCHOOL"),
        )
        record = OrganizationEnrichment.objects.get(organization=self.organization)
        assert record.data["icp_score"] == 0
        assert record.data["icp_dq_reason"] == "company_type=SCHOOL"

        write_organization_enrichment(
            organization_id=str(self.organization.id), fields=None, pha_client=pha_client, score=_scored()
        )
        record.refresh_from_db()
        assert record.data["icp_score"] == 72
        assert "icp_dq_reason" not in record.data

    def test_mirror_writes_the_person_only_with_a_numeric_score(self):
        pha_client = MagicMock()
        write_organization_enrichment(
            organization_id=str(self.organization.id),
            fields=None,
            pha_client=pha_client,
            score=_scored(score=55),
            mirror_distinct_id="signer",
        )
        pha_client.set.assert_called_once_with(
            distinct_id="signer", properties={"icp_score": 55, "icp_score_version": "v0.5"}
        )

        pha_client.reset_mock()
        write_organization_enrichment(
            organization_id=str(self.organization.id),
            fields=None,
            pha_client=pha_client,
            score=IcpScoreResult(status="insufficient_data"),
            mirror_distinct_id="signer",
        )
        pha_client.set.assert_not_called()

    def test_record_signup_work_email_lowercases_and_skips_blank_roles(self):
        record_signup_work_email(organization_id=str(self.organization.id), work_email=True, signup_role="Founder")
        record = OrganizationEnrichment.objects.get(organization=self.organization)
        assert record.data == {"work_email": True, "signup_role": "founder"}

        record_signup_work_email(organization_id=str(self.organization.id), work_email=True, signup_role="  ")
        record.refresh_from_db()
        assert record.data["signup_role"] == "founder"  # blank role never clobbers a recorded one
