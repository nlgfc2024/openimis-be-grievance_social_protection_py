"""
Tests for the new GrievanceTypeConfigurationGQLType resolvers added
to the EXISTING grievanceConfig query — no new query. Also regression-covers
the grievance_category_staff_roles fix: default_attending_staff_role_ids
values can now be a plain role-id list (legacy) or a {role_ids, strategy, scope} dict, 
and the resolver must handle both.
"""
from django.test import TestCase

from grievance_social_protection.apps import TicketConfig
from grievance_social_protection.gql_queries import GrievanceTypeConfigurationGQLType


class GrievanceConfigQueryTest(TestCase):

    ATTRS = (
        'ticket_statuses', 'referral_entities', 'participant_fields',
        'search_filters', 'search_result_columns', 'sla', 'enable_export',
        'default_attending_staff_role_ids', 'processed_categories',
    )

    def setUp(self):
        self._snapshot = {attr: getattr(TicketConfig, attr) for attr in self.ATTRS}
        self.config_type = GrievanceTypeConfigurationGQLType()

    def tearDown(self):
        for attr, value in self._snapshot.items():
            setattr(TicketConfig, attr, value)

    def test_resolve_ticket_statuses(self):
        TicketConfig.ticket_statuses = [
            {'code': 'OPEN', 'label': 'Open', 'initial': True},
            {'code': 'REFERRED', 'label': 'Referred', 'requires_referral_entity': True},
            {'code': 'RESOLVED', 'label': 'Resolved', 'terminal': True},
        ]
        statuses = self.config_type.resolve_ticket_statuses(None)
        by_code = {s.code: s for s in statuses}

        self.assertTrue(by_code['OPEN'].initial)
        self.assertFalse(by_code['OPEN'].terminal)
        self.assertTrue(by_code['REFERRED'].requires_referral_entity)
        self.assertTrue(by_code['RESOLVED'].terminal)

    def test_resolve_referral_entities(self):
        TicketConfig.referral_entities = ['Police', 'Court']
        self.assertEqual(self.config_type.resolve_referral_entities(None), ['Police', 'Court'])

    def test_resolve_participant_fields(self):
        TicketConfig.participant_fields = [
            {'key': 'nationalId', 'label': 'National ID', 'source': 'reporter.jsonExt.national_id'},
        ]
        fields = self.config_type.resolve_participant_fields(None)
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].key, 'nationalId')
        self.assertEqual(fields[0].source, 'reporter.jsonExt.national_id')

    def test_resolve_search_filters(self):
        TicketConfig.search_filters = ['formNumber', 'status']
        self.assertEqual(self.config_type.resolve_search_filters(None), ['formNumber', 'status'])

    def test_resolve_search_result_columns(self):
        TicketConfig.search_result_columns = [{'key': 'status', 'label': 'Status'}]
        columns = self.config_type.resolve_search_result_columns(None)
        self.assertEqual(len(columns), 1)
        self.assertEqual(columns[0].key, 'status')
        self.assertEqual(columns[0].label, 'Status')

    def test_resolve_sla_returns_config_as_is(self):
        TicketConfig.sla = {'row_colors': {'within_sla': 'yellow', 'breached': 'red', 'resolved': 'white'}}
        self.assertEqual(
            self.config_type.resolve_sla(None)['row_colors']['breached'], 'red')

    def test_resolve_enable_export_casts_to_bool(self):
        TicketConfig.enable_export = True
        self.assertIs(self.config_type.resolve_enable_export(None), True)

    def test_staff_roles_handles_legacy_list_config(self):
        TicketConfig.default_attending_staff_role_ids = {'Default': [1, 2]}
        roles = self.config_type.resolve_grievance_category_staff_roles(None)
        self.assertEqual(len(roles), 1)
        self.assertEqual(roles[0].category, 'Default')
        self.assertEqual(roles[0].role_ids, [1, 2])
        self.assertEqual(roles[0].strategy, 'random')
        self.assertIsNone(roles[0].scope)

    def test_staff_roles_handles_dict_config(self):
        """Regression: the pre-BE-18 resolver would pass a dict where a list was expected."""
        TicketConfig.default_attending_staff_role_ids = {
            'Claims': {'role_ids': [5], 'strategy': 'random', 'scope': 'district'},
        }
        roles = self.config_type.resolve_grievance_category_staff_roles(None)
        self.assertEqual(len(roles), 1)
        self.assertEqual(roles[0].category, 'Claims')
        self.assertEqual(roles[0].role_ids, [5])
        self.assertEqual(roles[0].strategy, 'random')
        self.assertEqual(roles[0].scope, 'district')

    def test_staff_roles_handles_mixed_legacy_and_dict_configs(self):
        TicketConfig.default_attending_staff_role_ids = {
            'Default': [1, 2],
            'Claims': {'role_ids': [5], 'strategy': 'round_robin'},
        }
        roles = {r.category: r for r in self.config_type.resolve_grievance_category_staff_roles(None)}
        self.assertEqual(roles['Default'].role_ids, [1, 2])
        self.assertEqual(roles['Default'].strategy, 'random')
        self.assertEqual(roles['Claims'].role_ids, [5])
        self.assertEqual(roles['Claims'].strategy, 'round_robin')

    def test_category_workflows_omits_categories_without_workflow(self):
        TicketConfig.processed_categories = {
            'Unpaid wages': {'workflow': None},
        }
        self.assertEqual(self.config_type.resolve_grievance_category_workflows(None), [])

    def test_category_workflows_resolves_configured_workflow(self):
        TicketConfig.processed_categories = {
            'Unpaid wages': {'workflow': None},
            'Unpaid wages > Partial wages': {
                'workflow': {
                    'maker_checker': True,
                    'on_approved_signal': 'payments.arrears.create',
                    'on_resolve_task': 'tasks_management',
                },
            },
        }
        workflows = self.config_type.resolve_grievance_category_workflows(None)
        self.assertEqual(len(workflows), 1)
        workflow = workflows[0]
        self.assertEqual(workflow.category, 'Unpaid wages > Partial wages')
        self.assertTrue(workflow.maker_checker)
        self.assertEqual(workflow.on_approved_signal, 'payments.arrears.create')
        self.assertEqual(workflow.on_resolve_task, 'tasks_management')

    def test_category_workflows_defaults_maker_checker_false(self):
        TicketConfig.processed_categories = {
            'Partial wages': {'workflow': {'on_resolve_task': 'tasks_management'}},
        }
        workflows = self.config_type.resolve_grievance_category_workflows(None)
        self.assertEqual(len(workflows), 1)
        self.assertFalse(workflows[0].maker_checker)
        self.assertIsNone(workflows[0].on_approved_signal)
