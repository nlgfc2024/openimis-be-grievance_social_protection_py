import logging

from core.models import User
from core.service_signals import ServiceSignalBindType
from core.signals import bind_service_signal

logger = logging.getLogger(__name__)


def bind_service_signals():
    def on_task_complete_partial_wages_approval(**kwargs):
        from grievance_social_protection.services import handle_partial_wages_task_completion
        try:
            result = kwargs.get('result')
            if not result or not result.get('success'):
                return
            task = result['data']['task']
            user = User.objects.get(id=result['data']['user']['id'])
            handle_partial_wages_task_completion(task, user)
        except Exception as exc:
            logger.error("Error while executing on_task_complete_partial_wages_approval", exc_info=exc)

    bind_service_signal(
        'task_service.complete_task',
        on_task_complete_partial_wages_approval,
        bind_type=ServiceSignalBindType.AFTER,
    )
