import logging
import re
from django.apps import apps
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from .apps import TicketConfig, CATEGORY_SEPARATOR

logger = logging.getLogger(__name__)


class GrievanceAccessControl:
    """
    Handles rights-based access control for grievance categories and flags.

    Access rules:
    1. Categories/flags can have permissions that generate automatic rights
    2. Users need specific rights to access categories/flags
    3. Restricted view shows limited information based on restricted_read right
    4. Unconfigured standard permission types fall back to the module's existing role-based permissions
    """

    # Access level constants
    ACCESS_NONE = 'none'
    ACCESS_RESTRICTED = 'restricted'
    ACCESS_READ = 'read'
    ACCESS_FULL = 'full'

    # Permission type constants
    PERM_RESTRICTED_READ = 'restricted_read'
    PERM_READ = 'read'
    PERM_CREATE = 'create'
    PERM_UPDATE = 'update'
    PERM_DELETE = 'delete'

    # Fallback to the module's standard ticket permissions when a category/flag
    # has generated_rights but the specific permission type is not among them.
    # Unconfigured standard types defer to the existing role-based permissions.
    _DEFAULT_PERM_FALLBACK = {
        PERM_READ: 'gql_query_tickets_perms',
        PERM_CREATE: 'gql_mutation_create_tickets_perms',
        PERM_UPDATE: 'gql_mutation_update_tickets_perms',
        PERM_DELETE: 'gql_mutation_delete_tickets_perms',
    }

    @staticmethod
    def parse_flags(flags):
        """Parse flags from string or list format to list."""
        if not flags:
            return []
        if isinstance(flags, str):
            return flags.split()
        return list(flags)

    @classmethod
    def _check_access(cls, user, name, access_type, config_attr):
        """
        Generic access checker for category or flag.

        Args:
            user: Django user object
            name: Category or flag name
            access_type: Type of access ('restricted_read', 'read', 'create', 'update', 'delete')
            config_attr: 'processed_categories' or 'processed_flags'

        Returns:
            bool: True if user has the requested access, False if denied
        """
        if not user or user.is_anonymous:
            return False

        processed = getattr(TicketConfig, config_attr, {})
        if not processed or name not in processed:
            return True

        info = processed[name]
        generated_rights = info.get('generated_rights', {})

        # If no rights generated, no restrictions
        if not generated_rights:
            return True

        # Check if user has the specific right
        required_right = generated_rights.get(access_type)

        if not required_right:
            # Permission type not configured — fall back to default core
            # ticket permissions for standard types (read/create/update/delete).
            # Grievance-specific types like restricted_read have no fallback.
            fallback_attr = cls._DEFAULT_PERM_FALLBACK.get(access_type)
            if not fallback_attr:
                if access_type not in (cls.PERM_RESTRICTED_READ,):
                    logger.warning(
                        "No fallback permission for access_type='%s' "
                        "(name=%s, config_attr=%s). Denying access.",
                        access_type, name, config_attr
                    )
                return False
            default_perms = getattr(TicketConfig, fallback_attr, None)
            if default_perms is None:
                logger.error(
                    "TicketConfig missing attribute '%s' for fallback "
                    "permission check (access_type=%s, name=%s). Denying access.",
                    fallback_attr, access_type, name
                )
                return False
            if not default_perms:
                return False
            return user.has_perms(default_perms)

        return user.has_perm(str(required_right))

    @classmethod
    def check_category_access(cls, user, category_name, access_type=PERM_READ):
        return cls._check_access(user, category_name, access_type, 'processed_categories')

    @classmethod
    def check_flag_access(cls, user, flag_name, access_type=PERM_READ):
        return cls._check_access(user, flag_name, access_type, 'processed_flags')

    @classmethod
    def can_view_category(cls, user, category_name):
        """Check if user can view a category (has either read or restricted_read access)"""
        return (cls.check_category_access(user, category_name, cls.PERM_READ)
                or cls.check_category_access(user, category_name, cls.PERM_RESTRICTED_READ))

    @classmethod
    def can_view_flag(cls, user, flag_name):
        """Check if user can view a flag (has either read or restricted_read access)"""
        return (cls.check_flag_access(user, flag_name, cls.PERM_READ)
                or cls.check_flag_access(user, flag_name, cls.PERM_RESTRICTED_READ))

    @classmethod
    def _has_restrictions(cls, name, config_attr):
        """Check if an item has any access restrictions"""
        processed = getattr(TicketConfig, config_attr, {})
        if not processed or name not in processed:
            return False
        return bool(processed[name].get('generated_rights', {}))

    @classmethod
    def has_category_restrictions(cls, category_name):
        """Check if a category has any access restrictions"""
        return cls._has_restrictions(category_name, 'processed_categories')

    @classmethod
    def has_flag_restrictions(cls, flag_name):
        """Check if a flag has any access restrictions"""
        return cls._has_restrictions(flag_name, 'processed_flags')

    @classmethod
    def get_user_access_level(cls, user, category_name=None, flag_names=None):
        """
        Determine the highest level of access a user has.

        Returns:
            str: 'full', 'read', 'restricted', or 'none'
        """

        def evaluate_access(check_func, name, has_restrictions_func):
            """
            Evaluate access level for a single category or flag.

            Returns:
                str: 'full', 'read', 'restricted', 'none', or None (no restrictions)
            """
            if not has_restrictions_func(name):
                return None  # No restrictions

            if any(check_func(user, name, t) for t in (cls.PERM_CREATE, cls.PERM_UPDATE, cls.PERM_DELETE)):
                return cls.ACCESS_FULL
            if check_func(user, name, cls.PERM_READ):
                return cls.ACCESS_READ
            if check_func(user, name, cls.PERM_RESTRICTED_READ):
                return cls.ACCESS_RESTRICTED
            return cls.ACCESS_NONE

        def min_access_level(levels):
            """Return most restrictive among present levels."""
            levels = [lvl for lvl in levels if lvl is not None]
            if not levels:
                return None

            for level in (cls.ACCESS_NONE, cls.ACCESS_RESTRICTED, cls.ACCESS_READ, cls.ACCESS_FULL):
                if level in levels:
                    return level
            return None

        # Evaluate category access
        category_access = evaluate_access(
            cls.check_category_access,
            category_name,
            cls.has_category_restrictions
        ) if category_name else None

        # Evaluate flag access (most restrictive across all flags)
        flag_accesses = []
        if flag_names:
            flag_list = cls.parse_flags(flag_names)
            for flag in flag_list:
                level = evaluate_access(
                    cls.check_flag_access,
                    flag,
                    cls.has_flag_restrictions
                )
                if level is not None:
                    flag_accesses.append(level)

        flag_access = min_access_level(flag_accesses) if flag_accesses else None

        # Most restrictive applies
        result = min_access_level([category_access, flag_access])
        return result if result is not None else cls.ACCESS_FULL

    @classmethod
    def get_accessible_categories(cls, user):
        """
        Return list of categories accessible to the user.

        Args:
            user: The user to check access for

        Returns:
            List of category names the user can access
        """
        all_categories = list(TicketConfig.grievance_types)

        if not TicketConfig.processed_categories:
            return all_categories

        return [name for name in all_categories if cls.can_view_category(user, name)]

    @classmethod
    def get_accessible_flags(cls, user):
        """Return list of flags accessible to the user"""
        all_flags = list(TicketConfig.grievance_flags)

        if not TicketConfig.processed_flags:
            return all_flags

        return [name for name in all_flags if cls.can_view_flag(user, name)]

    @classmethod
    def validate_ticket_access(cls, user, category, flags=None, access_type=PERM_CREATE):
        """
        Validate if user can perform action on ticket with given category and flags.
        Raises PermissionDenied if access is not allowed.
        """
        # Check category access
        if category:
            if not cls.check_category_access(user, category, access_type):
                raise PermissionDenied(f"User does not have {access_type} permission for category: {category}")

        # Check flags access
        if flags:
            flag_list = cls.parse_flags(flags)
            for flag in flag_list:
                if flag and not cls.check_flag_access(user, flag, access_type):
                    raise PermissionDenied(f"User does not have {access_type} permission for flag: {flag}")

    @classmethod
    def filter_ticket_queryset(cls, queryset, user):
        """
        Filter a ticket queryset based on user's access rights.

        Args:
            queryset: Django queryset of tickets
            user: User to check permissions for

        Returns:
            Filtered queryset
        """
        # Get categories user can at least view with restricted access
        accessible_categories = cls.get_accessible_categories(user)

        # Filter tickets by accessible categories.
        # Include NULL/blank category tickets if the default grievance type is accessible,
        # so legacy tickets without a category remain visible.
        if TicketConfig.processed_categories:
            category_filter = Q(category__in=accessible_categories)
            if TicketConfig.default_grievance_type in accessible_categories:
                category_filter |= Q(category__isnull=True) | Q(category='')
            queryset = queryset.filter(category_filter)

        # Further filter by flag permissions
        if TicketConfig.processed_flags:
            # Get flags that user cannot access at all
            restricted_flags = [
                flag_name
                for flag_name, flag_info in TicketConfig.processed_flags.items()
                if flag_info.get('generated_rights') and not cls.can_view_flag(user, flag_name)
            ]

            # Exclude tickets with completely restricted flags using whole-word matching.
            # Uses POSIX-compatible patterns (no lookbehind) for PostgreSQL compatibility.
            for flag in restricted_flags:
                queryset = queryset.exclude(
                    flags__regex=r'(^| )' + re.escape(flag) + r'( |$)'
                )

        return queryset

    @classmethod
    def apply_view_scope(cls, queryset, user):
        """
        view-scope on top of the category/flag filtering already applied by filter_ticket_queryset.
        this can only narrow further, never widen what category/flag access already allowed.

        Scope is derived from the user's roles against view_scope's {all_cases,district_scoped,creator_scoped}_roles, 
        falling back to view_scope.default when none of those role lists match:
        - all_cases (and 'none', an opt-out default): no extra restriction.
        - district_scoped: ticket's json_ext.district_code must be one of
          the user's districts (location.UserDistrict).
        - creator_scoped: ticket.user_created must be the user.
        """
        if not user or user.is_anonymous:
            return queryset.none()

        scope = cls._resolve_view_scope(user)

        if scope == 'creator_scoped':
            return queryset.filter(user_created=user)

        if scope == 'district_scoped':
            district_codes = cls._user_district_codes(user)
            if not district_codes:
                return queryset.none()
            return queryset.filter(json_ext__district_code__in=district_codes)

        # 'all_cases' and 'none' (opt-out default): no extra restriction.
        return queryset

    @classmethod
    def _resolve_view_scope(cls, user):
        view_scope = TicketConfig.view_scope
        if not view_scope:
            # Not configured at all (deployment opted out, or a test that
            # doesn't touch view_scope): no restriction. Only an explicitly
            # configured view_scope (even just a 'default') opts into scoping.
            return 'all_cases'

        i_user = getattr(user, 'i_user', None)
        user_role_ids = set()
        if i_user:
            # UserRole.user is an FK to InteractiveUser, not the core.User wrapper.
            user_role_model = apps.get_model('core', 'UserRole')
            user_role_ids = set(
                user_role_model.objects.filter(
                    user=i_user, *user_role_model.filter_validity()
                ).values_list('role_id', flat=True)
            )

        # Broadest match wins when a user's roles span more than one list.
        for scope_name, cfg_key in (
            ('all_cases', 'all_cases_roles'),
            ('district_scoped', 'district_scoped_roles'),
            ('creator_scoped', 'creator_scoped_roles'),
        ):
            role_ids = set(view_scope.get(cfg_key) or [])
            if role_ids & user_role_ids:
                return scope_name

        return view_scope.get('default', 'all_cases')

    @staticmethod
    def _user_district_codes(user):
        # UserDistrict.user is an FK to InteractiveUser, not the core.User wrapper.
        i_user = getattr(user, 'i_user', None)
        if not i_user:
            return []
        user_district_model = apps.get_model('location', 'UserDistrict')
        return list(
            user_district_model.objects.filter(
                user=i_user, *user_district_model.filter_validity()
            ).values_list('location__code', flat=True)
        )

    @classmethod
    def get_category_defaults(cls, category_name):
        """Get default flags and priority for a category"""

        if not TicketConfig.processed_categories or category_name not in TicketConfig.processed_categories:
            return {'priority': 'Medium', 'default_flags': []}

        category_info = TicketConfig.processed_categories[category_name]
        return {
            'priority': category_info.get('priority', 'Medium'),
            'default_flags': category_info.get('default_flags', [])
        }

    @classmethod
    def _get_priority_index(cls, priority, priorities):
        """
        Safely get the index of a priority value, returning default index if invalid.

        Args:
            priority: Priority string to look up
            priorities: List of valid priority values in order

        Returns:
            int: Index of priority in list, or index of 'Medium' if invalid
        """
        try:
            return priorities.index(priority)
        except ValueError:
            logger.warning(f"Invalid priority value '{priority}', using 'Medium' as default")
            return priorities.index('Medium')

    @classmethod
    def get_effective_priority(cls, category_name, flag_names=None):
        """
        Get the effective priority for a ticket based on category and flags.
        Higher priority wins (Critical > High > Medium > Low).
        """

        priorities = ['Low', 'Medium', 'High', 'Critical']
        max_idx = priorities.index('Medium')

        # Check category priority
        if TicketConfig.processed_categories and category_name in TicketConfig.processed_categories:
            cat_priority = TicketConfig.processed_categories[category_name].get('priority', 'Medium')
            cat_idx = cls._get_priority_index(cat_priority, priorities)
            if cat_idx > max_idx:
                max_idx = cat_idx

        # Check flag priorities
        if flag_names:
            flag_list = cls.parse_flags(flag_names)

            for flag_name in flag_list:
                if TicketConfig.processed_flags and flag_name in TicketConfig.processed_flags:
                    flag_priority = TicketConfig.processed_flags[flag_name].get('priority', 'Medium')
                    flag_idx = cls._get_priority_index(flag_priority, priorities)
                    if flag_idx > max_idx:
                        max_idx = flag_idx

        return priorities[max_idx]

    @classmethod
    def get_visible_fields(cls, user, category_name):
        """
        Get the list of fields visible to the user for a specific category.

        Args:
            user: Django user object
            category_name: Full category name

        Returns:
            list: List of field names the user can see, or None if all fields are visible
        """

        # Check user's access level
        access_level = cls.get_user_access_level(user, category_name)

        # Full access or no restrictions - all fields visible
        if access_level in (cls.ACCESS_FULL, cls.ACCESS_READ):
            return None  # None means all fields visible

        # No access - no fields visible
        if access_level == cls.ACCESS_NONE:
            return []

        # Restricted access - check visible_fields configuration
        if TicketConfig.processed_categories and category_name in TicketConfig.processed_categories:
            category_info = TicketConfig.processed_categories[category_name]
            visible_fields = category_info.get('visible_fields', [])

            # If visible_fields is defined, return it
            if visible_fields:
                return visible_fields.copy()

        # If no visible_fields configured, restricted users see basic fields only
        return ['id', 'status', 'category', 'priority', 'date_created']

    @classmethod
    def filter_fields_for_user(cls, user, category_name, available_fields):
        """
        Filter the available fields based on user's access level.

        Args:
            user: Django user object
            category_name: Full category name
            available_fields: Dict of all available fields

        Returns:
            dict: Filtered fields the user can access
        """
        visible_fields = cls.get_visible_fields(user, category_name)

        # If None, all fields are visible
        if visible_fields is None:
            return available_fields

        # Filter to only visible fields
        return {k: v for k, v in available_fields.items() if k in visible_fields}

    @classmethod
    def _build_category_dict(cls, name, info, user, parent_info=None, is_child=False):
        """Build a category dictionary with common fields"""
        base_dict = {
            'name': name.split(CATEGORY_SEPARATOR)[-1] if is_child else name,
            'full_name': name,
            'priority': info.get('priority', parent_info.get('priority', 'Medium') if parent_info else 'Medium'),
            'permissions': info.get('permissions', []),
            'default_flags': info.get('default_flags', []),
            'access_level': cls.get_user_access_level(user, name),
            'children': [],
        }

        return base_dict

    @classmethod
    def _build_subtree(cls, parent_name, parent_info, user):
        """Recursively build accessible children for a category"""
        children = []
        for child_name, child_info in TicketConfig.processed_categories.items():
            if child_info.get('parent') == parent_name:
                if cls.can_view_category(user, child_name):
                    child_dict = cls._build_category_dict(
                        child_name, child_info, user, parent_info, is_child=True)
                    child_dict['children'] = cls._build_subtree(child_name, child_info, user)
                    children.append(child_dict)
        return children

    @classmethod
    def get_category_hierarchy(cls, user):
        """Return hierarchical structure of categories accessible to the user"""

        if not TicketConfig.processed_categories:
            return [{"name": cat, "children": []} for cat in TicketConfig.grievance_types]

        hierarchy = []

        # Build hierarchy from processed categories
        for category_name, category_info in TicketConfig.processed_categories.items():
            if not category_info.get('parent'):
                if cls.can_view_category(user, category_name):
                    category_dict = cls._build_category_dict(category_name, category_info, user)
                    category_dict['children'] = cls._build_subtree(
                        category_name, category_info, user)
                    hierarchy.append(category_dict)

        return hierarchy
