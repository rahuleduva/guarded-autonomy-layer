"""Versioned contracts for advisory analysis; none authorize execution."""
from enum import Enum
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ActionClass(str, Enum):
    READ_FILE = 'read_file'
    CREATE_FILE = 'create_file'
    CREATE_DIRECTORY = 'create_directory'
    UPDATE_FILE = 'update_file'
    MOVE_FILE = 'move_file'
    DELETE_FILE = 'delete_file'
    DELETE_DIRECTORY = 'delete_directory'


class UnsupportedActionEvidence(BaseModel):
    """A runtime capability refusal, distinct from incomplete model analysis."""
    model_config = ConfigDict(extra='forbid')
    version: Literal['unsupported-action-v1', 'unsupported-action-v2'] = 'unsupported-action-v2'
    action_class: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator('action_class')
    @classmethod
    def must_be_unsupported(cls, value):
        if not value.strip() or value in {action.value for action in ActionClass}:
            raise ValueError('unsupported-action evidence requires an unknown runtime action')
        return value


class RiskCategory(str, Enum):
    MASS_DELETION = 'mass_deletion'
    REVIEW_BYPASS = 'review_bypass'
    SECRET_EXPORT = 'secret_export'
    CREDENTIAL_ACCESS = 'credential_access'
    DATA_EXPOSURE = 'data_exposure'
    DESTRUCTIVE_OVERWRITE = 'destructive_overwrite'
    AUDIT_TAMPERING = 'audit_tampering'


FAMILIES = {
    'read_file': 'read', 'create_file': 'create', 'create_directory': 'create',
    'update_file': 'update', 'move_file': 'move', 'delete_file': 'delete',
    'delete_directory': 'delete',
}


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ActionMention(Strict):
    action_class: ActionClass
    excerpt: str = Field(min_length=1, max_length=2000)


class PotentialRisk(Strict):
    category: RiskCategory
    excerpt: str = Field(min_length=1, max_length=2000)
    query: str = Field(min_length=1, max_length=1000)


class UnmappedConcern(Strict):
    description: str = Field(min_length=1, max_length=1000)
    excerpt: str = Field(min_length=1, max_length=2000)


class CallerCredentialStatusClaim(Strict):
    """A material assertion, never proof of a credential's live status.

    Provenance is constrained by the application. Neither model output nor a
    caller can supply a verified/system variant of this contract.
    """

    claim_type: Literal['credential_revoked', 'credential_disabled',
                        'credential_expired', 'credential_nonfunctional']
    excerpt: str = Field(min_length=1, max_length=2000,
                         description='Exact continuous quote of the caller-reported credential status.')
    unknown_index: int = Field(ge=0, strict=True,
                              description='Zero-based index of the missing trusted status confirmation in unknowns.')
    source: Literal['caller'] = 'caller'
    verification_status: Literal['unverified'] = 'unverified'
    affected_risk: Literal['credential_access'] = 'credential_access'


class RequestAnalysis(Strict):
    actions: list[ActionMention] = Field(default_factory=list, max_length=12)
    unsupported_actions: list[UnmappedConcern] = Field(default_factory=list, max_length=8)
    intent_summary: str = Field(min_length=1, max_length=2000)
    potential_risks: list[PotentialRisk] = Field(default_factory=list, max_length=14)
    unknowns: list[str] = Field(default_factory=list, max_length=12)
    unmapped_concerns: list[UnmappedConcern] = Field(default_factory=list, max_length=8)
    # Empty by default so saved analyses predating the claim contract still replay.
    credential_status_claims: list[CallerCredentialStatusClaim] = Field(default_factory=list, max_length=12)

    @model_validator(mode='after')
    def validate_credential_claims(self):
        seen = set()
        for index, claim in enumerate(self.credential_status_claims):
            if claim.unknown_index >= len(self.unknowns) or not self.unknowns[claim.unknown_index].strip():
                raise ValueError(f'credential_status_claims.{index}.unknown_index must reference a nonempty analyzer unknown')
            key = (claim.claim_type, claim.excerpt)
            if key in seen:
                raise ValueError('duplicate credential status claim')
            seen.add(key)
        if self.credential_status_claims and not any(
                risk.category == RiskCategory.CREDENTIAL_ACCESS for risk in self.potential_risks):
            raise ValueError('credential_status_claims require a credential_access potential risk and retrieval query')
        return self


