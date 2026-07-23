import copy
import json

from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models import ModuleConfiguration
from grievance_social_protection.apps import TicketConfig, DEFAULT_CFG


class ConfigProcessingTest(TestCase):
    """Test configuration processing for unified category and flag formats"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    def test_process_simple_string_categories(self):
        """Test processing simple string category list"""
        cfg = {
            'grievance_types': ['complaint', 'feedback', 'appeal']
        }

        TicketConfig._TicketConfig__process_unified_categories(cfg)

        # Check flat list maintained - includes 'uncategorized' added by default
        self.assertEqual(cfg['grievance_types'], ['uncategorized', 'complaint', 'feedback', 'appeal'])

        # Check processed structure - includes 'uncategorized'
        processed = cfg['processed_categories']
        self.assertEqual(len(processed), 4)

        # Check category details
        self.assertIn('complaint', processed)
        self.assertEqual(processed['complaint']['priority'], 'Medium')
        self.assertEqual(processed['complaint']['permissions'], [])
        self.assertEqual(processed['complaint']['parent'], None)

    def test_process_mixed_categories(self):
        """Test processing mixed string and dict categories"""
        cfg = {
            'grievance_types': [
                'simple',
                {
                    'name': 'complex',
                    'priority': 'High',
                    'permissions': ['read', 'create']
                },
                {
                    'name': 'detailed',
                    'priority': 'Critical',
                    'permissions': ['update', 'delete'],
                    'default_flags': ['urgent']
                }
            ]
        }

        TicketConfig._TicketConfig__process_unified_categories(cfg)

        # Check flat list - includes 'uncategorized' added by default
        self.assertEqual(set(cfg['grievance_types']), {'uncategorized', 'simple', 'complex', 'detailed'})

        # Check processed details
        processed = cfg['processed_categories']

        # Simple string
        self.assertEqual(processed['simple']['permissions'], [])

        # List permissions
        self.assertEqual(processed['complex']['priority'], 'High')
        self.assertEqual(processed['complex']['permissions'], ['read', 'create'])

        # Another list permissions format
        self.assertEqual(processed['detailed']['priority'], 'Critical')
        self.assertEqual(processed['detailed']['permissions'], ['update', 'delete'])
        self.assertEqual(processed['detailed']['default_flags'], ['urgent'])

    def test_process_hierarchical_categories(self):
        """Test processing hierarchical category structure"""
        cfg = {
            'grievance_types': [
                {
                    'name': 'parent',
                    'priority': 'High',
                    'permissions': ['read', 'create'],
                    'default_flags': ['important'],
                    'children': [
                        {
                            'name': 'child1',
                            'permissions': ['update']  # Override parent permissions
                        },
                        'child2',  # Simple string child
                        {
                            'name': 'child3',
                            'priority': 'Critical',  # Override parent priority
                        }
                    ]
                }
            ]
        }

        TicketConfig._TicketConfig__process_unified_categories(cfg)

        # Check flat list includes all levels - plus 'uncategorized' added by default
        expected = {'uncategorized', 'parent', 'parent > child1', 'parent > child2', 'parent > child3'}
        self.assertEqual(set(cfg['grievance_types']), expected)

        processed = cfg['processed_categories']

        # Check parent
        self.assertEqual(processed['parent']['priority'], 'High')
        self.assertEqual(processed['parent']['children'], {
            'child1': 'parent > child1',
            'child2': 'parent > child2',
            'child3': 'parent > child3'
        })

        # Check child inheritance
        self.assertEqual(processed['parent > child1']['parent'], 'parent')
        self.assertEqual(processed['parent > child1']['default_flags'], ['important'])
        self.assertEqual(processed['parent > child1']['permissions'], ['update'])

        self.assertEqual(processed['parent > child3']['default_flags'], ['important'])  # Inherited
        self.assertEqual(processed['parent > child3']['permissions'], ['read', 'create'])  # Inherited
        self.assertEqual(processed['parent > child3']['priority'], 'Critical')

        # Check simple string child
        self.assertEqual(processed['parent > child2']['priority'], 'High')  # Inherited
        self.assertEqual(processed['parent > child2']['permissions'], ['read', 'create'])  # Inherited from parent
        self.assertEqual(processed['parent > child2']['default_flags'], ['important'])  # Inherited

    def test_process_flags(self):
        """Test processing flag configurations"""
        cfg = {
            'grievance_flags': [
                'simple_flag',
                {
                    'name': 'complex_flag',
                    'priority': 'High',
                    'permissions': ['read', 'update']
                }
            ]
        }

        TicketConfig._TicketConfig__process_unified_flags(cfg)

        # Check flat list
        self.assertEqual(cfg['grievance_flags'], ['simple_flag', 'complex_flag'])

        processed = cfg['processed_flags']

        # Simple flag
        self.assertEqual(processed['simple_flag']['permissions'], [])

        # List permissions - stored as-is
        self.assertEqual(processed['complex_flag']['priority'], 'High')
        self.assertEqual(processed['complex_flag']['permissions'], ['read', 'update'])

    def test_category_resolution_times(self):
        """Test processing categories with resolution times"""
        cfg = {
            'grievance_types': [
                {
                    'name': 'urgent',
                    'resolution_times': '1,0',  # 1 day
                    'children': [
                        {
                            'name': 'very_urgent',
                            'resolution_times': '0,12'  # 12 hours
                        },
                        {
                            'name': 'less_urgent'
                            # Inherits parent's 1,0 during processing
                        }
                    ]
                },
                'normal'  # No resolution time specified
            ],
            'resolution_times': '5,0'  # Global default
        }

        TicketConfig._TicketConfig__process_unified_categories(cfg)

        processed = cfg['processed_categories']

        # Check resolution times are stored
        self.assertEqual(processed['urgent']['resolution_times'], '1,0')
        self.assertEqual(processed['urgent > very_urgent']['resolution_times'], '0,12')
        # Child without resolution_times inherits from parent during processing
        self.assertEqual(processed['urgent > less_urgent']['resolution_times'], '1,0')
        # Simple category should not have resolution_times
        self.assertIsNone(processed['normal']['resolution_times'])

    def test_resolution_times_with_mixed_configuration(self):
        """Test resolution times with both category-specific and default_resolution"""
        cfg = {
            'grievance_types': [
                {
                    'name': 'priority_cat',
                    'resolution_times': '2,0'  # Category-specific
                },
                'legacy_cat'
            ],
            'default_resolution': {
                'priority_cat': '3,0',  # Should be overridden by category-specific
                'legacy_cat': '4,0'     # Should be used
            },
            'resolution_times': '5,0'
        }

        TicketConfig._TicketConfig__process_unified_categories(cfg)
        TicketConfig._TicketConfig__validate_grievance_default_resolution_time(cfg)

        # Check unified resolution times mapping
        unified = cfg.get('unified_resolution_times', {})
        self.assertEqual(unified['priority_cat'], '2,0')  # Category-specific wins
        self.assertEqual(unified['legacy_cat'], '4,0')    # From default_resolution

    def test_category_default_flags_must_exist_in_grievance_flags(self):
        """Saving config where a category references an undefined default flag must fail"""
        invalid_config = {
            "grievance_types": [
                {
                    "name": "complaint",
                    "default_flags": ["urgent"]
                }
            ],
            "grievance_flags": ["sensitive"]
        }

        mc = ModuleConfiguration(
            module="grievance_social_protection",
            layer="be",
            version="1.0.0",
            config=json.dumps(invalid_config),
        )

        with self.assertRaises(ValidationError) as ctx:
            mc.save()

        self.assertIn('urgent', str(ctx.exception))
        self.assertIn('complaint', str(ctx.exception))

    def test_category_default_flags_valid_config_saves(self):
        """Saving config where default_flags are all defined in grievance_flags should succeed"""
        valid_config = {
            "grievance_types": [
                {
                    "name": "complaint",
                    "default_flags": ["urgent"]
                }
            ],
            "grievance_flags": ["urgent", "sensitive"]
        }

        mc = ModuleConfiguration(
            module="grievance_social_protection",
            layer="be",
            version="1.0.0",
            config=json.dumps(valid_config),
        )
        mc.save()
        self.assertIsNotNone(mc.pk)
        mc.delete()

    def test_nested_category_inherited_default_flags_must_exist(self):
        """Inherited default_flags from parent categories must also be validated on save"""
        invalid_config = {
            "grievance_types": [
                {
                    "name": "parent",
                    "default_flags": ["nonexistent"],
                    "children": ["child"]
                }
            ],
            "grievance_flags": ["other_flag"]
        }

        mc = ModuleConfiguration(
            module="grievance_social_protection",
            layer="be",
            version="1.0.0",
            config=json.dumps(invalid_config),
        )

        with self.assertRaises(ValidationError) as ctx:
            mc.save()

        self.assertIn('nonexistent', str(ctx.exception))

    def test_default_flags_with_no_grievance_flags_configured(self):
        """default_flags on a category with no grievance_flags configured must fail"""
        invalid_config = {
            "grievance_types": [
                {
                    "name": "complaint",
                    "default_flags": ["urgent"]
                }
            ],
            "grievance_flags": []
        }

        mc = ModuleConfiguration(
            module="grievance_social_protection",
            layer="be",
            version="1.0.0",
            config=json.dumps(invalid_config),
        )

        with self.assertRaises(ValidationError) as ctx:
            mc.save()

        self.assertIn('urgent', str(ctx.exception))
        self.assertIn('complaint', str(ctx.exception))

    def test_admin_form_rejects_invalid_config(self):
        """Regression: admin form should display validation error inline, not save"""
        from django.contrib.admin.sites import AdminSite
        from django.contrib.admin.options import ModelAdmin

        invalid_config = {
            "grievance_types": [
                {
                    "name": "complaint",
                    "default_flags": ["urgent"]
                }
            ],
            "grievance_flags": ["sensitive"]
        }

        admin = ModelAdmin(ModuleConfiguration, AdminSite())
        Form = admin.get_form(request=None)
        form = Form(data={
            'module': 'grievance_social_protection',
            'layer': 'be',
            'version': '1.0.0',
            'config': json.dumps(invalid_config),
        })

        self.assertFalse(form.is_valid())
        self.assertIn('urgent', str(form.errors))

    # ── Extended config keys & validation ────────────────────────

    def test_default_cfg_has_extended_config_keys(self):
        """The new config blocks ship in DEFAULT_CFG."""
        for key in ('ticket_statuses', 'referral_entities', 'view_scope', 'sla',
                    'notifications', 'participant_fields', 'search_filters',
                    'search_result_columns', 'enable_export'):
            self.assertIn(key, DEFAULT_CFG)

    def test_default_cfg_passes_validators(self):
        """An empty override merges onto DEFAULT_CFG — it must validate cleanly."""
        cfg = copy.deepcopy(DEFAULT_CFG)
        TicketConfig._TicketConfig__validate_ticket_statuses(cfg)
        TicketConfig._TicketConfig__validate_referral_entities(cfg)
        TicketConfig._TicketConfig__validate_attending_staff_roles(cfg)
        TicketConfig._TicketConfig__validate_view_scope(cfg)

    def test_ticket_statuses_reject_unknown_code(self):
        with self.assertRaises(ValidationError):
            TicketConfig._TicketConfig__validate_ticket_statuses(
                {'ticket_statuses': [{'code': 'BOGUS', 'initial': True}]})

    def test_ticket_statuses_require_single_initial(self):
        with self.assertRaises(ValidationError):
            TicketConfig._TicketConfig__validate_ticket_statuses(
                {'ticket_statuses': [{'code': 'OPEN', 'initial': True},
                                     {'code': 'RESOLVED', 'initial': True}]})
        with self.assertRaises(ValidationError):
            TicketConfig._TicketConfig__validate_ticket_statuses(
                {'ticket_statuses': [{'code': 'OPEN'}, {'code': 'RESOLVED'}]})

    def test_ticket_statuses_single_initial_ok(self):
        TicketConfig._TicketConfig__validate_ticket_statuses(
            {'ticket_statuses': [{'code': 'OPEN', 'initial': True},
                                 {'code': 'RESOLVED', 'terminal': True}]})

    def test_referral_entities_required_when_referred_enabled(self):
        with self.assertRaises(ValidationError):
            TicketConfig._TicketConfig__validate_referral_entities(
                {'ticket_statuses': [{'code': 'REFERRED'}], 'referral_entities': []})
        TicketConfig._TicketConfig__validate_referral_entities(
            {'ticket_statuses': [{'code': 'REFERRED'}], 'referral_entities': ['Police']})

    def test_attending_staff_roles_accept_list_and_dict(self):
        TicketConfig._TicketConfig__validate_attending_staff_roles(
            {'default_attending_staff_role_ids': {'Default': [1, 2]}})
        TicketConfig._TicketConfig__validate_attending_staff_roles(
            {'default_attending_staff_role_ids':
                {'Claims': {'role_ids': [5], 'strategy': 'random', 'scope': 'district'}}})

    def test_attending_staff_roles_reject_invalid(self):
        for value in ({'Claims': {'role_ids': [5], 'strategy': 'nope'}},
                      {'Claims': {'role_ids': [5], 'scope': 'planet'}},
                      {'Claims': ['five']}):
            with self.assertRaises(ValidationError):
                TicketConfig._TicketConfig__validate_attending_staff_roles(
                    {'default_attending_staff_role_ids': value})

    def test_view_scope_ok(self):
        TicketConfig._TicketConfig__validate_view_scope(
            {'view_scope': {'all_cases_roles': [1], 'district_scoped_roles': [2],
                            'creator_scoped_roles': [3], 'default': 'district_scoped'}})

    def test_view_scope_reject_invalid(self):
        with self.assertRaises(ValidationError):
            TicketConfig._TicketConfig__validate_view_scope({'view_scope': {'default': 'whoever'}})
        with self.assertRaises(ValidationError):
            TicketConfig._TicketConfig__validate_view_scope({'view_scope': {'all_cases_roles': ['x']}})

    def _process_workflow_cfg(self, workflow):
        cfg = {'grievance_types': [{'name': 'Claims', 'children': [
            {'name': 'Partial wages', 'workflow': workflow}]}]}
        TicketConfig._TicketConfig__process_unified_categories(cfg)
        return cfg

    def test_category_workflow_allows_known_signals(self):
        TicketConfig._TicketConfig__validate_category_workflows(
            self._process_workflow_cfg(
                {'maker_checker': True, 'on_approved_signal': 'payments.arrears.create'}))
        TicketConfig._TicketConfig__validate_category_workflows(
            self._process_workflow_cfg({'on_resolve_task': 'tasks_management'}))

    def test_category_workflow_rejects_unknown_signals(self):
        with self.assertRaises(ValidationError):
            TicketConfig._TicketConfig__validate_category_workflows(
                self._process_workflow_cfg({'on_approved_signal': 'totally.made.up'}))
        with self.assertRaises(ValidationError):
            TicketConfig._TicketConfig__validate_category_workflows(
                self._process_workflow_cfg({'on_resolve_task': 'nope'}))

    def test_category_workflow_passthrough_dict_only(self):
        """Dict children carry their workflow; string children never do."""
        cfg = {'grievance_types': [{'name': 'Claims', 'children': [
            'Unpaid wages',
            {'name': 'Partial wages', 'workflow': {'on_resolve_task': 'tasks_management'}}]}]}
        TicketConfig._TicketConfig__process_unified_categories(cfg)
        processed = cfg['processed_categories']
        partial = next(v for k, v in processed.items() if k.endswith('Partial wages'))
        unpaid = next(v for k, v in processed.items() if k.endswith('Unpaid wages'))
        self.assertEqual(partial['workflow'], {'on_resolve_task': 'tasks_management'})
        self.assertIsNone(unpaid['workflow'])
