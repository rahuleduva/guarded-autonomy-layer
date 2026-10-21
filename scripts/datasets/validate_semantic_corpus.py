"""Validate the authoritative retrieval corpus and its evaluation separation."""
import argparse
from collections import Counter, defaultdict
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path

from src.services.semantic_v3_corpus import Dataset

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'examples/semantic_v3'


def normalize(text):
    return ' '.join(text.casefold().split())


def validate(directory=DATA):
    directory = Path(directory)
    candidate = Dataset.model_validate_json((directory / 'corpus.json').read_text())
    changes = json.loads((directory / 'changes.json').read_text())
    assert candidate.fingerprint == changes['current_fingerprint'], 'Update revision history after corpus changes.'
    assert candidate.split == 'retrieval'
    assert candidate.review_status == 'pending_human_review'
    new = {case.id: case for case in candidate.cases}
    assert len(candidate.cases) == changes['expected_count']
    assert len(set(changes['added_ids'])) == len(changes['added_ids'])
    assert set(changes['added_ids']) <= set(new)
    assert all(c.review_status == 'pending_human_review' for c in candidate.cases)
    texts = {normalize(c.instruction) for c in candidate.cases}
    assert len(texts) == len(candidate.cases)
    evaluation = []
    for path in ('examples/semantic_v3/evaluation.json', 'examples/semantic_evaluation_v1/cases.json'):
        dataset = Dataset.model_validate_json((ROOT / path).read_text())
        assert dataset.split == 'held_out'
        assert not (set(new) & {c.id for c in dataset.cases})
        assert not (texts & {normalize(c.instruction) for c in dataset.cases})
        evaluation.extend((path, c) for c in dataset.cases)
    risks = defaultdict(Counter)
    # Global label quotas differ from per-finding assessments; expose both.
    risk_label_buckets = defaultdict(Counter)
    for c in candidate.cases:
        for risk in c.risk_aspects:
            risks[risk.category.value][risk.assessment] += 1
            risk_label_buckets[risk.category.value][c.label] += 1
    assert len(risks) == 7
    assert all(all(v[s] for s in ('applies', 'does_not_apply', 'uncertain')) for v in risks.values())
    assert all(all(v[s] for s in ('risky', 'benign', 'uncertain')) for v in risk_label_buckets.values())
    nearest = sorted((
        (SequenceMatcher(None, normalize(c.instruction), normalize(e.instruction)).ratio(), c.id, path, e.id)
        for c in candidate.cases if c.id in changes['added_ids']
        for path, e in evaluation
    ), reverse=True)[:8]
    return {
        'version': 'semantic-corpus-validation-v1',
        'status': 'validated_locally_cloud_state_not_checked',
        'corpus_fingerprint': candidate.fingerprint,
        'count': len(candidate.cases),
        'added': len(changes['added_ids']),
        'labels': dict(sorted(Counter(c.label for c in candidate.cases).items())),
        'action_counts': dict(sorted(Counter(a.action_class.value for c in candidate.cases for a in c.actions).items())),
        'risk_assessments': {k: dict(sorted(v.items())) for k, v in sorted(risks.items())},
        'risk_query_global_label_buckets': {k: dict(sorted(v.items())) for k, v in sorted(risk_label_buckets.items())},
        'nearest_added_to_evaluation': [dict(character_similarity=round(s, 4), candidate_id=c, evaluation_file=p, evaluation_id=e) for s, c, p, e in nearest],
        'overlap_note': 'No normalized exact overlap. Character similarity is a review aid, not semantic independence. The development suite informed this revision.',
        'sha256': {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in ('corpus.json', 'changes.json', 'README.md', 'evaluation.json', 'taxonomy.json', 'dataset.schema.json')},
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-manifest', action='store_true')
    parser.add_argument('--directory', type=Path, default=DATA)
    args = parser.parse_args()
    result = validate(args.directory)
    path = args.directory / 'manifest.json'
    if args.write_manifest:
        path.write_text(json.dumps(result, indent=2) + '\n')
    else:
        assert json.loads(path.read_text()) == result, 'Manifest differs; review changes before regeneration.'
    print(json.dumps(result, indent=2))
