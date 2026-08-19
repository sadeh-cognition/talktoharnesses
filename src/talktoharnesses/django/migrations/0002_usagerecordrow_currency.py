from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("talktoharnesses", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="usagerecordrow",
            name="currency",
            field=models.CharField(blank=True, max_length=8, null=True),
        ),
    ]
