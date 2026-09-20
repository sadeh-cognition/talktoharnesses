from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("talktoharnesses", "0003_sandboxrecord")]

    operations = [
        migrations.CreateModel(
            name="SandboxPolicyRecord",
            fields=[
                ("id", models.UUIDField(primary_key=True, serialize=False)),
                ("owner_id", models.CharField(max_length=255)),
                ("latest_revision", models.PositiveIntegerField(default=0)),
            ],
            options={"db_table": "talktoharnesses_sandbox_policy"},
        ),
        migrations.CreateModel(
            name="SandboxPolicyRevisionRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("revision", models.PositiveIntegerField()),
                ("rules", models.JSONField()),
                ("repository_directory", models.CharField(max_length=4096, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("policy", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="talktoharnesses.sandboxpolicyrecord")),
            ],
            options={
                "db_table": "talktoharnesses_sandbox_policy_revision",
                "constraints": [models.UniqueConstraint(fields=("policy", "revision"), name="tth_policy_revision_unique")],
            },
        ),
        # Legacy per-kind containers must not be reattached after cutover.
        # Only their runtime cache is discarded; conversation history is retained.
        migrations.DeleteModel(name="SandboxRecord"),
        migrations.CreateModel(
            name="SandboxRecord",
            fields=[
                ("scope", models.CharField(max_length=128, primary_key=True, editable=False, serialize=False)),
                ("kind", models.CharField(max_length=32)),
                ("container_name", models.CharField(max_length=128)),
                ("image", models.CharField(max_length=255)),
                ("host_port", models.PositiveIntegerField()),
                ("base_url", models.CharField(max_length=255)),
                ("split_token", models.CharField(max_length=128)),
                ("status", models.CharField(max_length=16, default="preparing")),
                ("created_at", models.DateTimeField()),
                ("updated_at", models.DateTimeField()),
                ("last_ready_at", models.DateTimeField(null=True, blank=True)),
            ],
            options={"db_table": "talktoharnesses_sandbox"},
        ),
    ]
