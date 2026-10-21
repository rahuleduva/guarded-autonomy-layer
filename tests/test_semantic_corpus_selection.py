"""Corpus selection and seeding contracts, with no cloud/provider calls."""
from types import SimpleNamespace

import pytest
from qdrant_client import models

from src.config import PROJECT_ROOT, settings
from src.services import semantic_engine
from src.services import semantic_v3_corpus as corpus

CORPUS = PROJECT_ROOT / 'examples/semantic_v3/corpus.json'


def test_runtime_and_explicit_selection_resolve_from_project_root(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'SEMANTIC_V3_CORPUS_FILE', CORPUS.relative_to(PROJECT_ROOT))
    monkeypatch.chdir(tmp_path)
    selected = corpus.load()
    assert len(selected.cases) == 100
    assert selected.fingerprint == corpus.load(path=CORPUS).fingerprint
    assert len(corpus.load('evaluation').cases) == 32


def test_held_out_file_cannot_be_loaded_as_retrieval():
    with pytest.raises(ValueError, match='incorrect dataset split'):
        corpus.load(path=PROJECT_ROOT / 'examples/semantic_evaluation_v1/cases.json')


def test_development_evaluation_overlap_is_rejected():
    selected = corpus.load(path=CORPUS).model_copy(deep=True)
    development = corpus.Dataset.model_validate_json(
        (PROJECT_ROOT / 'examples/semantic_evaluation_v1/cases.json').read_text())
    selected.cases[0].instruction = development.cases[0].instruction.upper()
    with pytest.raises(ValueError, match='retrieval/evaluation overlap'):
        corpus.validate_evaluation_overlap(selected)


def test_seed_override_writes_only_target_and_preserves_runtime_selection(monkeypatch):
    monkeypatch.setattr(settings, 'OFFLINE_MODE', False)
    monkeypatch.setattr(settings, 'SEMANTIC_V3_ALLOW_UNREVIEWED', True)
    previous_collection = settings.SEMANTIC_V3_COLLECTION
    previous_path = settings.SEMANTIC_V3_CORPUS_FILE
    selected = corpus.load(path=CORPUS)
    writes = []
    stored = {}

    class Client:
        def collection_exists(self, name):
            assert name == 'candidate-test'
            return False

        def create_collection(self, name, **kwargs):
            writes.append(name)
            assert kwargs['vectors_config'].size == 768

        def get_collection(self, name):
            assert name == 'candidate-test'
            return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(
                vectors=models.VectorParams(size=768, distance=models.Distance.COSINE))))

        def create_payload_index(self, name, *args, **kwargs):
            writes.append(name)

        def upsert(self, name, *, points, wait):
            writes.append(name)
            stored.update({p.id: p.payload for p in points})

        def retrieve(self, name, *, ids, **kwargs):
            assert name == 'candidate-test'
            return [SimpleNamespace(id=id, payload=stored[id]) for id in ids]

        def count(self, name, **kwargs):
            assert name == 'candidate-test'
            return SimpleNamespace(count=len(stored))

        def close(self):
            pass

    monkeypatch.setattr(semantic_engine, '_cloud_client', lambda **kwargs: Client())
    monkeypatch.setattr(semantic_engine, '_vector', lambda text: [1.0] + [0.0] * 767)
    monkeypatch.setattr(corpus.time, 'sleep', lambda seconds: None)
    result = corpus.seed(corpus_path=CORPUS, collection='candidate-test')
    assert result['seeded'] == 100
    assert result['corpus_hash'] == selected.fingerprint
    assert result['provisional'] is True
    assert set(writes) == {'candidate-test'}
    assert {p['example_id'] for p in stored.values()} == {c.id for c in selected.cases}
    assert all(p['corpus_hash'] == selected.fingerprint for p in stored.values())
    assert settings.SEMANTIC_V3_COLLECTION == previous_collection
    assert settings.SEMANTIC_V3_CORPUS_FILE == previous_path


def test_seed_cannot_target_baseline_collection(monkeypatch):
    monkeypatch.setattr(settings, 'OFFLINE_MODE', False)
    with pytest.raises(ValueError, match='separate collection'):
        corpus.seed(corpus_path=CORPUS, collection=settings.QDRANT_COLLECTION)


def test_cli_forwards_seed_arguments(monkeypatch, capsys):
    from src import cli
    calls = []
    monkeypatch.setattr('sys.argv', ['cli', 'seed-advisories-v3', '--corpus', str(CORPUS),
                                   '--collection', 'candidate-test'])
    monkeypatch.setattr(corpus, 'seed', lambda **kwargs: calls.append(kwargs) or {'seeded': 100})
    cli.main()
    assert calls == [dict(corpus_path=CORPUS, collection='candidate-test')]
    assert '100' in capsys.readouterr().out
