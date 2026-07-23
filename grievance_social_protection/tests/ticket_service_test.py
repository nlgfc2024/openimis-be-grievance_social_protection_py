from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase

from grievance_social_protection.models import Ticket
from grievance_social_protection.services import TicketService
from grievance_social_protection.tests.data import (
    service_add_ticket_payload,
    service_add_ticket_payload_bad_resolution,
    service_add_ticket_payload_bad_resolution_day,
    service_add_ticket_payload_bad_resolution_hour,
    service_update_ticket_payload
)
from grievance_social_protection.apps import DEFAULT_CFG
from grievance_social_protection.tests.test_helpers import (
    create_ticket,
    setup_grievance_config,
    restore_grievance_config,
)
from core.test_helpers import LogInHelper
from django.utils.translation import gettext as _


class TicketServiceTest(TestCase):
    user = None
    service = None
    query_all = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        setup_grievance_config(DEFAULT_CFG)
        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = TicketService(cls.user)
        cls.query_all = Ticket.objects.filter(is_deleted=False)
        cls.ticket = create_ticket(cls.user)

    def test_add_ticket(self):
        result = self.service.create(service_add_ticket_payload)
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        uuid = result.get('data', {}).get('uuid', None)
        query = self.query_all.filter(uuid=uuid)
        self.assertEqual(query.count(), 1)

    def test_add_ticket_validation(self):
        with self.assertRaises(ValidationError) as context:
            self.service.create(service_add_ticket_payload_bad_resolution)

        exception = context.exception
        self.assertIn(_('validations.TicketValidation.validate_resolution.invalid_format'), str(exception))

        with self.assertRaises(ValidationError) as context:
            self.service.create(service_add_ticket_payload_bad_resolution_day)

        exception = context.exception
        self.assertIn(_('validations.TicketValidation.validate_resolution.invalid_day_value'), str(exception))

        with self.assertRaises(ValidationError) as context:
            self.service.create(service_add_ticket_payload_bad_resolution_hour)

        exception = context.exception
        self.assertIn(_('validations.TicketValidation.validate_resolution.invalid_hour_value'), str(exception))

    def test_ticket_status_accepts_referred(self):
        """REFERRED is a valid Ticket status and round-trips through the DB."""
        self.assertIn('REFERRED', Ticket.TicketStatus.values)
        ticket = create_ticket(self.user)
        ticket.status = Ticket.TicketStatus.REFERRED
        ticket.save(user=self.user)
        reloaded = self.query_all.get(id=ticket.id)
        self.assertEqual(reloaded.status, Ticket.TicketStatus.REFERRED)

    def test_update_ticket(self):
        update_payload = {
            "id": self.ticket.uuid,
            **service_update_ticket_payload
        }
        result = self.service.update(update_payload)
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        uuid = result.get('data', {}).get('uuid', None)
        query = self.query_all.filter(uuid=uuid)
        self.assertEqual(query.count(), 1)
        updated_ticket = query.first()
        self.assertEqual(updated_ticket.title, update_payload.get('title'))
        self.assertEqual(updated_ticket.resolution, update_payload.get('resolution'))
        self.assertEqual(updated_ticket.priority, update_payload.get('priority'))
        self.assertEqual(updated_ticket.status, update_payload.get('status'))

    def test_update_ticket_validation(self):
        with self.assertRaises(ValidationError) as context:
            self.service.update({
                "id": self.ticket.uuid,
                **service_add_ticket_payload_bad_resolution
            })

        exception = context.exception
        self.assertIn(_('validations.TicketValidation.validate_resolution.invalid_format'), str(exception))

        with self.assertRaises(ValidationError) as context:
            self.service.update({
                "id": self.ticket.uuid,
                **service_add_ticket_payload_bad_resolution_day
            })

        exception = context.exception
        self.assertIn(_('validations.TicketValidation.validate_resolution.invalid_day_value'), str(exception))

        with self.assertRaises(ValidationError) as context:
            self.service.update({
                "id": self.ticket.uuid,
                **service_add_ticket_payload_bad_resolution_hour
            })

        exception = context.exception
        self.assertIn(_('validations.TicketValidation.validate_resolution.invalid_hour_value'), str(exception))


class TicketDueDateAndStatusTest(TestCase):
    """auto-computed due_date and default OPEN status on ticket creation."""

    _config_snapshot = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = TicketService(cls.user)
        cls._config_snapshot = setup_grievance_config({
            'grievance_types': [
                {'name': 'Claims', 'resolution_times': '30,0'},
                'no_sla_category',
            ],
        })

    @classmethod
    def tearDownClass(cls):
        restore_grievance_config(cls._config_snapshot)
        super().tearDownClass()

    def test_due_date_and_status_defaulted_on_create(self):
        result = self.service.create({
            "category": "Claims",
            "title": "Unpaid wages",
            "channel": "Channel A",
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.due_date, date.today() + timedelta(days=30))
        self.assertEqual(ticket.status, Ticket.TicketStatus.OPEN)

    def test_category_without_sla_gets_no_due_date(self):
        result = self.service.create({
            "category": "no_sla_category",
            "title": "General query",
            "channel": "Channel A",
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertIsNone(ticket.due_date)
        self.assertEqual(ticket.status, Ticket.TicketStatus.OPEN)

    def test_explicit_status_and_due_date_are_not_overridden(self):
        result = self.service.create({
            "category": "Claims",
            "title": "Already resolved",
            "channel": "Channel A",
            "status": Ticket.TicketStatus.RESOLVED,
            "due_date": date.today(),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.due_date, date.today())
        self.assertEqual(ticket.status, Ticket.TicketStatus.RESOLVED)
