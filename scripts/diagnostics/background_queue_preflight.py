"""Check CloudAMQP TLS connectivity and declare this project's durable queue."""
import json
from urllib.parse import urlsplit

from src.config import settings
from src.workers.celery_app import celery_app, workflow_queue


def main():
    broker = settings.CLOUDAMQP_URL.get_secret_value().strip()
    if not broker.startswith('amqps://'):
        raise SystemExit('CLOUDAMQP_URL must be configured with amqps://')
    parsed = urlsplit(broker)
    if not parsed.hostname:
        raise SystemExit('CLOUDAMQP_URL has no hostname')
    connection = celery_app.connection_for_write(connect_timeout=settings.CLOUDAMQP_CONNECTION_TIMEOUT_SECONDS)
    try:
        connection.ensure_connection(max_retries=0, timeout=settings.CLOUDAMQP_CONNECTION_TIMEOUT_SECONDS)
        channel = connection.channel()
        try:
            workflow_queue.bind(channel).declare()
        finally:
            channel.close()
    finally:
        connection.release()
    print(json.dumps({'cloudamqp_tls_connection': 'PASS', 'queue_declaration': 'PASS',
                      'broker_host': parsed.hostname, 'queue': settings.WORKFLOW_QUEUE_NAME,
                      'credentials_printed': False}, indent=2))


if __name__ == '__main__':
    main()
