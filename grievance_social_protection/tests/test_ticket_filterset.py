"""
Tests for TicketFilterSet — verifies that filter-level field restrictions
prevent information inference attacks by users with restricted_read access.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from django.test import TestCase

from core.test_helpers import create_test_interactive_user

from grievance_social_protection.apps import TicketConfig
from grievance_social_protection.gql_queries import TicketFilterSet, _ALWAYS_FILTERABLE
from grievance_social_protection.models import Ticket
from grievance_social_protection.tests.test_helpers import (
    setup_grievance_config, restore_grievance_config,
    assign_rights_to_user, get_rights,
)


class TicketFilterSetRestrictedFieldsTest(TestCase):
    """Test _get_restricted_fields for different access levels."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user_restricted = create_test_interactive_user(
            username='fs_restricted', roles=[1]
        )
        cls.user_full = create_test_interactive_user(
            username='fs_full', roles=[1]
        )
        cls.user_no_access = create_test_interactive_user(
            username='fs_no_access', roles=[1]
        )

    def setUp(self):
        super().setUp()
        self._snapshot = self._setup_config()

    def tearDown(self):
        restore_grievance_config(self._snapshot)
        super().tearDown()

    def _setup_config(self):
        cfg = {
            'grievance_types': [
                {
                    'name': 'restricted_cat',
                    'permissions': ['restricted_read', 'read'],
                    'visible_fields': ['id', 'status', 'category', 'priority'],
                },
                'open_cat',
            ],
            'grievance_flags': [],
        }
        snapshot = setup_grievance_config(cfg)

        cat_rights = get_rights('processed_categories', 'restricted_cat')

        if cat_rights.get('restricted_read'):
            assign_rights_to_user(
                self.user_restricted,
                [127000, cat_rights['restricted_read']],
                role_name='FSRestrictedRole',
            )

        if cat_rights.get('read'):
            assign_rights_to_user(
                self.user_full,
                [127000, cat_rights['read']],
                role_name='FSFullRole',
            )

        return snapshot

    # ── _get_restricted_fields tests ─────────────────────────────

    def test_restricted_user_has_restricted_fields(self):
        """Restricted user should have fields outside visible_fields blocked."""
        restricted = TicketFilterSet._get_restricted_fields(self.user_restricted)
        # 'description' is NOT in visible_fields so should be restricted
        self.assertIn('description', restricted)
        self.assertIn('resolution', restricted)
        self.assertIn('channel', restricted)
        self.assertIn('title', restricted)

    def test_restricted_user_allows_visible_fields(self):
        """Restricted user should NOT have visible_fields blocked."""
        restricted = TicketFilterSet._get_restricted_fields(self.user_restricted)
        self.assertNotIn('status', restricted)
        self.assertNotIn('category', restricted)
        self.assertNotIn('priority', restricted)

    def test_full_access_user_no_restricted_fields(self):
        """User with read access should have no restricted fields."""
        restricted = TicketFilterSet._get_restricted_fields(self.user_full)
        self.assertEqual(restricted, set())

    def test_no_access_user_no_restricted_fields(self):
        """User with no grievance rights returns empty set (queryset filtering
        handles access denial; filter restriction is an additional guard)."""
        restricted = TicketFilterSet._get_restricted_fields(self.user_no_access)
        # get_visible_fields returns [] for no access, which is falsy →
        # the `if visible_fields is not None and visible_fields:` guard skips it
        self.assertEqual(restricted, set())

    def test_anonymous_user_all_fields_restricted(self):
        """Anonymous user has all filterable fields restricted (defense-in-depth)."""
        anon = MagicMock()
        anon.is_anonymous = True
        restricted = TicketFilterSet._get_restricted_fields(anon)
        expected = set(TicketFilterSet.Meta.fields.keys()) - _ALWAYS_FILTERABLE
        self.assertEqual(restricted, expected)

    def test_none_user_all_fields_restricted(self):
        """None user has all filterable fields restricted (defense-in-depth)."""
        restricted = TicketFilterSet._get_restricted_fields(None)
        expected = set(TicketFilterSet.Meta.fields.keys()) - _ALWAYS_FILTERABLE
        self.assertEqual(restricted, expected)

    def test_always_filterable_not_restricted(self):
        """Fields in _ALWAYS_FILTERABLE are never in the restricted set."""
        restricted = TicketFilterSet._get_restricted_fields(self.user_restricted)
        for field in _ALWAYS_FILTERABLE:
            self.assertNotIn(field, restricted)

    def test_no_config_returns_empty(self):
        """When no categories are configured, nothing is restricted."""
        TicketConfig.processed_categories = {}
        restricted = TicketFilterSet._get_restricted_fields(self.user_restricted)
        self.assertEqual(restricted, set())


