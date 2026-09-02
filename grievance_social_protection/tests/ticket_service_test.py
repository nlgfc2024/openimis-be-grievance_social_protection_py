import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from tasks_management.models import Task
from tasks_management.services import TaskService

from location.models import Location, MicroCatchment, MicroCatchmentGVH, MicroCatchmentTA, Hotspot, UserDistrict
from individual.models import Group, GroupIndividual
from social_protection.models import BenefitPlan, Beneficiary, GroupBeneficiary, BeneficiaryStatus
from project_social_protection.models import (
    Activity, Project, BeneficiaryProjectEnrollment, BeneficiaryProjectTimeEntry,
    GroupBeneficiaryProjectEnrollment, GroupBeneficiaryProjectTimeEntry,
)

from grievance_social_protection.models import Ticket
from grievance_social_protection.services import TicketService, AssignmentService
from grievance_social_protection.tests.data import (
    service_add_ticket_payload,
    service_add_ticket_payload_bad_resolution,
    service_add_ticket_payload_bad_resolution_day,
    service_add_ticket_payload_bad_resolution_hour,
    service_update_ticket_payload
)
from grievance_social_protection.apps import DEFAULT_CFG
from grievance_social_protection.tests.test_helpers import (
    create_ticket,
    create_test_individual,
    setup_grievance_config,
    restore_grievance_config,
)
from core.test_helpers import LogInHelper, create_test_interactive_user, create_test_role
from core.utils import TimeUtils
from grievance_social_protection.apps import TicketConfig
from django.utils.translation import gettext as _


class TicketServiceTest(TestCase):
    user = None
    service = None
    query_all = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        setup_grievance_config(DEFAULT_CFG)
        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = TicketService(cls.user)
        cls.query_all = Ticket.objects.filter(is_deleted=False)
        cls.ticket = create_ticket(cls.user)

    def test_add_ticket(self):
        result = self.service.create(service_add_ticket_payload)
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        uuid = result.get('data', {}).get('uuid', None)
        query = self.query_all.filter(uuid=uuid)
        self.assertEqual(query.count(), 1)

    def test_add_ticket_validation(self):
        with self.assertRaises(ValidationError) as context:
            self.service.create(service_add_ticket_payload_bad_resolution)

        exception = context.exception
        self.assertIn(_('validations.TicketValidation.validate_resolution.invalid_format'), str(exception))

        with self.assertRaises(ValidationError) as context:
            self.service.create(service_add_ticket_payload_bad_resolution_day)

        exception = context.exception
        self.assertIn(_('validations.TicketValidation.validate_resolution.invalid_day_value'), str(exception))

        with self.assertRaises(ValidationError) as context:
            self.service.create(service_add_ticket_payload_bad_resolution_hour)

        exception = context.exception
        self.assertIn(_('validations.TicketValidation.validate_resolution.invalid_hour_value'), str(exception))

    def test_ticket_status_accepts_referred(self):
        """REFERRED is a valid Ticket status and round-trips through the DB."""
        self.assertIn('REFERRED', Ticket.TicketStatus.values)
        ticket = create_ticket(self.user)
        ticket.status = Ticket.TicketStatus.REFERRED
        ticket.save(user=self.user)
        reloaded = self.query_all.get(id=ticket.id)
        self.assertEqual(reloaded.status, Ticket.TicketStatus.REFERRED)

    def test_update_ticket(self):
        update_payload = {
            "id": self.ticket.uuid,
            **service_update_ticket_payload
        }
        result = self.service.update(update_payload)
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        uuid = result.get('data', {}).get('uuid', None)
        query = self.query_all.filter(uuid=uuid)
        self.assertEqual(query.count(), 1)
        updated_ticket = query.first()
        self.assertEqual(updated_ticket.title, update_payload.get('title'))
        self.assertEqual(updated_ticket.resolution, update_payload.get('resolution'))
        self.assertEqual(updated_ticket.priority, update_payload.get('priority'))
        self.assertEqual(updated_ticket.status, update_payload.get('status'))

    def test_update_ticket_validation(self):
        with self.assertRaises(ValidationError) as context:
            self.service.update({
                "id": self.ticket.uuid,
                **service_add_ticket_payload_bad_resolution
            })

        exception = context.exception
        self.assertIn(_('validations.TicketValidation.validate_resolution.invalid_format'), str(exception))

        with self.assertRaises(ValidationError) as context:
            self.service.update({
                "id": self.ticket.uuid,
                **service_add_ticket_payload_bad_resolution_day
            })

        exception = context.exception
        self.assertIn(_('validations.TicketValidation.validate_resolution.invalid_day_value'), str(exception))

        with self.assertRaises(ValidationError) as context:
            self.service.update({
                "id": self.ticket.uuid,
                **service_add_ticket_payload_bad_resolution_hour
            })

        exception = context.exception
        self.assertIn(_('validations.TicketValidation.validate_resolution.invalid_hour_value'), str(exception))


