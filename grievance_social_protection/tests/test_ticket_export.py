"""
Tests for tickets_export (BE-19), backed by core's ExportableQueryMixin —
reuses resolve_tickets (and therefore its category/flag access control and
BE-16 view scoping) as the base queryset before writing the CSV file.

Executed via a real GraphQL Client (not a bare resolver call): resolve_tickets
calls graphene_django_optimizer.query(query, info), which needs a genuine
ResolveInfo with real field-selection AST — a Mock(spec=ResolveInfo) makes it
hang trying to introspect the mock.
"""
import json

from django.core.exceptions import PermissionDenied
from django.test import TestCase
from graphene import Schema
from graphene.test import Client

from location.models import Location, UserDistrict

from core.models import ExportableQueryModel
from core.models.openimis_graphql_test_case import BaseTestContext
from core.test_helpers import LogInHelper, create_test_interactive_user, create_test_role
from grievance_social_protection.apps import TicketConfig
from grievance_social_protection.models import Ticket
from grievance_social_protection.schema import Query
from grievance_social_protection.tests.test_helpers import (
    setup_grievance_config, restore_grievance_config, assign_rights_to_user,
)


class TicketExportTest(TestCase):

    _config_snapshot = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = Schema(query=Query)
        cls.user = LogInHelper().get_or_create_user_api()
        cls._config_snapshot = setup_grievance_config({
            'grievance_types': ['Default', 'Other'],
            'enable_export': True,
            # These tests exercise filters/columns/gating, not scoping
            # itself; opt out of BE-16 view scoping (which AND-s on top).
            'view_scope': {'default': 'all_cases'},
        })

        cls.district_a = Location.objects.create(code='BE19-DA', name='District A', type='R')

        cls.ticket_default = Ticket(
            title='Default ticket', category='Default', status='OPEN', code='TCK-DEF',
            json_ext={'district_code': cls.district_a.code},
        )
        cls.ticket_default.save(user=cls.user)
        cls.ticket_other = Ticket(
            title='Other ticket', category='Other', status='RESOLVED', code='TCK-OTH',
        )
        cls.ticket_other.save(user=cls.user)

    @classmethod
    def tearDownClass(cls):
        Ticket.objects.filter(id__in=[cls.ticket_default.id, cls.ticket_other.id]).delete()
        restore_grievance_config(cls._config_snapshot)
        Location.objects.filter(code__startswith='BE19-').delete()
        super().tearDownClass()

    # Filter args written as bare identifiers (GraphQL enum literals, e.g.
    # status: RESOLVED) rather than quoted strings.
    _ENUM_FILTER_ARGS = frozenset({'status'})

    def _export_query(self, fields, fields_columns, **filter_args):
        fields_literal = json.dumps(fields)
        columns_literal = json.dumps(fields_columns).replace('"', '\\"')
        filters = ''.join(
            f', {k}: {v}' if k in self._ENUM_FILTER_ARGS else f', {k}: "{v}"'
            for k, v in filter_args.items()
        )
        return f'''
            {{
                ticketsExport(fields: {fields_literal}, fieldsColumns: "{columns_literal}"{filters})
            }}
        '''

    def _export(self, user=None, fields=None, fields_columns=None, **filter_args):
        query = self._export_query(
            fields or ['code', 'title', 'status'],
            fields_columns or {'code': 'Code', 'title': 'Title', 'status': 'Status'},
            **filter_args,
        )
        client = Client(self.schema)
        context = BaseTestContext(user or self.user)
        result = client.execute(query, context=context.get_request())
        return result

    @staticmethod
    def _content_for(result):
        assert not result.get('errors'), result.get('errors')
        filename = result['data']['ticketsExport']
        export = ExportableQueryModel.objects.get(name=filename)
        return export.content.read().decode('utf-8')

    def test_export_returns_configured_columns_and_matching_rows(self):
        content = self._content_for(self._export())
        self.assertIn('Code,Title,Status', content)
        self.assertIn('Default ticket', content)
        self.assertIn('Other ticket', content)

    def test_export_honours_first_class_filter(self):
        content = self._content_for(self._export(status='RESOLVED'))
        self.assertNotIn('Default ticket', content)
        self.assertIn('Other ticket', content)

    def test_export_honours_category_filter(self):
        content = self._content_for(self._export(category='Default'))
        self.assertIn('Default ticket', content)
        self.assertNotIn('Other ticket', content)

    def test_export_disabled_raises(self):
        original = TicketConfig.enable_export
        TicketConfig.enable_export = False
        try:
            result = self._export()
            self.assertTrue(result.get('errors'))
        finally:
            TicketConfig.enable_export = original

    def test_export_respects_view_scoping(self):
        """
        A district-scoped caller only exports tickets in their own district
        (BE-16 AND-ed in): scoped to district_a, they see only the ticket
        with a matching district_code, not the other one.

        Note: deliberately keeps at least one visible ticket rather than
        testing a fully scoped-out (zero-row) caller — core's
        ExportableQueryModel.create_csv_export raises EmptyResultSet for a
        queryset with zero possible rows (qs.query.sql_with_params(), used
        only to log the SQL, isn't guarded for Django's .none() case). This
        is a pre-existing bug in core shared by every ExportableQueryMixin
        consumer, not specific to grievance; left as a known limitation.
        """
        district_role = create_test_role(name='BE19DistrictRole')
        district_user = create_test_interactive_user(username='be19_district', roles=[district_role.id])
        assign_rights_to_user(district_user, TicketConfig.gql_query_tickets_perms, 'BE19DistrictExportRights')
        UserDistrict.objects.create(
            user=district_user.i_user, location=self.district_a, audit_user_id=district_user.i_user.id,
        )

        original_view_scope = TicketConfig.view_scope
        TicketConfig.view_scope = {
            'district_scoped_roles': [district_role.id],
            'default': 'district_scoped',
        }
        try:
            content = self._content_for(self._export(user=district_user))
            self.assertIn('Default ticket', content)
            self.assertNotIn('Other ticket', content)
        finally:
            TicketConfig.view_scope = original_view_scope

    def test_export_unfolds_json_ext_columns(self):
        ticket = Ticket(
            title='With json_ext', category='Default', status='OPEN', code='TCK-JE',
            json_ext={'form_number': 'FN-42'},
        )
        ticket.save(user=self.user)
        try:
            content = self._content_for(self._export(
                fields=['code', 'json_ext'],
                fields_columns={'code': 'Code', 'form_number': 'Form Number'},
            ))
            self.assertIn('Form Number', content)
            self.assertIn('FN-42', content)
        finally:
            ticket.delete(username=self.user.username)
