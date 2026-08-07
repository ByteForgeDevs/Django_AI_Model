"""A management command, which is where DJP-008 is willing to speak.

The rule restricts itself to migrations, management commands and scheduled
tasks, because those are the contexts whose row count is unbounded by
construction -- a table small enough to render in a response is small enough to
hold in memory, and a request handler doing this is not obviously wrong. So the
same read appears twice in this fixture: reported here, and a control in
`inventory/views.py`.
"""

from django.core.management.base import BaseCommand

from inventory.models import AuditEvent, Device


class Command(BaseCommand):
    help = "Write every audit event to stdout."

    def handle(self, *args, **options):
        # DJP-008: the whole table, materialised at once.
        events = list(AuditEvent.objects.all())
        for event in events:
            self.stdout.write(f"{event.created} {event.action}")

        # Control: the same table, streamed.
        for device in Device.objects.all().iterator():
            self.stdout.write(device.name)
