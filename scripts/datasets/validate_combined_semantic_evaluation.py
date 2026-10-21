"""Validate the 88-case evaluation merge locally, without provider calls."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

from src.services.semantic_evaluation import load_suite
from src.services.semantic_v3_corpus import Dataset

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'examples/semantic_evaluation_combined_v1'
SOURCES = {
    'legacy-held-out': ROOT / 'examples/semantic_v3/evaluation.json',
    'semantic-development-v1': ROOT / 'examples/semantic_evaluation_v1/cases.json',
}


def normalize(text):
    return ' '.join(text.casefold().split())


def validate():
    dataset, expected = load_suite('semantic-combined-v1')
    originals = {name: Dataset.model_validate_json(path.read_text()) for name, path in SOURCES.items()}
    original_cases = [c for source in originals.values() for c in source.cases]
    assert len(dataset.cases) == 88
    assert len(original_cases) == len({c.id for c in original_cases})
    assert dataset.cases == original_cases, 'Merged case records differ from sources or their order.'
    normalized = {normalize(c.instruction) for c in dataset.cases}
    assert len(normalized) == 88, 'Normalized duplicate instruction.'
    old_expectations = json.loads((ROOT / 'examples/semantic_evaluation_v1/parser_expectations.json').read_text())['cases']
    for id, original in old_expectations.items():
        assert all(expected[id][key] == value for key, value in original.items())
    source_ids = {name: {c.id for c in source.cases} for name, source in originals.items()}
    coverage = defaultdict(Counter)
    for case in dataset.cases:
        annotation = expected[case.id]
        assert case.id in source_ids[annotation['source_suite']]
        assert annotation['policy_outcome'] == 'not_asserted_without_policy_and_autonomy_fixture'
        assert annotation['expected_environment'] in {'production', 'staging', 'development'}
        for action in annotation['expected_actions']:
            assert action['target_resource'] in case.instruction
            for key in ('destination', 'content'):
                if key in action['parameters']:
                    assert action['parameters'][key] in case.instruction
            if annotation['parser_disposition'] == 'parsed':
                if action['action_class'] == 'move_file':
                    assert 'destination' in action['parameters']
                if action['action_class'] in {'create_file', 'update_file'}:
                    assert 'content' in action['parameters']
        for risk in case.risk_aspects:
            coverage[risk.category.value][risk.assessment] += 1
    omitted = {id for id, a in expected.items() if not a.get('action_classes_required', True)}
    assert omitted == {'eval-deletion-03', 'eval-bypass-03'}
    for id in omitted:
        assert expected[id]['parser_disposition'] == 'clarification_or_rejection'
        assert expected[id]['compatibility_note']
    for path in ('examples/semantic_v3/corpus.json',):
        corpus = Dataset.model_validate_json((ROOT / path).read_text())
        assert not (normalized & {normalize(c.instruction) for c in corpus.cases})
        assert not ({c.id for c in dataset.cases} & {c.id for c in corpus.cases})
    files = ('cases.json', 'parser_expectations.json', 'compatibility_review.json', 'README.md')
    return {
        'version': 'semantic-combined-evaluation-v1',
        'case_count': len(dataset.cases),
        'source_counts': {name: len(data.cases) for name, data in originals.items()},
        'source_fingerprints': {name: data.fingerprint for name, data in originals.items()},
        'dataset_fingerprint': dataset.fingerprint,
        'labels': dict(sorted(Counter(c.label for c in dataset.cases).items())),
        'parser_dispositions': dict(sorted(Counter(a['parser_disposition'] for a in expected.values()).items())),
        'strict_action_class_cases': len(dataset.cases) - len(omitted),
        'ambiguous_action_class_ids': sorted(omitted),
        'risk_assessments': {k: dict(sorted(v.items())) for k, v in sorted(coverage.items())},
        'records_preserved_exactly': True,
        'exact_cross_corpus_overlap': False,
        'semantic_independence_claimed': False,
        'review_status': dataset.review_status,
        'sha256': {name: hashlib.sha256((DATA / name).read_bytes()).hexdigest() for name in files},
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-manifest', action='store_true')
    args = parser.parse_args()
    result = validate()
    path = DATA / 'manifest.json'
    if args.write_manifest:
        path.write_text(json.dumps(result, indent=2) + '\n')
    else:
        assert json.loads(path.read_text()) == result, 'Manifest differs; inspect changes before regenerating.'
    print(json.dumps(result, indent=2))
