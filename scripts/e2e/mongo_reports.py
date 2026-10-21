"""Async MongoDB persistence behind the synchronous E2E runner's boundary.

One event loop and AsyncMongoClient live for the entire invocation. No client
is shared across loops. Mongo errors abort reporting rather than silently losing
evidence or creating an unexpected local report directory.
"""
from __future__ import annotations

import asyncio
import json

from pymongo import AsyncMongoClient


class MongoReports:
    def __init__(self, settings, run_id, invocation_id):
        self.database_name = settings.MONGODB_DATABASE
        self.prefix = settings.MONGODB_COLLECTION_PREFIX
        self.run_id, self.invocation_id = run_id, invocation_id
        self.key = f'{run_id}/{invocation_id}'
        self.loop = asyncio.Runner()
        try:
            self.loop.run(self._open(settings.MONGODB_CONNECTION_STRING.get_secret_value()))
        except Exception:
            self.close()
            raise RuntimeError('MongoDB report storage unavailable; no scenarios were started') from None

    async def _open(self, uri):
        self.client = AsyncMongoClient(uri, serverSelectionTimeoutMS=10000,
                                      connectTimeoutMS=10000, socketTimeoutMS=20000,
                                      timeoutMS=30000, appname='guarded-autonomy-layers-e2e')
        await self.client.admin.command('ping')
        db = self.client[self.database_name]
        self.runs = db[self.prefix + '-e2e-runs']
        self.events = db[self.prefix + '-e2e-evidence']
        await self.runs.create_index([('run_id', 1), ('started_at', -1)])
        await self.events.create_index([
            ('run_id', 1), ('invocation_id', 1), ('case_id', 1), ('event_index', 1)])

    @property
    def reference(self):
        return f'mongodb:{self.database_name}/{self.prefix}-e2e-runs/{self.key}'

    def save_report(self, report):
        self.loop.run(self._save_report(report))

    async def _save_report(self, report):
        # Keep the same JSON representation as the previous file reports.
        document = json.loads(json.dumps(report, default=str, allow_nan=False))
        document['_id'] = self.key
        document['counts'] = {status: sum(c['status'] == status for c in report['cases'])
                              for status in ('PASS', 'FAIL', 'BLOCKED', 'SKIPPED', 'NOT RUN')}
        await self.runs.replace_one({'_id': self.key}, document, upsert=True)

    def save_evidence(self, case_id, events):
        self.loop.run(self._save_evidence(case_id, events))
        return f'mongodb:{self.database_name}/{self.prefix}-e2e-evidence/{self.key}/{case_id}'

    async def _save_evidence(self, case_id, events):
        # Separate HTTP events avoid one growing evidence-array document.
        for index, event in enumerate(events):
            key = f'{self.key}/{case_id}/{index}'
            document = {'_id': key, 'run_id': self.run_id,
                        'invocation_id': self.invocation_id, 'case_id': case_id,
                        'event_index': index, 'event': event}
            await self.events.replace_one({'_id': key}, document, upsert=True)

    def close(self):
        try:
            if hasattr(self, 'client'):
                self.loop.run(self.client.close())
        finally:
            self.loop.close()