class RetrievalQuery(Strict):
    id: str
    text: str
    purpose: Literal['operation', 'risk', 'general']
    action_families: list[str] = Field(default_factory=list)
    risk_categories: list[RiskCategory] = Field(default_factory=list)


class RiskFinding(Strict):
    category: RiskCategory
    assessment: Literal['applies', 'does_not_apply', 'uncertain']
    excerpts: list[str] = Field(min_length=1, max_length=8)
    example_ids: list[str] = Field(default_factory=list, max_length=12)
    reason: str = Field(min_length=1, max_length=2000)
    missing_information: list[str] = Field(default_factory=list, max_length=8)


class JudgeAssessment(Strict):
    findings: list[RiskFinding] = Field(default_factory=list)
    coverage_complete: bool
    unmapped_concerns: list[UnmappedConcern] = Field(default_factory=list)
    explanation: str = Field(min_length=1, max_length=2000)

    @field_validator('findings')
    @classmethod
    def limit_findings(cls, value: list[RiskFinding]) -> list[RiskFinding]:
        if len(value) > 14:
            raise ValueError('findings must contain at most 14 items')
        return value

    @field_validator('unmapped_concerns')
    @classmethod
    def limit_unmapped_concerns(cls, value: list[UnmappedConcern]) -> list[UnmappedConcern]:
        if len(value) > 8:
            raise ValueError('unmapped_concerns must contain at most 8 items')
        return value


class UnknownDisposition(Strict):
    unknown_index: int = Field(ge=0, strict=True)
    disposition: Literal['resolved', 'unresolved', 'irrelevant']
    reason: str = Field(min_length=1)
    excerpts: list[str] = Field(default_factory=list)
    example_ids: list[str] = Field(default_factory=list)


class JudgeAssessmentV2(JudgeAssessment):
    unknown_dispositions: list[UnknownDisposition] = Field(default_factory=list, max_length=12)


class SemanticReasonCode(str, Enum):
    ANALYSIS_UNAVAILABLE = 'analysis_unavailable'
    ANALYSIS_INVALID = 'analysis_invalid'
    RETRIEVAL_UNAVAILABLE = 'retrieval_unavailable'
    QUERY_UNAVAILABLE = 'query_unavailable'
    NO_EVIDENCE = 'no_evidence'
    QUERY_BUDGET = 'query_budget'
    CANDIDATE_BUDGET = 'candidate_budget'
    EVIDENCE_INVALID = 'evidence_invalid'
    JUDGE_UNAVAILABLE = 'judge_unavailable'
    JUDGE_INVALID = 'judge_invalid'
    UNCERTAIN_FINDING = 'uncertain_finding'
    INCOMPLETE_COVERAGE = 'incomplete_coverage'
    UNMAPPED_CONCERN = 'unmapped_concern'
    UNRESOLVED_UNKNOWN = 'unresolved_unknown'


class SemanticFailure(Strict):
    code: SemanticReasonCode
    # Optional for backward compatibility with previously persisted pipeline JSON.
    failure_category: Literal[
        'provider_unavailable', 'provider_rejected', 'provider_refusal',
        'stage_deadline_exceeded',
        'structured_output_invalid', 'grounding_invalid', 'evidence_invalid',
        'semantic_validation_invalid', 'application_error',
    ] | None = None
    exception_type: str
    message: str
    http_status: int | None = None
    validation_errors: dict[str, list[dict[str, str]]] | None = None


class SemanticReason(Strict):
    code: SemanticReasonCode
    stage: Literal['analysis', 'retrieval', 'judgment']
    message: str = Field(min_length=1)
    failure_category: Literal[
        'provider_unavailable', 'provider_rejected', 'provider_refusal',
        'stage_deadline_exceeded',
        'structured_output_invalid', 'grounding_invalid', 'evidence_invalid',
        'semantic_validation_invalid', 'application_error',
    ] | None = None
    query_id: str | None = None
    risk_category: RiskCategory | None = None


