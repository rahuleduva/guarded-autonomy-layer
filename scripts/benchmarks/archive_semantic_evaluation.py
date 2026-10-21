"""Run or archive a semantic evaluation in the existing MongoDB run history.

This preserves per-case model/retrieval evidence separately from the run summary.
Archive mode makes no model calls. Suite mode checkpoints each completed case.
Neither mode seeds corpus data. Completed uploads are read-back verified.
"""
import argparse
import json
from pathlib import Path

from scripts.e2e.initialize import secret_values
from scripts.e2e.mongo_reports import MongoReports
from scripts.e2e.reporting import now, redact, run_name
from src.config import settings


def case_status(row):
    """Unavailable assessments must not be reported as successful detection."""
    if 'unavailable' in row['status']:
        return 'BLOCKED'
    parser = row.get('parser_expectations', {})
    if (parser.get('expected_disposition') == 'clarification_or_rejection'
            and parser.get('handling_match') is True
            and parser.get('actual_executable_action_returned') is False):
        return 'PASS'
    if row['status'] == 'unsupported_action':
        return 'FAIL'
    if row['status'] not in {'assessed', 'needs_clarification', 'split_required'}:
        return 'BLOCKED'
    checks = [parser.get('handling_match'), row.get('action_match'),
              parser.get('parsed_action_details_match')]
    if row['status'] in {'assessed', 'split_required'}:
        checks.append(parser.get('all_targets_match'))
    if row['status'] == 'assessed':
        checks += [row.get('flag_match'), *row.get('finding_matches', {}).values()]
    if not any(value is not None for value in checks):
        return 'BLOCKED'
    return 'FAIL' if any(value is False for value in checks) else 'PASS'


def archive(path, run_id, invocation_id, snapshot_path=None):
    source = json.loads(path.read_text())
    return archive_report(source, run_id, invocation_id, snapshot_path)


def archive_report(source, run_id, invocation_id, snapshot_path=None):
    """Archive an in-memory report without creating a local result file."""
    source = json.loads(json.dumps(source))
    if source.get('version') != 'semantic-evaluation-v2':
        raise ValueError('expected a semantic-evaluation-v2 report')
    if source.get('answers_supplied_to_models') is not False:
        raise ValueError('report must confirm evaluation answers were withheld')
    secrets = secret_values()
    configuration = None
    if snapshot_path is not None:
        configuration = redact(json.loads(snapshot_path.read_text())[
            'snapshot']['configuration']['evidence'], secrets)
    storage = MongoReports(settings, run_id, invocation_id)
    try:
        rows = []
        for row in source['cases']:
            clean = redact(row, secrets)
            reference = storage.save_evidence(row['id'], [clean])
            rows.append({'id': row['id'], 'status': case_status(row),
                         'semantic_status': row['status'], 'evidence': reference})
        report = redact({
            'run_id': run_id, 'invocation_id': invocation_id,
            'started_at': source['created_at'], 'finished_at': now(),
            'category': ('comprehensive' if source['counts']['total'] == source['dataset_total']
                         else 'narrow'), 'profile': 'semantic-evaluation',
            'scope': 'service-level', 'test_type': 'risk-quality',
            'case_status_version': 2,
            'summary': {k: v for k, v in source.items() if k != 'cases'},
            'cases': rows,
            'status': 'INCOMPLETE' if source['unattempted_case_ids'] else 'COMPLETED',
            'scoring_note': 'PASS requires available annotated checks to match; '
                            'see original evidence for denominators and partial labels.',
        }, secrets)
        if configuration is not None:
            report['configuration_snapshot'] = configuration
        storage.save_report(report)

        async def verify():
            saved = await storage.runs.find_one({'_id': storage.key})
            if not saved or any(saved.get(key) != value for key, value in report.items()):
                raise RuntimeError('MongoDB report read-back mismatch')
            for row in source['cases']:
                key = f"{storage.key}/{row['id']}/0"
                saved = await storage.events.find_one({'_id': key})
                if not saved or saved['event'] != redact(row, secrets):
                    raise RuntimeError('MongoDB case read-back mismatch: ' + row['id'])

        storage.loop.run(verify())
        print(json.dumps({'reference': storage.reference, 'verified_cases': len(rows),
                          'unattempted': len(source['unattempted_case_ids'])}))
    finally:
        storage.close()