class TicketDueDateAndStatusTest(TestCase):
    """auto-computed due_date and default OPEN status on ticket creation."""

    _config_snapshot = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = TicketService(cls.user)
        cls._config_snapshot = setup_grievance_config({
            'grievance_types': [
                {'name': 'Claims', 'resolution_times': '30,0'},
                'no_sla_category',
            ],
        })

    @classmethod
    def tearDownClass(cls):
        restore_grievance_config(cls._config_snapshot)
        super().tearDownClass()

    def test_due_date_and_status_defaulted_on_create(self):
        result = self.service.create({
            "category": "Claims",
            "title": "Unpaid wages",
            "channel": "Channel A",
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.due_date, date.today() + timedelta(days=30))
        self.assertEqual(ticket.status, Ticket.TicketStatus.OPEN)

    def test_category_without_sla_gets_no_due_date(self):
        result = self.service.create({
            "category": "no_sla_category",
            "title": "General query",
            "channel": "Channel A",
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertIsNone(ticket.due_date)
        self.assertEqual(ticket.status, Ticket.TicketStatus.OPEN)

    def test_explicit_status_and_due_date_are_not_overridden(self):
        result = self.service.create({
            "category": "Claims",
            "title": "Already resolved",
            "channel": "Channel A",
            "status": Ticket.TicketStatus.RESOLVED,
            "due_date": date.today(),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.due_date, date.today())
        self.assertEqual(ticket.status, Ticket.TicketStatus.RESOLVED)

    def test_resolution_filled_from_category_when_not_supplied(self):
        result = self.service.create({
            "category": "Claims",
            "title": "SLA from category",
            "channel": "Channel A",
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.resolution, '30,0')

    def test_due_date_recomputed_on_category_change(self):
        result = self.service.create({
            "category": "no_sla_category",
            "title": "Wrong type",
            "channel": "Channel A",
        })
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertIsNone(ticket.due_date)

        self.service.update({"id": ticket.uuid, "category": "Claims"})
        ticket.refresh_from_db()
        self.assertEqual(ticket.due_date, ticket.date_created.date() + timedelta(days=30))
        self.assertEqual(ticket.resolution, '30,0')

    def test_due_date_not_touched_when_category_unchanged_on_update(self):
        result = self.service.create({
            "category": "Claims",
            "title": "Stable timer",
            "channel": "Channel A",
        })
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        original_due = ticket.due_date

        self.service.update({"id": ticket.uuid, "category": "Claims", "title": "Stable timer edited"})
        ticket.refresh_from_db()
        self.assertEqual(ticket.due_date, original_due)


class TicketReporterDenormalizationTest(TestCase):
    """denormalise reporter jsonExt fields into ticket.json_ext at create."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_grievance_config(DEFAULT_CFG)
        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = TicketService(cls.user)

    def test_individual_reporter_fields_denormalized(self):
        individual = create_test_individual(self.user, json_ext={
            'form_number': 'FN-001',
            'national_id': 'NID-123',
            'location_code': 'LOC1',
            'location_name': 'Test Village',
            'traditional_authority_name': 'Test TA',
            'group_village_head_name': 'Test GVH',
            'unrelated_key': 'should not leak onto the ticket',
        })
        result = self.service.create({
            "category": "Default",
            "title": "Reporter test",
            "channel": "Channel A",
            "reporter_type": "individual",
            "reporter_id": str(individual.id),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.json_ext.get('form_number'), 'FN-001')
        self.assertEqual(ticket.json_ext.get('national_id'), 'NID-123')
        self.assertEqual(ticket.json_ext.get('location_code'), 'LOC1')
        self.assertEqual(ticket.json_ext.get('location_name'), 'Test Village')
        self.assertEqual(ticket.json_ext.get('traditional_authority_name'), 'Test TA')
        self.assertEqual(ticket.json_ext.get('group_village_head_name'), 'Test GVH')
        self.assertNotIn('unrelated_key', ticket.json_ext)

    def test_individual_reporter_missing_keys_skipped_without_error(self):
        individual = create_test_individual(self.user, json_ext={'form_number': 'FN-002'})
        result = self.service.create({
            "category": "Default",
            "title": "Partial reporter data",
            "channel": "Channel A",
            "reporter_type": "individual",
            "reporter_id": str(individual.id),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.json_ext.get('form_number'), 'FN-002')
        self.assertNotIn('national_id', ticket.json_ext)

    def test_ticket_without_reporter_creates_without_error(self):
        result = self.service.create({
            "category": "Default",
            "title": "No reporter",
            "channel": "Channel A",
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.json_ext or {}, {})

    def test_unregistered_reporter_captured_into_json_ext(self):
        result = self.service.create({
            "category": "Default",
            "title": "Walk-in complainant",
            "channel": "Channel A",
            "reporter_first_name": "Jane",
            "reporter_last_name": "Phiri",
            "reporter_dob": date(1990, 5, 1),
            "reporter_phone": "0999000111",
            "reporter_national_id": "NID-WALKIN-1",
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertIsNone(ticket.reporter_type_id)
        self.assertIsNone(ticket.reporter_id)
        self.assertEqual(ticket.json_ext['unregistered_reporter'], {
            'first_name': 'Jane', 'last_name': 'Phiri', 'dob': '1990-05-01',
            'phone': '0999000111', 'national_id': 'NID-WALKIN-1',
        })
        # mirrored onto the keys the filter wizard / participant panel read
        self.assertEqual(ticket.json_ext.get('national_id'), 'NID-WALKIN-1')
        self.assertEqual(ticket.json_ext.get('household_mobile_number'), '0999000111')

    def test_unregistered_reporter_ignored_when_reporter_id_present(self):
        individual = create_test_individual(self.user)
        result = self.service.create({
            "category": "Default",
            "title": "Registered wins",
            "channel": "Channel A",
            "reporter_type": "individual",
            "reporter_id": str(individual.id),
            "reporter_first_name": "Ignored",
            "reporter_last_name": "Ignored",
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertNotIn('unregistered_reporter', ticket.json_ext or {})


class TicketWageAmountTest(TestCase):
    """wage_amount storage for partial-wages approval (maker-checker → arrears)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_grievance_config(DEFAULT_CFG)
        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = TicketService(cls.user)

    def test_wage_amount_round_trips(self):
        result = self.service.create({
            "category": "Default",
            "title": "Partial wages",
            "channel": "Channel A",
            "wage_amount": "150.50",
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.wage_amount, Decimal('150.50'))

    def test_wage_amount_optional(self):
        result = self.service.create({
            "category": "Default",
            "title": "No wage amount",
            "channel": "Channel A",
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertIsNone(ticket.wage_amount)

    def test_wage_amount_rejects_negative(self):
        with self.assertRaises(ValidationError) as context:
            self.service.create({
                "category": "Default",
                "title": "Negative wage",
                "channel": "Channel A",
                "wage_amount": "-10",
            })
        self.assertIn(
            _('validations.TicketValidation.validate_wage_amount.negative_value'),
            str(context.exception),
        )

    def test_wage_amount_rejects_non_numeric(self):
        with self.assertRaises(ValidationError) as context:
            self.service.create({
                "category": "Default",
                "title": "Bad wage",
                "channel": "Channel A",
                "wage_amount": "not-a-number",
            })
        self.assertIn(
            _('validations.TicketValidation.validate_wage_amount.invalid_format'),
            str(context.exception),
        )


class TicketDerivedDistrictTest(TestCase):
    """Derive district_code (Region ancestor) from the participant's location.

    Deployment level mapping: R/Region = District, D/District = Traditional
    Authority, W/Municipality = Group Village Head, V/Village = Village.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_grievance_config(DEFAULT_CFG)
        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = TicketService(cls.user)

    def setUp(self):
        self.region = Location.objects.create(code='BE05-R', name='Test Region', type='R')
        self.district = Location.objects.create(
            code='BE05-D', name='Test TA', type='D', parent=self.region)
        self.municipality = Location.objects.create(
            code='BE05-W', name='Test GVH', type='W', parent=self.district)
        self.village = Location.objects.create(
            code='BE05-V', name='Test Village', type='V', parent=self.municipality)

    def tearDown(self):
        Location.objects.filter(code__startswith='BE05-').delete()

    def test_district_derived_from_village_location(self):
        individual = create_test_individual(self.user, json_ext={
            'location_code': self.village.code,
        })
        result = self.service.create({
            "category": "Default",
            "title": "District derivation",
            "channel": "Channel A",
            "reporter_type": "individual",
            "reporter_id": str(individual.id),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.json_ext.get('district_code'), self.region.code)
        self.assertEqual(ticket.json_ext.get('district_name'), self.region.name)

    def test_missing_location_code_no_error(self):
        individual = create_test_individual(self.user, json_ext={'form_number': 'FN-DIST-1'})
        result = self.service.create({
            "category": "Default",
            "title": "No location code",
            "channel": "Channel A",
            "reporter_type": "individual",
            "reporter_id": str(individual.id),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertNotIn('district_code', ticket.json_ext)

    def test_partial_hierarchy_no_district_found(self):
        """A location with no ancestor of type R must not error, just skip district."""
        orphan_village = Location.objects.create(code='BE05-ORPHAN', name='Orphan Village', type='V')
        individual = create_test_individual(self.user, json_ext={
            'location_code': orphan_village.code,
        })
        result = self.service.create({
            "category": "Default",
            "title": "Orphan location",
            "channel": "Channel A",
            "reporter_type": "individual",
            "reporter_id": str(individual.id),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertNotIn('district_code', ticket.json_ext)
        orphan_village.delete()


class TicketDerivedMicroCatchmentTest(TestCase):
    """Derive micro_catchment from the participant's GVH code (fallback TA code)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_grievance_config(DEFAULT_CFG)
        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = TicketService(cls.user)

    def setUp(self):
        now = TimeUtils.now()
        self.gvh_location = Location.objects.create(code='BE06-W', name='Test GVH Location', type='W')
        self.ta_location = Location.objects.create(code='BE06-D', name='Test TA Location', type='D')

        self.gvh_micro_catchment = MicroCatchment.objects.create(
            code='BE06-MC-GVH', name='GVH Micro-Catchment', audit_user_id=-1)
        MicroCatchmentGVH.objects.create(
            micro_catchment=self.gvh_micro_catchment, location=self.gvh_location,
            audit_user_id=-1, validity_from=now)

        self.ta_micro_catchment = MicroCatchment.objects.create(
            code='BE06-MC-TA', name='TA Micro-Catchment', audit_user_id=-1)
        MicroCatchmentTA.objects.create(
            micro_catchment=self.ta_micro_catchment, location=self.ta_location,
            audit_user_id=-1, validity_from=now)

    def tearDown(self):
        MicroCatchmentGVH.objects.filter(location__code__startswith='BE06-').delete()
        MicroCatchmentTA.objects.filter(location__code__startswith='BE06-').delete()
        MicroCatchment.objects.filter(code__startswith='BE06-').delete()
        Location.objects.filter(code__startswith='BE06-').delete()

    def test_micro_catchment_derived_from_gvh_code(self):
        individual = create_test_individual(self.user, json_ext={
            'group_village_head_code': self.gvh_location.code,
        })
        result = self.service.create({
            "category": "Default",
            "title": "GVH micro-catchment",
            "channel": "Channel A",
            "reporter_type": "individual",
            "reporter_id": str(individual.id),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.json_ext.get('micro_catchment'), 'GVH Micro-Catchment')

    def test_micro_catchment_falls_back_to_ta_code(self):
        individual = create_test_individual(self.user, json_ext={
            'traditional_authority_code': self.ta_location.code,
        })
        result = self.service.create({
            "category": "Default",
            "title": "TA micro-catchment fallback",
            "channel": "Channel A",
            "reporter_type": "individual",
            "reporter_id": str(individual.id),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.json_ext.get('micro_catchment'), 'TA Micro-Catchment')

    def test_gvh_code_takes_precedence_over_ta_code(self):
        individual = create_test_individual(self.user, json_ext={
            'group_village_head_code': self.gvh_location.code,
            'traditional_authority_code': self.ta_location.code,
        })
        result = self.service.create({
            "category": "Default",
            "title": "GVH precedence",
            "channel": "Channel A",
            "reporter_type": "individual",
            "reporter_id": str(individual.id),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.json_ext.get('micro_catchment'), 'GVH Micro-Catchment')

    def test_no_mapping_found_no_error(self):
        individual = create_test_individual(self.user, json_ext={
            'group_village_head_code': 'BE06-UNMAPPED',
        })
        result = self.service.create({
            "category": "Default",
            "title": "No mapping",
            "channel": "Channel A",
            "reporter_type": "individual",
            "reporter_id": str(individual.id),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertNotIn('micro_catchment', ticket.json_ext)


class TicketDerivedProjectFieldsTest(TestCase):
    """Derive project_name (benefit plan) and days_worked (project time entries)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_grievance_config(DEFAULT_CFG)
        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = TicketService(cls.user)
        cls.benefit_plan = BenefitPlan(
            code='BE07PLN', name='Test Cash Transfer', type=BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE)
        cls.benefit_plan.save(user=cls.user)
        cls.group_plan = BenefitPlan(
            code='BE07GRP', name='PWP Phase 3', type=BenefitPlan.BenefitPlanType.GROUP_TYPE)
        cls.group_plan.save(user=cls.user)

    @classmethod
    def tearDownClass(cls):
        cls.group_plan.delete()
        cls.benefit_plan.delete()
        super().tearDownClass()

    def _enrolled_household_member(self, *, with_time_entries=None, with_hotspot=False):
        """
        Build an Individual who is a member of a household (Group) enrolled, via
        its GroupBeneficiary, in a project on the GROUP benefit plan. An
        enrollment is created when `with_time_entries` is an iterable (of
        (day_number, percent_complete) pairs) or `with_hotspot` is True.
        Returns (individual, group_beneficiary, enrollment).
        """
        individual = create_test_individual(self.user)
        group = Group(code=f'HH{individual.id.hex[:6]}')
        group.save(username=self.user.username)
        GroupIndividual(individual_id=individual.id, group_id=group.id, role='HEAD') \
            .save(username=self.user.username)
        group_beneficiary = GroupBeneficiary(
            group=group, benefit_plan=self.group_plan, status=BeneficiaryStatus.ACTIVE)
        group_beneficiary.save(user=self.user)

        enrollment = None
        if with_time_entries is not None or with_hotspot:
            suffix = individual.id.hex[:6]
            activity = Activity(name='PWP Activity')
            activity.save(user=self.user)
            location = Location.objects.create(code=f'BE07G-V-{suffix}', name='PWP Village', type='V')
            hotspot = None
            if with_hotspot:
                micro_catchment = MicroCatchment.objects.create(
                    code=f'MC-{suffix}', name='PWP Micro-Catchment', audit_user_id=-1)
                hotspot = Hotspot.objects.create(
                    code=f'HS-{suffix}', name='PWP Hotspot', micro_catchment=micro_catchment)
            project = Project(
                benefit_plan=self.group_plan, name='PWP Phase 3 Project', activity=activity,
                location=location, target_beneficiaries=10, working_days=5, hotspot=hotspot)
            project.save(user=self.user)
            enrollment = GroupBeneficiaryProjectEnrollment(
                group_beneficiary=group_beneficiary, project=project)
            enrollment.save(user=self.user)
            for day_number, percent_complete in (with_time_entries or ()):
                GroupBeneficiaryProjectTimeEntry(
                    enrollment=enrollment, day_number=day_number, percent_complete=percent_complete) \
                    .save(user=self.user)

        return individual, group_beneficiary, enrollment

    def test_beneficiary_reporter_yields_project_name(self):
        individual = create_test_individual(self.user)
        beneficiary = Beneficiary(
            individual=individual, benefit_plan=self.benefit_plan, status=BeneficiaryStatus.ACTIVE)
        beneficiary.save(user=self.user)
        result = self.service.create({
            "category": "Default",
            "title": "Beneficiary project name",
            "channel": "Channel A",
            "reporter_type": "beneficiary",
            "reporter_id": str(beneficiary.id),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.json_ext.get('project_name'), 'Test Cash Transfer')

    def test_individual_without_beneficiary_no_error(self):
        individual = create_test_individual(self.user)
        result = self.service.create({
            "category": "Default",
            "title": "No beneficiary link",
            "channel": "Channel A",
            "reporter_type": "individual",
            "reporter_id": str(individual.id),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertNotIn('project_name', ticket.json_ext or {})

    def test_beneficiary_reporter_yields_days_worked(self):
        activity = Activity(name='BE07 Activity')
        activity.save(user=self.user)
        location = Location.objects.create(code='BE07-V', name='BE07 Village', type='V')
        project = Project(
            benefit_plan=self.benefit_plan, name='BE07 Project', activity=activity,
            location=location, target_beneficiaries=10, working_days=5)
        project.save(user=self.user)

        individual = create_test_individual(self.user)
        beneficiary = Beneficiary(
            individual=individual, benefit_plan=self.benefit_plan, status=BeneficiaryStatus.ACTIVE)
        beneficiary.save(user=self.user)
        enrollment = BeneficiaryProjectEnrollment(beneficiary=beneficiary, project=project)
        enrollment.save(user=self.user)

        # 3 worked days (percent_complete > 0), 1 absent day (0%) that must not count.
        for day_number, percent_complete in ((1, 100), (2, 50), (3, 0), (4, 25)):
            entry = BeneficiaryProjectTimeEntry(
                enrollment=enrollment, day_number=day_number, percent_complete=percent_complete)
            entry.save(user=self.user)

        result = self.service.create({
            "category": "Default",
            "title": "Beneficiary days worked",
            "channel": "Channel A",
            "reporter_type": "beneficiary",
            "reporter_id": str(beneficiary.id),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.json_ext.get('days_worked'), 3)

    def test_beneficiary_without_time_entries_no_error(self):
        individual = create_test_individual(self.user)
        beneficiary = Beneficiary(
            individual=individual, benefit_plan=self.benefit_plan, status=BeneficiaryStatus.ACTIVE)
        beneficiary.save(user=self.user)
        result = self.service.create({
            "category": "Default",
            "title": "No time entries",
            "channel": "Channel A",
            "reporter_type": "beneficiary",
            "reporter_id": str(beneficiary.id),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.json_ext.get('days_worked'), 0)

    def test_group_member_reporter_yields_project_name(self):
        individual, _, _ = self._enrolled_household_member()
        result = self.service.create({
            "category": "Default",
            "title": "Group member project name",
            "channel": "Channel A",
            "reporter_type": "individual",
            "reporter_id": str(individual.id),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.json_ext.get('project_name'), 'PWP Phase 3')

    def test_group_member_reporter_yields_days_worked(self):
        # 3 worked days (percent_complete > 0), 1 absent day (0%) that must not count.
        individual, _, _ = self._enrolled_household_member(
            with_time_entries=((1, 100), (2, 50), (3, 0), (4, 25)))
        result = self.service.create({
            "category": "Default",
            "title": "Group member days worked",
            "channel": "Channel A",
            "reporter_type": "individual",
            "reporter_id": str(individual.id),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.json_ext.get('days_worked'), 3)

    def test_group_member_without_enrollment_yields_zero_days(self):
        individual, _, _ = self._enrolled_household_member()
        result = self.service.create({
            "category": "Default",
            "title": "Group member no enrollment",
            "channel": "Channel A",
            "reporter_type": "individual",
            "reporter_id": str(individual.id),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.json_ext.get('days_worked'), 0)

    def test_reporter_project_hotspot_denormalized(self):
        individual, _, _ = self._enrolled_household_member(with_hotspot=True)
        result = self.service.create({
            "category": "Default",
            "title": "Group member hotspot",
            "channel": "Channel A",
            "reporter_type": "individual",
            "reporter_id": str(individual.id),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.json_ext.get('hotspot_name'), 'PWP Hotspot')

    def test_preview_reporter_derived_fields_without_persisting(self):
        individual, _, _ = self._enrolled_household_member(
            with_time_entries=((1, 100), (2, 100)), with_hotspot=True)
        before = Ticket.objects.count()
        preview = self.service.preview_reporter_derived_fields('individual', str(individual.id))
        self.assertEqual(Ticket.objects.count(), before)
        self.assertEqual(preview.get('project_name'), 'PWP Phase 3')
        self.assertEqual(preview.get('days_worked'), 2)
        self.assertEqual(preview.get('hotspot_name'), 'PWP Hotspot')

    def test_preview_reporter_derived_fields_unknown_reporter_returns_empty(self):
        self.assertEqual(self.service.preview_reporter_derived_fields('individual', '0' * 32), {})
        self.assertEqual(self.service.preview_reporter_derived_fields(None, None), {})


class TicketReporterEnrolmentContextTest(TestCase):
    """
    Derived project fields must follow the Phase -> Project -> Household selected
    at intake, not fall back to an unrelated first enrolment.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_grievance_config(DEFAULT_CFG)
        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = TicketService(cls.user)
        cls.group_plan = BenefitPlan(
            code=f'G{uuid.uuid4().hex[:7]}', name='PWP Group Plan',
            type=BenefitPlan.BenefitPlanType.GROUP_TYPE)
        cls.group_plan.save(user=cls.user)
        cls.individual_plan = BenefitPlan(
            code=f'I{uuid.uuid4().hex[:7]}', name='Cash Plan',
            type=BenefitPlan.BenefitPlanType.INDIVIDUAL_TYPE)
        cls.individual_plan.save(user=cls.user)
        cls.activity = Activity(name='Enrolment Activity')
        cls.activity.save(user=cls.user)

    @classmethod
    def tearDownClass(cls):
        cls.individual_plan.delete()
        cls.group_plan.delete()
        super().tearDownClass()

    def _project(self, name, *, hotspot_name=None):
        tag = uuid.uuid4().hex[:6]
        location = Location.objects.create(code=f'ENRV{tag}', name=f'{name} Village', type='V')
        hotspot = None
        if hotspot_name:
            micro_catchment = MicroCatchment.objects.create(
                code=f'ENRMC{tag}', name=f'{name} MC', audit_user_id=-1)
            hotspot = Hotspot.objects.create(
                code=f'ENRHS{tag}', name=hotspot_name, micro_catchment=micro_catchment)
        project = Project(
            benefit_plan=self.group_plan, name=f'{name} {tag}', activity=self.activity,
            location=location, target_beneficiaries=10, working_days=30, hotspot=hotspot)
        project.save(user=self.user)
        return project

    def _enrol_in_household(self, individual, project, *, worked_days=0, deleted_enrolment=False):
        tag = uuid.uuid4().hex[:6]
        group = Group(code=f'HH{tag}')
        group.save(username=self.user.username)
        GroupIndividual(individual_id=individual.id, group_id=group.id, role='HEAD') \
            .save(username=self.user.username)
        gb = GroupBeneficiary(
            group=group, benefit_plan=project.benefit_plan, status=BeneficiaryStatus.ACTIVE)
        gb.save(user=self.user)
        enr = GroupBeneficiaryProjectEnrollment(group_beneficiary=gb, project=project)
        enr.save(user=self.user)
        for day in range(1, worked_days + 1):
            GroupBeneficiaryProjectTimeEntry(
                enrollment=enr, day_number=day, percent_complete=100).save(user=self.user)
        if deleted_enrolment:
            enr.delete(user=self.user)
        return gb, enr

    def _create_with_context(self, individual, *, project=None, group_beneficiary=None):
        payload = {
            "category": "Default", "title": "context", "channel": "Channel A",
            "reporter_type": "individual", "reporter_id": str(individual.id),
        }
        if project is not None:
            payload["reporter_project_id"] = str(project.id)
        if group_beneficiary is not None:
            payload["reporter_group_beneficiary_id"] = str(group_beneficiary.id)
        result = self.service.create(payload)
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        return Ticket.objects.get(uuid=result['data']['uuid']).json_ext or {}

    def test_selected_project_wins_over_unrelated_first_membership(self):
        individual = create_test_individual(self.user)
        project_a = self._project('Project A', hotspot_name='Hotspot A')
        project_b = self._project('Project B', hotspot_name='Hotspot B')
        gb_a, _ = self._enrol_in_household(individual, project_a, worked_days=5)
        self._enrol_in_household(individual, project_b, worked_days=9)

        ext_a = self._create_with_context(individual, project=project_a, group_beneficiary=gb_a)
        self.assertEqual(ext_a.get('hotspot_name'), 'Hotspot A')
        self.assertEqual(ext_a.get('days_worked'), 5)

        ext_b = self._create_with_context(individual, project=project_b)
        self.assertEqual(ext_b.get('hotspot_name'), 'Hotspot B')
        self.assertEqual(ext_b.get('days_worked'), 9)

    def test_direct_beneficiary_does_not_shadow_selected_group_project(self):
        individual = create_test_individual(self.user)
        # a direct INDIVIDUAL-plan beneficiary that the old "beneficiary_set.first()"
        # path would have latched onto
        Beneficiary(
            individual=individual, benefit_plan=self.individual_plan,
            status=BeneficiaryStatus.ACTIVE).save(user=self.user)
        project = self._project('Group Project', hotspot_name='Group Hotspot')
        gb, _ = self._enrol_in_household(individual, project, worked_days=4)

        ext = self._create_with_context(individual, project=project, group_beneficiary=gb)
        self.assertEqual(ext.get('project_name'), 'PWP Group Plan')
        self.assertEqual(ext.get('hotspot_name'), 'Group Hotspot')
        self.assertEqual(ext.get('days_worked'), 4)

    def test_reporter_not_in_selected_project_derives_nothing(self):
        individual = create_test_individual(self.user)
        enrolled_project = self._project('Enrolled', hotspot_name='Enrolled Hotspot')
        self._enrol_in_household(individual, enrolled_project, worked_days=3)
        other_project = self._project('Other', hotspot_name='Other Hotspot')

        ext = self._create_with_context(individual, project=other_project)
        self.assertNotIn('project_name', ext)
        self.assertNotIn('hotspot_name', ext)
        self.assertNotIn('days_worked', ext)

    def test_deleted_enrolment_is_ignored(self):
        individual = create_test_individual(self.user)
        project = self._project('Deleted Enr', hotspot_name='DE Hotspot')
        self._enrol_in_household(individual, project, worked_days=7, deleted_enrolment=True)

        ext = self._create_with_context(individual, project=project)
        self.assertNotIn('days_worked', ext)
        self.assertNotIn('hotspot_name', ext)

    def test_deleted_direct_beneficiary_excluded_from_fallback(self):
        individual = create_test_individual(self.user)
        beneficiary = Beneficiary(
            individual=individual, benefit_plan=self.individual_plan,
            status=BeneficiaryStatus.ACTIVE)
        beneficiary.save(user=self.user)
        beneficiary.delete(user=self.user)
        project = self._project('Live Group', hotspot_name='LG Hotspot')
        self._enrol_in_household(individual, project, worked_days=2)

        # no context -> the deleted beneficiary must not win; the live group
        # enrolment is used instead
        ext = self._create_with_context(individual)
        self.assertEqual(ext.get('project_name'), 'PWP Group Plan')
        self.assertEqual(ext.get('days_worked'), 2)

    def test_preview_honours_project_context(self):
        individual = create_test_individual(self.user)
        project_a = self._project('Prev A', hotspot_name='Prev Hotspot A')
        project_b = self._project('Prev B', hotspot_name='Prev Hotspot B')
        self._enrol_in_household(individual, project_a, worked_days=6)
        self._enrol_in_household(individual, project_b, worked_days=1)

        preview = self.service.preview_reporter_derived_fields(
            'individual', str(individual.id), str(project_b.id))
        self.assertEqual(preview.get('hotspot_name'), 'Prev Hotspot B')
        self.assertEqual(preview.get('days_worked'), 1)


class TicketAutoAssignmentTest(TestCase):
    """Auto-assign attending_staff from default_attending_staff_role_ids config."""

    _config_snapshot = None
    _original_role_ids_cfg = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # default_attending_staff_role_ids isn't part of setup_grievance_config's
        # snapshot/restore set, so save/restore it ourselves.
        cls._original_role_ids_cfg = TicketConfig.default_attending_staff_role_ids

        cls.dpm_role = create_test_role(name='BE10DPMRole')
        cls.dpm_user_1 = create_test_interactive_user(username='be10_dpm1', roles=[cls.dpm_role.id])
        cls.dpm_user_2 = create_test_interactive_user(username='be10_dpm2', roles=[cls.dpm_role.id])

        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = TicketService(cls.user)
        cls._config_snapshot = setup_grievance_config({
            'grievance_types': ['Claims', 'no_assignment_category'],
            'default_attending_staff_role_ids': {
                'Claims': {'role_ids': [cls.dpm_role.id], 'strategy': 'random'},
            },
        })

    @classmethod
    def tearDownClass(cls):
        restore_grievance_config(cls._config_snapshot)
        TicketConfig.default_attending_staff_role_ids = cls._original_role_ids_cfg
        super().tearDownClass()

    def test_auto_assigns_to_role_holder(self):
        result = self.service.create({
            "category": "Claims", "title": "Unpaid wages", "channel": "Channel A",
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertIn(ticket.attending_staff_id, [self.dpm_user_1.id, self.dpm_user_2.id])

    def test_no_config_leaves_unassigned(self):
        result = self.service.create({
            "category": "no_assignment_category", "title": "No assignment config", "channel": "Channel A",
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertIsNone(ticket.attending_staff)

    def test_explicit_attending_staff_not_overridden(self):
        result = self.service.create({
            "category": "Claims", "title": "Manually assigned", "channel": "Channel A",
            "attending_staff_id": self.dpm_user_1.id,
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(ticket.attending_staff_id, self.dpm_user_1.id)

    def test_round_robin_cycles_through_candidates(self):
        ordered = sorted([self.dpm_user_1, self.dpm_user_2], key=lambda u: u.id)
        category = 'Claims'
        baseline = Ticket.objects.filter(category=category).count()

        first = AssignmentService._pick(ordered, AssignmentService.STRATEGY_ROUND_ROBIN, category)
        self.assertEqual(first.id, ordered[baseline % 2].id)

        Ticket(code=f'RR-{baseline}', category=category, attending_staff=first).save(user=self.user)
        second = AssignmentService._pick(ordered, AssignmentService.STRATEGY_ROUND_ROBIN, category)
        self.assertEqual(second.id, ordered[(baseline + 1) % 2].id)

    def test_least_loaded_prefers_user_with_fewer_open_tickets(self):
        Ticket(code='LL-BUSY', category='Claims', status=Ticket.TicketStatus.OPEN,
               attending_staff=self.dpm_user_1).save(user=self.user)
        chosen = AssignmentService._pick(
            [self.dpm_user_1, self.dpm_user_2], AssignmentService.STRATEGY_LEAST_LOADED, 'Claims')
        self.assertEqual(chosen.id, self.dpm_user_2.id)

    def test_district_scope_filters_to_matching_user_district(self):
        location = Location.objects.create(code='BE10-R', name='BE10 District', type='R')
        UserDistrict(user=self.dpm_user_1.i_user, location=location, audit_user_id=-1).save()

        candidates = AssignmentService._eligible_users(
            [self.dpm_role.id], scope='district', district_code=location.code)
        candidate_ids = {u.id for u in candidates}
        self.assertIn(self.dpm_user_1.id, candidate_ids)
        self.assertNotIn(self.dpm_user_2.id, candidate_ids)


class TicketStatusTransitionTest(TestCase):
    """Status-transition validation, sticky referral, and resolved_date."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_grievance_config(DEFAULT_CFG)
        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = TicketService(cls.user)

    def _create_ticket(self, **overrides):
        payload = {"category": "Default", "title": "Status transition test", "channel": "Channel A"}
        payload.update(overrides)
        result = self.service.create(payload)
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        return Ticket.objects.get(uuid=result['data']['uuid'])

    def test_referred_without_referred_to_rejected(self):
        ticket = self._create_ticket()
        with self.assertRaises(ValidationError) as context:
            self.service.update({"id": ticket.uuid, "status": Ticket.TicketStatus.REFERRED})
        self.assertIn(
            _('validations.TicketValidation.validate_status_transition.referred_to_required'),
            str(context.exception),
        )

    def test_referred_with_invalid_authority_rejected(self):
        ticket = self._create_ticket()
        with self.assertRaises(ValidationError) as context:
            self.service.update({
                "id": ticket.uuid, "status": Ticket.TicketStatus.REFERRED,
                "referred_to": "Not A Real Authority",
            })
        self.assertIn(
            _('validations.TicketValidation.validate_status_transition.referred_to_required'),
            str(context.exception),
        )

    def test_referred_then_resolved_keeps_authority_and_sticky_flag(self):
        ticket = self._create_ticket()
        result = self.service.update({
            "id": ticket.uuid, "status": Ticket.TicketStatus.REFERRED, "referred_to": "Police",
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        referred_ticket = Ticket.objects.get(id=ticket.id)
        self.assertEqual(referred_ticket.json_ext.get('referred_to'), 'Police')
        self.assertTrue(referred_ticket.json_ext.get('was_referred'))
        self.assertIsNone(referred_ticket.json_ext.get('resolved_date'))

        result = self.service.update({"id": referred_ticket.uuid, "status": Ticket.TicketStatus.RESOLVED})
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        resolved_ticket = Ticket.objects.get(id=ticket.id)
        self.assertEqual(resolved_ticket.status, Ticket.TicketStatus.RESOLVED)
        self.assertTrue(resolved_ticket.json_ext.get('was_referred'))
        self.assertEqual(resolved_ticket.json_ext.get('referred_to'), 'Police')
        self.assertEqual(resolved_ticket.json_ext.get('resolved_date'), date.today().isoformat())

    def test_resolved_without_referral_sets_resolved_date_only(self):
        ticket = self._create_ticket()
        result = self.service.update({"id": ticket.uuid, "status": Ticket.TicketStatus.RESOLVED})
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        resolved_ticket = Ticket.objects.get(id=ticket.id)
        self.assertEqual(resolved_ticket.json_ext.get('resolved_date'), date.today().isoformat())
        self.assertNotIn('was_referred', resolved_ticket.json_ext or {})

    def test_status_not_in_enabled_list_rejected(self):
        original_statuses = TicketConfig.ticket_statuses
        TicketConfig.ticket_statuses = [
            {'code': 'OPEN', 'label': 'Open', 'initial': True},
            {'code': 'RESOLVED', 'label': 'Resolved', 'terminal': True},
        ]
        try:
            ticket = self._create_ticket()
            with self.assertRaises(ValidationError) as context:
                self.service.update({
                    "id": ticket.uuid, "status": Ticket.TicketStatus.REFERRED, "referred_to": "Police",
                })
            self.assertIn(
                _('validations.TicketValidation.validate_status_transition.status_not_enabled'),
                str(context.exception),
            )
        finally:
            TicketConfig.ticket_statuses = original_statuses


class TicketPartialWagesWorkflowTest(TestCase):
    """
    Resolving a workflow.maker_checker category with a wage_amount raises a tasks_management approval task;
    completing it fires the arrears hand-off (payroll.BenefitConsumptionService.create); rejecting it does nothing.
    """

    _config_snapshot = None
    PARTIAL_WAGES_BUSINESS_EVENT = 'grievance_social_protection.partial_wages_approval'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = TicketService(cls.user)
        cls._config_snapshot = setup_grievance_config({
            'ticket_statuses': DEFAULT_CFG['ticket_statuses'],
            'grievance_types': [
                {
                    'name': 'Partial wages',
                    'workflow': {
                        'maker_checker': True,
                        'requires_amount': True,
                        'on_approved_signal': 'payroll.benefit_consumption.create',
                    },
                },
                'no_workflow_category',
            ],
        })

    @classmethod
    def tearDownClass(cls):
        restore_grievance_config(cls._config_snapshot)
        super().tearDownClass()

    def _pending_task(self, ticket):
        return Task.objects.get(business_event=self.PARTIAL_WAGES_BUSINESS_EVENT, entity_id=str(ticket.id))

    def _task_count(self, ticket):
        return Task.objects.filter(business_event=self.PARTIAL_WAGES_BUSINESS_EVENT, entity_id=str(ticket.id)).count()

    def test_resolving_without_amount_is_rejected(self):
        with self.assertRaises(ValidationError) as context:
            self.service.create({
                "category": "Partial wages", "title": "No amount", "channel": "Channel A",
                "status": Ticket.TicketStatus.RESOLVED,
            })
        self.assertIn(
            _('validations.TicketValidation.validate_partial_wages_workflow.wage_amount_required'),
            str(context.exception),
        )

    def test_resolving_with_amount_creates_one_pending_task(self):
        result = self.service.create({
            "category": "Partial wages", "title": "Resolved with amount", "channel": "Channel A",
            "status": Ticket.TicketStatus.RESOLVED, "wage_amount": "200.00",
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])

        task = self._pending_task(ticket)
        self.assertEqual(task.status, Task.Status.RECEIVED)
        self.assertEqual(task.entity_id, str(ticket.id))

    def test_no_duplicate_task_on_unrelated_update(self):
        result = self.service.create({
            "category": "Partial wages", "title": "Resolved with amount", "channel": "Channel A",
            "status": Ticket.TicketStatus.RESOLVED, "wage_amount": "200.00",
        })
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(self._task_count(ticket), 1)

        result = self.service.update({"id": ticket.uuid, "title": "Renamed"})
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        self.assertEqual(self._task_count(ticket), 1)

    def test_category_without_maker_checker_creates_no_task(self):
        result = self.service.create({
            "category": "no_workflow_category", "title": "Plain resolve", "channel": "Channel A",
            "status": Ticket.TicketStatus.RESOLVED, "wage_amount": "50.00",
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        self.assertEqual(self._task_count(ticket), 0)

    @patch('payroll.services.BenefitConsumptionService.create')
    def test_task_completion_fires_arrears_creation(self, mock_bc_create):
        mock_bc_create.return_value = {'success': True, 'data': {}}
        individual = create_test_individual(self.user)
        result = self.service.create({
            "category": "Partial wages", "title": "Resolved with amount", "channel": "Channel A",
            "status": Ticket.TicketStatus.RESOLVED, "wage_amount": "300.00",
            "reporter_type": "individual", "reporter_id": str(individual.id),
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        task = self._pending_task(ticket)

        complete_result = TaskService(self.user).complete_task({'id': task.id})
        self.assertTrue(complete_result.get('success', False), complete_result.get('detail', ""))

        mock_bc_create.assert_called_once()
        payload = mock_bc_create.call_args[0][0]
        self.assertEqual(payload['individual'], individual)
        self.assertEqual(payload['amount'], Decimal('300.00'))
        self.assertEqual(payload['type'], 'ARREARS')

    @patch('payroll.services.BenefitConsumptionService.create')
    def test_task_rejection_does_not_fire_arrears(self, mock_bc_create):
        individual = create_test_individual(self.user)
        result = self.service.create({
            "category": "Partial wages", "title": "Resolved with amount", "channel": "Channel A",
            "status": Ticket.TicketStatus.RESOLVED, "wage_amount": "300.00",
            "reporter_type": "individual", "reporter_id": str(individual.id),
        })
        ticket = Ticket.objects.get(uuid=result['data']['uuid'])
        task = self._pending_task(ticket)

        complete_result = TaskService(self.user).complete_task({'id': task.id, 'failed': True})
        self.assertTrue(complete_result.get('success', False), complete_result.get('detail', ""))

        mock_bc_create.assert_not_called()


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class TicketAssignmentNotificationTest(TestCase):
    """Email the assignee when attending_staff is set/changed."""

    _config_snapshot = None
    _original_role_ids_cfg = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._original_role_ids_cfg = TicketConfig.default_attending_staff_role_ids
        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = TicketService(cls.user)
        cls.assignee = create_test_interactive_user(
            username='be11_assignee', roles=[create_test_role(name='BE11Role').id],
            custom_props={'email': 'assignee@example.com'})
        cls.assignee_2 = create_test_interactive_user(
            username='be11_assignee2', roles=[create_test_role(name='BE11Role2').id],
            custom_props={'email': 'assignee2@example.com'})
        cls.no_email_user = create_test_interactive_user(
            username='be11_no_email', roles=[create_test_role(name='BE11Role3').id])
        cls._config_snapshot = setup_grievance_config(DEFAULT_CFG)
        # Disable auto-assignment so these tests fully control attending_staff via explicit payload values.
        TicketConfig.default_attending_staff_role_ids = {}

    @classmethod
    def tearDownClass(cls):
        restore_grievance_config(cls._config_snapshot)
        TicketConfig.default_attending_staff_role_ids = cls._original_role_ids_cfg
        super().tearDownClass()

    def setUp(self):
        mail.outbox.clear()
        self._original_notifications = TicketConfig.notifications

    def tearDown(self):
        TicketConfig.notifications = self._original_notifications

    def _create_unassigned_ticket(self):
        result = self.service.create({
            "category": "Default", "title": "Unassigned", "channel": "Channel A",
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        return Ticket.objects.get(uuid=result['data']['uuid'])

    def test_assignment_sends_email_with_due_date(self):
        result = self.service.create({
            "category": "Default", "title": "Notify test", "channel": "Channel A",
            "attending_staff_id": self.assignee.id,
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertIn('assignee@example.com', sent.to)
        self.assertIn(result['data']['code'], sent.subject)
        self.assertIn('[OpenIMIS]', sent.subject)

    def test_on_assign_false_sends_no_email(self):
        TicketConfig.notifications = {**self._original_notifications, 'on_assign': False}
        result = self.service.create({
            "category": "Default", "title": "No notify", "channel": "Channel A",
            "attending_staff_id": self.assignee.id,
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        self.assertEqual(len(mail.outbox), 0)

    def test_email_channel_not_configured_sends_no_email(self):
        TicketConfig.notifications = {**self._original_notifications, 'channels': []}
        result = self.service.create({
            "category": "Default", "title": "No email channel", "channel": "Channel A",
            "attending_staff_id": self.assignee.id,
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        self.assertEqual(len(mail.outbox), 0)

    def test_reassignment_on_update_sends_email_to_new_assignee(self):
        ticket = self._create_unassigned_ticket()
        mail.outbox.clear()

        result = self.service.update({"id": ticket.uuid, "attending_staff_id": self.assignee.id})
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('assignee@example.com', mail.outbox[0].to)

        mail.outbox.clear()
        result = self.service.update({"id": ticket.uuid, "attending_staff_id": self.assignee_2.id})
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('assignee2@example.com', mail.outbox[0].to)

    def test_unrelated_update_does_not_resend_email(self):
        ticket = self._create_unassigned_ticket()
        self.service.update({"id": ticket.uuid, "attending_staff_id": self.assignee.id})
        mail.outbox.clear()

        result = self.service.update({"id": ticket.uuid, "title": "Renamed"})
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        self.assertEqual(len(mail.outbox), 0)

    def test_assignee_without_email_sends_nothing_no_crash(self):
        result = self.service.create({
            "category": "Default", "title": "No email address", "channel": "Channel A",
            "attending_staff_id": self.no_email_user.id,
        })
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        self.assertEqual(len(mail.outbox), 0)
