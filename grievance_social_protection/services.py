from datetime import date, timedelta

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError, PermissionDenied
from django.db.models import Max
from django.db import transaction

from core.services import BaseService
from core.signals import register_service_signal
from core.services.utils import check_authentication as check_authentication, output_exception, \
    model_representation, output_result_success
from grievance_social_protection.models import Ticket, Comment
from grievance_social_protection.validations import (
    TicketValidation,
    CommentValidation,
    validate_resolution,
    validate_wage_amount,
    parse_resolution_time
)
from grievance_social_protection.access_control import GrievanceAccessControl
from grievance_social_protection.apps import TicketConfig

# Reporter jsonExt keys (Individual `individual_schema`) denormalised onto
# ticket.json_ext at create, so the Ticket Custom Filter Wizard can
# filter on them without traversing the reporter's GenericForeignKey.
DENORMALIZED_REPORTER_JSON_EXT_FIELDS = (
    'form_number',
    'national_id',
    'location_code',
    'location_name',
    'traditional_authority_name',
    'group_village_head_name',
    'traditional_authority_code',
    'group_village_head_code',
)

# Safety cap on hierarchy walk depth, guarding against a parent cycle in
# location data. Today's 4 levels (R/D/W/V) need at most 3 hops (V->W->D->R);
# one hop of margin covers a future 5th level without needing this touched.
MAX_LOCATION_ANCESTOR_DEPTH = 4


def _resolve_district_ancestor(location_code):
    """
    Walk the location hierarchy up from `location_code` to the Region (R)
    ancestor. Returns (code, name), or (None, None) if the location isn't found or 
    has no R ancestor.
    """
    if not location_code:
        return None, None

    location_model = apps.get_model('location', 'Location')
    location = location_model.objects.select_related(
        'parent', 'parent__parent', 'parent__parent__parent'
    ).filter(code=location_code).first()

    depth = 0
    while location and location.type != 'R' and depth < MAX_LOCATION_ANCESTOR_DEPTH:
        location = location.parent
        depth += 1

    if location and location.type == 'R':
        return location.code, location.name
    return None, None


def _resolve_micro_catchment(gvh_code, ta_code):
    """
    Resolve the micro-catchment name from the reporter's Group Village
    Head code, falling back to their Traditional Authority
    code, via the location.MicroCatchmentGVH /
    MicroCatchmentTA link tables. Returns None if no code is given or no
    mapping exists.
    """
    if gvh_code:
        micro_catchment_gvh_model = apps.get_model('location', 'MicroCatchmentGVH')
        link = micro_catchment_gvh_model.objects.filter(
            *micro_catchment_gvh_model.filter_validity(), location__code=gvh_code
        ).select_related('micro_catchment').first()
        if link:
            return link.micro_catchment.name

    if ta_code:
        micro_catchment_ta_model = apps.get_model('location', 'MicroCatchmentTA')
        link = micro_catchment_ta_model.objects.filter(
            *micro_catchment_ta_model.filter_validity(), location__code=ta_code
        ).select_related('micro_catchment').first()
        if link:
            return link.micro_catchment.name

    return None


def _resolve_project_name(reporter_type, reporter_id):
    """
    Resolve the participant's project (benefit plan) name. Beneficiary
    reporters use their own benefit plan; individual reporters use their first
    Beneficiary record's plan (an individual may belong to more than one —
    first is a reasonable default pending product guidance).
    """
    model_object = reporter_type.get_object_for_this_type(pk=reporter_id)
    if not model_object:
        return None
    if reporter_type.name == 'beneficiary':
        return model_object.benefit_plan.name
    if reporter_type.name == 'individual':
        beneficiary = model_object.beneficiary_set.select_related('benefit_plan').first()
        if beneficiary:
            return beneficiary.benefit_plan.name
    return None


