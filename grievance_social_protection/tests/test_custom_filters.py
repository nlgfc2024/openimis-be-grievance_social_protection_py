"""
Tests for TicketCustomFilterWizard — advanced search over
ticket.json_ext (denormalised participant/location fields).
"""
import json

from django.test import TestCase

from core.custom_filters import CustomFilterRegistryPoint
from core.test_helpers import LogInHelper

from grievance_social_protection.apps import MODULE_NAME, TicketConfig
from grievance_social_protection.custom_filters import TicketCustomFilterWizard
from grievance_social_protection.models import Ticket


class TicketCustomFilterWizardRegistrationTest(TestCase):
    """The wizard is registered under this module and survives a config reload."""

    def test_wizard_is_registered(self):
        registered = CustomFilterRegistryPoint.REGISTERED_CUSTOM_FILTER_WIZARDS.get(MODULE_NAME, [])
        class_names = [entry['class_reference'].__name__ for entry in registered]
        self.assertIn('TicketCustomFilterWizard', class_names)

    def test_wizard_reports_ticket_object_type(self):
        wizard = TicketCustomFilterWizard()
        self.assertEqual(wizard.get_type_of_object(), 'Ticket')

    def test_wizard_survives_reinitialization(self):
        """Re-running registration (as happens on config reload) stays a single entry."""
        # noqa: mirrors the private-method test convention already used in this suite
        TicketConfig._TicketConfig__initialize_custom_filters()
        TicketConfig._TicketConfig__initialize_custom_filters()
        registered = CustomFilterRegistryPoint.REGISTERED_CUSTOM_FILTER_WIZARDS.get(MODULE_NAME, [])
        class_names = [entry['class_reference'].__name__ for entry in registered]
        self.assertEqual(class_names.count('TicketCustomFilterWizard'), 1)


class TicketCustomFilterWizardApplyFilterTest(TestCase):
    """apply_filter_to_queryset filters tickets by their own json_ext."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        cls.wizard = TicketCustomFilterWizard()

    def setUp(self):
        self.ticket_match = Ticket(
            code='CF-TEST-001', title='Matching ticket',
            json_ext={'form_number': 'FN-001', 'location_name': 'Village A', 'days_worked': 3},
        )
        self.ticket_match.save(user=self.user)
        self.ticket_other = Ticket(
            code='CF-TEST-002', title='Other ticket',
            json_ext={'form_number': 'FN-002', 'location_name': 'Village B', 'days_worked': 5},
        )
        self.ticket_other.save(user=self.user)
        self.base_qs = Ticket.objects.filter(code__startswith='CF-TEST-')

    def tearDown(self):
        Ticket.objects.filter(code__startswith='CF-TEST-').delete()

    def test_filters_by_string_field(self):
        result = self.wizard.apply_filter_to_queryset(
            ["form_number__string='FN-001'"], self.base_qs)
        codes = list(result.values_list('code', flat=True))
        self.assertIn('CF-TEST-001', codes)
        self.assertNotIn('CF-TEST-002', codes)

    def test_filters_by_derived_string_field(self):
        result = self.wizard.apply_filter_to_queryset(
            ["location_name__string='Village B'"], self.base_qs)
        codes = list(result.values_list('code', flat=True))
        self.assertIn('CF-TEST-002', codes)
        self.assertNotIn('CF-TEST-001', codes)

    def test_filters_by_integer_field(self):
        result = self.wizard.apply_filter_to_queryset(
            ["days_worked__integer=5"], self.base_qs)
        codes = list(result.values_list('code', flat=True))
        self.assertIn('CF-TEST-002', codes)
        self.assertNotIn('CF-TEST-001', codes)

    def test_no_match_returns_empty(self):
        result = self.wizard.apply_filter_to_queryset(
            ["form_number__string='UNKNOWN'"], self.base_qs)
        self.assertEqual(result.count(), 0)


class TicketCustomFilterWizardLoadDefinitionTest(TestCase):
    """load_definition exposes both the Individual schema fields and grievance extras."""

    def test_load_definition_includes_expected_fields(self):
        """
        Control individual_schema explicitly rather than assume the ambient
        environment's configured schema contains specific fields — it's a
        live ModuleConfiguration value and varies by deployment.
        """
        from collections import namedtuple
        from individual.apps import IndividualConfig

        original_schema = IndividualConfig.individual_schema
        IndividualConfig.individual_schema = json.dumps({
            "properties": {
                "form_number": {"type": "string"},
                "national_id": {"type": "string"},
                "location_name": {"type": "string"},
                "traditional_authority_name": {"type": "string"},
                "group_village_head_name": {"type": "string"},
            }
        })
        try:
            wizard = TicketCustomFilterWizard()
            tuple_type = namedtuple('Ticket', ['field', 'filter', 'type'])
            definitions = wizard.load_definition(tuple_type)
            fields = {d.field for d in definitions}
            # Individual schema fields required by the acceptance criteria.
            for expected in ('form_number', 'national_id', 'location_name',
                             'traditional_authority_name', 'group_village_head_name'):
                self.assertIn(expected, fields)
            # Grievance-specific derived extras not in the Individual schema.
            for expected in ('district_code', 'micro_catchment', 'project_name', 'days_worked'):
                self.assertIn(expected, fields)
        finally:
            IndividualConfig.individual_schema = original_schema
