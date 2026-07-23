import logging

import graphene
import django_filters
from django_filters.constants import EMPTY_VALUES
from django.db import models
from graphene import ObjectType
from graphene_django import DjangoObjectType
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext as _

from core.gql_queries import UserGQLType
from .apps import TicketConfig
from .models import Ticket, Comment
from .access_control import GrievanceAccessControl

from core import prefix_filterset, ExtendedConnection
from .util import model_obj_to_json
from .validations import user_associated_with_ticket

logger = logging.getLogger(__name__)

RESTRICTED_VALUE = "[Restricted]"

# Fields that are always safe to filter on regardless of access level
_ALWAYS_FILTERABLE = frozenset({'id', 'version'})

# Shared filter field definitions used by TicketFilterSet and CommentGQLType
TICKET_FILTER_FIELDS = {
    "id": ["exact", "isnull"],
    "version": ["exact"],
    "key": ["exact", "istartswith", "icontains", "iexact"],
    "code": ["exact", "istartswith", "icontains", "iexact"],
    "title": ["exact", "istartswith", "icontains", "iexact"],
    "description": ["exact", "istartswith", "icontains", "iexact"],
    "status": ["exact", "istartswith", "icontains", "iexact"],
    "priority": ["exact", "istartswith", "icontains", "iexact"],
    "category": ["exact", "istartswith", "icontains", "iexact"],
    "flags": ["exact", "istartswith", "icontains", "iexact"],
    "channel": ["exact", "istartswith", "icontains", "iexact"],
    "resolution": ["exact", "istartswith", "icontains", "iexact"],
    'reporter_id': ["exact"],
    "due_date": ["exact", "istartswith", "icontains", "iexact"],
    "date_of_incident": ["exact", "istartswith", "icontains", "iexact"],
    "date_created": ["exact", "istartswith", "icontains", "iexact", "gte", "lte"],
    **prefix_filterset("attending_staff__", UserGQLType._meta.filter_fields),
}


class TicketFilterSet(django_filters.FilterSet):
    """Custom FilterSet that enforces field-level filter restrictions.

    Users with restricted_read access to a category can only filter on
    fields listed in their visible_fields configuration. This prevents
    information inference attacks via filter parameters.
    """

    class Meta:
        model = Ticket
        fields = TICKET_FILTER_FIELDS

    def filter_queryset(self, queryset):
        """Override to skip filters on fields the user cannot see."""
        user = getattr(self.request, 'user', None) if self.request else None
        restricted = self._get_restricted_fields(user)

        for name, value in self.form.cleaned_data.items():
            filter_obj = self.filters.get(name)
            if not filter_obj:
                continue
            # Skip empty values (matches django-filter base behavior)
            if value in EMPTY_VALUES:
                continue
            if filter_obj.field_name in restricted:
                logger.info(
                    "User %s blocked from filtering on restricted field '%s'",
                    getattr(user, 'username', '?'), filter_obj.field_name
                )
                continue
            queryset = filter_obj.filter(queryset, value)
            assert isinstance(queryset, models.QuerySet), (
                "Expected '%s.%s' to return a QuerySet, but got a %s instead."
                % (type(self).__name__, name, type(queryset).__name__)
            )
        return queryset

    @staticmethod
    def _get_restricted_fields(user):
        """Get fields that should not be filterable for this user.

        If the user has restricted_read access to ANY accessible category,
        fields not in that category's visible_fields are blocked from filtering.
        Because restrictions are accumulated across all categories via set union,
        a field blocked in any one restricted category is blocked everywhere.

        Anonymous/unauthenticated users are blocked from filtering on all fields
        as defense-in-depth (check_ticket_perms should block them upstream).
        """
        if not user or user.is_anonymous:
            # Block all filterable fields — anonymous users should not reach
            # this point (check_ticket_perms gates upstream), but if they do,
            # deny all filtering rather than allowing it.
            return set(TicketFilterSet.Meta.fields.keys()) - _ALWAYS_FILTERABLE

        restricted = set()
        processed_categories = TicketConfig.processed_categories

        if not processed_categories:
            return restricted

        all_filterable = set(TicketFilterSet.Meta.fields.keys()) - _ALWAYS_FILTERABLE

        for cat_name in processed_categories:
            visible_fields = GrievanceAccessControl.get_visible_fields(user, cat_name)
            # visible_fields is None for full/read access, [] for no access,
            # or a list of field names for restricted access
            if visible_fields is not None and visible_fields:
                restricted |= all_filterable - set(visible_fields)

        return restricted


