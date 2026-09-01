import graphene
import pandas as pd
from django.contrib.auth.models import AnonymousUser

from core.schema import OrderedDjangoFilterConnectionField
from core.schema import signal_mutation_module_validate
from django.db.models import Q
import graphene_django_optimizer as gql_optimizer

from core.custom_filters import CustomFilterWizardStorage
from core.gql.export_mixin import ExportableQueryMixin
from core.utils import append_validity_filter
from .apps import MODULE_NAME
from .access_control import GrievanceAccessControl

from .gql_queries import (
    TicketGQLType, CommentGQLType, GrievanceTypeConfigurationGQLType,
)
from .gql_mutations import (
    CreateTicketMutation, UpdateTicketMutation, DeleteTicketMutation,
    CreateCommentMutation, ResolveGrievanceByCommentMutation, ReopenTicketMutation,
)
from .models import Ticket, Comment, TicketMutation
from .apps import TicketConfig
from .validations import user_associated_with_ticket
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext_lazy as _


def _unfold_json_ext(data_df):
    """
    Flatten json_ext into separate columns, so a caller can request denormalised/reporter fields
    (e.g. form_number, location_name) by including 'json_ext' in `fields`.
    Note: computed, non-DB values (e.g. duration_days) aren't reachable via
    values_list at all and so can't be exported this way — out of scope here.
    """
    if 'json_ext' in data_df:
        df_unfolded = pd.json_normalize(data_df['json_ext'])
        df_final = pd.concat([data_df, df_unfolded], axis=1)
        df_final = df_final.drop('json_ext', axis=1)
        return df_final
    return data_df


class Query(ExportableQueryMixin, graphene.ObjectType):
    # auto-generates a `tickets_export` field reusing resolve_tickets
    # (and therefore its category/flag access control + view scoping)
    # as the base queryset, then applies the same first-class/custom filters
    # as the tickets query before writing out a CSV export.
    export_patches = {'tickets': [_unfold_json_ext]}
    exportable_fields = ['tickets']
    module_name = MODULE_NAME
    object_type = "Ticket"

    tickets = OrderedDjangoFilterConnectionField(
        TicketGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        show_history=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        ticket_version=graphene.Int(),
        customFilters=graphene.List(of_type=graphene.String),
    )

    ticketsStr = OrderedDjangoFilterConnectionField(
        TicketGQLType,
        str=graphene.String(),
    )
    # ticket_attachments = DjangoFilterConnectionField(TicketAttachmentGQLType)

    ticket_details = OrderedDjangoFilterConnectionField(
        TicketGQLType,
        # showHistory=graphene.Boolean(),
        orderBy=graphene.List(of_type=graphene.String),
    )

    grievance_config = graphene.Field(GrievanceTypeConfigurationGQLType)

    # Participant json_ext a ticket would get for the given reporter, without
    # creating one — lets the intake form show district / catchment / project /
    # hotspot before save.
    grievance_reporter_derived_fields = graphene.JSONString(
        reporter_type=graphene.String(required=True),
        reporter_id=graphene.String(required=True),
    )

    comments = OrderedDjangoFilterConnectionField(
        CommentGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )

    def resolve_comments(self, info, **kwargs):
        user = info.context.user

        if not (user_associated_with_ticket(user) or user.has_perms(TicketConfig.gql_query_comments_perms)):
            raise PermissionDenied(_("Unauthorized"))

        return gql_optimizer.query(Comment.objects.all(), info)

    def resolve_ticket_details(self, info, **kwargs):
        if not info.context.user.has_perms(TicketConfig.gql_query_tickets_perms):
            raise PermissionDenied(_("unauthorized"))

        query = Ticket.objects.filter(*append_validity_filter(**kwargs)).all().order_by('title')

        # Apply category and flag permission filtering
        query = GrievanceAccessControl.filter_ticket_queryset(query, info.context.user)
        query = GrievanceAccessControl.apply_view_scope(query, info.context.user)

        return gql_optimizer.query(query, info)

    def resolve_tickets(self, info, **kwargs):
        """
        Extra steps to perform when Scheme is queried
        """
        # Check if user has permission
        if not info.context.user.has_perms(TicketConfig.gql_query_tickets_perms):
            raise PermissionDenied(_("unauthorized"))
        filters = []
        model = Ticket

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        # Used to specify if user want to see all records including invalid records as history
        show_history = kwargs.get('show_history', False)
        ticket_version = kwargs.get('ticket_version', False)
        if show_history or ticket_version:
            if ticket_version:
                filters.append(Q(version=ticket_version))
            query = model.history.filter(*filters).all().as_instances()
        else:
            query = model.objects.filter(*filters, is_deleted=False).all()

        # Apply category and flag permission filtering
        query = GrievanceAccessControl.filter_ticket_queryset(query, info.context.user)
        query = GrievanceAccessControl.apply_view_scope(query, info.context.user)

        custom_filters = kwargs.get("customFilters")
        if custom_filters:
            query = CustomFilterWizardStorage.build_custom_filters_queryset(
                MODULE_NAME, 'Ticket', custom_filters, query,
            )

        return gql_optimizer.query(query, info)

    def resolve_ticketsStr(self, info, **kwargs):
        """
        Extra steps to perform when Scheme is queried
        """
        # Check if user has permission
        if not info.context.user.has_perms(TicketConfig.gql_query_tickets_perms):
            raise PermissionDenied(_("unauthorized"))
        filters = []

        # Used to specify if user want to see all records including invalid records as history
        show_history = kwargs.get('show_history', False)
        if not show_history:
            filters += append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        # str = kwargs.get('str')
        # if str is not None:
        #     filters += [Q(code__icontains=str) | Q(name__icontains=str)]

        query = Ticket.objects.filter(*filters).all()

        # Apply category and flag permission filtering
        query = GrievanceAccessControl.filter_ticket_queryset(query, info.context.user)
        query = GrievanceAccessControl.apply_view_scope(query, info.context.user)

        return gql_optimizer.query(query, info)

    # def resolve_claim_attachments(self, info, **kwargs):
    #     if not info.context.user.has_perms(TicketConfig.gql_query_tickets_perms):
    #         raise PermissionDenied(_("unauthorized"))

    def resolve_grievance_config(self, info, **kwargs):
        user = info.context.user
        if type(user) is AnonymousUser:
            raise PermissionDenied(_("unauthorized"))
        if not info.context.user.has_perms(TicketConfig.gql_query_tickets_perms):
            raise PermissionDenied(_("unauthorized"))
        return GrievanceTypeConfigurationGQLType()

    def resolve_grievance_reporter_derived_fields(self, info, reporter_type, reporter_id, **kwargs):
        user = info.context.user
        if isinstance(user, AnonymousUser) or not user.has_perms(TicketConfig.gql_query_tickets_perms):
            raise PermissionDenied(_("unauthorized"))
        from .services import TicketService
        return TicketService(user).preview_reporter_derived_fields(reporter_type, reporter_id)