class TicketFilterSetFilterQuerysetTest(TestCase):
    """Test that filter_queryset actually skips restricted filters."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user_restricted = create_test_interactive_user(
            username='fq_restricted', roles=[1]
        )
        cls.user_full = create_test_interactive_user(
            username='fq_full', roles=[1]
        )

    def setUp(self):
        super().setUp()
        self._snapshot = self._setup_config()
        self._create_tickets()

    def tearDown(self):
        Ticket.objects.filter(code__startswith='FSTEST').delete()
        restore_grievance_config(self._snapshot)
        super().tearDown()

    def _setup_config(self):
        cfg = {
            'grievance_types': [
                {
                    'name': 'filter_cat',
                    'permissions': ['restricted_read', 'read'],
                    'visible_fields': ['id', 'status', 'category', 'priority'],
                },
            ],
            'grievance_flags': [],
        }
        snapshot = setup_grievance_config(cfg)

        cat_rights = get_rights('processed_categories', 'filter_cat')

        if cat_rights.get('restricted_read'):
            assign_rights_to_user(
                self.user_restricted,
                [127000, cat_rights['restricted_read']],
                role_name='FQRestrictedRole',
            )

        if cat_rights.get('read'):
            assign_rights_to_user(
                self.user_full,
                [127000, cat_rights['read']],
                role_name='FQFullRole',
            )

        return snapshot

    def _create_tickets(self):
        self.ticket_match = Ticket(
            code='FSTEST001',
            title='Sensitive Title',
            description='secret information',
            category='filter_cat',
            status='OPEN',
            priority='High',
        )
        self.ticket_match.save(user=self.user_full)

        self.ticket_other = Ticket(
            code='FSTEST002',
            title='Other Title',
            description='other info',
            category='filter_cat',
            status='OPEN',
            priority='Low',
        )
        self.ticket_other.save(user=self.user_full)

    def _build_filterset(self, user, data):
        """Build a TicketFilterSet with a mock request carrying the given user."""
        request = MagicMock()
        request.user = user
        qs = Ticket.objects.filter(code__startswith='FSTEST')
        fs = TicketFilterSet(data=data, queryset=qs, request=request)
        # Trigger form validation so cleaned_data is populated
        fs.form.is_valid()
        return fs

    def test_full_user_can_filter_on_description(self):
        """Full access user can filter on description and get matching results."""
        fs = self._build_filterset(
            self.user_full, {'description__icontains': 'secret'}
        )
        result = fs.filter_queryset(fs.queryset)
        codes = list(result.values_list('code', flat=True))
        self.assertIn('FSTEST001', codes)
        self.assertNotIn('FSTEST002', codes)

    def test_restricted_user_cannot_filter_on_description(self):
        """Restricted user's description filter is silently skipped."""
        fs = self._build_filterset(
            self.user_restricted, {'description__icontains': 'secret'}
        )
        result = fs.filter_queryset(fs.queryset)
        codes = list(result.values_list('code', flat=True))
        # Both tickets should appear because the filter was skipped
        self.assertIn('FSTEST001', codes)
        self.assertIn('FSTEST002', codes)

    def test_restricted_user_can_filter_on_status(self):
        """Restricted user can still filter on visible fields like status."""
        fs = self._build_filterset(
            self.user_restricted, {'status': 'OPEN'}
        )
        result = fs.filter_queryset(fs.queryset)
        codes = list(result.values_list('code', flat=True))
        self.assertIn('FSTEST001', codes)
        self.assertIn('FSTEST002', codes)

    def test_restricted_user_can_filter_on_priority(self):
        """Restricted user can filter on priority (in visible_fields)."""
        fs = self._build_filterset(
            self.user_restricted, {'priority': 'High'}
        )
        result = fs.filter_queryset(fs.queryset)
        codes = list(result.values_list('code', flat=True))
        self.assertIn('FSTEST001', codes)
        self.assertNotIn('FSTEST002', codes)

    def test_restricted_user_title_filter_skipped(self):
        """title is not in visible_fields → filter should be skipped."""
        fs = self._build_filterset(
            self.user_restricted, {'title__icontains': 'Sensitive'}
        )
        result = fs.filter_queryset(fs.queryset)
        codes = list(result.values_list('code', flat=True))
        # Filter skipped → both tickets returned
        self.assertIn('FSTEST001', codes)
        self.assertIn('FSTEST002', codes)

    def test_full_user_title_filter_works(self):
        """Full access user CAN filter on title."""
        fs = self._build_filterset(
            self.user_full, {'title__icontains': 'Sensitive'}
        )
        result = fs.filter_queryset(fs.queryset)
        codes = list(result.values_list('code', flat=True))
        self.assertIn('FSTEST001', codes)
        self.assertNotIn('FSTEST002', codes)

    def test_date_created_range_filter(self):
        """date_created supports gte/lte for date-range search (start/end date filed)."""
        now = datetime.now()
        self.ticket_match.date_created = now - timedelta(days=10)
        self.ticket_match.save(user=self.user_full)
        self.ticket_other.date_created = now
        self.ticket_other.save(user=self.user_full)

        cutoff = (now - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')

        fs_gte = self._build_filterset(self.user_full, {'date_created__gte': cutoff})
        codes_gte = list(fs_gte.filter_queryset(fs_gte.queryset).values_list('code', flat=True))
        self.assertNotIn('FSTEST001', codes_gte)
        self.assertIn('FSTEST002', codes_gte)

        fs_lte = self._build_filterset(self.user_full, {'date_created__lte': cutoff})
        codes_lte = list(fs_lte.filter_queryset(fs_lte.queryset).values_list('code', flat=True))
        self.assertIn('FSTEST001', codes_lte)
        self.assertNotIn('FSTEST002', codes_lte)