def _resolve_days_worked(reporter_type, reporter_id):
    """
    Resolve total days worked as the count of BeneficiaryProjectTimeEntry
    rows with percent_complete > 0 across the participant's project enrollments
    (project_social_protection.BeneficiaryProjectEnrollment/TimeEntry — a
    cash-for-work program's daily muster roll). Beneficiary reporters use their
    own enrollments; individual reporters use their first Beneficiary record's,
    mirroring _resolve_project_name.
    """
    model_object = reporter_type.get_object_for_this_type(pk=reporter_id)
    if not model_object:
        return None

    if reporter_type.name == 'beneficiary':
        beneficiary = model_object
    elif reporter_type.name == 'individual':
        beneficiary = model_object.beneficiary_set.first()
    else:
        beneficiary = None
    if not beneficiary:
        return None

    time_entry_model = apps.get_model('project_social_protection', 'BeneficiaryProjectTimeEntry')
    return time_entry_model.objects.filter(
        enrollment__beneficiary_id=beneficiary.id, percent_complete__gt=0
    ).count()


class TicketService(BaseService):
    OBJECT_TYPE = Ticket

    def __init__(self, user, validation_class=TicketValidation):
        super().__init__(user, validation_class)

    @register_service_signal('ticket_service.create')
    def create(self, obj_data):
        self._get_content_type(obj_data)
        self._generate_code(obj_data)
        # Assign default category before access control so permission
        # checks always have a category to validate against.
        if not obj_data.get('category'):
            obj_data['category'] = TicketConfig.default_grievance_type
        self._validate_access_control(obj_data, access_type=GrievanceAccessControl.PERM_CREATE)
        self._apply_category_defaults(obj_data)
        # Re-validate after defaults may have added restricted flags
        self._validate_access_control(obj_data, access_type=GrievanceAccessControl.PERM_CREATE)
        resolution_error = validate_resolution(obj_data)
        if resolution_error:
            raise ValidationError(resolution_error)
        wage_amount_error = validate_wage_amount(obj_data)
        if wage_amount_error:
            raise ValidationError(wage_amount_error)
        self._apply_default_status(obj_data)
        self._apply_due_date(obj_data)
        self._denormalize_reporter_fields(obj_data)
        self._apply_derived_district(obj_data)
        self._apply_derived_micro_catchment(obj_data)
        self._apply_derived_project_fields(obj_data)
        return super().create(obj_data)

    @register_service_signal('ticket_service.update')
    def update(self, obj_data):
        self._get_content_type(obj_data)
        self._validate_existing_ticket_access(obj_data, access_type=GrievanceAccessControl.PERM_UPDATE)
        self._validate_access_control(obj_data, access_type=GrievanceAccessControl.PERM_UPDATE)
        self._apply_category_defaults(obj_data)
        # Re-validate after defaults may have added restricted flags
        self._validate_access_control(obj_data, access_type=GrievanceAccessControl.PERM_UPDATE)
        resolution_error = validate_resolution(obj_data)
        if resolution_error:
            raise ValidationError(resolution_error)
        wage_amount_error = validate_wage_amount(obj_data)
        if wage_amount_error:
            raise ValidationError(wage_amount_error)
        return super().update(obj_data)

    @register_service_signal('ticket_service.delete')
    def delete(self, obj_data):
        self._validate_existing_ticket_access(obj_data, access_type=GrievanceAccessControl.PERM_DELETE)
        return super().delete(obj_data)

    def _check_access_or_raise(self, category, flags, access_type):
        """Validate ticket access and convert PermissionDenied to ValidationError"""
        try:
            GrievanceAccessControl.validate_ticket_access(
                self.user, category, flags, access_type
            )
        except PermissionDenied as e:
            raise ValidationError(str(e))

    def _validate_existing_ticket_access(self, obj_data, access_type):
        """Validate user has permission for the existing ticket's category and flags"""
        ticket_uuid = obj_data.get('uuid')
        ticket_id = obj_data.get('id')
        if not ticket_uuid and not ticket_id:
            return

        ticket = None
        base_qs = Ticket.filter_queryset()
        if ticket_uuid:
            ticket = base_qs.filter(uuid=ticket_uuid).first()
        if not ticket and ticket_id:
            if isinstance(ticket_id, int) or (isinstance(ticket_id, str) and ticket_id.isdigit()):
                ticket = base_qs.filter(id=ticket_id).first()
            else:
                ticket = base_qs.filter(uuid=ticket_id).first()
        if not ticket:
            raise ValidationError("Ticket does not exist.")

        self._check_access_or_raise(ticket.category, ticket.flags, access_type)

    @register_service_signal('ticket_service.reopen_ticket')
    @check_authentication
    def reopen_ticket(self, obj_data):
        try:
            with transaction.atomic():
                self.validation_class.validate_update(self.user, **obj_data)
                ticket_id = obj_data.get('id')
                ticket = Ticket.objects.filter(id=ticket_id).first()
                ticket.status = Ticket.TicketStatus.OPEN
                self._check_if_comment_resolution(ticket_id)
                ticket.save(user=self.user)
                return {
                    "success": True,
                    "message": "Ok",
                    "detail": "reopen_ticket",
                }
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="reopen_ticket", exception=exc)

    @transaction.atomic
    def _check_if_comment_resolution(self, ticket_id):
        comment_queryset = Comment.objects.filter(ticket_id=ticket_id, is_resolution=True)
        if comment_queryset.exists():
            comment = comment_queryset.first()
            comment.is_resolution = False
            comment.save(user=self.user)

    def _get_content_type(self, obj_data):
        if 'reporter_type' in obj_data:
            content_type = ContentType.objects.get(model=obj_data['reporter_type'].lower())
            obj_data['reporter_type'] = content_type

    def _generate_code(self, obj_data):
        if not obj_data.get('code'):
            last_ticket_code = Ticket.objects.filter(code__startswith='GRS').aggregate(Max('code')).get('code__max')
            if last_ticket_code is None:
                last_ticket_code_numeric = 0
            else:
                last_ticket_code_numeric = int(last_ticket_code[3:])

            new_ticket_code = f'GRS{last_ticket_code_numeric + 1:08}'
            obj_data['code'] = new_ticket_code

    def _validate_access_control(self, obj_data, access_type=GrievanceAccessControl.PERM_CREATE):
        """Validate user has permission to use selected category and flags"""
        category = obj_data.get('category')
        if category and TicketConfig.grievance_types:
            if category not in TicketConfig.grievance_types:
                raise ValidationError(
                    f"Unknown category: '{category}'. "
                    f"Must be one of the configured grievance types."
                )
        flags = obj_data.get('flags')
        if flags and TicketConfig.grievance_flags:
            flag_list = GrievanceAccessControl.parse_flags(flags)
            for flag in flag_list:
                if flag not in TicketConfig.grievance_flags:
                    raise ValidationError(
                        f"Unknown flag: '{flag}'. "
                        f"Must be one of the configured grievance flags."
                    )
        self._check_access_or_raise(
            obj_data.get('category'), obj_data.get('flags'), access_type
        )

    def _apply_category_defaults(self, obj_data):
        """Apply category defaults (flags, priority) if not already set"""
        category = obj_data.get('category')
        if not category:
            return

        # Get category defaults
        defaults = GrievanceAccessControl.get_category_defaults(category)

        # Apply default flags
        default_flags = defaults.get('default_flags', [])
        if default_flags:
            existing_flags = GrievanceAccessControl.parse_flags(obj_data.get('flags'))
            for flag in default_flags:
                if flag not in existing_flags:
                    existing_flags.append(flag)
            obj_data['flags'] = ' '.join(existing_flags)

        # Get effective priority if not set
        if not obj_data.get('priority'):
            obj_data['priority'] = GrievanceAccessControl.get_effective_priority(
                category, obj_data.get('flags')
            )

    def _apply_default_status(self, obj_data):
        """Default new tickets to the configured initial status (OPEN) when unset."""
        if obj_data.get('status'):
            return
        obj_data['status'] = self._get_initial_status()

    @staticmethod
    def _get_initial_status():
        for status in TicketConfig.ticket_statuses or []:
            if isinstance(status, dict) and status.get('initial') and status.get('code'):
                return status['code']
        # Sane fallback if ticket_statuses is misconfigured/empty at runtime
        return Ticket.TicketStatus.OPEN

    def _apply_due_date(self, obj_data):
        """
        Auto-compute due_date on create from the category's configured SLA
        (resolution_times), falling back to the ticket's own `resolution` value
        when the category has none configured. Categories without any SLA are
        left without a due_date — no error.
        """
        if obj_data.get('due_date'):
            return
        if not (TicketConfig.sla or {}).get('set_due_date_on_create', True):
            return

        category = obj_data.get('category')
        # processed_categories carries the current resolution_times directly (set
        # during category processing); unified_resolution_times additionally folds
        # in the legacy default_resolution mapping for plain-string categories.
        resolution_time = (TicketConfig.processed_categories or {}).get(category, {}).get('resolution_times')
        if not resolution_time:
            resolution_time = (TicketConfig.unified_resolution_times or {}).get(category)
        if not resolution_time:
            resolution_time = obj_data.get('resolution')

        parsed = parse_resolution_time(resolution_time)
        if not parsed:
            return

        days, hours = parsed
        due_date = date.today() + timedelta(days=days)
        if hours:
            # due_date is date-only; round a partial-day SLA up to the next day.
            due_date += timedelta(days=1)
        obj_data['due_date'] = due_date

    def _denormalize_reporter_fields(self, obj_data):
        """
        Copy searchable participant fields from the reporter's jsonExt into
        ticket.json_ext at create time. Django can't `.filter()` across the
        reporter's GenericForeignKey, so the Ticket Custom Filter Wizard
        filters the ticket's own json_ext instead.
        Missing individual data or an unsupported reporter type (e.g. User) is
        skipped silently; already-present json_ext keys are left untouched.
        """
        reporter_type = obj_data.get('reporter_type')
        reporter_id = obj_data.get('reporter_id')
        if not reporter_type or not reporter_id:
            return

        individual = self._resolve_reporter_individual(reporter_type, reporter_id)
        if not individual:
            return

        source = individual.json_ext or {}
        json_ext = dict(obj_data.get('json_ext') or {})
        for field in DENORMALIZED_REPORTER_JSON_EXT_FIELDS:
            value = source.get(field)
            if value not in (None, ''):
                json_ext[field] = value
        if json_ext:
            obj_data['json_ext'] = json_ext

    @staticmethod
    def _resolve_reporter_individual(reporter_type, reporter_id):
        """
        Return the Individual behind a reporter — directly, or via Beneficiary —
        or None. Mirrors the reporter_type.name branching already used in
        TicketGQLType.resolve_reporter_first_name/_last_name.
        """
        model_object = reporter_type.get_object_for_this_type(pk=reporter_id)
        if not model_object:
            return None
        if reporter_type.name == 'individual':
            return model_object
        if reporter_type.name == 'beneficiary':
            return model_object.individual
        return None  # 'user' reporters have no household jsonExt to denormalize

    def _apply_derived_district(self, obj_data):
        """
        Derive district_code/district_name from the location_code
        already denormalised onto ticket.json_ext by _denormalize_reporter_fields,
        walking up to the Region (R) ancestor. No location_code, or no R
        ancestor found, leaves the ticket without a district — no error.
        """
        json_ext = obj_data.get('json_ext') or {}
        location_code = json_ext.get('location_code')
        if not location_code:
            return

        district_code, district_name = _resolve_district_ancestor(location_code)
        if district_code:
            json_ext['district_code'] = district_code
            json_ext['district_name'] = district_name
            obj_data['json_ext'] = json_ext

    def _apply_derived_micro_catchment(self, obj_data):
        """
        Derive micro_catchment from the group_village_head_code /
        traditional_authority_code already denormalised onto ticket.json_ext
        by _denormalize_reporter_fields. No code, or no mapping found, leaves
        the ticket without a micro-catchment — no error.
        """
        json_ext = obj_data.get('json_ext') or {}
        gvh_code = json_ext.get('group_village_head_code')
        ta_code = json_ext.get('traditional_authority_code')
        if not gvh_code and not ta_code:
            return

        micro_catchment_name = _resolve_micro_catchment(gvh_code, ta_code)
        if micro_catchment_name:
            json_ext['micro_catchment'] = micro_catchment_name
            obj_data['json_ext'] = json_ext

    def _apply_derived_project_fields(self, obj_data):
        """
        Derive project_name and days_worked from the reporter's
        benefit plan / project enrollments. No reporter, or nothing found,
        leaves the ticket without these fields — no error.
        """
        reporter_type = obj_data.get('reporter_type')
        reporter_id = obj_data.get('reporter_id')
        if not reporter_type or not reporter_id:
            return

        json_ext = dict(obj_data.get('json_ext') or {})

        project_name = _resolve_project_name(reporter_type, reporter_id)
        if project_name:
            json_ext['project_name'] = project_name

        days_worked = _resolve_days_worked(reporter_type, reporter_id)
        if days_worked is not None:
            json_ext['days_worked'] = days_worked

        if json_ext:
            obj_data['json_ext'] = json_ext


