from django.db import migrations, models

from posthog.migration_helpers import SafeAddIndexConcurrently


class Migration(migrations.Migration):
    atomic = False  # required for the concurrent index build

    dependencies = [
        ("posthog", "1304_organization_has_active_subscription"),
    ]

    operations = [
        migrations.AddField(
            model_name="teamprovisioningconfig",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, null=True),
        ),
        SafeAddIndexConcurrently(
            model_name="teamprovisioningconfig",
            index=models.Index(fields=["application", "created_at"], name="tpc_application_created_idx"),
        ),
    ]