def load_resume(storage, source_invocation, suite, configuration, code_change_note=None):
    """Read completed checkpoints; never mutate the interrupted invocation."""
    from src.services.semantic_evaluation import load_suite
    source_key = f'{storage.run_id}/{source_invocation}'
    if source_key == storage.key:
        raise ValueError('resume requires a fresh destination invocation ID')

    async def read():
        source = await storage.runs.find_one({'_id': source_key})
        events = await storage.events.find({
            'run_id': storage.run_id, 'invocation_id': source_invocation,
            'event_index': 0, 'case_id': {'$ne': 'unfinished-case'},
        }).to_list(length=None)
        return source, events

    source, events = storage.loop.run(read())
    if not source or source.get('status') not in {'INTERRUPTED', 'INCOMPLETE'}:
        raise ValueError('resume source must be an interrupted or incomplete invocation')
    if source.get('suite', source.get('summary', {}).get('suite')) != suite:
        raise ValueError('resume source suite differs')
    previous = source.get('configuration_snapshot')
    if not previous or not configuration:
        raise ValueError('resume requires source and current configuration snapshots')
    differences = {key for key in set(previous) | set(configuration)
                   if previous.get(key) != configuration.get(key)}
    if differences - {'code_hash'}:
        raise ValueError('resume configuration/data changed: ' + ', '.join(sorted(differences)))
    if 'code_hash' in differences and not (code_change_note or '').strip():
        raise ValueError('code changed; inspect changes and supply --resume-code-change-note')
    dataset, _ = load_suite(suite)
    by_id = {case.id: case for case in dataset.cases}
    records = {event['case_id']: event['event'] for event in events}
    listed = [case['id'] for case in source['cases']]
    if len(set(listed)) != len(listed) or set(records) != set(listed):
        raise ValueError('source checkpoint rows and evidence disagree')
    rows = []
    for case_id in listed:
        row, case = records[case_id], by_id.get(case_id)
        if (case is None or row.get('id') != case_id
                or row.get('expected_label') != case.label
                or row.get('expected_actions') != [a.action_class.value for a in case.actions]
                or row.get('expected_handling') != case.expected_request_handling):
            raise ValueError('source case annotations differ: ' + case_id)
        rows.append(row)
    return rows, {
        'source_run': source_key, 'source_status': source['status'],
        'reused_case_ids': listed, 'remaining_case_ids': [
            case.id for case in dataset.cases if case.id not in records],
        'source_code_hash': previous['code_hash'],
        'continuation_code_hash': configuration['code_hash'],
        'code_change_note': code_change_note,
        'note': 'Completed rows reused verbatim; interrupted in-flight calls may repeat.',
    }


