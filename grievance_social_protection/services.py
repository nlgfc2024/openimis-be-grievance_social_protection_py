import logging
import random
from datetime import date, timedelta

from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError, PermissionDenied
from django.core.mail import send_mail, BadHeaderError
from django.db.models import Max
from django.db import transaction
from django.template import loader

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
    validate_status_transition,
    parse_resolution_time
)
from grievance_social_protection.access_control import GrievanceAccessControl
from grievance_social_protection.apps import TicketConfig

logger = logging.getLogger(__name__)

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
    Walk the location hierarchy up from `location_code` to the District (type R)
    ancestor — in Malawi data type R is the District, not a Region. Returns
    (code, name), or (None, None) if the location isn't found or has no type-R
    ancestor.
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


class AssignmentService:
    """
    Auto-assignment: picks an attending-staff user for a newly
    created ticket from its category's `default_attending_staff_role_ids`
    config (a role-id list, or a `{role_ids, strategy, scope}` dict).
    No config, no role holder, or (when scope=district) no holder in the
    ticket's district leaves the ticket unassigned — logged, not an error.
    """

    STRATEGY_RANDOM = 'random'
    STRATEGY_ROUND_ROBIN = 'round_robin'
    STRATEGY_LEAST_LOADED = 'least_loaded'

    _TERMINAL_STATUSES = {Ticket.TicketStatus.RESOLVED, Ticket.TicketStatus.CLOSED}

    @classmethod
    def get_assignee(cls, category, district_code=None):
        config = (TicketConfig.default_attending_staff_role_ids or {}).get(category)
        if not config:
            return None

        role_ids, strategy, scope = cls._parse_config(config)
        if not role_ids:
            return None

        candidates = cls._eligible_users(role_ids, scope, district_code)
        if not candidates:
            logger.info(
                "No eligible attending-staff user for category '%s' (role_ids=%s, "
                "scope=%s, district_code=%s); leaving ticket unassigned.",
                category, role_ids, scope, district_code,
            )
            return None

        return cls._pick(candidates, strategy, category)

    @staticmethod
    def _parse_config(config):
        if isinstance(config, dict):
            return (
                list(config.get('role_ids') or []),
                config.get('strategy') or AssignmentService.STRATEGY_RANDOM,
                config.get('scope'),
            )
        # Legacy plain role-id list — no strategy/scope.
        return list(config or []), AssignmentService.STRATEGY_RANDOM, None

    @staticmethod
    def _eligible_users(role_ids, scope, district_code):
        user_role_model = apps.get_model('core', 'UserRole')
        user_model = apps.get_model('core', 'User')

        interactive_user_ids = user_role_model.objects.filter(
            role_id__in=role_ids, *user_role_model.filter_validity()
        ).values_list('user_id', flat=True).distinct()

        if scope == 'district' and district_code:
            user_district_model = apps.get_model('location', 'UserDistrict')
            interactive_user_ids = user_district_model.objects.filter(
                user_id__in=interactive_user_ids,
                location__code=district_code,
                *user_district_model.filter_validity(),
            ).values_list('user_id', flat=True).distinct()

        return list(
            user_model.objects.filter(i_user_id__in=interactive_user_ids, i_user__isnull=False).distinct()
        )

    @classmethod
    def _pick(cls, candidates, strategy, category):
        if strategy == cls.STRATEGY_LEAST_LOADED:
            open_statuses = [s for s in Ticket.TicketStatus.values if s not in cls._TERMINAL_STATUSES]
            return min(
                candidates,
                key=lambda u: Ticket.objects.filter(attending_staff=u, status__in=open_statuses).count(),
            )
        if strategy == cls.STRATEGY_ROUND_ROBIN:
            # Stateless rotation: order candidates deterministically and cycle
            # through them based on how many tickets already exist in this
            # category, rather than persisting a separate counter.
            ordered = sorted(candidates, key=lambda u: u.id)
            total_in_category = Ticket.objects.filter(category=category).count()
            return ordered[total_in_category % len(ordered)]
        return random.choice(candidates)  # 'random' (default)


# plain-text template, mirroring core's password_reset.txt convention.
ASSIGNMENT_NOTIFICATION_TEMPLATE = 'ticket_assignment_notification.txt'
ASSIGNMENT_NOTIFICATION_SUBJECT = "[OpenIMIS] New case assigned: %s"


def _resolve_user_email(user):
    """Return an email address for a core User (interactive or technical), or None."""
    if not user:
        return None
    i_user = getattr(user, 'i_user', None)
    if i_user and getattr(i_user, 'email', None):
        return i_user.email
    t_user = getattr(user, 't_user', None)
    if t_user and getattr(t_user, 'email', None):
        return t_user.email
    return None


