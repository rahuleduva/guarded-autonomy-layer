"""Strict draft-v3 data loading, versioning, and explicit cloud seeding."""
import hashlib
import json
import time
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5
from pydantic import Field, model_validator
from src.config import PROJECT_ROOT, settings
from src.models.semantic import ActionClass, FAMILIES, RiskCategory, Strict
from src.services import embeddings

DATA_ROOT = PROJECT_ROOT / 'examples/semantic_v3'
TEXT_FORMAT = 'instruction-v1'


def validate_taxonomy(value):
    if not isinstance(value, dict):
        raise ValueError('maintained risk taxonomy must be an object')
    definitions = value.get('risk_categories')
    if (not isinstance(value.get('version'), str) or not value['version'].strip()
            or not isinstance(definitions, dict)
            or set(definitions) != {category.value for category in RiskCategory}
            or any(not isinstance(text, str) or not text.strip() for text in definitions.values())):
        raise ValueError('maintained risk taxonomy is incomplete or invalid')
    return {'version': value['version'], 'risk_categories': definitions}


def load_taxonomy():
    """Load the maintained risk definitions, without evaluation cases or cloud I/O."""
    from src.services.llm_errors import LLMConfigurationError
    try:
        return validate_taxonomy(json.loads((DATA_ROOT / 'taxonomy.json').read_text()))
    except (OSError, ValueError) as exc:
        raise LLMConfigurationError('maintained risk taxonomy is unavailable or invalid') from exc


class CorpusAction(Strict):
    action_class: ActionClass
    action_family: Literal['read', 'create', 'update', 'move', 'delete']


class CorpusRisk(Strict):
    category: RiskCategory
    assessment: Literal['applies', 'does_not_apply', 'uncertain']
    instruction_excerpt: str = Field(min_length=1)


class CorpusCase(Strict):
    id: str = Field(min_length=1)
    scenario_group: str
    instruction: str = Field(min_length=1, max_length=16384)
    actions: list[CorpusAction] = Field(min_length=1)
    scope: Literal['single_resource', 'recursive_tree', 'wildcard', 'unknown', 'multiple_actions']
    risk_aspects: list[CorpusRisk] = Field(min_length=1)
    label: Literal['risky', 'benign', 'uncertain']
    expected_semantic_flag: bool
    expected_request_handling: Literal['single_action', 'split_required']
    reason: str
    missing_information: list[str]
    review_status: Literal['pending_human_review', 'approved', 'rejected']

    @model_validator(mode='after')
    def consistent(self):
        for action in self.actions:
            if FAMILIES[action.action_class.value] != action.action_family:
                raise ValueError('action family disagrees with action class')
        categories = [r.category for r in self.risk_aspects]
        if len(set(categories)) != len(categories):
            raise ValueError('duplicate risk category')
        if any(r.instruction_excerpt not in self.instruction for r in self.risk_aspects):
            raise ValueError('risk excerpt is not present in instruction')
        statuses = {r.assessment for r in self.risk_aspects}
        expected = 'risky' if 'applies' in statuses else ('uncertain' if 'uncertain' in statuses else 'benign')
        if self.label != expected or self.expected_semantic_flag != (expected != 'benign'):
            raise ValueError('label, flag, and risk annotations disagree')
        if 'uncertain' in statuses and not self.missing_information:
            raise ValueError('uncertainty requires missing information')
        handling = 'split_required' if len(self.actions) > 1 else 'single_action'
        if self.expected_request_handling != handling:
            raise ValueError('compound handling disagrees with action count')
        return self


class Dataset(Strict):
    schema_version: Literal["semantic-dataset-v3-draft-1"]
    origin: str
    review_status: Literal['pending_human_review', 'approved', 'rejected']
    created_on: str
    limitations: list[str]
    split: Literal['retrieval', 'held_out']
    cases: list[CorpusCase] = Field(min_length=1)

    @model_validator(mode='after')
    def unique(self):
        if len({r.id for r in self.cases}) != len(self.cases):
            raise ValueError('duplicate example ID')
        if len({r.instruction for r in self.cases}) != len(self.cases):
            raise ValueError('duplicate instruction')
        return self

    @property
    def fingerprint(self):
        value = json.dumps(self.model_dump(mode='json'), sort_keys=True, separators=(',', ':'), allow_nan=False)
        return hashlib.sha256(value.encode()).hexdigest()


def load(name='corpus', *, path: Path | str | None = None) -> Dataset:
    if name not in {'corpus', 'evaluation'}:
        raise ValueError('unknown dataset')
    if path is not None and name != 'corpus':
        raise ValueError('path override is only supported for the retrieval corpus')
    selected = Path(path if path is not None else settings.SEMANTIC_V3_CORPUS_FILE) if name == 'corpus' else DATA_ROOT / 'evaluation.json'
    if not selected.is_absolute():
        selected = PROJECT_ROOT / selected
    result = Dataset.model_validate_json(selected.read_text())
    if result.split != ('retrieval' if name == 'corpus' else 'held_out'):
        raise ValueError('incorrect dataset split')
    return result


