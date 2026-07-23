# Add wage_amount to Ticket for partial-wages approval amounts
# (maker-checker hand-off to Payments arrears). Scoped to this field only;
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('grievance_social_protection', '0019_alter_ticket_status_referred'),
    ]

    operations = [
        migrations.AddField(
            model_name='historicalticket',
            name='wage_amount',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='ticket',
            name='wage_amount',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True),
        ),
    ]