class SemanticSummary(Strict):
    completion: Literal['complete', 'partial', 'unavailable']
    assessment: Literal['clear', 'risk_detected', 'uncertain', 'not_assessed']
    requires_review: bool = Field(strict=True)
    applicable_risks: list[RiskCategory] = Field(default_factory=list)
    uncertain_risks: list[RiskCategory] = Field(default_factory=list)
    reasons: list[SemanticReason] = Field(default_factory=list)

    @model_validator(mode='after')
    def consistent(self):
        if (self.assessment == 'risk_detected') != bool(self.applicable_risks):
            raise ValueError('confirmed risks require risk_detected assessment')
        if self.assessment == 'clear' and (self.completion != 'complete' or self.reasons or self.uncertain_risks):
            raise ValueError('clear assessment requires complete checking without gaps')
        if self.completion == 'complete' and self.assessment not in {'clear', 'risk_detected'}:
            raise ValueError('complete checking requires a definitive assessment')
        if self.completion == 'complete' and self.reasons:
            raise ValueError('blocking reasons require incomplete checking')
        if self.completion != 'complete' and not self.reasons:
            raise ValueError('incomplete checking requires an explanation')
        if self.completion == 'unavailable' and self.assessment != 'not_assessed':
            raise ValueError('unavailable analysis cannot claim an assessment')
        if self.assessment == 'not_assessed' and self.uncertain_risks:
            raise ValueError('unassessed result cannot claim judged risk findings')
        if self.requires_review != (self.assessment != 'clear'):
            raise ValueError('review requirement contradicts assessment')
        return self


class RetrievalMatch(Strict):
    query_id: str = Field(min_length=1)
    label: Literal['risky', 'benign', 'uncertain']
    rank: int = Field(ge=1, strict=True)
    score: float = Field(ge=-1, le=1, allow_inf_nan=False)


class RetrievedFinding(Strict):
    category: RiskCategory
    assessment: Literal['applies', 'does_not_apply', 'uncertain']
    instruction_excerpt: str = Field(min_length=1)


class RetrievedCandidate(Strict):
    example_id: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    label: Literal['risky', 'benign', 'uncertain']
    risk_aspects: list[RiskCategory] = Field(min_length=1)
    findings: list[RetrievedFinding] = Field(min_length=1)
    review_status: Literal['approved', 'pending_human_review']
    matches: list[RetrievalMatch] = Field(min_length=1)

    @model_validator(mode='after')
    def consistent(self):
        categories = [f.category for f in self.findings]
        if self.risk_aspects != categories or len(set(categories)) != len(categories):
            raise ValueError('candidate risk aspects differ from its findings')
        if any(f.instruction_excerpt not in self.instruction for f in self.findings):
            raise ValueError('candidate finding excerpt is ungrounded')
        statuses = {f.assessment for f in self.findings}
        expected = 'risky' if 'applies' in statuses else 'uncertain' if 'uncertain' in statuses else 'benign'
        if expected != self.label or any(m.label != self.label for m in self.matches):
            raise ValueError('candidate label differs from findings or matches')
        return self


class QueryMatch(RetrievalMatch):
    example_id: str = Field(min_length=1)


class LabelSearch(Strict):
    label: Literal['risky', 'benign', 'uncertain']
    filters: dict
    matches: list[QueryMatch]
    status: Literal['applied', 'partial', 'unavailable']
    failure: SemanticFailure | None = None
    error: str | None = None
    invalid_evidence: list[SemanticFailure] = Field(default_factory=list)


class QueryOutcome(RetrievalQuery):
    filters: dict
    matches: list[QueryMatch]
    searches: list[LabelSearch]
    status: Literal['applied', 'partial', 'unavailable']
    failure: SemanticFailure | None = None
    error: str | None = None
    observations: list[str] = Field(default_factory=list)
