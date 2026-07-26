import graphene

from core.gql.gql_mutations.base_mutation import BaseHistoryModelCreateMutationMixin, BaseMutation, \
    BaseHistoryModelUpdateMutationMixin, BaseHistoryModelDeleteMutationMixin
from core.schema import OpenIMISMutation
from .models import Ticket, TicketMutation, Comment

from django.core.exceptions import ValidationError, PermissionDenied
from .apps import TicketConfig
from django.utils.translation import gettext_lazy as _

from .services import TicketService, CommentService
from .validations import user_associated_with_ticket


class CreateTicketInputType(OpenIMISMutation.Input):
    class TicketStatusEnum(graphene.Enum):
        RECEIVED = Ticket.TicketStatus.RECEIVED
        OPEN = Ticket.TicketStatus.OPEN
        IN_PROGRESS = Ticket.TicketStatus.IN_PROGRESS
        REFERRED = Ticket.TicketStatus.REFERRED
        RESOLVED = Ticket.TicketStatus.RESOLVED
        CLOSED = Ticket.TicketStatus.CLOSED

    key = graphene.String(required=False)
    title = graphene.String(required=False)
    description = graphene.String(required=False)
    reporter_type = graphene.String(required=False, max_lenght=255)
    reporter_id = graphene.String(required=False, max_lenght=255)
    attending_staff_id = graphene.UUID(required=False)
    date_of_incident = graphene.Date(required=False)
    status = graphene.Field(TicketStatusEnum, required=False)
    priority = graphene.String(required=False)
    due_date = graphene.Date(required=False)
    category = graphene.String(required=False)
    flags = graphene.String(required=False)
    channel = graphene.String(required=False)
    resolution = graphene.String(required=False)
    wage_amount = graphene.Decimal(required=False)
    referred_to = graphene.String(required=False)


class UpdateTicketInputType(CreateTicketInputType):
    id = graphene.UUID(required=True)


class ResolveGrievanceByCommentInputType(OpenIMISMutation.Input):
    id = graphene.UUID(required=True)


class CreateCommentInputType(OpenIMISMutation.Input):
    ticket_id = graphene.UUID(required=True)
    commenter_type = graphene.String(required=False, max_lenght=255)
    commenter_id = graphene.String(required=False, max_lenght=255)
    comment = graphene.String(required=True)


class CreateTicketMutation(BaseHistoryModelCreateMutationMixin, BaseMutation):
    _mutation_class = "CreateTicketMutation"
    _mutation_module = "grievance_social_protection"
    _model = Ticket

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(TicketConfig.gql_mutation_create_tickets_perms):
            raise PermissionDenied(_("unauthorized"))

    @classmethod
    def _mutate(cls, user, **data):
        client_mutation_id = data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = TicketService(user)
        response = service.create(data)
        if client_mutation_id:
            ticket_id = response['data']['id']
            ticket = Ticket.objects.get(id=ticket_id)
            TicketMutation.object_mutated(user, client_mutation_id=client_mutation_id, ticket=ticket)

        if not response['success']:
            return response
        return None

    class Input(CreateTicketInputType):
        pass


class UpdateTicketMutation(BaseHistoryModelUpdateMutationMixin, BaseMutation):
    _mutation_class = "UpdateTicketMutation"
    _mutation_module = "grievance_social_protection"
    _model = Ticket

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(TicketConfig.gql_mutation_update_tickets_perms):
            raise PermissionDenied(_("unauthorized"))

    @classmethod
    def _mutate(cls, user, **data):
        client_mutation_id = data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = TicketService(user)
        response = service.update(data)
        if client_mutation_id:
            ticket_id = response['data']['id']
            ticket = Ticket.objects.get(id=ticket_id)
            TicketMutation.object_mutated(user, client_mutation_id=client_mutation_id, ticket=ticket)
        if not response['success']:
            return response
        return None

    class Input(UpdateTicketInputType):
        pass


class DeleteTicketMutation(BaseHistoryModelDeleteMutationMixin, BaseMutation):
    _mutation_class = "DeleteTicketMutation"
    _mutation_module = "grievance_social_protection"
    _model = Ticket

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(
                TicketConfig.gql_mutation_delete_tickets_perms):
            raise ValidationError("mutation.authentication_required")

    class Input(OpenIMISMutation.Input):
        ids = graphene.List(graphene.UUID)


class CreateCommentMutation(BaseHistoryModelCreateMutationMixin, BaseMutation):
    _mutation_class = "CreateCommentMutation"
    _mutation_module = "grievance_social_protection"
    _model = Comment

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if user.has_perms(TicketConfig.gql_mutation_delete_tickets_perms):
            return
        if user_associated_with_ticket(user):
            return
        raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        if "commenter_type" in data:
            data['commenter_type'] = data.get('commenter_type', '').lower()
        service = CommentService(user)
        response = service.create(data)

        if not response['success']:
            return response
        return None

    class Input(CreateCommentInputType):
        pass


