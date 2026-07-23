import copy

from django.apps import apps

from core.datetimes.ad_datetime import datetime
from core.models import Role, RoleRight, UserRole
from core.test_helpers import create_test_interactive_user, create_test_role
from grievance_social_protection.apps import TicketConfig
from grievance_social_protection.models import (
    Comment,
    Ticket
)
from grievance_social_protection.rights import GrievanceRightsManager
from grievance_social_protection.tests.data import service_add_ticket_payload


def create_test_grievance_user(username='user_authorized'):
    perm_names = [
        "gql_query_tickets_perms",
        "gql_query_comments_perms",
        "gql_mutation_create_tickets_perms",
        "gql_mutation_update_tickets_perms",
        "gql_mutation_delete_tickets_perms",
        "gql_mutation_create_comment_perms",
        "gql_mutation_resolve_grievance_perms",
    ]
    role = create_test_role(perm_names=perm_names, name="GrievanceUserRole")
    return create_test_interactive_user(username=username, roles=[role.id])


def create_ticket(user):
    ticket = Ticket(**service_add_ticket_payload)
    ticket.save(user=user)
    return ticket


def create_test_individual(user, json_ext=None):
    """Create a minimal Individual for reporter-denormalization tests."""
    individual_model = apps.get_model('individual', 'Individual')
    individual = individual_model(
        first_name='TestFN',
        last_name='TestLN',
        dob=datetime.now(),
        json_ext=json_ext or {},
    )
    individual.save(username=user.username)
    return individual


def create_comment_for_existing_ticket(user, ticket, resolved=False):
    comment = Comment(**{
        "ticket_id": ticket.id,
        "comment": "awesome comment",
        "is_resolution": True if resolved else False
    })
    comment.save(user=user)
    if resolved:
        ticket.status = 'CLOSED'
        ticket.save(user=user)
    return comment


def save_grievance_config():
    """Snapshot TicketConfig class-level state so it can be restored later."""
    return {
        'processed_categories': dict(TicketConfig.processed_categories),
        'processed_flags': dict(TicketConfig.processed_flags),
        'grievance_types': list(TicketConfig.grievance_types),
        'grievance_flags': list(TicketConfig.grievance_flags),
        'generated_rights': dict(TicketConfig.generated_rights),
        'unified_resolution_times': dict(TicketConfig.unified_resolution_times),
    }


def restore_grievance_config(snapshot):
    """Restore TicketConfig class-level state from a snapshot."""
    for attr, value in snapshot.items():
        setattr(TicketConfig, attr, value)


def setup_grievance_config(cfg):
    """Process a grievance config dict and generate rights on TicketConfig.

    Handles the standard sequence: process categories, process flags,
    load config, and generate automatic rights.

    Returns:
        dict: Snapshot of previous state (pass to restore_grievance_config).
    """
    cfg = copy.deepcopy(cfg)
    snapshot = save_grievance_config()
    TicketConfig._TicketConfig__process_unified_categories(cfg)
    TicketConfig._TicketConfig__process_unified_flags(cfg)
    TicketConfig._TicketConfig__load_config(cfg)
    GrievanceRightsManager.generate_automatic_rights(TicketConfig)
    return snapshot


def assign_rights_to_user(user, right_ids, role_name=None):
    """Create a role with given right IDs and assign it to the user.

    Uses get_or_create for the Role to be safe with --keepdb.
    """
    if not (hasattr(user, 'i_user') and user.i_user) or not right_ids:
        return
    role, _ = Role.objects.get_or_create(
        name=role_name or f'TestRole_{getattr(user, "username", "unknown")}',
        defaults={'is_system': 0, 'is_blocked': False, 'audit_user_id': -1}
    )
    for right_id in right_ids:
        RoleRight.objects.get_or_create(
            role=role, right_id=int(right_id),
            defaults={'audit_user_id': -1}
        )
    UserRole.objects.get_or_create(
        user=user.i_user, role=role,
        defaults={'audit_user_id': -1}
    )


def get_rights(config_attr, name):
    """Get generated_rights dict for a category or flag from TicketConfig.

    Args:
        config_attr: 'processed_categories' or 'processed_flags'
        name: Category or flag name

    Returns:
        dict of {access_type: right_id}, or empty dict if not found
    """
    return getattr(TicketConfig, config_attr, {}).get(name, {}).get('generated_rights', {})


def collect_all_rights():
    """Collect all generated right IDs across categories and flags."""
    right_ids = set()
    for info in TicketConfig.processed_categories.values():
        right_ids.update(info.get('generated_rights', {}).values())
    for info in TicketConfig.processed_flags.values():
        right_ids.update(info.get('generated_rights', {}).values())
    return right_ids