def run_suite(suite, run_id, invocation_id, snapshot_path, resume_from=None,
              resume_code_change_note=None, resume_check=False, case_id=None):
    from src.services.semantic_evaluation import run
    from src.services import monitoring

    secrets = secret_values()
    report = {'run_id': run_id, 'invocation_id': invocation_id,
              'started_at': now(), 'status': 'RUNNING', 'suite': suite,
              'category': 'narrow' if case_id else 'comprehensive', 'profile': 'semantic-evaluation',
              'scope': 'service-level', 'test_type': 'risk-quality', 'cases': []}
    report['case_status_version'] = 2
    if snapshot_path is not None:
        snapshot = json.loads(snapshot_path.read_text())
        report['configuration_snapshot'] = redact(
            snapshot['snapshot']['configuration']['evidence'], secrets)
        from scripts.e2e.evidence import local_snapshot
        if report['configuration_snapshot'] != redact(local_snapshot(), secrets):
            raise ValueError('current configuration/code differs from snapshot; refresh setup plan')

    storage = MongoReports(settings, run_id, invocation_id)
    try:
        existing = storage.loop.run(storage.runs.find_one({'_id': storage.key}))
        if existing is not None:
            raise ValueError('destination invocation already exists; choose a fresh ID')
        completed = []
        if resume_from:
            completed, report['continuation'] = load_resume(
                storage, resume_from, suite, report.get('configuration_snapshot'),
                resume_code_change_note)
        if resume_check:
            print(json.dumps(report['continuation']))
            storage.close()
            return
    except BaseException:
        storage.close()
        raise
    inherited_ids = {row['id'] for row in completed}
    traces = {}
    attempt, token = monitoring.begin()
    attempt.route = 'semantic-evaluation'

    def checkpoint(row):
        # Preserve full existing attempt traces, including correction feedback.
        # These are test evidence; no application database event rows are added.
        traces[row['id']] = (row.get('attempt_events', []) if row['id'] in inherited_ids
                             else list(attempt.events))
        row['attempt_events'] = traces[row['id']]
        reference = storage.save_evidence(row['id'], [redact(row, secrets)])
        report['cases'].append({'id': row['id'], 'status': case_status(row),
                                'semantic_status': row['status'], 'evidence': reference})
        storage.save_report(report)
        attempt.events.clear()
        reused = ' reused' if row['id'] in inherited_ids else ''
        print(f"{len(report['cases'])}: {row['id']} {row['status']}{reused}", flush=True)

    try:
        storage.save_report(report)
        result = run(suite=suite, case_id=case_id, on_case=checkpoint, completed_rows=completed)
        if 'continuation' in report:
            result['continuation'] = report['continuation']
        for row in result['cases']:
            row['attempt_events'] = traces[row['id']]
        report.update(status='FINALIZING', finished_at=now(),
                      summary={k: v for k, v in result.items() if k != 'cases'})
        storage.save_report(report)
    except BaseException as exc:
        report.update(status='INTERRUPTED', error_type=type(exc).__name__, finished_at=now())
        if attempt.events:
            report['unfinished_attempt_evidence'] = storage.save_evidence(
                'unfinished-case', [redact({'attempt_events': attempt.events}, secrets)])
        storage.save_report(report)
        raise
    finally:
        monitoring.end(token)
        storage.close()
    archive_report(result, run_id, invocation_id, snapshot_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--input', type=Path)
    mode.add_argument('--suite', choices=['semantic-combined-v1', 'semantic-development-v1'])
    parser.add_argument('--run-id', type=run_name, required=True)
    parser.add_argument('--invocation-id', type=run_name, required=True)
    parser.add_argument('--snapshot', type=Path,
                        help='Initializer setup-plan.json captured before this evaluation')
    parser.add_argument('--resume-from', type=run_name,
                        help='Interrupted source invocation in the same run; destination must be new')
    parser.add_argument('--resume-code-change-note',
                        help='Required explanation if reviewed code changes occurred since interruption')
    parser.add_argument('--resume-check', action='store_true',
                        help='Verify source evidence/configuration without starting model calls')
    parser.add_argument('--case-id', help='Repeat one selected case with a fresh invocation ID')
    args = parser.parse_args()
    if (args.resume_from or args.resume_check or args.resume_code_change_note) and not args.suite:
        parser.error('resume options require --suite')
    if args.resume_check and not args.resume_from:
        parser.error('--resume-check requires --resume-from')
    if args.case_id and (not args.suite or args.resume_from):
        parser.error('--case-id requires --suite and cannot be combined with --resume-from')
    if args.suite:
        run_suite(args.suite, args.run_id, args.invocation_id, args.snapshot,
                  args.resume_from, args.resume_code_change_note, args.resume_check, args.case_id)
    else:
        archive(args.input, args.run_id, args.invocation_id, args.snapshot)


if __name__ == '__main__':
    main()
