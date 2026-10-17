"""Long-running outbox publisher; launch as a separate process from API/workers."""
import logging
import signal
from threading import Event
import time

from src.config import settings
from src.db.database import engine
from src.services.workflow_dispatch import publish_due, recover_stale
from src.services import monitoring
from src.workers.celery_app import celery_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
_stop = Event()


def validate_runtime():
    if not settings.CLOUDAMQP_URL.get_secret_value().strip().startswith('amqps://'):
        raise RuntimeError('dispatcher requires a valid amqps CLOUDAMQP_URL')
    from src.db.workflow_schema import validate_workflow_schema
    with engine.connect() as connection:
        validate_workflow_schema(connection)


def main():
    validate_runtime()
    signal.signal(signal.SIGINT, lambda *_: _stop.set())
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    logger.info('workflow_dispatcher_started queue=%s', settings.WORKFLOW_QUEUE_NAME)
    last_heartbeat = 0.0
    while not _stop.is_set():
        try:
            recover_stale()
            # One bounded publish per iteration lets shutdown stop taking new
            # claims while the current confirmation finishes.
            publish_due(celery_app, limit=1)
            if time.monotonic() - last_heartbeat >= 60:
                monitoring.system_event(engine, 'workflow_dispatcher', 'dispatcher_heartbeat', 'healthy')
                last_heartbeat = time.monotonic()
        except Exception as exc:
            logger.error('workflow_dispatcher_iteration_failed error_type=%s', type(exc).__name__)
        _stop.wait(settings.WORKFLOW_DISPATCH_POLL_SECONDS)
    logger.info('workflow_dispatcher_stopped')


if __name__ == '__main__':
    main()