def check_ticket_perms(info):
    if not info.context.user.has_perms(TicketConfig.gql_query_tickets_perms):
        raise PermissionDenied(_("unauthorized"))


def check_comment_perms(info):
    user = info.context.user
    if not (user_associated_with_ticket(user) or user.has_perms(TicketConfig.gql_query_comments_perms)):
        raise PermissionDenied(_("Unauthorized"))


class TicketGQLType(DjangoObjectType):
    client_mutation_id = graphene.String()
    reporter = graphene.JSONString()
    reporter_type = graphene.Int()
    reporter_type_name = graphene.String()
    is_history = graphene.Boolean()

    reporter_first_name = graphene.String()
    reporter_last_name = graphene.String()
    reporter_dob = graphene.String()

    # Access level for this ticket based on user's rights
    access_level = graphene.String()

    @staticmethod
    def resolve_access_level(root, info):
        user = info.context.user
        return GrievanceAccessControl.get_user_access_level(user, root.category, root.flags)

    @staticmethod
    def _should_restrict_field(field_name, root, info):
        """
        Check if a field should be restricted based on user's access level and visible_fields configuration.

        Access level is determined by both category and flags (most restrictive wins).
        Visible fields configuration is per-category only.
        """
        user = info.context.user
        visible_fields = GrievanceAccessControl.get_visible_fields(user, root.category)

        # None means unrestricted access based on category - but check flags too
        if visible_fields is None:
            # Double-check with flags included for full access determination
            access_level = GrievanceAccessControl.get_user_access_level(user, root.category, root.flags)
            return access_level not in (GrievanceAccessControl.ACCESS_FULL, GrievanceAccessControl.ACCESS_READ)

        # Empty list means no access
        if not visible_fields:
            return True

        # Check if field is in visible list
        return field_name not in visible_fields

    @staticmethod
    def _restricted_resolve(root, info, field_name, restricted_value=RESTRICTED_VALUE, preserve_none=False):
        """Return restricted_value when field is restricted, otherwise return the field value.

        When preserve_none is True, returns None instead of restricted_value if the
        underlying field is falsy (avoids revealing that a value exists).
        """
        if TicketGQLType._should_restrict_field(field_name, root, info):
            if preserve_none and not getattr(root, field_name):
                return None
            return restricted_value
        return getattr(root, field_name)

    @staticmethod
    def resolve_reporter_type(root, info):
        check_ticket_perms(info)
        # Use _should_restrict_field for consistent access control
        if TicketGQLType._should_restrict_field('reporter_type', root, info):
            return None
        return root.reporter_type.id if root.reporter_type else None

    @staticmethod
    def resolve_reporter_type_name(root, info):
        check_ticket_perms(info)
        if TicketGQLType._should_restrict_field('reporter_type', root, info):
            return None
        return root.reporter_type.name if root.reporter_type else None

    @staticmethod
    def resolve_reporter(root, info):
        check_ticket_perms(info)
        if TicketGQLType._should_restrict_field('reporter', root, info):
            return None
        return model_obj_to_json(root.reporter) if root.reporter else None

    @staticmethod
    def resolve_is_history(root, info):
        check_ticket_perms(info)
        return not root.version == Ticket.objects.get(id=root.id).version

    @staticmethod
    def resolve_reporter_first_name(root, info):
        check_ticket_perms(info)
        if TicketGQLType._should_restrict_field('reporter_first_name', root, info):
            return RESTRICTED_VALUE
        if root.reporter_type:
            content_type = ContentType.objects.get_for_model(root.reporter_type.model_class())
            if content_type:
                model_object = content_type.get_object_for_this_type(pk=root.reporter_id)
                if model_object:
                    if root.reporter_type.name == 'individual':
                        return model_object.first_name
                    elif root.reporter_type.name == 'beneficiary':
                        return model_object.individual.first_name
                    elif root.reporter_type.name == 'user':
                        return None
        return None

    @staticmethod
    def resolve_reporter_last_name(root, info):
        check_ticket_perms(info)
        if TicketGQLType._should_restrict_field('reporter_last_name', root, info):
            return RESTRICTED_VALUE
        if root.reporter_type:
            content_type = ContentType.objects.get_for_model(root.reporter_type.model_class())
            if content_type:
                model_object = content_type.get_object_for_this_type(pk=root.reporter_id)
                if model_object:
                    if root.reporter_type.name == 'individual':
                        return model_object.last_name
                    elif root.reporter_type.name == 'beneficiary':
                        return model_object.individual.last_name
                    elif root.reporter_type.name == 'user':
                        return None
        return None

    @staticmethod
    def resolve_reporter_dob(root, info):
        check_ticket_perms(info)
        if TicketGQLType._should_restrict_field('reporter_dob', root, info):
            return None
        if root.reporter_type:
            content_type = ContentType.objects.get_for_model(root.reporter_type.model_class())
            if content_type:
                model_object = content_type.get_object_for_this_type(pk=root.reporter_id)
                if model_object:
                    if root.reporter_type.name == 'individual':
                        return model_object.dob
                    elif root.reporter_type.name == 'beneficiary':
                        return model_object.individual.dob
                    elif root.reporter_type.name == 'user':
                        return None
        return None

    @staticmethod
    def resolve_description(root, info):
        return TicketGQLType._restricted_resolve(root, info, 'description')

    @staticmethod
    def resolve_resolution(root, info):
        return TicketGQLType._restricted_resolve(root, info, 'resolution')

    @staticmethod
    def resolve_reporter_id(root, info):
        return TicketGQLType._restricted_resolve(root, info, 'reporter_id', restricted_value=None)

    @staticmethod
    def resolve_channel(root, info):
        return TicketGQLType._restricted_resolve(root, info, 'channel')

    @staticmethod
    def resolve_attending_staff(root, info):
        return TicketGQLType._restricted_resolve(root, info, 'attending_staff', restricted_value=None)

    @staticmethod
    def resolve_json_ext(root, info):
        return TicketGQLType._restricted_resolve(root, info, 'json_ext', restricted_value=None)

    @staticmethod
    def resolve_category(root, info):
        return TicketGQLType._restricted_resolve(root, info, 'category', preserve_none=True)

    @staticmethod
    def resolve_flags(root, info):
        return TicketGQLType._restricted_resolve(root, info, 'flags', preserve_none=True)

    @staticmethod
    def resolve_title(root, info):
        return TicketGQLType._restricted_resolve(root, info, 'title', preserve_none=True)

    @staticmethod
    def resolve_status(root, info):
        return TicketGQLType._restricted_resolve(root, info, 'status', restricted_value=None)

    @staticmethod
    def resolve_priority(root, info):
        return TicketGQLType._restricted_resolve(root, info, 'priority', restricted_value=None)

    @staticmethod
    def resolve_date_created(root, info):
        return TicketGQLType._restricted_resolve(root, info, 'date_created', restricted_value=None)

    @staticmethod
    def resolve_date_of_incident(root, info):
        return TicketGQLType._restricted_resolve(root, info, 'date_of_incident', restricted_value=None)

    @staticmethod
    def resolve_due_date(root, info):
        return TicketGQLType._restricted_resolve(root, info, 'due_date', restricted_value=None)

    class Meta:
        model = Ticket
        interfaces = (graphene.relay.Node,)
        # TicketFilterSet enforces field-level filter restrictions:
        # users with restricted_read access cannot filter on fields
        # outside their visible_fields configuration.
        filterset_class = TicketFilterSet

        connection_class = ExtendedConnection

    def resolve_client_mutation_id(self, info):
        ticket_mutation = self.mutations.select_related(
            'mutation').filter(mutation__status=0).first()
        return ticket_mutation.mutation.client_mutation_id if ticket_mutation else None


