from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from location.models import Location, MicroCatchment, MicroCatchmentGVH, MicroCatchmentTA

from grievance_social_protection.models import Ticket
from grievance_social_protection.services import TicketService
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
from core.test_helpers import LogInHelper
from core.utils import TimeUtils
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