def _send_assignment_email(ticket, include_due_date=True):
    """Email the ticket's attending_staff that they've been assigned a case."""
    email = _resolve_user_email(ticket.attending_staff)
    if not email:
        logger.info(
            "Assignee '%s' has no email address; skipping assignment notification for ticket %s.",
            getattr(ticket.attending_staff, 'username', ticket.attending_staff_id), ticket.code,
        )
        return

    context = {'ticket': ticket, 'due_date': ticket.due_date if include_due_date else None}
    try:
        message = loader.render_to_string(ASSIGNMENT_NOTIFICATION_TEMPLATE, context)
        send_mail(
            subject=ASSIGNMENT_NOTIFICATION_SUBJECT % (ticket.code or ticket.title or ticket.uuid),
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
    except BadHeaderError:
        logger.warning("Invalid header while sending assignment notification for ticket %s.", ticket.code)
    except Exception as exc:
        # Notifications are best-effort. This runs after the ticket has already
        # been saved, and the create/update mutation is wrapped in a
        # transaction — so letting an SMTP error (mail server down, timeout,
        # TLS failure, ...) propagate here would roll back the ticket write and
        # surface a 500 for a ticket that actually persisted. Log and move on.
        logger.warning(
            "Failed to send assignment notification for ticket %s: %s", ticket.code, exc,
        )


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
        self._apply_auto_assignment(obj_data)
        self._apply_status_transition(obj_data)
        result = super().create(obj_data)
        self._notify_assignee_if_needed(result, previous_attending_staff_id=None)
        return result

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
        existing_ticket = self._get_existing_ticket(obj_data)
        previous_attending_staff_id = existing_ticket.attending_staff_id if existing_ticket else None
        self._apply_status_transition(obj_data, existing_ticket=existing_ticket)
        result = super().update(obj_data)
        self._notify_assignee_if_needed(result, previous_attending_staff_id)
        return result

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

    def _get_existing_ticket(self, obj_data):
        """Look up the ticket being updated/deleted by uuid or id, or None if not given/found."""
        ticket_uuid = obj_data.get('uuid')
        ticket_id = obj_data.get('id')
        if not ticket_uuid and not ticket_id:
            return None

        base_qs = Ticket.filter_queryset()
        ticket = None
        if ticket_uuid:
            ticket = base_qs.filter(uuid=ticket_uuid).first()
        if not ticket and ticket_id:
            if isinstance(ticket_id, int) or (isinstance(ticket_id, str) and ticket_id.isdigit()):
                ticket = base_qs.filter(id=ticket_id).first()
            else:
                ticket = base_qs.filter(uuid=ticket_id).first()
        return ticket

    def _validate_existing_ticket_access(self, obj_data, access_type):
        """Validate user has permission for the existing ticket's category and flags"""
        if not obj_data.get('uuid') and not obj_data.get('id'):
            return

        ticket = self._get_existing_ticket(obj_data)
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
        walking up to the District (type R) ancestor — in Malawi data type R is
        the District, not a Region. No location_code, or no type-R ancestor
        found, leaves the ticket without a district — no error.
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

    def _apply_auto_assignment(self, obj_data):
        """
        Auto-assign attending_staff via AssignmentService, when the
        caller hasn't already supplied one.
        """
        if obj_data.get('attending_staff') or obj_data.get('attending_staff_id'):
            return
        category = obj_data.get('category')
        if not category:
            return

        district_code = (obj_data.get('json_ext') or {}).get('district_code')
        assignee = AssignmentService.get_assignee(category, district_code)
        if assignee:
            obj_data['attending_staff'] = assignee

    def _notify_assignee_if_needed(self, result, previous_attending_staff_id=None):
        """
        Email the assignee when attending_staff is set/changed, gated
        by notifications.on_assign and the 'email' channel. Runs after the
        ticket is actually saved, so it reflects the final persisted state
        (including auto-assignment). No email when attending_staff is unset,
        unchanged from before this call, or the config disables it.
        """
        if not result or not result.get('success'):
            return

        notifications_cfg = TicketConfig.notifications or {}
        if not notifications_cfg.get('on_assign', True):
            return
        if 'email' not in (notifications_cfg.get('channels') or []):
            return

        ticket_id = (result.get('data') or {}).get('id')
        if not ticket_id:
            return
        ticket = Ticket.objects.filter(id=ticket_id).first()
        if not ticket or not ticket.attending_staff_id:
            return
        if ticket.attending_staff_id == previous_attending_staff_id:
            return

        _send_assignment_email(ticket, include_due_date=notifications_cfg.get('include_due_date', True))

    def _apply_status_transition(self, obj_data, existing_ticket=None):
        """
        Validate + apply status-transition side effects:
        - the new status must be one of the deployment's enabled
          ticket_statuses;
        - REFERRED requires a valid `referred_to` (popped out of obj_data
          since it isn't a model field — stored in json_ext instead);
        - `was_referred` stays sticky once set, even through a later
          RESOLVED, so the referral authority is never lost;
        - `resolved_date` is set the first time a ticket reaches a terminal
          status (per config `ticket_statuses[].terminal`).
        Runs on both create and update; on create `existing_ticket` is None
        so there's nothing to preserve from a prior state.
        """
        if 'status' not in obj_data and 'referred_to' not in obj_data:
            return

        new_status = obj_data.get('status') or (existing_ticket.status if existing_ticket else None)
        referred_to = obj_data.pop('referred_to', None)

        transition_error = validate_status_transition(new_status, referred_to)
        if transition_error:
            raise ValidationError(transition_error)

        existing_json_ext = (existing_ticket.json_ext if existing_ticket else None) or {}
        # Merge onto the existing json_ext so the create-time derived fields
        # (district_code, project_name, micro_catchment, ...) survive a partial
        # payload update instead of being replaced wholesale by an incoming
        # json_ext that only carries a subset of keys.
        json_ext = {**existing_json_ext, **(obj_data.get('json_ext') or {})}

        if new_status == Ticket.TicketStatus.REFERRED:
            json_ext['was_referred'] = True
            if referred_to:
                json_ext['referred_to'] = referred_to
        elif existing_json_ext.get('was_referred'):
            json_ext['was_referred'] = True
            if 'referred_to' not in json_ext and existing_json_ext.get('referred_to'):
                json_ext['referred_to'] = existing_json_ext['referred_to']

        terminal_codes = {
            status['code'] for status in TicketConfig.ticket_statuses or []
            if isinstance(status, dict) and status.get('terminal')
        }
        if new_status in terminal_codes and not existing_json_ext.get('resolved_date'):
            json_ext['resolved_date'] = date.today().isoformat()

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
