import copy
import logging
import sys
import os

from django.apps import AppConfig
from django.db import OperationalError, ProgrammingError
from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)

MODULE_NAME = "grievance_social_protection"

DEFAULT_STRING = 'Default'
# CRON timedelta: {days},{hours}
DEFAULT_TIME_RESOLUTION = '5,0'
DEFAULT_GRIEVANCE_TYPE = 'uncategorized'
VALID_PERMISSION_TYPES = frozenset({'restricted_read', 'read', 'create', 'update', 'delete'})
CATEGORY_SEPARATOR = ' > '

VALID_ASSIGNMENT_STRATEGIES = frozenset({'random', 'round_robin', 'least_loaded'})
VALID_ASSIGNMENT_SCOPES = frozenset({'district', 'national'})
VALID_VIEW_SCOPE_DEFAULTS = frozenset({'all_cases', 'district_scoped', 'creator_scoped', 'none'})
ALLOWED_WORKFLOW_APPROVED_SIGNALS = frozenset({'payroll.benefit_consumption.create'})
ALLOWED_WORKFLOW_RESOLVE_TASKS = frozenset({'tasks_management'})

DEFAULT_CFG = {
    "default_validations_disabled": False,
    "default_grievance_type": DEFAULT_GRIEVANCE_TYPE,
    "gql_query_tickets_perms": ["127000"],
    "gql_query_comments_perms": ["127004"],
    "gql_mutation_create_tickets_perms": ["127001"],
    "gql_mutation_update_tickets_perms": ["127002"],
    "gql_mutation_delete_tickets_perms": ["127003"],
    "gql_mutation_create_comment_perms": ["127005"],
    "gql_mutation_resolve_grievance_perms": ["127006"],
    "tickets_attachments_root_path": None,

    "grievance_types": [DEFAULT_STRING, 'Category A', 'Category B'],
    "grievance_flags": [DEFAULT_STRING, 'Flag A', 'Flag B'],
    "grievance_channels": [DEFAULT_STRING, 'Channel A', 'Channel B'],
    "default_responses": {DEFAULT_STRING: DEFAULT_STRING},
    "grievance_anonymized_fields": {DEFAULT_STRING: []},
    # CRON timedelta: {days},{hours}
    "resolution_times": DEFAULT_TIME_RESOLUTION,
    "default_resolution": {DEFAULT_STRING: DEFAULT_TIME_RESOLUTION, 'Category A': '4,0', 'Category B': '6,12'},

    "attending_staff_role_ids": [],
    "default_attending_staff_role_ids": {DEFAULT_STRING: [1, 2]},
    "ticket_statuses": [
        {"code": "OPEN", "label": "Open", "initial": True},
        {"code": "IN_PROGRESS", "label": "In Progress"},
        {"code": "REFERRED", "label": "Referred", "requires_referral_entity": True},
        {"code": "RESOLVED", "label": "Resolved", "terminal": True},
    ],
    "referral_entities": ["Police", "Traditional Leader", "Court", "Ministry of Gender", "Other"],
    "view_scope": {
        "all_cases_roles": [],
        "district_scoped_roles": [],
        "creator_scoped_roles": [],
        "default": "district_scoped",
    },
    "sla": {
        "set_due_date_on_create": True,
        "duration_basis": {"pending": "days_since_created", "resolved": "days_created_to_resolved"},
        "row_colors": {"within_sla": "yellow", "breached": "red", "resolved": "white"},
    },
    "notifications": {"on_assign": True, "include_due_date": True, "channels": ["email"]},
    "participant_fields": [],
    "search_filters": ["formNumber", "location", "nationalId", "status", "priority", "dateRange"],
    "search_result_columns": [
        {"key": "formNumber", "label": "HH Form Number"},
        {"key": "location", "label": "Location"},
        {"key": "status", "label": "Status"},
        {"key": "priority", "label": "Priority"},
        {"key": "attendingStaff", "label": "Officer Assigned"},
        {"key": "duration", "label": "Duration"},
    ],
    "enable_export": True,
}


class TicketConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = MODULE_NAME
    gql_query_tickets_perms = []
    gql_query_comments_perms = []
    gql_mutation_create_tickets_perms = []
    gql_mutation_update_tickets_perms = []
    gql_mutation_delete_tickets_perms = []
    gql_mutation_resolve_grievance_perms = []
    gql_mutation_create_comment_perms = []
    tickets_attachments_root_path = None

    grievance_types = []
    grievance_flags = []
    grievance_channels = []
    default_responses = {}
    grievance_anonymized_fields = {}
    resolution_times = {}
    default_resolution = {}
    default_grievance_type = DEFAULT_GRIEVANCE_TYPE
    attending_staff_role_ids = []
    default_attending_staff_role_ids = {}

    ticket_statuses = []
    referral_entities = []
    view_scope = {}
    sla = {}
    notifications = {}
    participant_fields = []
    search_filters = []
    search_result_columns = []
    enable_export = False

    # Processed structures for enhanced configurations
    processed_categories = {}
    processed_flags = {}
    unified_resolution_times = {}
    generated_rights = {}  # Store dynamically generated rights

    @classmethod
    def get_all_permissions(cls):
        """
        Get all permissions including dynamically generated ones.
        This method can be used by external systems to get the complete permission list.
        """
        all_perms = {}

        # Start with static permissions from DEFAULT_CFG
        for key, value in DEFAULT_CFG.items():
            if key.endswith('_perms'):
                all_perms[key] = value

        # Add dynamically generated permissions
        for right_name, right_id in cls.generated_rights.items():
            if right_name not in all_perms:
                all_perms[right_name] = []
            if right_id not in all_perms[right_name]:
                all_perms[right_name].append(right_id)

        return all_perms

    def ready(self):
        from core.models import ModuleConfiguration
        from core.module_config_registry import register_validator, register_reloader

        cfg = ModuleConfiguration.get_or_default(MODULE_NAME, DEFAULT_CFG)
        self.__process_config(cfg)
        self.__validate_config(cfg)
        self.__load_config(cfg)

        # Generate rights only when the database is available and ready.
        # Skip during migrations, when NO_DATABASE is set, or when
        # core tables (django_content_type, auth_permission) don't exist yet.
        if os.environ.get("NO_DATABASE") != "True" and 'migrate' not in sys.argv and 'makemigrations' not in sys.argv:
            try:
                from .rights import GrievanceRightsManager
                GrievanceRightsManager.generate_automatic_rights(TicketConfig)
            except (OperationalError, ProgrammingError):
                logger.info("Database tables not ready, skipping automatic rights generation.")

        self.__initialize_custom_filters()

        register_validator(MODULE_NAME, self._validate_module_config)
        register_reloader(MODULE_NAME, self._reload_module_config)

    @classmethod
    def __initialize_custom_filters(cls):
        """Register the Ticket Custom Filter Wizard for advanced case search."""
        from core.custom_filters import CustomFilterRegistryPoint
        from .custom_filters import TicketCustomFilterWizard

        CustomFilterRegistryPoint.register_custom_filters(
            module_name=MODULE_NAME,
            custom_filter_class_list=[TicketCustomFilterWizard],
        )

    def __process_config(self, cfg):
        self.__process_unified_categories(cfg)
        self.__process_unified_flags(cfg)

    def __validate_config(self, cfg):
        self.__validate_category_default_flags(cfg)
        self.__validate_grievance_dict_fields(cfg, 'default_responses')
        self.__validate_grievance_dict_fields(cfg, 'grievance_anonymized_fields')
        self.__validate_grievance_dict_fields(cfg, 'default_resolution')
        self.__validate_grievance_default_resolution_time(cfg)
        self.__validate_ticket_statuses(cfg)
        self.__validate_referral_entities(cfg)
        self.__validate_attending_staff_roles(cfg)
        self.__validate_view_scope(cfg)
        self.__validate_category_workflows(cfg)

    def _merge_with_defaults(self, instance):
        return {**copy.deepcopy(DEFAULT_CFG), **instance._cfg}

    def _validate_module_config(self, instance):
        cfg = self._merge_with_defaults(instance)
        self.__process_config(cfg)
        self.__validate_config(cfg)

    def _reload_module_config(self, instance):
        cfg = self._merge_with_defaults(instance)
        self.__process_config(cfg)
        self.__load_config(cfg)
        self.__initialize_custom_filters()

    @classmethod
    def __validate_grievance_dict_fields(cls, cfg, field_name):
        def get_grievance_type_options_msg(types):
            types_string = ", ".join(types)
            return logger.info(f'Available grievance types: {types_string}')

        dict_field = cfg.get(field_name, {})
        if not dict_field:
            return

        grievance_types = cfg.get('grievance_types', [])
        if not grievance_types:
            logger.warning('Please specify grievance_types if you want to setup %s.', field_name)

        if not isinstance(dict_field, dict):
            get_grievance_type_options_msg(grievance_types)
            return

        for field_key in dict_field.keys():
            if field_key not in grievance_types:
                logger.warning('%s in %s not in grievance_types', field_key, field_name)
                get_grievance_type_options_msg(grievance_types)

    @classmethod
    def __validate_grievance_default_resolution_time(cls, cfg):
        """
        Validate resolution times from both legacy default_resolution and new category-specific resolution_times.
        This method also builds a unified resolution times mapping that considers:
        1. Category-specific resolution_times (from processed_categories)
        2. Legacy default_resolution configuration
        3. Global resolution_times fallback
        """
        # First, validate legacy default_resolution
        dict_field = cfg.get("default_resolution", {})
        if dict_field:
            for key in dict_field:
                value = dict_field[key]
                if value in ['', None]:
                    raise ValidationError(
                        f"'{key}' in 'default_resolution' has no value. "
                        f"Expected format: 'days,hours' (e.g. '{DEFAULT_TIME_RESOLUTION}')."
                    )
                else:
                    cls.__validate_resolution_time_format(value, f"default_resolution[{key}]")

        # Then, validate resolution_times in processed categories
        processed_categories = cfg.get("processed_categories", {})
        for category_name, category_info in processed_categories.items():
            resolution_time = category_info.get('resolution_times')
            if resolution_time:
                cls.__validate_resolution_time_format(resolution_time, f"category '{category_name}'")

        # Build unified resolution times mapping for backward compatibility
        unified_resolution = {}

        # Start with legacy default_resolution
        if dict_field:
            unified_resolution.update(dict_field)

        # Override/add with category-specific resolution_times
        for category_name, category_info in processed_categories.items():
            resolution_time = category_info.get('resolution_times')
            if resolution_time:
                unified_resolution[category_name] = resolution_time

        # Store the unified mapping back
        cfg['unified_resolution_times'] = unified_resolution

    @classmethod
    def __validate_resolution_time_format(cls, value, context=""):
        if ',' not in value:
            raise ValidationError(
                f"Invalid resolution time format for {context}. "
                "Configuration should contain two integers representing days and hours, "
                "separated by a comma."
            )

        try:
            parts = value.split(',')
            days = int(parts[0])
            hours = int(parts[1])
        except (ValueError, IndexError):
            raise ValidationError(
                f"Invalid resolution time format for {context}. "
                "Expected format: 'days,hours' where both are integers."
            )

        if not (0 <= days < 99 and 0 <= hours < 24):
            raise ValidationError(
                f"Invalid resolution time values for {context}. "
                "Days must be 0-98 and hours must be 0-23."
            )

    @classmethod
    def __validate_category_default_flags(cls, cfg):
        """
        Validate that every default_flags entry in grievance_types
        references a flag defined in grievance_flags.
        """
        grievance_flags = cfg.get('grievance_flags', [])
        processed_categories = cfg.get('processed_categories', {})
        for category_name, category_info in processed_categories.items():
            for flag in category_info.get('default_flags', []):
                if flag not in grievance_flags:
                    raise ValidationError(
                        f"Category '{category_name}' references default flag '{flag}' "
                        f"which is not defined in grievance_flags. "
                        f"Available flags: {grievance_flags}"
                    )

    @classmethod
    def __validate_ticket_statuses(cls, cfg):
        """
        `ticket_statuses` only selects which model statuses are enabled and their
        labels — the model's Ticket.TicketStatus stays the source of truth. Every
        code must be a valid choice and exactly one status must be `initial`.
        """
        statuses = cfg.get('ticket_statuses', [])
        if not statuses:
            return

        from .models import Ticket
        valid_codes = set(Ticket.TicketStatus.values)

        initial_count = 0
        for status in statuses:
            if not isinstance(status, dict) or not status.get('code'):
                raise ValidationError("Each entry in 'ticket_statuses' must be a dict with a 'code'.")
            code = status['code']
            if code not in valid_codes:
                raise ValidationError(
                    f"ticket_statuses code '{code}' is not a valid Ticket.TicketStatus "
                    f"(one of {sorted(valid_codes)}). Add it to the model first."
                )
            if status.get('initial'):
                initial_count += 1

        if initial_count != 1:
            raise ValidationError(
                f"'ticket_statuses' must define exactly one 'initial' status, found {initial_count}."
            )

    @classmethod
    def __validate_referral_entities(cls, cfg):
        """`referral_entities` must be non-empty when the REFERRED status is enabled."""
        statuses = cfg.get('ticket_statuses', [])
        referred_enabled = any(
            isinstance(s, dict) and s.get('code') == 'REFERRED' for s in statuses
        )
        if referred_enabled and not cfg.get('referral_entities'):
            raise ValidationError(
                "'referral_entities' must be non-empty when the 'REFERRED' status is enabled."
            )

    @classmethod
    def __validate_attending_staff_roles(cls, cfg):
        """
        `default_attending_staff_role_ids` maps category → roles. Each value is
        either a plain list of role ids (legacy) or a dict adding strategy/scope.
        """
        mapping = cfg.get('default_attending_staff_role_ids', {})
        if not isinstance(mapping, dict):
            raise ValidationError("'default_attending_staff_role_ids' must be a dict of category → roles.")

        for category, value in mapping.items():
            if isinstance(value, list):
                role_ids = value
            elif isinstance(value, dict):
                role_ids = value.get('role_ids', [])
                if not isinstance(role_ids, list):
                    raise ValidationError(
                        f"default_attending_staff_role_ids['{category}'].role_ids must be a list."
                    )
                strategy = value.get('strategy')
                if strategy is not None and strategy not in VALID_ASSIGNMENT_STRATEGIES:
                    raise ValidationError(
                        f"default_attending_staff_role_ids['{category}'].strategy '{strategy}' is "
                        f"invalid. Allowed: {sorted(VALID_ASSIGNMENT_STRATEGIES)}."
                    )
                scope = value.get('scope')
                if scope is not None and scope not in VALID_ASSIGNMENT_SCOPES:
                    raise ValidationError(
                        f"default_attending_staff_role_ids['{category}'].scope '{scope}' is "
                        f"invalid. Allowed: {sorted(VALID_ASSIGNMENT_SCOPES)}."
                    )
            else:
                raise ValidationError(
                    f"default_attending_staff_role_ids['{category}'] must be a list of role ids "
                    f"or a {{role_ids, strategy, scope}} dict."
                )
            if not all(isinstance(r, int) for r in role_ids):
                raise ValidationError(
                    f"default_attending_staff_role_ids['{category}'] role ids must be integers."
                )

    @classmethod
    def __validate_view_scope(cls, cfg):
        """`view_scope` role lists must be integer ids; `default` from the allowed set."""
        view_scope = cfg.get('view_scope', {})
        if not view_scope:
            return
        if not isinstance(view_scope, dict):
            raise ValidationError("'view_scope' must be a dict.")

        for key in ('all_cases_roles', 'district_scoped_roles', 'creator_scoped_roles'):
            role_ids = view_scope.get(key, [])
            if not isinstance(role_ids, list) or not all(isinstance(r, int) for r in role_ids):
                raise ValidationError(f"view_scope['{key}'] must be a list of integer role ids.")

        default = view_scope.get('default', 'district_scoped')
        if default not in VALID_VIEW_SCOPE_DEFAULTS:
            raise ValidationError(
                f"view_scope['default'] '{default}' is invalid. "
                f"Allowed: {sorted(VALID_VIEW_SCOPE_DEFAULTS)}."
            )

    @classmethod
    def __validate_category_workflows(cls, cfg):
        """Per-category `workflow` hand-off signals must be in the allow-lists."""
        processed = cfg.get('processed_categories', {})
        for name, info in processed.items():
            workflow = info.get('workflow')
            if not workflow:
                continue
            if not isinstance(workflow, dict):
                raise ValidationError(f"Category '{name}' workflow must be a dict.")

            approved_signal = workflow.get('on_approved_signal')
            if approved_signal is not None and approved_signal not in ALLOWED_WORKFLOW_APPROVED_SIGNALS:
                raise ValidationError(
                    f"Category '{name}' workflow.on_approved_signal '{approved_signal}' is not "
                    f"allowed. Allowed: {sorted(ALLOWED_WORKFLOW_APPROVED_SIGNALS)}."
                )
            resolve_task = workflow.get('on_resolve_task')
            if resolve_task is not None and resolve_task not in ALLOWED_WORKFLOW_RESOLVE_TASKS:
                raise ValidationError(
                    f"Category '{name}' workflow.on_resolve_task '{resolve_task}' is not "
                    f"allowed. Allowed: {sorted(ALLOWED_WORKFLOW_RESOLVE_TASKS)}."
                )

    @classmethod
    def __process_unified_categories(cls, cfg):
        """
        Process grievance_types configuration supporting both string and dict formats
        """
        categories = cfg.get('grievance_types', [])
        processed_categories = {}
        flat_types = []

        # Ensure default_grievance_type is in the categories list
        default_grievance_type = cfg.get('default_grievance_type', DEFAULT_GRIEVANCE_TYPE)
        if default_grievance_type:
            # Check if default_grievance_type exists in categories
            def get_category_name(cat):
                if isinstance(cat, str):
                    return cat
                if isinstance(cat, dict):
                    return cat.get('name')
                return None

            type_exists = any(get_category_name(cat) == default_grievance_type for cat in categories)

            # If not found, add it with default permissions so uncategorized
            # tickets are subject to access control like all other categories.
            if not type_exists:
                categories.insert(0, {
                    'name': default_grievance_type,
                    'permissions': ['read', 'update']
                })
                logger.info(
                    f"Added default grievance type '{default_grievance_type}' with permissions ['read', 'update']")

        def process_category_item(item, parent_name=None, parent_info=None):
            """Process a single category item (string or dict)"""
            parent = parent_info or {}

            if isinstance(item, str):
                # Simple string format (backward compatible)
                full_name = f"{parent_name}{CATEGORY_SEPARATOR}{item}" if parent_name else item
                processed_categories[full_name] = {
                    'priority': parent.get('priority', 'Medium'),
                    'permissions': list(parent.get('permissions', [])),
                    'default_flags': list(parent.get('default_flags', [])),
                    'resolution_times': parent.get('resolution_times'),
                    'visible_fields': list(parent.get('visible_fields', [])),
                    # workflow is leaf-specific and defined only on dict items, so
                    # string categories never carry one (no inheritance).
                    'workflow': None,
                    'parent': parent_name,
                    'children': {},
                    'generated_rights': {}
                }
                flat_types.append(full_name)
                return full_name

            elif isinstance(item, dict):
                # Enhanced dict format with permissions
                cat_name = item.get('name')
                if not cat_name:
                    raise ValidationError("Each category dict in 'grievance_types' must have a 'name' field.")

                full_name = f"{parent_name}{CATEGORY_SEPARATOR}{cat_name}" if parent_name else cat_name

                # Process permissions — copy to avoid mutating parent's list
                permissions = list(item.get('permissions', parent.get('permissions', [])))
                invalid = set(permissions) - VALID_PERMISSION_TYPES
                if invalid:
                    raise ValidationError(
                        f"Category '{cat_name}' has invalid permission types: {invalid}. "
                        f"Allowed: {sorted(VALID_PERMISSION_TYPES)}"
                    )

                # Process visible_fields with inheritance constraints
                visible_fields = item.get('visible_fields', [])
                if visible_fields:
                    # If visible_fields is defined, ensure restricted_read and read permissions exist
                    required_permissions = ['restricted_read', 'read']
                    for perm in required_permissions:
                        if perm not in permissions:
                            permissions.append(perm)
                            logger.info(
                                f"Auto-adding '{perm}' permission to category '{cat_name}' due to visible_fields")

                    # Validate against parent's visible_fields
                    parent_visible_fields = parent.get('visible_fields')
                    if parent_visible_fields:
                        parent_visible = set(parent_visible_fields)
                        child_visible = set(visible_fields)

                        # Check if child tries to expose fields hidden by parent
                        invalid_fields = child_visible - parent_visible
                        if invalid_fields:
                            raise ValidationError(
                                f"Category '{cat_name}' cannot make fields {invalid_fields} visible — "
                                f"they are not in parent '{parent_name}' visible_fields."
                            )
                elif parent.get('visible_fields'):
                    # Inherit parent's visible_fields if not specified
                    visible_fields = parent['visible_fields'].copy()

                # Inherit from parent if not specified
                priority = item.get('priority', parent.get('priority', 'Medium'))
                default_flags = list(item.get('default_flags', parent.get('default_flags', [])))

                # Handle resolution_times - inherit from parent if not specified
                resolution_times = item.get('resolution_times')
                if not resolution_times:
                    resolution_times = parent.get('resolution_times')

                category_info = {
                    'priority': priority,
                    'permissions': permissions,
                    'default_flags': default_flags,
                    'resolution_times': resolution_times,
                    'visible_fields': visible_fields,
                    # workflow hand-off (maker-checker / tasks_management); leaf-specific,
                    # not inherited from the parent.
                    'workflow': item.get('workflow'),
                    'parent': parent_name,
                    'children': {},
                    'generated_rights': {}  # Will be populated by rights generation
                }

                processed_categories[full_name] = category_info
                flat_types.append(full_name)

                # Process children recursively
                children = item.get('children', [])
                for child in children:
                    child_full_name = process_category_item(child, full_name, category_info)
                    if child_full_name:
                        child_short_name = child_full_name.split(CATEGORY_SEPARATOR)[-1]
                        category_info['children'][child_short_name] = child_full_name

                return full_name

            return None

        # Process all top-level categories
        for category in categories:
            process_category_item(category)

        # Store processed data
        cfg['processed_categories'] = processed_categories
        # Update flat list for backward compatibility
        cfg['grievance_types'] = flat_types
        TicketConfig.processed_categories = processed_categories

    @classmethod
    def __process_unified_flags(cls, cfg):
        """
        Process grievance_flags configuration supporting both string and dict formats
        """
        flags = cfg.get('grievance_flags', [])
        processed_flags = {}
        flat_flags = []

        for flag in flags:
            if isinstance(flag, str):
                # Simple string format (backward compatible)
                processed_flags[flag] = {
                    'priority': 'Medium',
                    'permissions': [],  # No restrictions - empty list
                    'generated_rights': {}
                }
                flat_flags.append(flag)

            elif isinstance(flag, dict):
                # Enhanced dict format with permissions
                flag_name = flag.get('name')
                if not flag_name:
                    raise ValidationError("Each flag dict in 'grievance_flags' must have a 'name' field.")

                # Process permissions
                permissions = flag.get('permissions', [])
                invalid = set(permissions) - VALID_PERMISSION_TYPES
                if invalid:
                    raise ValidationError(
                        f"Flag '{flag_name}' has invalid permission types: {invalid}. "
                        f"Allowed: {sorted(VALID_PERMISSION_TYPES)}"
                    )

                processed_flags[flag_name] = {
                    'priority': flag.get('priority', 'Medium'),
                    'permissions': permissions,
                    'generated_rights': {}
                }
                flat_flags.append(flag_name)

        # Store processed data
        cfg['processed_flags'] = processed_flags
        # Update flat list for backward compatibility
        cfg['grievance_flags'] = flat_flags
        TicketConfig.processed_flags = processed_flags

    @classmethod
    def __load_config(cls, cfg):
        """
        Load all config fields that match current AppConfig class fields, all custom fields have to be loaded separately
        """
        for field in cfg:
            if hasattr(TicketConfig, field):
                setattr(TicketConfig, field, cfg[field])