class ResolveGrievanceByCommentMutation(BaseHistoryModelUpdateMutationMixin, BaseMutation):
    _mutation_class = "ResolveGrievanceByCommentMutation"
    _mutation_module = "grievance_social_protection"
    _model = Comment

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(TicketConfig.gql_mutation_resolve_grievance_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        client_mutation_id = data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = CommentService(user)
        response = service.resolve_grievance_by_comment(data)
        if client_mutation_id:
            comment_id = data.get('id')
            ticket = Comment.objects.get(id=comment_id).ticket
            TicketMutation.object_mutated(user, client_mutation_id=client_mutation_id, ticket=ticket)

        if not response['success']:
            return response
        return None

    class Input(ResolveGrievanceByCommentInputType):
        pass


class ReopenTicketMutation(BaseHistoryModelUpdateMutationMixin, BaseMutation):
    _mutation_class = "ReopenTicketMutation"
    _mutation_module = "grievance_social_protection"
    _model = Ticket

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(TicketConfig.gql_mutation_update_tickets_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        client_mutation_id = data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = TicketService(user)
        response = service.reopen_ticket(data)
        if client_mutation_id:
            ticket_id = data.get('id')
            ticket = Ticket.objects.get(id=ticket_id)
            TicketMutation.object_mutated(user, client_mutation_id=client_mutation_id, ticket=ticket)

        if not response['success']:
            return response
        return None

    class Input(ResolveGrievanceByCommentInputType):
        pass

# class CreateTicketAttachmentMutation(OpenIMISMutation):
#     _mutation_module = "grievance_social_protection"
#     _mutation_class = "CreateTicketAttachmentMutation"
#
#     class Input(AttachmentInputType):
#         pass
#
#     @classmethod
#     def async_mutate(cls, user, **data):
#         ticket = None
#         try:
#             if user.is_anonymous or not user.has_perms(TicketConfig.gql_mutation_update_tickets_perms):
#                 raise PermissionDenied(_("unauthorized"))
#             if "client_mutation_id" in data:
#                 data.pop('client_mutation_id')
#             if "client_mutation_label" in data:
#                 data.pop('client_mutation_label')
#             ticket_uuid = data.pop('ticket_uuid')
#             queryset = Ticket.objects.filter(*filter_validity())
#             ticket = queryset.filter(uuid=ticket_uuid).first()
#             if not ticket:
#                 raise PermissionDenied(_("unathorized"))
#             create_attachment(ticket.id, data)
#             return None
#         except Exception as exc:
#             return [{
#                 'message': _("ticket.mutation.failed_to_attach_document"),
#                 'detail': str(exc)}]


# class UpdateTicketAttachmentMutation(OpenIMISMutation):
#     _mutation_module = "grievance_social_protection"
#     _mutation_class = "UpdateTicketAttachmentMutation"
#
#     class Input(BaseAttachmentInputType):
#         pass
#
#     @classmethod
#     def async_mutate(cls, user, **data):
#
#         try:
#             if not user.has_perms(TicketConfig.gql_mutation_update_tickets_perms):
#                 raise PermissionDenied(_("unauthorized"))
#             # get ticketattachment uuid
#             ticketattachment_uuid = data.pop('uuid')
#             queryset = TicketAttachment.objects.filter(*filter_validity())
#             if ticketattachment_uuid:
#                 # fetch ticketattachment uuid
#                 ticketattachment = queryset.filter(uuid=ticketattachment_uuid).first()
#                 [setattr(ticketattachment, key, data[key]) for key in data]
#             else:
#                 # raise an error if uuid is not valid or does not exist
#                 raise PermissionDenied(_("unauthorized"))
#             # saves update dta
#             ticketattachment.save()
#             return None
#         except Exception as exc:
#             return [{
#
#                 'message': _("ticket.mutation.failed_to_attach_document"),
#                 'detail': str(exc)}]

# class BaseAttachment:
#     id = graphene.String(required=False, read_only=True)
#     uuid = graphene.String(required=False)
#     filename = graphene.String(required=False)
#     mime_type = graphene.String(required=False)
#     url = graphene.String(required=False)
#     date = graphene.Date(required=False)


# class BaseAttachmentInputType(BaseAttachment, OpenIMISMutation.Input):
#     """
#     Ticket attachment (without the document), used on its own
#     """
#     ticket_uuid = graphene.String(required=False)
#
#
# class Attachment(BaseAttachment):
#     document = graphene.String(required=False)


# class TicketAttachmentInputType(Attachment, InputObjectType):
#     """
#     Ticket attachment, used nested in claim object
#     """
#     pass


# class AttachmentInputType(Attachment, OpenIMISMutation.Input):
#     """
#     Ticket attachment, used on its own
#     """
#     ticket_uuid = graphene.String(required=False)


# def create_file(date, ticket_id, document):
#     date_iso = date.isoformat()
#     root = TicketConfig.tickets_attachments_root_path
#     file_dir = '%s/%s/%s/%s' % (
#         date_iso[0:4],
#         date_iso[5:7],
#         date_iso[8:10],
#         ticket_id
#     )
#
#     file_path = '%s/%s' % (file_dir, uuid.uuid4())
#     pathlib.Path('%s/%s' % (root, file_dir)).mkdir(parents=True, exist_ok=True)
#     f = open('%s/%s' % (root, file_path), "xb")
#     f.write(base64.b64decode(document))
#     f.close()
#     return file_path
#
#
# def create_attachment(ticket_id, data):
#     if "client_mutation_id" in data:
#         data.pop('client_mutation_id')
#     # Check if client_mutation_label is passed in data
#     if "client_mutation_label" in data:
#         data.pop('client_mutation_label')
#     data['ticket_id'] = ticket_id
#     now = timezone.now()
#     if TicketConfig.tickets_attachments_root_path:
#         data['url'] = create_file(now, ticket_id, data.pop('document'))
#     data['validity_from'] = now
#     attachment = TicketAttachment.objects.create(**data)
#     attachment.save()
#     return attachment
#
#
# def create_attachments(ticket_id, attachments):
#     for attachment in attachments:
#         create_attachment(ticket_id, attachment)
