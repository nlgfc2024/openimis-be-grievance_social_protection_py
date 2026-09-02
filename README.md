# openIMIS Backend Grievance Social Protection reference module
This repository holds the files of the openIMIS Backend grievance social protection reference module.
It is dedicated to be deployed as a module of [openimis-be_py](https://github.com/openimis/openimis-be_py).

## Features

- **Grievance Management**: Create, update, and track grievances
- **Comments System**: Add comments and resolutions to grievance
- **Configurable Categories and Flags**: Define custom grievance types and flags
- **Automatic Rights Generation**: Auto-generate rights based on category/flag permissions
- **Restricted View Access**: Three-tier access levels (restricted/read/full) with automatic data filtering
- **Hierarchical Categories**: Support for multi-level category structures with permission inheritance
- **Priority Management**: Automatic priority assignment based on category/flag configuration
- **Resolution Tracking**: Configure resolution times by category
- **Default Category Support**: Automatic assignment of 'uncategorized' type for tickets without explicit category

## Configuration options (can be changed via core.ModuleConfiguration)

### Basic Configuration

* `default_grievance_type`: The category name assigned to tickets when no category is specified. This should be a simple string value. If this category doesn't exist in `grievance_types`, it will be auto-added with `['read', 'update']` permissions.
(default: `'uncategorized'`)

* `resolution_times`: time to resolution in form of CRON timedelta: {days},{hours} where days are values between <0, 99) and hours are between 0 and 24. 
(default: `5,0`)

* `default_resolution`: The field will be in form of the JSON dictionary with pairs like: 
Key - type of the grievance, 
Value - time to resolution in form of CRON timedelta: `{days},{hours}` where days are values between <0, 99) and hours are between 0 and 24.
(default: `{Default: '5,0'}`)
Note: If for given type of the grievance time is not provided then default value is used from `resolution_times`.

### Category Separator

The system uses ` > ` (space-greater than-space) as the hierarchy separator in category full names. For example, a child category `Missing Documents` under parent `Enrollment` has the full name `Enrollment > Missing Documents`. This separator is used universally across the backend (internal keys, database storage, permission names) and frontend (display, Cascader values).

**Important:** Category names must not contain ` > ` as this would break hierarchy parsing.

The separator is defined as `CATEGORY_SEPARATOR` in `apps.py` and imported where needed.

### Categories Configuration

The module supports both simple and enhanced category configurations:

#### Simple Format (backward compatible)
```json
{
  "grievance_types": ["complaint", "feedback", "appeal"]
}
```

#### Enhanced Format with Auto-Generated Permission IDs

> **Note:** `permissions` values are permission-type strings (`restricted_read`, `read`, `create`, `update`, `delete`), not numeric right IDs. The system generates numeric IDs automatically.
```jsonc
{
  "default_grievance_type": "uncategorized",
  "grievance_flags": [
    "public",
    {
      "name": "sensitive",
      "priority": "Critical",
      "permissions": ["restricted_read", "read", "create"]
    }
  ],
  "grievance_types": [
    "simple_category",
    {
      "name": "complaint",
      "priority": "High",
      "permissions": ["restricted_read", "read", "create", "update"],
      "visible_fields": ["id", "status", "category", "priority", "date_created"],
      "default_flags": ["urgent"],
      "resolution_times": "3,0",  // 3 days, 0 hours
      "children": [
        {
          "name": "vbg_complaint",
          "permissions": ["restricted_read", "read", "create"],
          "visible_fields": ["id", "status", "category"],  // More restrictive than parent
          "resolution_times": "2,12",  // Overrides parent's resolution time
          "default_flags": ["confidential"]
        },
        {
          "name": "general_complaint"
          // Inherits parent's permissions and visible_fields
        }
      ]
    }
  ]
}
```

##### Permission ID Auto-Generation
The grievance module automatically generates permission IDs using Django's auth_permission table:

1. **ID Range Convention**:
   - Static permissions: 127000-127099 (hardcoded module permissions)
   - Dynamic permissions: 127100-127999 (auto-generated for categories/flags)

2. **Auto-Generated ID Pattern**:
   Permission IDs are automatically assigned following a suffix pattern:
   - IDs ending in `0` - read permission (for gql_query_*)
   - IDs ending in `1` - create permission (for gql_mutation_create_*)
   - IDs ending in `2` - update permission (for gql_mutation_update_*)
   - IDs ending in `3` - delete permission (for gql_mutation_delete_*)
   - IDs ending in `4` - restricted_read permission

3. **Permission Types**:
   - `"restricted_read"` - Limited view access
   - `"read"` - Full read access
   - `"create"` - Create/write access
   - `"update"` - Update/modify access
   - `"delete"` - Delete access

