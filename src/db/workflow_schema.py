"""Shared startup gate for API, dispatcher, and worker schema compatibility."""
from sqlalchemy import inspect


def validate_workflow_schema(connection):
    if connection.dialect.name != 'postgresql':
        raise RuntimeError('workflow processing requires PostgreSQL')
    inspector = inspect(connection)
    required = {
        'workflow_operations': {'caller_principal_id', 'execution_mode', 'input_envelope',
            'generation', 'run_count', 'automatic_retry_count', 'next_run_at',
            'queue_deadline', 'run_deadline', 'heartbeat_at'},
        'workflow_dispatches': {'operation_id', 'generation', 'publisher_owner',
            'publisher_lease_until', 'available_at', 'published_at'},
        'request_attempt_events': {'operation_id'},
    }
    for table, columns in required.items():
        if not inspector.has_table(table) or not columns <= {
                item['name'] for item in inspector.get_columns(table)}:
            raise RuntimeError(f'{table} schema is outdated; apply the reviewed workflow migration')
    for table, name, values in (
        ('workflow_operations', 'ck_workflow_status', ('QUEUED', 'RETRY_WAIT')),
        ('workflow_operations', 'ck_workflow_background_schedule', ()),
        ('workflow_dispatches', 'ck_workflow_dispatch_published', ()),
    ):
        constraint = next((item for item in inspector.get_check_constraints(table)
                           if item['name'] == name), None)
        if constraint is None or any("'" + value + "'" not in constraint['sqltext'].upper() for value in values):
            raise RuntimeError(f'{name} schema is outdated; include named CHECK changes in the reviewed migration')
    if 'uq_request_attempt_sequence' not in {
            item['name'] for item in inspector.get_unique_constraints('request_attempt_events')}:
        raise RuntimeError('request-attempt uniqueness schema is missing; apply the reviewed workflow migration')
