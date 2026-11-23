from django.db import migrations


def add(apps, schema_editor):
    Team = apps.get_model("work_orders", "Team")
    for t in Team.objects.all():
        secs = list(t.default_sections or [])
        if "items" in secs and "qty_unit" not in secs:
            secs.insert(secs.index("items") + 1, "qty_unit")
            t.default_sections = secs
            t.save(update_fields=["default_sections"])
    WorkOrder = apps.get_model("work_orders", "WorkOrder")
    for wo in WorkOrder.objects.all():          # keep existing work orders looking the same
        secs = list(wo.sections or [])
        if "items" in secs and "qty_unit" not in secs:
            secs.insert(secs.index("items") + 1, "qty_unit")
            wo.sections = secs
            wo.save(update_fields=["sections"])


class Migration(migrations.Migration):
    dependencies = [("work_orders", "0005_checklist_assignee")]
    operations = [migrations.RunPython(add, migrations.RunPython.noop)]
