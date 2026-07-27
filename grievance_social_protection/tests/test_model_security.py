"""
Test that security filtering is properly implemented at the model level.
This ensures security is enforced consistently across all endpoints (GraphQL, FHIR, etc.)
"""
from unittest.mock import Mock

from django.test import TestCase
from django.conf import settings
from graphql import ResolveInfo

from core.test_helpers import create_test_interactive_user
from grievance_social_protection.models import Ticket, Comment
from grievance_social_protection.tests.test_helpers import (
    setup_grievance_config, restore_grievance_config,
    assign_rights_to_user, collect_all_rights,
)


class ModelSecurityTest(TestCase):
    """Test security filtering at the model level"""

    _config_snapshot = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._config_snapshot = cls._setup_test_config()
        cls._create_test_users()
        cls._create_test_tickets()

    @classmethod
    def _setup_test_config(cls):
        """Set up test configuration"""
        cfg = {
            'grievance_types': [
                'public_category',
                {
                    'name': 'restricted_category',
                    'permissions': ['restricted_read', 'read', 'create'],
                    'visible_fields': ['id', 'status', 'priority', 'date_created']
                },
                {
                    'name': 'highly_restricted',
                    'visible_fields': ['id', 'status'],
                    'permissions': ['restricted_read', 'read']
                }
            ],
            'grievance_flags': [
                'public',
                {
                    'name': 'confidential',
                    'permissions': ['read', 'create']
                }
            ],
            # This test exercises category/flag-based model-level security
            # specifically; opt out of view scoping (which AND-s on
            # top) so it isn't also gated by district/creator scope.
            'view_scope': {'default': 'all_cases'},
        }
        return setup_grievance_config(cfg)

    @classmethod
    def _create_test_users(cls):
        """Create test users with different permission levels"""
        # Admin with all permissions
        cls.admin_user = create_test_interactive_user(username='test_admin', roles=[7])
        base_perms = [127000, 127001, 127002, 127003, 127004, 127005, 127006]
        all_right_ids = base_perms + list(collect_all_rights())
        assign_rights_to_user(cls.admin_user, all_right_ids, 'MSAdminRole')

        # Limited user with only base query permission
        cls.limited_user = create_test_interactive_user(username='test_limited', roles=[1])
        assign_rights_to_user(cls.limited_user, [127000], 'MSLimitedRole')

    @classmethod
    def _create_test_tickets(cls):
        """Create test tickets"""
        cls.tickets = {}

        # Public ticket - should be visible to all
        ticket = Ticket(
            title='Public ticket',
            category='public_category',
            flags='public',
            description='This is a public ticket'
        )
        ticket.save(user=cls.admin_user)
        cls.tickets['public'] = ticket

        # Create comment on public ticket
        comment = Comment(
            ticket=ticket,
            comment='Public comment',
            is_resolution=False
        )
        comment.save(user=cls.admin_user)
        cls.public_comment = comment

        # Restricted category ticket - should only be visible to admin
        ticket = Ticket(
            title='Restricted ticket',
            category='restricted_category',
            flags='public',
            description='This is a restricted ticket'
        )
        ticket.save(user=cls.admin_user)
        cls.tickets['restricted'] = ticket

        # Create comment on restricted ticket
        comment = Comment(
            ticket=ticket,
            comment='Restricted comment',
            is_resolution=False
        )
        comment.save(user=cls.admin_user)
        cls.restricted_comment = comment

        # Public category with confidential flag - should only be visible to admin
        ticket = Ticket(
            title='Confidential ticket',
            category='public_category',
            flags='confidential',
            description='This is a confidential ticket'
        )
        ticket.save(user=cls.admin_user)
        cls.tickets['confidential'] = ticket

    @classmethod
    def tearDownClass(cls):
        """Clean up test data"""
        Comment.objects.filter(ticket__in=cls.tickets.values()).delete()
        Ticket.objects.filter(id__in=[t.id for t in cls.tickets.values()]).delete()
        if cls._config_snapshot:
            restore_grievance_config(cls._config_snapshot)
        super().tearDownClass()

    def test_ticket_security_with_row_security_enabled(self):
        """Test ticket filtering with ROW_SECURITY enabled"""
        original_setting = getattr(settings, 'ROW_SECURITY', True)
        settings.ROW_SECURITY = True

        try:
            # Test admin user
            test_ticket_ids = [t.id for t in self.tickets.values()]
            queryset = Ticket.objects.filter(id__in=test_ticket_ids)
            filtered = Ticket.get_queryset(queryset, self.admin_user)

            # Admin should see all 3 test tickets
            self.assertEqual(filtered.count(), 3)
            for name, ticket in self.tickets.items():
                self.assertTrue(
                    filtered.filter(id=ticket.id).exists(),
                    f"Admin should see {name} ticket"
                )

            # Test limited user
            queryset = Ticket.objects.filter(id__in=test_ticket_ids)
            filtered = Ticket.get_queryset(queryset, self.limited_user)

            # Limited user should only see public ticket
            self.assertEqual(filtered.count(), 1)
            self.assertTrue(
                filtered.filter(id=self.tickets['public'].id).exists(),
                "Limited user should see public ticket"
            )
            self.assertFalse(
                filtered.filter(id=self.tickets['restricted'].id).exists(),
                "Limited user should not see restricted ticket"
            )
            self.assertFalse(
                filtered.filter(id=self.tickets['confidential'].id).exists(),
                "Limited user should not see confidential ticket"
            )
        finally:
            settings.ROW_SECURITY = original_setting

    def test_ticket_security_with_row_security_disabled(self):
        """Test ticket filtering with ROW_SECURITY disabled"""
        original_setting = getattr(settings, 'ROW_SECURITY', True)
        settings.ROW_SECURITY = False

        try:
            # With ROW_SECURITY disabled, all users should see all tickets
            our_ticket_ids = [t.id for t in self.tickets.values()]
            queryset = Ticket.objects.filter(id__in=our_ticket_ids)
            filtered = Ticket.get_queryset(queryset, self.limited_user)

            # Should see all tickets when security is disabled
            self.assertEqual(filtered.count(), 3)
        finally:
            settings.ROW_SECURITY = original_setting

    def test_comment_security_inherits_from_ticket(self):
        """Test that comment security inherits from parent ticket"""
        original_setting = getattr(settings, 'ROW_SECURITY', True)
        settings.ROW_SECURITY = True

        try:
            # Test admin user
            queryset = Comment.objects.all()
            filtered = Comment.get_queryset(queryset, self.admin_user)

            # Admin should see all comments
            self.assertTrue(
                filtered.filter(id=self.public_comment.id).exists(),
                "Admin should see public comment"
            )
            self.assertTrue(
                filtered.filter(id=self.restricted_comment.id).exists(),
                "Admin should see restricted comment"
            )

            # Test limited user
            queryset = Comment.objects.all()
            filtered = Comment.get_queryset(queryset, self.limited_user)

            # Limited user should only see comment on public ticket
            self.assertTrue(
                filtered.filter(id=self.public_comment.id).exists(),
                "Limited user should see public comment"
            )
            self.assertFalse(
                filtered.filter(id=self.restricted_comment.id).exists(),
                "Limited user should not see restricted comment"
            )
        finally:
            settings.ROW_SECURITY = original_setting

    def test_graphql_info_object_handling(self):
        """Test that GraphQL ResolveInfo objects are handled correctly"""
        # Create a mock ResolveInfo with user
        mock_info = Mock(spec=ResolveInfo)
        mock_info.context = Mock()
        mock_info.context.user = self.limited_user

        original_setting = getattr(settings, 'ROW_SECURITY', True)
        settings.ROW_SECURITY = True

        try:
            # Test that ResolveInfo is properly handled
            test_ticket_ids = [t.id for t in self.tickets.values()]
            queryset = Ticket.objects.filter(id__in=test_ticket_ids)
            filtered = Ticket.get_queryset(queryset, mock_info)

            # Should extract user from ResolveInfo and apply filtering
            self.assertEqual(filtered.count(), 1)
            self.assertTrue(
                filtered.filter(id=self.tickets['public'].id).exists(),
                "Should handle ResolveInfo object correctly"
            )
        finally:
            settings.ROW_SECURITY = original_setting
