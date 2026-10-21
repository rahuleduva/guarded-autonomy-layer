"""Validate the fresh evaluation data locally; makes no provider/database calls.

Run from the repository root with PYTHONPATH=. Use --write-manifest after an
intentional dataset revision; the default verifies the committed manifest.
"""
import argparse
from collections import Counter, defaultdict
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path

from src.services.semantic_v3_corpus import Dataset

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'examples/semantic_evaluation_v1'


def normalized(value):
    return ' '.join(value.casefold().split())


def validate():
    dataset = Dataset.model_validate_json((DATA / 'cases.json').read_text())
    assertions = json.loads((DATA / 'parser_expectations.json').read_text())
    assert dataset.split == 'held_out'
    assert dataset.review_status == 'pending_human_review'
    assert len(dataset.cases) == 56
    assert set(assertions['cases']) == {c.id for c in dataset.cases}
    existing = []
    for filename in ('corpus.json', 'evaluation.json'):
        rows = json.loads((ROOT / 'examples/semantic_v3' / filename).read_text())['cases']
        existing.extend((filename, row['id'], row['instruction']) for row in rows)
    assert not ({c.id for c in dataset.cases} & {row[1] for row in existing})
    instructions = [normalized(c.instruction) for c in dataset.cases]
    assert len(set(instructions)) == len(instructions)
    assert not (set(instructions) & {normalized(row[2]) for row in existing})
    coverage = defaultdict(Counter)
    for case in dataset.cases:
        check = assertions['cases'][case.id]
        assert case.review_status == 'pending_human_review'
        assert check['parser_disposition'] in {'parsed', 'split_required', 'clarification_or_rejection'}
        assert check['expected_environment'] in {'production', 'development', 'staging'}
        assert check['policy_outcome'] == 'not_asserted_without_policy_and_autonomy_fixture'
        assert len(check['expected_actions']) == len(case.actions)
        assert (check['parser_disposition'] == 'split_required') == (len(case.actions) > 1)
        if check['parser_disposition'] == 'clarification_or_rejection':
            assert case.missing_information
        for expected, action in zip(check['expected_actions'], case.actions):
            assert expected['action_class'] == action.action_class.value
            assert expected['target_resource'] in case.instruction
            params = expected['parameters']
            if 'destination' in params:
                assert params['destination'] in case.instruction
            if 'content' in params:
                assert params['content'] in case.instruction
                if params['content'] == '':
                    assert 'empty' in case.instruction
            if check['parser_disposition'] == 'parsed':
                if action.action_class.value == 'move_file':
                    assert 'destination' in params
                if action.action_class.value in {'create_file', 'update_file'}:
                    assert 'content' in params
        for finding in case.risk_aspects:
            coverage[finding.category.value][finding.assessment] += 1
    assert len(coverage) == 7
    assert all(all(counts[status] >= 1 for status in ('applies', 'does_not_apply', 'uncertain'))
               for counts in coverage.values())
    closest = sorted((
        (SequenceMatcher(None, normalized(case.instruction), normalized(text)).ratio(), case.id, filename, old_id)
        for case in dataset.cases for filename, old_id, text in existing
    ), reverse=True)[:8]
    files = ['cases.json', 'parser_expectations.json', 'README.md']
    return {
        'version': 'semantic-evaluation-v1-draft-2',
        'intended_use': 'development_evaluation_not_independent_final_holdout',
        'case_count': len(dataset.cases),
        'labels': dict(sorted(Counter(c.label for c in dataset.cases).items())),
        'action_counts': dict(sorted(Counter(a.action_class.value for c in dataset.cases for a in c.actions).items())),
        'parser_dispositions': dict(sorted(Counter(c['parser_disposition'] for c in assertions['cases'].values()).items())),
        'risk_assessments': {k: dict(sorted(v.items())) for k, v in sorted(coverage.items())},
        'review_status': dataset.review_status,
        'validation': 'Pydantic dataset invariants, parser expectation consistency, exact normalized duplicate checks against existing v3 retrieval/evaluation files. Not independent label approval.',
        'closest_existing_text_pairs': [dict(character_similarity=round(score, 4), case_id=id, existing_file=file, existing_id=old) for score, id, file, old in closest],
        'similarity_limit': 'Character similarity identifies review candidates only; it cannot establish semantic independence. Shared scenario mechanisms are intentional.',
        'sha256': {name: hashlib.sha256((DATA / name).read_bytes()).hexdigest() for name in files},
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-manifest', action='store_true')
    args = parser.parse_args()
    result = validate()
    manifest = DATA / 'manifest.json'
    if args.write_manifest:
        manifest.write_text(json.dumps(result, indent=2) + '\n')
    else:
        assert json.loads(manifest.read_text()) == result, 'Manifest differs; inspect dataset changes before regenerating.'
    print(json.dumps(result, indent=2))
