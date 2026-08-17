from posthog.test.base import BaseTest
from unittest.mock import MagicMock, patch

from asgiref.sync import async_to_sync
from parameterized import parameterized

from products.growth.backend.enrichment.core import enrich_organization
from products.growth.backend.enrichment.fields import EnrichmentFields
from products.growth.backend.enrichment.icp_lists import clear_lists_cache
from products.growth.backend.enrichment.providers import EnrichmentProvider, ProviderLookup
from products.growth.backend.models import IcpScoringConfig, OrganizationEnrichment, OrganizationEnrichmentFetch


class _FakeProvider(EnrichmentProvider):
    name = "harmonic"

    def __init__(self, lookup: ProviderLookup):
        self._lookup = lookup

    async def enrich_by_domain(self, domain: str) -> ProviderLookup:
        return self._lookup


# A GraphQL-shaped company that scores 100 with the test lists: traction 35 (120k visits,
# +71% 90d), capital 30 ($12M + YC batch), AI 15 (tag), headcount growth 10 (+50% 180d),
# software 10 (eng headcount).
def _company(**overrides):
    company = {
        "companyType": "STARTUP",
        "headcount": 12,
        "description": "AI developer platform",
        "funding": {"fundingTotal": 12_000_000, "investors": [{"name": "Y Combinator"}]},
        "tagsV2": [
            {"displayValue": "Artificial Intelligence", "type": "MARKET"},
            {"displayValue": "S25", "type": "YC_BATCH"},
        ],
        "tractionMetrics": {
            "webTraffic": {
                "latestMetricValue": 120_000,
                "metrics": [
                    {"timestamp": "2026-08-01T00:00:00Z", "metricValue": 120_000},
                    {"timestamp": "2026-04-01T00:00:00Z", "metricValue": 70_000},
                ],
            },
            "headcount": {
                "latestMetricValue": 12,
                "metrics": [
                    {"timestamp": "2026-08-01T00:00:00Z", "metricValue": 12},
                    {"timestamp": "2026-01-01T00:00:00Z", "metricValue": 8},
                ],
            },
            "headcountEngineering": {"latestMetricValue": 8, "metrics": []},
        },
    }
    company.update(overrides)
    return company


# Matched but empty-shell: no headcount, no funding, no tags, no traffic -> insufficient_data.
def _empty_shell():
    return {"companyType": "STARTUP", "funding": {}, "tagsV2": [], "tractionMetrics": {}}


