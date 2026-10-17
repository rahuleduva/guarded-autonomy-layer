"""Detect checked-out application transactions before external work."""
from threading import local
from weakref import WeakSet
from sqlalchemy import event
from sqlalchemy.orm import Session

_local = local()


def _sessions():
    if not hasattr(_local, 'sessions'):
        _local.sessions = WeakSet()
    return _local.sessions


@event.listens_for(Session, 'after_begin')
def _began(session, transaction, connection):
    _sessions().add(session)


@event.listens_for(Session, 'after_transaction_end')
def _ended(session, transaction):
    if transaction.parent is None:
        _sessions().discard(session)


def assert_no_transaction():
    if any(session.in_transaction() for session in list(_sessions())):
        raise RuntimeError('slow work attempted with an open database transaction')