class CommentGQLType(DjangoObjectType):
    commenter = graphene.JSONString()
    commenter_type = graphene.Int()
    commenter_type_name = graphene.String()

    commenter_first_name = graphene.String()
    commenter_last_name = graphene.String()
    commenter_dob = graphene.String()

    @staticmethod
    def resolve_commenter_type(root, info):
        check_comment_perms(info)
        return root.commenter_type.id if root.commenter_type else None

    @staticmethod
    def resolve_commenter_type_name(root, info):
        check_comment_perms(info)
        return root.commenter_type.name if root.commenter_type else None

    @staticmethod
    def resolve_commenter(root, info):
        check_comment_perms(info)
        return model_obj_to_json(root.commenter) if root.commenter else None

    @staticmethod
    def resolve_commenter_first_name(root, info):
        check_comment_perms(info)
        if root.commenter_type:
            content_type = ContentType.objects.get_for_model(root.commenter_type.model_class())
            if content_type:
                model_object = content_type.get_object_for_this_type(pk=root.commenter_id)
                if model_object:
                    if root.commenter_type.name == 'individual':
                        return model_object.first_name
                    elif root.commenter_type.name == 'beneficiary':
                        return model_object.individual.first_name
                    elif root.commenter_type.name == 'user':
                        return None
        return None

    @staticmethod
    def resolve_commenter_last_name(root, info):
        check_comment_perms(info)
        if root.commenter_type:
            content_type = ContentType.objects.get_for_model(root.commenter_type.model_class())
            if content_type:
                model_object = content_type.get_object_for_this_type(pk=root.commenter_id)
                if model_object:
                    if root.commenter_type.name == 'individual':
                        return model_object.last_name
                    elif root.commenter_type.name == 'beneficiary':
                        return model_object.individual.last_name
                    elif root.commenter_type.name == 'user':
                        return None
        return None

    @staticmethod
    def resolve_commenter_dob(root, info):
        check_comment_perms(info)
        if root.commenter_type:
            content_type = ContentType.objects.get_for_model(root.commenter_type.model_class())
            if content_type:
                model_object = content_type.get_object_for_this_type(pk=root.commenter_id)
                if model_object:
                    if root.commenter_type.name == 'individual':
                        return model_object.dob
                    elif root.commenter_type.name == 'beneficiary':
                        return model_object.individual.dob
                    elif root.commenter_type.name == 'user':
                        return None
        return None

    class Meta:
        model = Comment
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact", "isnull"],
            "comment": ["exact", "istartswith", "icontains", "iexact"],
            "date_created": ["exact", "istartswith", "icontains", "iexact"],
            "is_resolution": ["exact"],
            **prefix_filterset("ticket__", TICKET_FILTER_FIELDS),
        }

        connection_class = ExtendedConnection


