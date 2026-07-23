import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from django.contrib.contenttypes.models import ContentType

from core.models import User
from core.validation import BaseModelValidation, ObjectExistsValidationMixin
from grievance_social_protection.models import Ticket, Comment


class TicketValidation(BaseModelValidation):
    OBJECT_TYPE = Ticket

    @classmethod
    def validate_create(cls, user, **data):
        errors = []

        unique_code_errors = validate_ticket_unique_code(data)
        for error in unique_code_errors:
            errors.append(ValidationError(error, code='unique_code_error'))

        if errors:
            raise ValidationError(errors)

        super().validate_create(user, **data)

    @classmethod
    def validate_update(cls, user, **data):
        errors = []

        unique_code_errors = validate_ticket_unique_code(data)
        for error in unique_code_errors:
            errors.append(ValidationError(error, code='unique_code_error'))

        if errors:
            raise ValidationError(errors)

        super().validate_update(user, **data)


class CommentValidation(ObjectExistsValidationMixin):
    OBJECT_TYPE = Comment

    @classmethod
    def validate_create(cls, user, **data):
        errors = [
            *validate_ticket_exists(data),
        ]
        if errors:
            raise ValidationError(errors)

    @classmethod
    def validate_resolve_grievance_by_comment(cls, user, **data):
        cls.validate_object_exists(data.get('id'))
        errors = []
        if errors:
            raise ValidationError(errors)


def validate_ticket_exists(data):
    ticket_id = data.get('ticket_id')
    if not Ticket.objects.filter(id=ticket_id).exists():
        return [{"message": _("validations.CommentValidation.validate_ticket_exists") % {"ticket_id": ticket_id}}]
    return []


RESOLUTION_TIME_PATTERN = re.compile(r"^(?P<days>[0-9]{1,2}),(?P<hours>[0-9]{1,2})$")


def parse_resolution_time(value):
    """
    Parse a '{days},{hours}' SLA string (as used by resolution_times / resolution)
    into (days, hours) ints. Returns None if `value` is falsy or out of range,
    rather than raising — callers use this to opt out of an SLA silently.
    """
    if not value:
        return None
    match = RESOLUTION_TIME_PATTERN.match(value)
    if not match:
        return None
    days = int(match.group("days"))
    hours = int(match.group("hours"))
    if not (0 <= days < 99 and 0 <= hours < 24):
        return None
    return days, hours


def validate_resolution(data):
    """
    Validates that `value` is in the format '{days},{hours}'
    where days are in the range <0, 99) and hours are in the range <0, 24).
    """
    resolution = data.get('resolution')
    if not resolution:
        return None

    pattern = r"^(?P<days>[0-9]{1,2}),(?P<hours>[0-9]{1,2})$"
    match = re.match(pattern, resolution)
    if not match:
        return {"message": _("validations.TicketValidation.validate_resolution.invalid_format")}
    else:
        days = int(match.group("days"))
        hours = int(match.group("hours"))

        if not (0 <= days < 99):
            return {"message": _("validations.TicketValidation.validate_resolution.invalid_day_value")}
        if not (0 <= hours < 24):
            return {"message": _("validations.TicketValidation.validate_resolution.invalid_hour_value")}

    return None


def validate_wage_amount(data):
    """
    Validates that `wage_amount`, if provided, is a non-negative numeric value
    """
    wage_amount = data.get('wage_amount')
    if wage_amount in (None, ''):
        return None

    try:
        value = Decimal(str(wage_amount))
    except (InvalidOperation, ValueError, TypeError):
        return {"message": _("validations.TicketValidation.validate_wage_amount.invalid_format")}

    if value < 0:
        return {"message": _("validations.TicketValidation.validate_wage_amount.negative_value")}

    return None


def validate_commenter_exists(data):
    commenter_type = data.get('commenter_type')
    commenter_id = data.get('commenter_id')
    model_class = commenter_type.model_class()

    if not model_class.objects.filter(id=commenter_id).exists():
        return [{"message": _("validations.CommentValidation.validate_commenter_exists")}]

    return []


def validate_commenter_associated_with_ticket(data):
    commenter_type = data.get('commenter_type')
    commenter_id = data.get('commenter_id')

    model_class = commenter_type.model_class()
    commenter = model_class.objects.get(id=commenter_id)

    if isinstance(commenter, User):
        attending_staff_tickets = Ticket.objects.filter(attending_staff=commenter)
        if attending_staff_tickets.exists():
            return []

    reporter_tickets = Ticket.objects.filter(reporter_type=commenter_type, reporter_id=commenter_id)
    if reporter_tickets.exists():
        return []

    return [{"message": _("validations.CommentValidation.commenter_not_associated_with_ticket")}]


def user_associated_with_ticket(user):
    if isinstance(user, User):
        if Ticket.objects.filter(attending_staff=user).exists():
            return True
    return False


def validate_ticket_unique_code(data):
    code = data.get('code')
    ticket_id = data.get('id')

    if not code:
        return []

    ticket_queryset = Ticket.objects.filter(code=code)
    if ticket_id:
        ticket_queryset.exclude(id=ticket_id)
    if ticket_queryset.exists():
        return [{"message": _("validations.TicketValidation.validate_ticket_unique_code") % {"code": code}}]
    return []


def validate_reporter(data):
    reporter_type = data.get("reporter_type")
    reporter_id = data.get("reporter_id")

    if reporter_type and reporter_id:
        if reporter_type not in ["User", "Individual"]:
            return [{"message": _("validations.TicketValidation.invalid_reporter_type")}]

        try:
            content_type = ContentType.objects.get(model=reporter_type.lower())
        except Exception:
            return [{"message": _("validations.TicketValidation.reporter_type_invalid")}]

        try:
            content_type.get_object_for_this_type(id=reporter_id)
        except Exception:
            return [{"message": _("validations.TicketValidation.reporter_not_found")}]

        return []

    error_messages = []
    if not reporter_type:
        error_messages.append({"message": _("validations.TicketValidation.reporter_type_required")})
    if not reporter_id:
        error_messages.append({"message": _("validations.TicketValidation.reporter_id_required")})

    return error_messages
