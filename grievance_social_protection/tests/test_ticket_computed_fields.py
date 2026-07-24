"""
Tests for TicketGQLType's computed duration_days / sla_state — the
single source the FE uses for row colouring. Pure logic over fixed dates;
no DB access needed.
"""
from datetime import date as real_date, datetime
from unittest.mock import patch

from django.test import SimpleTestCase

from grievance_social_protection.gql_queries import TicketGQLType
from grievance_social_protection.models import Ticket


class FixedDate(real_date):
    """A `date` subclass with `.today()` pinned, so sla_state/duration are deterministic."""
    _fixed_today = real_date(2026, 1, 15)

    @classmethod
    def today(cls):
        return cls._fixed_today


def make_ticket(created, due_date=None, resolved_date=None):
    ticket = Ticket(date_created=created, due_date=due_date)
    if resolved_date:
        ticket.json_ext = {'resolved_date': resolved_date}
    return ticket


@patch('grievance_social_protection.gql_queries.date', FixedDate)
class TicketComputedFieldsTest(SimpleTestCase):

    def test_sla_within_when_no_due_date(self):
        ticket = make_ticket(created=datetime(2026, 1, 1))
        self.assertEqual(TicketGQLType._compute_sla_state(ticket), 'within')

    def test_sla_within_when_due_date_in_future(self):
        ticket = make_ticket(created=datetime(2026, 1, 1), due_date=real_date(2026, 1, 20))
        self.assertEqual(TicketGQLType._compute_sla_state(ticket), 'within')

    def test_sla_breached_when_due_date_in_past(self):
        ticket = make_ticket(created=datetime(2026, 1, 1), due_date=real_date(2026, 1, 10))
        self.assertEqual(TicketGQLType._compute_sla_state(ticket), 'breached')

    def test_sla_resolved_even_if_past_due_date(self):
        """A ticket resolved after breaching SLA is coloured 'resolved', not 'breached'."""
        ticket = make_ticket(
            created=datetime(2026, 1, 1), due_date=real_date(2026, 1, 10),
            resolved_date='2026-01-12',
        )
        self.assertEqual(TicketGQLType._compute_sla_state(ticket), 'resolved')

    def test_duration_days_pending_uses_today(self):
        ticket = make_ticket(created=datetime(2026, 1, 1))
        self.assertEqual(TicketGQLType._compute_duration_days(ticket), 14)  # Jan 1 -> Jan 15

    def test_duration_days_resolved_uses_resolved_date_not_today(self):
        ticket = make_ticket(created=datetime(2026, 1, 1), resolved_date='2026-01-05')
        self.assertEqual(TicketGQLType._compute_duration_days(ticket), 4)  # Jan 1 -> Jan 5

    def test_duration_days_none_when_no_date_created(self):
        ticket = make_ticket(created=None)
        self.assertIsNone(TicketGQLType._compute_duration_days(ticket))

    def test_sla_state_ignores_malformed_resolved_date(self):
        ticket = make_ticket(
            created=datetime(2026, 1, 1), due_date=real_date(2026, 1, 10),
            resolved_date='not-a-date',
        )
        self.assertEqual(TicketGQLType._compute_sla_state(ticket), 'breached')