# class TicketAttachmentGQLType(DjangoObjectType):
#     class Meta:
#         model = TicketAttachment
#         interfaces = (graphene.relay.Node,)
#         filter_fields = {
#             "id": ["exact"],
#             "filename": ["exact", "icontains"],
#             "mime_type": ["exact", "icontains"],
#             "url": ["exact", "icontains"],
#             **prefix_filterset("ticket__", TICKET_FILTER_FIELDS),
#         }
#         connection_class = ExtendedConnection
#
#     @classmethod
#     def get_queryset(cls, queryset, info):
#         queryset = queryset.filter(*filter_validity())
#         return queryset


class AttendingStaffRoleGQLType(ObjectType):
    category = graphene.String()
    role_ids = graphene.List(graphene.String)


class ResolutionTimesByCategoryGQLType(ObjectType):
    category = graphene.String()
    resolution_time = graphene.String()


class GrievanceCategoryGQLType(ObjectType):
    name = graphene.String()
    full_name = graphene.String()
    priority = graphene.String()
    permissions = graphene.JSONString()
    default_flags = graphene.List(graphene.String)
    children = graphene.List(lambda: GrievanceCategoryGQLType)


class GrievanceFlagGQLType(ObjectType):
    name = graphene.String()
    priority = graphene.String()
    permissions = graphene.JSONString()


