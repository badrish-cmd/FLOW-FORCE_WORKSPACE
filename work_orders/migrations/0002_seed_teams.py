from django.db import migrations

TEAMS = [("ENG", "Engineer"), ("WH", "Warehouse"), ("PRC", "Procurement"), ("ADM", "Admin")]


def seed(apps, schema_editor):
    from work_orders.models import TEAM_DEFAULTS, TEAM_DEFAULT_STAGES
    Team = apps.get_model("work_orders", "Team")
    for i, (code, name) in enumerate(TEAMS):
        Team.objects.get_or_create(code=code, defaults={"name": name, "position": i,
                                                        "default_sections": TEAM_DEFAULTS[code],
                                                        "default_stages": TEAM_DEFAULT_STAGES[code]})


class Migration(migrations.Migration):
    dependencies = [("work_orders", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