class TestEnrichmentCore(BaseTest):
    def setUp(self):
        super().setUp()
        IcpScoringConfig.objects.create(
            version="test-lists-1",
            tags=[
                {"tag": "Artificial Intelligence", "recommendation": "ai_positive"},
                {"tag": "Developer Tools", "recommendation": "software_positive"},
            ],
            quality_investors=[{"investor": "Y Combinator", "aliases": ["YC"]}],
            is_active=True,
        )
        clear_lists_cache()

    def tearDown(self):
        clear_lists_cache()
        super().tearDown()

    def _enrich(
        self,
        lookup: ProviderLookup,
        is_recheck: bool = False,
        role_at_organization=None,
        pha_client=None,
        distinct_id=None,
        person=None,
        geoip_country_code=None,
        domain="stripe.com",
    ):
        person_patch_kwargs = {"side_effect": person} if isinstance(person, Exception) else {"return_value": person}
        with patch("products.growth.backend.enrichment.core.get_person_by_distinct_id", **person_patch_kwargs):
            return async_to_sync(enrich_organization)(
                organization_id=str(self.organization.id),
                domain=domain,
                provider=_FakeProvider(lookup),
                pha_client=pha_client or MagicMock(),
                is_recheck=is_recheck,
                role_at_organization=role_at_organization,
                geoip_country_code=geoip_country_code,
                distinct_id=distinct_id,
            )

    def test_archives_raw_payload_and_writes_live_stores_on_match(self):
        company = {"companyType": "STARTUP", "funding": {"fundingStage": "SEED"}}
        fields = EnrichmentFields(company_type="STARTUP")
        outcome = self._enrich(ProviderLookup(fields=fields, raw_payload=company))

        assert outcome.provider_fields is fields
        row = OrganizationEnrichmentFetch.objects.get(organization=self.organization)
        assert row.provider == "harmonic"
        assert row.is_recheck is False
        assert row.payload == company  # verbatim, un-transformed
        assert OrganizationEnrichment.objects.filter(organization=self.organization).exists()

    def test_recheck_labels_the_archive_row(self):
        self._enrich(ProviderLookup(fields=None, raw_payload=None), is_recheck=True)
        assert OrganizationEnrichmentFetch.objects.get(organization=self.organization).is_recheck is True

    def test_each_fetch_is_a_separate_row(self):
        self._enrich(ProviderLookup(fields=None, raw_payload=None))
        self._enrich(
            ProviderLookup(fields=EnrichmentFields(company_type="STARTUP"), raw_payload={"companyType": "STARTUP"}),
            is_recheck=True,
        )
        rows = OrganizationEnrichmentFetch.objects.filter(organization=self.organization).order_by("fetched_at")
        assert [r.is_recheck for r in rows] == [False, True]

    def test_scores_the_raw_payload_and_writes_all_three_key_groups(self):
        pha_client = MagicMock()
        fields = EnrichmentFields(company_type="STARTUP", headcount=12)
        outcome = self._enrich(
            ProviderLookup(fields=fields, raw_payload=_company()), pha_client=pha_client, domain="acme.ai"
        )

        assert outcome.score is not None and outcome.score.score == 100
        record = OrganizationEnrichment.objects.get(organization=self.organization)
        assert record.data["icp_score"] == 100
        assert record.data["icp_score_version"] == "v0.5"
        assert record.data["icp_score_status"] == "scored"
        assert record.data["icp_lists_version"] == "test-lists-1"
        assert record.data["icp_score_components"] == {
            "traction": 35,
            "capital": 30,
            "ai_pilled": 15,
            "headcount_growth": 10,
            "software_relevance": 10,
        }
        assert record.data["icp_score_flags"]["quality_investor"] is True
        properties = pha_client.group_identify.call_args.kwargs["properties"]
        assert properties["icp_score"] == 100
        assert properties["icp_score_version"] == "v0.5"
        assert properties["icp_score_status"] == "scored"
        # First attempt: the person mirror stays recheck-only.
        pha_client.set.assert_not_called()

    def test_student_role_disqualifies_regardless_of_the_payload(self):
        pha_client = MagicMock()
        fields = EnrichmentFields(company_type="STARTUP")
        self._enrich(
            ProviderLookup(fields=fields, raw_payload=_company()),
            role_at_organization="Student",
            pha_client=pha_client,
        )

        record = OrganizationEnrichment.objects.get(organization=self.organization)
        assert record.data["icp_score"] == 0
        assert record.data["icp_score_status"] == "disqualified"
        assert record.data["icp_dq_reason"] == "role=student"

    def test_insufficient_data_writes_status_and_strips_stale_numeric_keys(self):
        # A pre-flip record still carries a clay-parity score; the V0.5 evaluation of a
        # matched-but-empty profile must replace it with the status, not sit next to it.
        OrganizationEnrichment.objects.create(
            organization=self.organization,
            data={"icp_score": 6, "icp_score_version": "clay-parity-2", "work_email": True},
        )
        pha_client = MagicMock()
        fields = EnrichmentFields(company_type="STARTUP")
        outcome = self._enrich(ProviderLookup(fields=fields, raw_payload=_empty_shell()), pha_client=pha_client)

        assert outcome.score is not None and outcome.score.status == "insufficient_data"
        record = OrganizationEnrichment.objects.get(organization=self.organization)
        assert "icp_score" not in record.data
        assert record.data["icp_score_status"] == "insufficient_data"
        assert record.data["icp_score_version"] == "v0.5"
        assert record.data["work_email"] is True  # other writers' keys survive
        properties = pha_client.group_identify.call_args.kwargs["properties"]
        assert properties["icp_score_status"] == "insufficient_data"
        # Group properties cannot be deleted, so no numeric/version keys ride along with a
        # score-less status — the stale group pair stays attributed to its own version.
        assert "icp_score" not in properties
        assert "icp_score_version" not in properties

    def test_miss_scores_from_the_latest_matched_archived_payload(self):
        # First attempt matched and archived; the recheck misses. The score must come from
        # the archived payload (the record's EnrichmentFields lack description/tags/series).
        OrganizationEnrichmentFetch.objects.create(
            organization=self.organization, provider="harmonic", payload=_company()
        )
        OrganizationEnrichment.objects.create(organization=self.organization, data={"headcount": 12})
        pha_client = MagicMock()

        outcome = self._enrich(
            ProviderLookup(fields=None, raw_payload=None), is_recheck=True, pha_client=pha_client, domain="acme.ai"
        )

        assert outcome.provider_fields is None  # the workflow's matched signal tracks the lookup
        assert outcome.score is not None and outcome.score.score == 100
        record = OrganizationEnrichment.objects.get(organization=self.organization)
        assert record.data["icp_score"] == 100

    def test_sentinel_rows_are_not_matched_payloads(self):
        # An archive full of miss placeholders is a genuinely never-matched org.
        OrganizationEnrichmentFetch.objects.create(
            organization=self.organization, provider="harmonic", payload={"companyFound": False}
        )
        outcome = self._enrich(ProviderLookup(fields=None, raw_payload=None))

        assert outcome.score is not None and outcome.score.status == "not_found"
        record = OrganizationEnrichment.objects.get(organization=self.organization)
        assert record.data["icp_score_status"] == "not_found"
        assert "icp_score" not in record.data

    def test_fresh_miss_records_not_found_for_the_reenrichment_sweep(self):
        pha_client = MagicMock()
        outcome = self._enrich(ProviderLookup(fields=None, raw_payload=None), pha_client=pha_client)

        assert outcome.provider_fields is None
        record = OrganizationEnrichment.objects.get(organization=self.organization)
        assert record.data == {
            "icp_score_status": "not_found",
            "icp_score_version": "v0.5",
            "icp_lists_version": "test-lists-1",
        }

    def test_work_email_only_record_still_scores_not_found_without_fields(self):
        # The work_email row written before every dispatch is not provider data: no
        # firmographic write happens, but the evaluation status still lands.
        OrganizationEnrichment.objects.create(organization=self.organization, data={"work_email": True})
        pha_client = MagicMock()

        outcome = self._enrich(ProviderLookup(fields=None, raw_payload=None), pha_client=pha_client)

        assert outcome.provider_fields is None
        record = OrganizationEnrichment.objects.get(organization=self.organization)
        assert record.data["work_email"] is True
        assert record.data["icp_score_status"] == "not_found"
        assert "headcount" not in record.data

    def test_no_active_lists_degrades_to_a_fields_only_write(self):
        IcpScoringConfig.objects.update(is_active=False)
        clear_lists_cache()
        pha_client = MagicMock()
        fields = EnrichmentFields(company_type="STARTUP")

        with patch("products.growth.backend.enrichment.core.capture_exception") as capture_mock:
            outcome = self._enrich(ProviderLookup(fields=fields, raw_payload=_company()), pha_client=pha_client)

        capture_mock.assert_called_once()
        assert outcome.provider_fields is fields
        assert outcome.score is None
        record = OrganizationEnrichment.objects.get(organization=self.organization)
        assert record.data.get("company_type") == "STARTUP"
        assert "icp_score" not in record.data
        assert "icp_score_status" not in record.data

    def test_no_active_lists_and_a_miss_writes_nothing(self):
        IcpScoringConfig.objects.update(is_active=False)
        clear_lists_cache()
        pha_client = MagicMock()

        outcome = self._enrich(ProviderLookup(fields=None, raw_payload=None), pha_client=pha_client)

        assert outcome.provider_fields is None and outcome.score is None
        assert not OrganizationEnrichment.objects.filter(organization=self.organization).exists()
        pha_client.group_identify.assert_not_called()

    @parameterized.expand(
        [
            ("geoip_fills_missing_provider_country", None, "US", "US"),
            ("provider_country_wins_over_geoip", "DE", "BR", "DE"),
            ("both_missing_stores_none", None, None, None),
        ]
    )
    def test_country_falls_back_to_signup_geoip(self, _name, provider_country, geoip_country, stored_country):
        pha_client = MagicMock()
        fields = EnrichmentFields(headcount=12, country=provider_country)
        self._enrich(
            ProviderLookup(fields=fields, raw_payload=_company()),
            geoip_country_code=geoip_country,
            pha_client=pha_client,
        )

        record = OrganizationEnrichment.objects.get(organization=self.organization)
        assert record.data.get("country") == stored_country
        properties = pha_client.group_identify.call_args.kwargs["properties"]
        assert properties.get("icp_country") == stored_country

    def test_no_person_write_without_a_distinct_id(self):
        pha_client = MagicMock()
        fields = EnrichmentFields(headcount=12)
        self._enrich(ProviderLookup(fields=fields, raw_payload=_company()), is_recheck=True, pha_client=pha_client)

        pha_client.group_identify.assert_called_once()
        pha_client.set.assert_not_called()

    @parameterized.expand(
        [
            ("no_prior_person", None, True),
            ("person_with_no_icp_score", MagicMock(properties={}), True),
            ("person_with_clay_owned_score", MagicMock(properties={"icp_score": 18}), False),
            (
                "person_with_our_own_versioned_score",
                MagicMock(properties={"icp_score": 9, "icp_score_version": "clay-parity-2"}),
                True,
            ),
            ("person_lookup_raises", RuntimeError("personhog down"), False),
        ]
    )
    def test_recheck_mirror_policy(self, _name, person, expect_mirror):
        pha_client = MagicMock()
        fields = EnrichmentFields(headcount=12)
        self._enrich(
            ProviderLookup(fields=fields, raw_payload=_company()),
            is_recheck=True,
            pha_client=pha_client,
            distinct_id="signer-distinct-id",
            person=person,
        )

        assert pha_client.set.called is expect_mirror
        if expect_mirror:
            assert pha_client.set.call_args.kwargs["properties"] == {"icp_score": 100, "icp_score_version": "v0.5"}

    def test_scoreless_outcomes_never_mirror(self):
        pha_client = MagicMock()
        fields = EnrichmentFields(company_type="STARTUP")
        self._enrich(
            ProviderLookup(fields=fields, raw_payload=_empty_shell()),
            is_recheck=True,
            pha_client=pha_client,
            distinct_id="signer-distinct-id",
            person=None,
        )

        pha_client.set.assert_not_called()

    def test_first_attempt_does_not_look_up_the_person(self):
        fields = EnrichmentFields(headcount=12)
        with patch("products.growth.backend.enrichment.core.get_person_by_distinct_id") as person_mock:
            async_to_sync(enrich_organization)(
                organization_id=str(self.organization.id),
                domain="stripe.com",
                provider=_FakeProvider(ProviderLookup(fields=fields, raw_payload=_company())),
                pha_client=MagicMock(),
                is_recheck=False,
                distinct_id="signer-distinct-id",
            )

        person_mock.assert_not_called()

    def test_scoring_failure_degrades_to_a_fields_only_write(self):
        pha_client = MagicMock()
        fields = EnrichmentFields(company_type="STARTUP")
        with (
            patch(
                "products.growth.backend.enrichment.core.score_company",
                side_effect=RuntimeError("scorer exploded"),
            ),
            patch("products.growth.backend.enrichment.core.capture_exception") as capture_mock,
        ):
            outcome = self._enrich(ProviderLookup(fields=fields, raw_payload=_company()), pha_client=pha_client)

        capture_mock.assert_called_once()
        assert outcome.provider_fields is fields
        assert outcome.score is None
        record = OrganizationEnrichment.objects.get(organization=self.organization)
        assert record.data.get("company_type") == "STARTUP"
        assert "icp_score" not in record.data

    def test_archive_failure_does_not_break_enrich(self):
        fields = EnrichmentFields(company_type="STARTUP")
        with (
            patch(
                "products.growth.backend.enrichment.writer.OrganizationEnrichmentFetch.objects.create",
                side_effect=RuntimeError("db down"),
            ),
            patch("products.growth.backend.enrichment.writer.capture_exception") as capture_mock,
        ):
            outcome = self._enrich(ProviderLookup(fields=fields, raw_payload=_company()))

        assert outcome.provider_fields is fields
        capture_mock.assert_called_once()
        # The live-store write still happened despite the archive failure.
        assert OrganizationEnrichment.objects.filter(organization=self.organization).exists()