class GrievanceTypeConfigurationGQLType(ObjectType):
    grievance_types = graphene.List(graphene.String)
    grievance_flags = graphene.List(graphene.String)
    grievance_channels = graphene.List(graphene.String)
    grievance_category_staff_roles = graphene.List(AttendingStaffRoleGQLType)
    grievance_default_resolutions_by_category = graphene.List(ResolutionTimesByCategoryGQLType)
    # Enhanced fields
    grievance_categories_hierarchical = graphene.List(GrievanceCategoryGQLType)
    grievance_categories_json = graphene.JSONString()
    grievance_flags_detailed = graphene.List(GrievanceFlagGQLType)

    def resolve_grievance_types(self, info):
        # Return accessible categories in flat format for backward compatibility
        user = info.context.user
        return GrievanceAccessControl.get_accessible_categories(user)

    def resolve_grievance_flags(self, info):
        # Return accessible flags
        user = info.context.user
        return GrievanceAccessControl.get_accessible_flags(user)

    def resolve_grievance_channels(self, info):
        return TicketConfig.grievance_channels

    def resolve_grievance_categories_json(self, info):
        """Return full category hierarchy as JSON — no depth limitation"""
        user = info.context.user
        return GrievanceAccessControl.get_category_hierarchy(user)

    def resolve_grievance_categories_hierarchical(self, info):
        """Return hierarchical category structure with access control"""
        user = info.context.user
        hierarchy = GrievanceAccessControl.get_category_hierarchy(user)

        def build_gql_category(cat_dict):
            """Convert dict to GQL type"""
            children = [build_gql_category(child) for child in cat_dict.get('children', [])]
            return GrievanceCategoryGQLType(
                name=cat_dict['name'],
                full_name=cat_dict.get('full_name', cat_dict['name']),
                priority=cat_dict.get('priority', 'Medium'),
                permissions=cat_dict.get('permissions', {}),
                default_flags=cat_dict.get('default_flags', []),
                children=children
            )

        return [build_gql_category(cat) for cat in hierarchy]

    def resolve_grievance_flags_detailed(self, info):
        """Return detailed flag information with access control"""

        user = info.context.user
        accessible_flags = GrievanceAccessControl.get_accessible_flags(user)
        processed_flags = getattr(TicketConfig, 'processed_flags', {})

        flags = []
        for flag_name in accessible_flags:
            flag_info = processed_flags.get(flag_name, {})
            flags.append(GrievanceFlagGQLType(
                name=flag_name,
                priority=flag_info.get('priority', 'Medium'),
                permissions=flag_info.get('permissions', {})
            ))

        return flags

    def resolve_grievance_category_staff_roles(self, info):
        return [
            AttendingStaffRoleGQLType(category=category_key, role_ids=role_ids)
            for category_key, role_ids in TicketConfig.default_attending_staff_role_ids.items()
        ]

    def resolve_grievance_default_resolutions_by_category(self, info):
        resolution_mapping = getattr(
            TicketConfig, 'unified_resolution_times', TicketConfig.default_resolution
        )
        return [
            ResolutionTimesByCategoryGQLType(category=category_key, resolution_time=resolution_time)
            for category_key, resolution_time in resolution_mapping.items()
        ]
