"""Run registered live scenarios and publish explicit PASS/FAIL/coverage evidence."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
import traceback
from uuid import uuid4

from scripts.e2e import registry
from scripts.e2e.common import Blocked, Context
from scripts.e2e.fixtures import run_lock
from scripts.e2e.reporting import PRIVATE, ROOT, now, redact, save_report, write_json


def main(default_group=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--profile', choices=['smoke', 'core', 'recovery', 'model'], default='smoke')
    parser.add_argument('--group', action='append', default=[])
    parser.add_argument('--case', action='append', default=[])
    parser.add_argument('--list-cases', action='store_true')
    parser.add_argument('--validate', action='store_true', help='check static route coverage and handler references; no infrastructure calls')
    parser.add_argument('--timeout-seconds', type=float, default=1200)
    args = parser.parse_args()
    rows = registry.cases()
    from scripts.e2e.scenarios import HANDLERS
    if args.validate:
        require_missing = [route for route, ids in registry.inventory().items() if not ids]
        invalid = [r['id'] for r in rows if r.get('handler') and r['handler'] not in HANDLERS]
        invalid += sorted({route for r in rows for route in r['routes']} - set(registry.routes()))
        if require_missing or invalid:
            parser.error('unmapped routes or unknown handlers: ' + str(require_missing + invalid))
        print(json.dumps({'cases': len(rows), 'implemented': sum(bool(r.get('handler')) for r in rows),
                          'routes': len(registry.routes()), 'registry_hash': registry.fingerprint()}))
        return
    groups = args.group or ([default_group] if default_group else [])
    unknown = set(args.case) - {r['id'] for r in rows}
    unknown_groups = set(groups) - {r['group'] for r in rows}
    if unknown or unknown_groups:
        parser.error('unknown cases/groups: ' + str(sorted(unknown | unknown_groups)))
    selected = {r['id'] for r in rows if (r['id'] in args.case if args.case else
                r['group'] in groups if groups else args.profile in r['profiles'])}
    # Include explicit dependencies; recursive expansion is deterministic.
    for _ in rows:
        previous = set(selected)
        selected |= {dep for r in rows if r['id'] in selected for dep in r.get('dependencies', [])}
        if selected == previous:
            break
    if args.list_cases:
        print(json.dumps([{'id': r['id'], 'group': r['group'], 'title': r['title'],
                           'implemented': bool(r.get('handler')), 'expected': r['expected']}
                          for r in rows if r['id'] in selected], indent=2))
        return
    if not args.manifest:
        parser.error('--manifest is required to execute live scenarios')
    if not 1 <= args.timeout_seconds <= 7200:
        parser.error('timeout must be between 1 and 7200 seconds')
    manifest = json.loads(args.manifest.read_text())
    if manifest['registry_hash'] != registry.fingerprint():
        parser.error('scenario registry changed; initialize a fresh run')
    # Guard local infrastructure/configuration before importing scenario DB tools.
    from scripts.e2e.evidence import local_snapshot, database_snapshot
    if local_snapshot() != manifest['configuration']:
        parser.error('local configuration/code/data changed; initialize a fresh run')
    if database_snapshot()['policies'] != manifest['database']['policies']:
        parser.error('policy snapshot changed; initialize a fresh run')
    with run_lock(manifest['run_id']):
        from scripts.e2e.initialize import verify_callers
        verify_callers(manifest)
        invocation = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '_' + uuid4().hex[:6]
        category = 'comprehensive' if args.profile == 'core' and not args.case and not groups else 'narrow'
        directory = ROOT / 'reports/e2e' / category / manifest['run_id'] / invocation
        report = {'schema_version': 1, 'run_id': manifest['run_id'], 'invocation_id': invocation,
            'started_at': now(), 'registry_hash': registry.fingerprint(), 'manifest': manifest,
            'selected': sorted(selected), 'controlled_faults': False,
            'cases': [{'id': r['id'], 'title': r['title'], 'expected': r['expected'],
                       'status': 'NOT RUN', 'reason': 'not selected'} for r in rows]}
        context = Context(manifest, directory, args.timeout_seconds)
        from src.config import settings
        if settings.MONGODB_CONNECTION_STRING.get_secret_value().strip():
            from scripts.e2e.mongo_reports import MongoReports
            try:
                context.storage = MongoReports(settings, manifest['run_id'], invocation)
            except Exception:
                context.close()
                raise
        completed_path = PRIVATE / manifest['run_id'] / 'completed.json'
        completed = json.loads(completed_path.read_text()) if completed_path.exists() else {}
        by_id = {r['id']: r for r in report['cases']}
        try:
            save_report(directory, report, context.secrets, storage=context.storage)
            for case in rows:
                if case['id'] not in selected:
                    continue
                row = by_id[case['id']]
                row['reason'] = 'selected; interrupted before completion'
                context.begin(case['id'])
                started = time.monotonic()
                if case['id'] in completed:
                    row.update(status='SKIPPED', reason='already passed in this run; use a fresh run to repeat',
                               prior_evidence=completed[case['id']])
                elif not case.get('handler'):
                    row.update(status='NOT RUN', reason=case.get('limitation', 'isolated fixture not implemented'))
                elif any(by_id[dep]['status'] not in {'PASS', 'SKIPPED'} for dep in case.get('dependencies', [])):
                    row.update(status='BLOCKED', reason='scenario prerequisite did not pass')
                else:
                    try:
                        actual = HANDLERS[case['handler']](context, case)
                        row.update(status='PASS', actual=actual, reason='all assertions passed')
                    except Blocked as exc:
                        row.update(status='BLOCKED', reason=str(exc))
                    except Exception as exc:
                        row.update(status='FAIL', reason=str(exc), error_type=type(exc).__name__,
                                   traceback=traceback.format_exc())
                    finally:
                        row['evidence'] = context.evidence()
                    if row['status'] == 'PASS':
                        completed[case['id']] = {'invocation_id': invocation, 'evidence': (
                            row['evidence'] if context.storage is not None else str(directory / row['evidence']))}
                        write_json(completed_path, completed, private=True)
                row['seconds'] = time.monotonic() - started
                save_report(directory, report, context.secrets, storage=context.storage)
                print(case['id'] + ': ' + row['status'], flush=True)
        finally:
            report['finished_at'] = now()
            report['leftovers'] = {'resource_prefix': manifest['resource_prefix'],
                'note': 'run-owned principals, resources and pending reviews are retained for audit; no shared cleanup'}
            try:
                save_report(directory, report, context.secrets, storage=context.storage)
            finally:
                context.close()
        print('Report: ' + (context.storage.reference if context.storage is not None else str(directory / 'REPORT.md')))
        statuses = [r['status'] for r in report['cases'] if r['id'] in selected]
        # Incomplete selected coverage cannot yield a successful exit code.
        raise SystemExit(1 if 'FAIL' in statuses else 2 if any(s in {'BLOCKED', 'NOT RUN'} for s in statuses) else 0)


if __name__ == '__main__':
    main()
