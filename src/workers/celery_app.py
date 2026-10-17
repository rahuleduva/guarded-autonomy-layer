"""Celery configuration; importing this module never opens a broker connection."""
import ssl
from threading import Event, Thread
from urllib.parse import urlsplit

from celery import Celery
from kombu import Exchange, Queue
from celery.signals import worker_init, worker_ready, worker_shutdown

from src.config import settings


def _broker_url():
    value = settings.CLOUDAMQP_URL.get_secret_value().strip()
    if value and not value.startswith('amqps://'):
        raise RuntimeError('CLOUDAMQP_URL must use amqps:// with certificate validation')
    if value and not urlsplit(value).hostname:
        raise RuntimeError('CLOUDAMQP_URL requires a broker hostname')
    return value


queue_name = settings.WORKFLOW_QUEUE_NAME
workflow_exchange = Exchange(queue_name, type='direct', durable=True)
workflow_queue = Queue(queue_name, workflow_exchange, routing_key=queue_name, durable=True)
celery_app = Celery('guarded_autonomy_layer', broker=_broker_url(), include=['src.workers.tasks'])
celery_app.conf.update(
    task_serializer='json', accept_content=['json'], result_serializer='json',
    result_backend=None, task_ignore_result=True, task_acks_late=True, task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1, task_track_started=False,
    task_default_queue=queue_name, task_default_exchange=queue_name,
    task_default_exchange_type='direct', task_default_routing_key=queue_name,
    task_default_delivery_mode='persistent', task_protocol=2,
    task_queues=(workflow_queue,), task_routes={'src.workers.tasks.run_workflow': {'queue': queue_name}},
    broker_connection_timeout=settings.CLOUDAMQP_CONNECTION_TIMEOUT_SECONDS,
    broker_connection_retry=True, broker_connection_retry_on_startup=True,
    broker_transport_options={
        'confirm_publish': True,
        'connect_timeout': settings.CLOUDAMQP_CONNECTION_TIMEOUT_SECONDS,
        'read_timeout': settings.CLOUDAMQP_PUBLISH_TIMEOUT_SECONDS,
        'write_timeout': settings.CLOUDAMQP_PUBLISH_TIMEOUT_SECONDS,
    },
    broker_heartbeat=30, broker_heartbeat_checkrate=2,
    broker_use_ssl={'cert_reqs': ssl.CERT_REQUIRED,
                    'server_hostname': urlsplit(_broker_url()).hostname} if _broker_url().startswith('amqps://') else None,
    task_publish_retry=False, timezone='UTC', enable_utc=True,
    task_time_limit=settings.WORKFLOW_MAX_RUN_SECONDS + 30,
    broker_pool_limit=1, task_create_missing_queues=False,
    worker_enable_remote_control=False, worker_send_task_events=False, task_send_sent_event=False,
)

_worker_health_stop = Event()
_worker_health_thread = None


def _worker_health_loop():
    from src.db.database import engine
    from src.services.monitoring import system_event
    while not _worker_health_stop.wait(60):
        system_event(engine, 'workflow_worker', 'worker_heartbeat', 'healthy')


@worker_ready.connect(weak=False)
def _start_worker_health(**_kwargs):
    global _worker_health_thread
    _worker_health_stop.clear()
    _worker_health_thread = Thread(target=_worker_health_loop, name='workflow-worker-health', daemon=True)
    _worker_health_thread.start()


@worker_shutdown.connect(weak=False)
def _stop_worker_health(**_kwargs):
    _worker_health_stop.set()
    if _worker_health_thread is not None:
        _worker_health_thread.join(timeout=2)


def _validate_worker_schema():
    if not _broker_url().startswith('amqps://'):
        raise RuntimeError('worker requires a valid amqps CLOUDAMQP_URL')
    from src.db.database import engine
    from src.db.workflow_schema import validate_workflow_schema
    with engine.connect() as connection:
        validate_workflow_schema(connection)


@worker_init.connect(weak=False)
def _require_worker_schema(**_kwargs):
    try:
        _validate_worker_schema()
    except Exception as exc:
        # Celery signals catch Exception and continue startup. SystemExit makes
        # this gate actually stop an invalid worker without printing credentials.
        raise SystemExit(f'worker startup validation failed ({type(exc).__name__}); verify broker configuration and reviewed schema') from None