def eligible(dataset):
    if dataset.split != 'retrieval':
        raise ValueError('held-out examples must never be seeded')
    return [r for r in dataset.cases if r.review_status == 'approved' or
            (settings.SEMANTIC_V3_ALLOW_UNREVIEWED and r.review_status == 'pending_human_review')]


def validate_evaluation_overlap(corpus):
    """Check both maintained evaluation suites before admitting retrieval data."""
    evaluations = [load('evaluation')]
    development_path = PROJECT_ROOT / 'examples/semantic_evaluation_v1/cases.json'
    if development_path.exists():
        development = Dataset.model_validate_json(development_path.read_text())
        if development.split != 'held_out':
            raise ValueError('evaluation dataset must not have retrieval split')
        evaluations.append(development)
    ids = {r.id for r in corpus.cases}
    normalize = lambda value: ' '.join(value.casefold().split())
    instructions = {normalize(r.instruction) for r in corpus.cases}
    for evaluation in evaluations:
        if (ids & {r.id for r in evaluation.cases} or
                instructions & {normalize(r.instruction) for r in evaluation.cases}):
            raise ValueError('retrieval/evaluation overlap')


def text(instruction):
    return 'Instruction: ' + instruction


def point_id(dataset, example):
    return str(uuid5(NAMESPACE_URL, ':'.join((dataset.fingerprint, embeddings.specification(), TEXT_FORMAT, example.id))))


def seed(*, corpus_path: Path | str | None = None, collection: str | None = None):
    from qdrant_client import models
    from src.services.semantic_engine import _cloud_client, _vector
    if settings.OFFLINE_MODE:
        raise ValueError('v3 seeding requires online mode')
    collection = (collection if collection is not None else settings.SEMANTIC_V3_COLLECTION).strip()
    if not collection:
        raise ValueError('v3 collection name must not be empty')
    if collection == settings.QDRANT_COLLECTION:
        raise ValueError('v3 must use a separate collection')
    corpus = load(path=corpus_path)
    validate_evaluation_overlap(corpus)
    cases = eligible(corpus)
    if not cases:
        raise ValueError('no eligible cases: review labels or explicitly enable provisional development seeding')
    vectors = []
    for row in cases:
        vectors.append(_vector(text(row.instruction)))
        time.sleep(0.25)
    dimension = len(vectors[0])
    if any(len(v) != dimension for v in vectors):
        raise ValueError('inconsistent embedding dimensions')
    # Maintenance uploads need more time than interactive retrieval requests.
    client = _cloud_client(timeout=30)
    try:
        if not client.collection_exists(collection):
            client.create_collection(collection, vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE))
        params = client.get_collection(collection).config.params.vectors
        if not isinstance(params, models.VectorParams) or params.size != dimension or params.distance != models.Distance.COSINE:
            raise ValueError('v3 collection vector configuration mismatch')
        for key in ('action_families', 'risk_aspects', 'label', 'corpus_hash', 'embedding_spec', 'text_format', 'review_status'):
            client.create_payload_index(collection, key, field_schema=models.PayloadSchemaType.KEYWORD, wait=True)
        points = []
        for row, vector in zip(cases, vectors):
            payload = row.model_dump(mode='json')
            payload.update(example_id=row.id, text=text(row.instruction),
                           action_families=sorted({a.action_family for a in row.actions}),
                           risk_aspects=[r.category.value for r in row.risk_aspects],
                           findings=[r.model_dump(mode='json') for r in row.risk_aspects],
                           corpus_hash=corpus.fingerprint, corpus_version=corpus.schema_version,
                           embedding_spec=embeddings.specification(), text_format=TEXT_FORMAT, origin=corpus.origin)
            points.append(models.PointStruct(id=point_id(corpus, row), vector=vector, payload=payload))
        for offset in range(0, len(points), 10):
            client.upsert(collection, points=points[offset:offset + 10], wait=True)
        stored = client.retrieve(collection, ids=[p.id for p in points], with_payload=True, with_vectors=False)
        expected = {str(p.id): p.payload for p in points}
        if len(stored) != len(points) or any(expected.get(str(p.id)) != p.payload for p in stored):
            raise ValueError('seed payload verification failed')
        count = client.count(collection, count_filter=models.Filter(must=[models.FieldCondition(
            key='corpus_hash', match=models.MatchValue(value=corpus.fingerprint))]), exact=True).count
        if count != len(points):
            raise ValueError('unexpected points exist for this corpus revision')
        return dict(collection=collection, corpus_hash=corpus.fingerprint, seeded=len(points),
                    embedding_spec=embeddings.specification(), text_format=TEXT_FORMAT, dimensions=dimension,
                    provisional=any(r.review_status != 'approved' for r in cases))
    finally:
        client.close()
