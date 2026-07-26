import json
import logging
import re
from collections import namedtuple
from typing import List

from django.db.models.query import QuerySet

from core.custom_filters import CustomFilterWizardInterface
from .models import Ticket

logger = logging.getLogger(__name__)

# Grievance-specific fields derived onto ticket.json_ext that
# aren't part of the Individual module's schema, so they're added on top of
# it rather than duplicating the whole schema here.
EXTRA_SEARCHABLE_FIELDS = {
    'district_code': 'string',
    'district_name': 'string',
    'micro_catchment': 'string',
    'project_name': 'string',
    'days_worked': 'integer',
}


class TicketCustomFilterWizard(CustomFilterWizardInterface):
    """
    Advanced-filter wizard over Ticket.json_ext — the participant/location
    fields denormalised onto the ticket at create. Reuses the Individual 
    module's individual_schema so case search shares the same field names 
    and advanced-criteria UX (form number, national id, village/TA/GVH), 
    plus the grievance-specific derived fields it doesn't cover
    (district, micro-catchment, project, days worked).
    """

    OBJECT_CLASS = Ticket

    def get_type_of_object(self) -> str:
        return self.OBJECT_CLASS.__name__

    def load_definition(self, tuple_type: type, **kwargs) -> List[namedtuple]:
        properties = self._load_individual_schema_properties()
        tuples = [
            tuple_type(
                field=key,
                filter=self.FILTERS_BASED_ON_FIELD_TYPE.get(value.get('type'), []),
                type=value.get('type'),
            )
            for key, value in properties.items()
        ]
        for key, value_type in EXTRA_SEARCHABLE_FIELDS.items():
            if key in properties:
                continue
            tuples.append(tuple_type(
                field=key,
                filter=self.FILTERS_BASED_ON_FIELD_TYPE.get(value_type, []),
                type=value_type,
            ))
        return tuples

    def apply_filter_to_queryset(self, custom_filters: List[namedtuple], query: QuerySet, relation=None) -> QuerySet:
        """
        Filters the ticket's OWN json_ext (relation is always None in practice
        for tickets — `reporter` is a GenericForeignKey, which Django cannot
        traverse in `.filter()`; that's why denormalise onto the ticket itself instead). 
        `relation` is accepted for interface compliance / consistency with other wizards.
        """
        for filter_part in custom_filters:
            field, value = filter_part.split('=')
            field, value_type = field.rsplit('__', 1)
            value = self._cast_value(value, value_type)
            filter_kwargs = {f"{relation}__json_ext__{field}" if relation else f"json_ext__{field}": value}
            query = query.filter(**filter_kwargs)
        return query

    @staticmethod
    def _load_individual_schema_properties():
        try:
            from individual.apps import IndividualConfig
        except ImportError:
            return {}
        schema = getattr(IndividualConfig, 'individual_schema', None)
        if not schema:
            return {}
        try:
            return json.loads(schema).get('properties', {})
        except (TypeError, ValueError):
            logger.warning("Could not parse Individual schema for the Ticket filter wizard.")
            return {}

    @staticmethod
    def _cast_value(value, value_type):
        if value_type == 'integer':
            return int(value)
        if value_type == 'string':
            return str(value[1:-1])
        if value_type == 'numeric':
            return float(value)
        if value_type == 'boolean':
            cleaned_value = re.sub(r'[^\w\s]', '', value)
            return cleaned_value.lower() == 'true'
        return None