4. **Persistence**:
   - Permissions are stored in Django's `auth_permission` table
   - Once assigned, an ID remains stable even if configuration changes
   - Permissions can be managed through Django admin interface
   - Deleted permissions free up their IDs for reuse

3. **Access Levels** (based on user's rights):
   - **Restricted Access** (`restricted_read` right): 
     - If `visible_fields` is configured: Users see only fields listed in `visible_fields`
     - If `visible_fields` is not configured: Users see only basic info (id, status, category, priority, date_created)
     - Non-visible fields show as "[Restricted]" for text fields or null for other types
     - Can only filter queries on visible fields
   - **Read Access** (`read` right): 
     - Users see all ticket information including descriptions and resolutions
     - Can filter on all fields
     - Cannot modify tickets
   - **Full Access** (`create`/`update` rights): 
     - Users can view and modify tickets
     - Can create new tickets in categories where they have `create` permission

##### Resolution Times Priority
Resolution times are determined in the following order:
1. Category-specific `resolution_times` (with inheritance from parent categories)
2. Legacy `default_resolution` configuration for the category
3. Global `resolution_times` configuration
4. Default value `5,0` (5 days, 0 hours)

##### How the SLA is applied to a ticket

The SLA is driven by the grievance's **category**, not entered by hand:

- **`resolution`** (the `{days},{hours}` string on the ticket) — `TicketService`
  fills it from the category's configured resolution time when the client
  hasn't supplied one. The frontend edit form shows it **read-only** and
  re-derives it whenever the category is changed.
- **`due_date`** (the timer) — always computed category-first
  (`_category_resolution_time(category)` falls back to the ticket's `resolution`
  only when the category has none). Set on create from the case-creation date;
  on update it is **only** recomputed when the category actually changes, and it
  keeps the original creation date as the clock's origin. An explicitly supplied
  `due_date` is respected on create.

##### Example Configuration
```jsonc
{
  "resolution_times": "5,0",  // Global default: 5 days
  "grievance_types": [
    {
      "name": "complaint",
      "resolution_times": "3,0",  // Category-specific: 3 days
      "children": [
        {
          "name": "urgent_complaint",
          "resolution_times": "1,0"  // Override: 1 day
        },
        {
          "name": "general_complaint"  // Inherits parent's 3 days
        }
      ]
    },
    "feedback"  // Uses default_resolution: 7 days
  ]
}

#### Visible Fields Configuration

The `visible_fields` feature controls field visibility for users with `restricted_read` permission:

```json
{
  "name": "complaint",
  "visible_fields": ["id", "status", "category", "priority", "date_created"],
  "permissions": ["restricted_read", "read", "create"]
}
```

**Key Features:**

1. **Automatic Permission Requirements**: When `visible_fields` is defined, the system automatically adds `restricted_read` and `read` permissions if not already present.

2. **Inheritance with Security Boundaries**: 
   - Child categories inherit parent's `visible_fields` by default
   - **Security Boundary**: Children can only make fields MORE restrictive, never less
   - A child cannot expose fields that the parent has hidden
   - This ensures security boundaries are maintained through the category hierarchy

3. **Field Visibility Rules**:
   - Users with `restricted_read`: See only fields in `visible_fields` list
   - Users with `read` or higher: See all fields
   - Non-visible fields return `[Restricted]` for text fields or `null` for other types

4. **Example Inheritance**:
```jsonc
{
  "name": "complaint",
  "visible_fields": ["id", "status", "category", "priority", "date_created"],
  "children": [
    {
      "name": "vbg_complaint",
      "visible_fields": ["id", "status"]  // ✓ Valid - subset of parent
    },
    {
      "name": "fraud_complaint",
      "visible_fields": ["id", "status", "title"]  // ✗ Invalid - "title" not in parent
      // System will raise a ValueError until this is corrected
    }
  ]
}
```

5. **Default Behavior**: If `visible_fields` is not configured, restricted users see a default set of basic fields: `id`, `status`, `category`, `priority`, `date_created`.

6. **Interaction with Default Flags**: 
   - Categories cannot reduce security set by their default flags
   - If a category has `default_flags: ["confidential"]` and the confidential flag restricts certain fields, the category's `visible_fields` must respect those restrictions
   - This prevents accidental exposure of sensitive information through misconfiguration

### Flags Configuration

Flags are single-level configurations only and do NOT support hierarchical/nested structures. Unlike categories (`grievance_types`) which support parent-child relationships with permission inheritance, flags are always flat.

#### Simple Format
```json
{
  "grievance_flags": ["urgent", "sensitive"]
}
```

#### Enhanced Format with Permissions
```jsonc
{
  "grievance_flags": [
    "public",
    {
      "name": "sensitive",
      "priority": "Critical",
      "permissions": ["read", "create"]  // Auto-generates permission IDs
    }
  ]
}
```

## Permissions

### Query Permissions
- `127000`: View grievances
- `127001`: Create grievances
- `127002`: Update grievances
- `127003`: Delete grievances
- `127004`: View comments
- `127005`: Create comments
- `127006`: Resolve grievances

### Category and Flag Permissions
- Child categories inherit parent permissions unless explicitly overridden
- Grievances queries are automatically filtered based on user permissions

### Reporter capture — cross-module read permissions

The grievance intake form lets the officer identify the reporter as an
**individual** (anyone, registered or not) or a **beneficiary** (an individual
enrolled in a phase and one of that phase's projects). The beneficiary path
reads from other modules, so a grievance-officer role that creates
beneficiary-reporter tickets must **also** hold:

| Right    | Module                     | Used for |
|----------|----------------------------|----------|
| `160001` | `social_protection`        | Phase (benefit plan) picker — `gql_benefit_plan_search_perms` |
| `209001` | `project_social_protection`| Project picker and the enrolled-household lookup (`projectEligibleGroupBeneficiaries` / `project`) — `gql_project_search_perms` |
| individual read | `individual`         | Household-member picker (`individual(groupId:)`); grievance officers normally have this already |

These rights are read-only and gate only the **creation** form. Viewing or
updating an existing grievance does not require them. The plain *individual*
reporter path and `user` reporters need none of them.

### Walk-in / unregistered reporters

When the reporter is not in the system, the officer captures their details by
hand (name, date of birth, phone, national ID). These are sent as their own
mutation fields (`reporterFirstName`, `reporterLastName`, `reporterDob`,
`reporterPhone`, `reporterNationalId`) and stored by `TicketService` on
`ticket.json_ext['unregistered_reporter']`, with `national_id` /
`household_mobile_number` mirrored onto the keys the Custom Filter Wizard and
participant panel already read. The ticket carries **no** `reporter_type` /
`reporter_id`; the `reporterFirstName` / `reporterLastName` / `reporterDob`
GraphQL resolvers fall back to this block. Supplying a real `reporter_id`
discards any hand-captured details.

**Why they are not auto-registered as an `individual.Individual`:** the
Individual registry is the system of record for programme participants —
targeting, enrolment candidate lists, deduplication, payments, reporting
headcounts. Auto-creating a row for every walk-in (including unverified or
malicious callers) would:

- pollute enrolment/targeting queries and headcount reports with non-participants;
- generate duplicate rows — `IndividualService.create` does not deduplicate — that
  then surface as noise in the deduplication review queue;
- fan a grievance create out into another module's pipeline via
  `individual_service.create` signals (deduplication, OpenSearch indexing, tasks),
  so an individual-side failure could block grievance intake;
- require grievance officers to hold individual-create rights and leave
  location-less, unverified person records with no cleanup path if the grievance
  is later rejected as spam.

Keeping the details on the grievance avoids all of that while still making the
complainant searchable and rendering them in the participant panel. If a walk-in
later needs to be a real participant, that should be an explicit, human-reviewed
"register this person" step — not a side effect of logging a grievance.

### Derived participant fields

On create, `TicketService` denormalises the reporter's participant details into
`ticket.json_ext`:

- `district_code` / `district_name` — walked up the `location` tree from the
  reporter's village to the District (type `R`) ancestor;
- `micro_catchment` — from the reporter's GVH / TA via the
  `location.MicroCatchmentGVH` / `MicroCatchmentTA` link tables;
- `project_name` — the reporter's benefit-plan name;
- `days_worked` — count of project time-entry rows with `percent_complete > 0`
  for the resolved enrolment;
- `hotspot_name` — the `hotspot` of the resolved project.

**Which enrolment.** The `createTicket` mutation carries the intake selection as
`reporter_project_id` / `reporter_group_beneficiary_id` (create-only hints, never
persisted). When present they pin the exact `*ProjectEnrollment`, and the
reporter must actually belong to it — the individual behind the `Beneficiary`, or
a non-deleted member of the household — otherwise no project-derived field is
set. Without the hints, the reporter's first non-deleted enrolment is used
(individual `Beneficiary` → group `GroupBeneficiary` fallback). Deleted
beneficiary, group-beneficiary, membership and enrolment rows are excluded
throughout. This runs as the ticket service, not the requesting user, so it needs
no extra rights, but `social_protection` and `project_social_protection` must be
installed. Missing data is skipped silently.

The `grievanceReporterDerivedFields(reporterType, reporterId, projectId,
groupBeneficiaryId)` query returns the same `json_ext` **without persisting
anything**, so the intake form can show district / micro-catchment / project /
hotspot for the selected reporter (and selected project/household) before the
grievance is saved. It is gated by `gql_query_tickets_perms` (`127000`).

## GraphQL API

### Configuration Query Fields
The `grievance_config` query returns:
- `grievance_types`: Flat list of accessible categories
- `grievance_flags`: List of accessible flags
- `grievance_channels`: Available channels
- `grievance_categories_hierarchical`: Hierarchical category structure (recursive `GrievanceCategoryGQLType`; depth depends on client query)
- `grievance_categories_json`: Full category hierarchy as JSON (no depth limitation)
- `grievance_flags_detailed`: Detailed flag information with permissions
- `accessible_categories`: Categories user can create grievances in
- `accessible_flags`: Flags user can apply to grievances

## Access Control Features

1. **Two-Tier Permission System**:
   - **Tier 1**: General ticket permissions (required for ALL ticket operations)
     - 127000: Query tickets
     - 127001: Create tickets
     - 127002: Update tickets
     - 127003: Delete tickets
   - **Tier 2**: Category/flag-specific permissions (additional requirements for restricted categories)

2. **Role Composition Pattern**:
   The system supports hierarchical role composition for clean permission management:
   ```
   GRIEVANCE_VIEWER (Base role)
   ├── General ticket query permissions
   └── Access to unrestricted categories
   
   COMPLAINT_VIEWER (Category role) 
   ├── Requires: GRIEVANCE_VIEWER
   └── Complaint category view permissions
   
   GBV_SPECIALIST (Specialized role)
   ├── Requires: GRIEVANCE_VIEWER + COMPLAINT_VIEWER
   └── VBG-specific permissions
   ```
   
   Users are assigned multiple roles to compose their complete permission set.

3. **Category-Based Filtering**: Users only see grievances in categories they have permission to view

4. **Flag-Based Filtering**: Grievances with restricted flags are hidden from unauthorized users

5. **Hierarchical Permissions**: 
   - Children inherit parent permissions by default
   - Children can override with more restrictive permissions
   - Users see only accessible parts of the hierarchy

6. **Create/Update Validation**: System validates permissions before allowing grievance operations

7. **Mixed Flag Handling**: When a grievance has multiple flags:
   - Flags without restrictions are ignored in access calculations
   - Access level is determined by the most restrictive flag that has permissions defined

## Security Implementation

Fine-grained security is enforced at the Django model level by overriding the `get_queryset` method. This ensures that security applies consistently across all endpoint technologies (GraphQL, FHIR, REST, etc.).

### Model-Level Security

Security filtering is implemented in the models themselves:

```python
# grievance_social_protection/models.py

class Ticket(HistoryBusinessModel):
    @classmethod
    def get_queryset(cls, queryset, user):
        queryset = cls.filter_queryset(queryset)
        # GraphQL calls with an info object while Rest calls with the user itself
        if isinstance(user, ResolveInfo):
            user = user.context.user
        if settings.ROW_SECURITY and user.is_anonymous:
            return queryset.filter(id=None)
        if settings.ROW_SECURITY:
            # Apply category and flag permission filtering
            from .access_control import GrievanceAccessControl
            queryset = GrievanceAccessControl.filter_ticket_queryset(queryset, user)
        return queryset
```

### Field-Level Access Control

While queryset filtering happens at the model level, field-level access control (showing "[Restricted]" for sensitive fields) is implemented in the GraphQL type resolvers to provide granular control over field visibility based on the user's access level

## Permission Management

### Auto-Generated Permissions

The module uses Django's `auth_permission` table to store and manage permissions:

1. **Automatic Creation**: On module startup, permissions are automatically created in the `auth_permission` table based on your configuration
2. **Stable IDs**: Once created, permission IDs remain stable even if configuration changes
3. **Django Admin Management**: Permissions can be viewed and managed through Django's admin interface
4. **ID Reuse**: Deleted permissions free up their IDs for potential reuse following the suffix pattern

### Permission Lifecycle

1. **Creation**:
   - Configure categories/flags with permission requirements in ModuleConfiguration
   - On startup, system checks if permissions exist in `auth_permission`
   - New permissions are created with auto-generated IDs following the suffix pattern
   - Existing permissions retain their IDs

2. **Updates**:
   - Configuration changes don't affect existing permission IDs
   - New permissions in configuration create new entries
   - Removed permissions from configuration remain in `auth_permission` until manually deleted

3. **Deletion**:
   - Permissions must be explicitly deleted through Django admin
   - Deleting a permission frees its ID for reuse
   - Roles referencing deleted permissions lose that access

### Managing Permissions

Use the provided management command to view and manage permissions:

```bash
# List all grievance permissions
python manage.py manage_grievance_permissions list

# Export permission mapping for backup
python manage.py manage_grievance_permissions export --format=json > permissions_backup.json

# Find and remove orphaned permissions
python manage.py manage_grievance_permissions cleanup --dry-run
python manage.py manage_grievance_permissions cleanup

# Sync permissions with current configuration
python manage.py manage_grievance_permissions sync
```
