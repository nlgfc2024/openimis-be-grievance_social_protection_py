"""
Test restricted view functionality for grievance tickets based on user access levels.
"""
from django.test import TestCase
from core.test_helpers import create_test_interactive_user
from core.models.openimis_graphql_test_case import openIMISGraphQLTestCase, BaseTestContext
from graphene import Schema
from graphene.test import Client

from grievance_social_protection.models import Ticket
from grievance_social_protection.schema import Query
from grievance_social_protection.access_control import GrievanceAccessControl
from grievance_social_protection.tests.test_helpers import (
    setup_grievance_config, restore_grievance_config,
    assign_rights_to_user, get_rights,
)


class RestrictedViewTest(openIMISGraphQLTestCase):
    """Test that sensitive information is hidden based on user access levels"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Create test users with different access levels
        cls.user_restricted = create_test_interactive_user(username='restricted_viewer', roles=[1])
        cls.user_full_viewer = create_test_interactive_user(username='full_viewer', roles=[1])
        cls.user_manager = create_test_interactive_user(username='manager', roles=[1])
        cls.user_no_access = create_test_interactive_user(username='no_access', roles=[1])

        # Create GraphQL schema
        cls.schema = Schema(query=Query)

    def setUp(self):
        """Set up test data"""
        super().setUp()
        self._snapshot = self._setup_test_config()
        self._assign_rights_to_users()
        self._create_test_ticket()

    def _setup_test_config(self):
        """Set up test configuration with restricted categories"""
        cfg = {
            'grievance_types': [
                {
                    'name': 'vbg_complaint',
                    'priority': 'Critical',
                    'permissions': ['restricted_read', 'read', 'create'],
                    'default_flags': ['confidential'],
                    'visible_fields': ['id', 'code', 'status', 'flags']
                },
                {
                    'name': 'public_feedback',
                    'priority': 'Low'
                }
            ],
            'grievance_flags': [
                {
                    'name': 'confidential',
                    'priority': 'Critical',
                    'permissions': ['restricted_read', 'read', 'create']
                },
                'public'
            ],
            # This test exercises category/flag-based restricted-view
            # filtering specifically; opt out of view scoping (which
            # AND-s on top) so it isn't also gated by district/creator scope.
            'view_scope': {'default': 'all_cases'},
        }
        return setup_grievance_config(cfg)

    def _assign_rights_to_users(self):
        """Assign rights to test users through roles"""
        vbg_rights = get_rights('processed_categories', 'vbg_complaint')
        conf_rights = get_rights('processed_flags', 'confidential')
        base = 127000

        # Restricted viewer - only restricted_read
        restricted_ids = [base]
        if vbg_rights.get('restricted_read'):
            restricted_ids.append(vbg_rights['restricted_read'])
        if conf_rights.get('restricted_read'):
            restricted_ids.append(conf_rights['restricted_read'])
        assign_rights_to_user(self.user_restricted, restricted_ids, 'TestRestrictedRole')

        # Full viewer - read access
        viewer_ids = [base]
        if vbg_rights.get('read'):
            viewer_ids.append(vbg_rights['read'])
        if conf_rights.get('read'):
            viewer_ids.append(conf_rights['read'])
        assign_rights_to_user(self.user_full_viewer, viewer_ids, 'TestViewerRole')

        # Manager - all rights
        manager_ids = [base] + list(vbg_rights.values()) + list(conf_rights.values())
        assign_rights_to_user(self.user_manager, manager_ids, 'TestManagerRole')

        # No access user - only base query permission
        assign_rights_to_user(self.user_no_access, [base], 'TestNoAccessRole')

    def _create_test_ticket(self):
        """Create a test ticket with sensitive information"""
        ticket = Ticket(
            code='TEST001',
            title='Sensitive VBG Case',
            description='This contains very sensitive personal information that should be restricted',
            category='vbg_complaint',
            flags='confidential',
            priority='Critical',
            status='OPEN',
            channel='Web',
            reporter_id=1,
            reporter_type_id=1,
            resolution='This is the confidential resolution details'
        )
        ticket.save(user=self.user_manager.user)
        self.ticket = ticket

        public_ticket = Ticket(
            code='PUBLIC001',
            title='Public Feedback',
            description='This is public information',
            category='public_feedback',
            flags='public',
            priority='Low',
            status='OPEN',
            channel='Web'
        )
        public_ticket.save(user=self.user_manager.user)
        self.public_ticket = public_ticket

    def tearDown(self):
        """Clean up test data and restore config"""
        if hasattr(self, 'ticket'):
            self.ticket.delete(user=self.user_manager.user)
        if hasattr(self, 'public_ticket'):
            self.public_ticket.delete(user=self.user_manager.user)
        restore_grievance_config(self._snapshot)
        super().tearDown()

    def _execute_ticket_query(self, user):
        """Execute GraphQL query to get ticket details"""
        query = '''
            query {
                tickets {
                    edges {
                        node {
                            id
                            code
                            title
                            description
                            category
                            flags
                            priority
                            status
                            resolution
                            accessLevel
                        }
                    }
                }
            }
        '''

        client = Client(self.schema)
        context = BaseTestContext(user)
        result = client.execute(query, context=context.get_request())

        return result

    def test_restricted_viewer_sees_limited_info(self):
        """Test that restricted viewer sees only basic information"""
        result = self._execute_ticket_query(self.user_restricted)

        self.assertIsNotNone(result)
        self.assertIn('data', result)

        edges = result['data']['tickets']['edges']

        # Find the VBG ticket
        vbg_ticket = next(
            (edge['node'] for edge in edges if edge['node']['code'] == 'TEST001'),
            None
        )

        if vbg_ticket:
            # Should see basic info
            self.assertEqual(vbg_ticket['code'], 'TEST001')
            self.assertEqual(vbg_ticket['title'], '[Restricted]')  # Title should be restricted
            self.assertEqual(vbg_ticket['category'], '[Restricted]')  # Category should be restricted
            self.assertEqual(vbg_ticket['status'], 'OPEN')
            self.assertIsNone(vbg_ticket['priority'])  # Priority is restricted

            # Should NOT see sensitive info
            self.assertEqual(vbg_ticket['description'], '[Restricted]')
            self.assertEqual(vbg_ticket['resolution'], '[Restricted]')

            # Should see access level
            self.assertEqual(vbg_ticket['accessLevel'], 'restricted')

    def test_full_viewer_sees_all_info(self):
        """Test that full viewer sees all information"""
        result = self._execute_ticket_query(self.user_full_viewer)

        edges = result['data']['tickets']['edges']
        vbg_ticket = next(
            (edge['node'] for edge in edges if edge['node']['code'] == 'TEST001'),
            None
        )

        if vbg_ticket:
            # Should see all info including sensitive data
            self.assertEqual(vbg_ticket['description'],
                             'This contains very sensitive personal information that should be restricted')
            self.assertEqual(vbg_ticket['resolution'],
                             'This is the confidential resolution details')
            self.assertEqual(vbg_ticket['accessLevel'], 'read')

    def test_no_access_user_sees_only_public(self):
        """Test that user with no access sees only public tickets"""
        result = self._execute_ticket_query(self.user_no_access)

        edges = result['data']['tickets']['edges']

        # Should only see public ticket
        codes = [edge['node']['code'] for edge in edges]
        self.assertIn('PUBLIC001', codes)
        self.assertNotIn('TEST001', codes)

    def test_manager_has_full_access(self):
        """Test that manager sees everything and has full access"""
        result = self._execute_ticket_query(self.user_manager)

        edges = result['data']['tickets']['edges']
        vbg_ticket = next(
            (edge['node'] for edge in edges if edge['node']['code'] == 'TEST001'),
            None
        )

        if vbg_ticket:
            # Should see all info
            self.assertNotEqual(vbg_ticket['description'], '[Restricted]')
            self.assertNotEqual(vbg_ticket['resolution'], '[Restricted]')
            self.assertEqual(vbg_ticket['accessLevel'], 'full')

    def test_public_ticket_accessible_to_all(self):
        """Test that tickets without restrictions are accessible to all"""
        # Even user with no rights should see full info for public tickets
        result = self._execute_ticket_query(self.user_no_access)

        edges = result['data']['tickets']['edges']
        public_data = next(
            (edge['node'] for edge in edges if edge['node']['code'] == 'PUBLIC001'),
            None
        )

        self.assertIsNotNone(public_data)
        self.assertEqual(public_data['description'], 'This is public information')
        self.assertEqual(public_data['accessLevel'], 'full')  # No restrictions

    def test_access_level_calculation(self):
        """Test access level calculation based on actual rights"""
        # Test with real user rights
        access_level = GrievanceAccessControl.get_user_access_level(
            self.user_restricted, 'vbg_complaint', 'confidential'
        )
        self.assertEqual(access_level, 'restricted')

        access_level = GrievanceAccessControl.get_user_access_level(
            self.user_full_viewer, 'vbg_complaint', 'confidential'
        )
        self.assertEqual(access_level, 'read')

        access_level = GrievanceAccessControl.get_user_access_level(
            self.user_manager, 'vbg_complaint', 'confidential'
        )
        self.assertEqual(access_level, 'full')

        access_level = GrievanceAccessControl.get_user_access_level(
            self.user_no_access, 'vbg_complaint', 'confidential'
        )
        self.assertEqual(access_level, 'none')


class RestrictedViewIntegrationTest(TestCase):
    """Integration tests for restricted view with actual rights checking"""

    def setUp(self):
        """Set up test environment"""
        self.user = create_test_interactive_user(username='test_rights_user', roles=[1])
        self._snapshot = self._setup_config_and_rights()

    def _setup_config_and_rights(self):
        """Set up configuration and create rights"""
        cfg = {
            'grievance_types': [{
                'name': 'test_category',
                'permissions': ['restricted_read', 'read', 'create']
            }]
        }
        return setup_grievance_config(cfg)

    def test_rights_based_filtering(self):
        """Test that rights-based filtering works correctly"""
        # Give user only restricted_read right
        cat_rights = get_rights('processed_categories', 'test_category')
        if self.user.i_user and cat_rights.get('restricted_read'):
            assign_rights_to_user(
                self.user,
                [cat_rights['restricted_read']],
                role_name='TestRestrictedOnly',
            )

            # User should have restricted access
            access_level = GrievanceAccessControl.get_user_access_level(
                self.user, 'test_category'
            )
            self.assertEqual(access_level, 'restricted')

            # User should be able to check restricted access
            self.assertTrue(
                GrievanceAccessControl.check_category_access(
                    self.user, 'test_category', 'restricted_read'
                )
            )
            self.assertFalse(
                GrievanceAccessControl.check_category_access(
                    self.user, 'test_category', 'read'
                )
            )

    def tearDown(self):
        """Clean up test data"""
        restore_grievance_config(self._snapshot)
        super().tearDown()