# ExportableQueryMixin auto-generates resolve_tickets_export with no
# enable_export hook of its own; wrap it so a deployment can disable export
# without losing the mixin's filtering/permission reuse.
_auto_resolve_tickets_export = Query.resolve_tickets_export


def _resolve_tickets_export_gated(self, info, **kwargs):
    if not TicketConfig.enable_export:
        raise PermissionDenied(_("Export is disabled for this deployment."))
    return _auto_resolve_tickets_export(self, info, **kwargs)


Query.resolve_tickets_export = _resolve_tickets_export_gated


class Mutation(graphene.ObjectType):
    create_Ticket = CreateTicketMutation.Field()
    update_Ticket = UpdateTicketMutation.Field()
    delete_Ticket = DeleteTicketMutation.Field()

    create_comment = CreateCommentMutation.Field()

    resolve_grievance_by_comment = ResolveGrievanceByCommentMutation.Field()
    reopen_ticket = ReopenTicketMutation.Field()

    # create_ticket_attachment = CreateTicketAttachmentMutation.Field()
    # update_ticket_attachment = UpdateTicketAttachmentMutation.Field()


def on_bank_mutation(kwargs, k='uuid'):
    """
    This method is called on signal binding for scheme mutation
    """

    # get uuid from data
    ticket_uuid = kwargs['data'].get('uuid', None)
    if not ticket_uuid:
        return []
    # fetch the scheme object by uuid
    impacted_ticket = Ticket.objects.get(Q(uuid=ticket_uuid))
    # Create a mutation object
    TicketMutation.objects.create(Bank=impacted_ticket, mutation_id=kwargs['mutation_log_id'])
    return []


def on_ticket_mutation(**kwargs):
    uuids = kwargs["data"].get("uuids", [])
    if not uuids:
        uuid = kwargs["data"].get("claim_uuid", None)
        uuids = [uuid] if uuid else []
    if not uuids:
        return []
    impacted_tickets = Ticket.objects.filter(uuid__in=uuids).all()
    for ticket in impacted_tickets:
        TicketMutation.objects.create(Ticket=ticket, mutation_id=kwargs["mutation_log_id"])
    return []


def bind_signals():
    signal_mutation_module_validate[MODULE_NAME].connect(on_ticket_mutation)
