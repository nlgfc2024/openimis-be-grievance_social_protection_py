# add REFERRED to Ticket.TicketStatus.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('grievance_social_protection', '0018_alter_comment_user_created_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='historicalticket',
            name='status',
            field=models.CharField(
                choices=[('RECEIVED', 'Received'), ('OPEN', 'Open'), ('IN_PROGRESS', 'In Progress'),
                         ('REFERRED', 'Referred'), ('RESOLVED', 'Resolved'), ('CLOSED', 'Closed')],
                default='RECEIVED', max_length=20),
        ),
        migrations.AlterField(
            model_name='ticket',
            name='status',
            field=models.CharField(
                choices=[('RECEIVED', 'Received'), ('OPEN', 'Open'), ('IN_PROGRESS', 'In Progress'),
                         ('REFERRED', 'Referred'), ('RESOLVED', 'Resolved'), ('CLOSED', 'Closed')],
                default='RECEIVED', max_length=20),
        ),
    ]
