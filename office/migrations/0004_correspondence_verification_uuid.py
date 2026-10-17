import uuid
from django.db import migrations,models


def populate(apps,schema_editor):
    model=apps.get_model("office","Correspondence")
    for pk in model.objects.values_list("pk",flat=True).iterator():
        model.objects.filter(pk=pk).update(verification_uuid=uuid.uuid4())


class Migration(migrations.Migration):
    dependencies=[("office","0003_correspondence_issuer_name_and_more")]
    operations=[
        migrations.AddField(model_name="correspondence",name="verification_uuid",field=models.UUIDField(null=True,unique=True,editable=False)),
        migrations.RunPython(populate,migrations.RunPython.noop),
        migrations.AlterField(model_name="correspondence",name="verification_uuid",field=models.UUIDField(default=uuid.uuid4,unique=True,editable=False)),
    ]