class CommentService:
    OBJECT_TYPE = Comment

    def __init__(self, user, validation_class=CommentValidation):
        self.user = user
        self.validation_class = validation_class

    @register_service_signal('comment_service.create')
    @check_authentication
    def create(self, obj_data):
        try:
            with transaction.atomic():
                self._get_content_type(obj_data)
                ticket_id = obj_data.get('ticket_id')
                self.validation_class.validate_create(self.user, **obj_data)

                comment_obj = self.OBJECT_TYPE(**obj_data)
                response_data = self.save_instance(comment_obj)
                self._update_ticket_comment_ids(ticket_id, response_data['data']['id'])

                return response_data

        except Exception as exc:
            return output_exception(
                model_name=self.OBJECT_TYPE.__name__,
                method="create",
                exception=exc
            )

    @transaction.atomic
    def _update_ticket_comment_ids(self, ticket_id, comment_id):
        ticket = Ticket.objects.filter(id=ticket_id).first()
        if ticket:
            json_ext = ticket.json_ext or {}
            comment_ids = json_ext.get('comment_ids', [])
            comment_ids.append(comment_id)
            json_ext['comment_ids'] = comment_ids
            ticket.json_ext = json_ext
            ticket.save(user=self.user)

    @register_service_signal('comment_service.resolve_grievance_by_comment')
    @check_authentication
    def resolve_grievance_by_comment(self, obj_data):
        try:
            with transaction.atomic():
                self.validation_class.validate_resolve_grievance_by_comment(self.user, **obj_data)
                comment = Comment.objects.filter(id=obj_data.get('id')).first()
                ticket = comment.ticket
                ticket.status = Ticket.TicketStatus.CLOSED
                comment.is_resolution = True
                ticket.save(user=self.user)
                comment.save(user=self.user)
                return {
                    "success": True,
                    "message": "Ok",
                    "detail": "resolve_grievance_by_comment",
                }
        except Exception as exc:
            return output_exception(
                model_name=self.OBJECT_TYPE.__name__,
                method="resolve_grievance_by_comment",
                exception=exc
            )

    def save_instance(self, obj_):
        obj_.save(user=self.user)
        dict_repr = model_representation(obj_)
        return output_result_success(dict_representation=dict_repr)

    def _get_content_type(self, obj_data):
        if 'commenter_type' in obj_data:
            content_type = ContentType.objects.get(model=obj_data['commenter_type'].lower())
            obj_data['commenter_type'] = content_type
