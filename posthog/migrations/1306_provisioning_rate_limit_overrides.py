from django.db import migrations

import structlog

logger = structlog.get_logger(__name__)

BATCH_SIZE = 500

UNLIMITED_OVERRIDE = -1


def migrate_rate_limit_overrides(apps, schema_editor):
    """Move stored provisioning rate limits onto override-only semantics.

    Rate limits used to mix two kinds of value in one place: admin overrides and the
    CIMD verified/unverified tier defaults (10/100), disambiguated by rate_limit_source.
    Tiers are now derived per request, so the persisted tier values must go; only
    admin-authored overrides stay. Concretely, per row:

    - rate_limit_source in (default_verified, default_unverified): drop the
      account_requests value (the only key the tiering ever wrote). Other keys are
      kept: the source tracked account_requests alone, so an admin-set value on
      another endpoint can coexist with a default source.
    - null values (the old fixed-field shape's "no override") are dropped.
    - 0 (the old "unlimited") becomes UNLIMITED_OVERRIDE (-1).
    - rate_limit_source itself is removed.
    """
    OAuthApplication = apps.get_model("posthog", "OAuthApplication")

    # Pinned to the alias `migrate` is running on rather than letting the router pick;
    # see 1274 for why (server-side cursors on default_direct).
    db_alias = schema_editor.connection.alias

    candidates = OAuthApplication.objects.using(db_alias).exclude(_provisioning_config={}).order_by("pk")

    cleared_tier_values = []
    updated = []
    for app in candidates.iterator(chunk_size=BATCH_SIZE):
        config = app._provisioning_config or {}
        raw_limits = config.get("rate_limits") or {}
        source = config.get("rate_limit_source", "")
        if "rate_limits" not in config and "rate_limit_source" not in config:
            continue

        limits = {}
        for key, value in raw_limits.items():
            if value is None:
                continue
            if key == "account_requests" and source in ("default_verified", "default_unverified"):
                cleared_tier_values.append(str(app.pk))
                continue
            limits[key] = UNLIMITED_OVERRIDE if value <= 0 else value

        new_config = {k: v for k, v in config.items() if k != "rate_limit_source"}
        new_config["rate_limits"] = limits
        if new_config == config:
            continue

        app._provisioning_config = new_config
        updated.append(app)
        if len(updated) >= BATCH_SIZE:
            OAuthApplication.objects.using(db_alias).bulk_update(updated, ["_provisioning_config"])
            updated = []

    if updated:
        OAuthApplication.objects.using(db_alias).bulk_update(updated, ["_provisioning_config"])

    if cleared_tier_values:
        logger.info(
            "provisioning_tier_rate_limits_cleared",
            application_ids=cleared_tier_values,
            count=len(cleared_tier_values),
        )


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "1305_teamprovisioningconfig_created_at"),
    ]

    operations = [
        migrations.RunPython(migrate_rate_limit_overrides, migrations.RunPython.noop, elidable=True),
    ]
