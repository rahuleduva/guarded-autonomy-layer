"""Environment and OFFLINE_MODE configuration (C.8).

Every field has a default so ``Settings()`` constructs with no secrets present;
that is what lets the offline path (SQLite, stubbed Qdrant, stubbed Gemini) run
with zero configuration. Override anything via the environment or a local ``.env``.

``sandbox_root`` is deliberately absent (C.4): scope is declared by the policy
artifact, not by config, so the boundary is policy-dependent rather than
location-dependent. A relative root would make every containment verdict depend
on the process working directory (A1).
"""

from pathlib import Path
from typing import Literal, Optional

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_POSTGRES_URL = "postgresql://postgres:postgres@localhost:5432/guarded_autonomy"
OFFLINE_SQLITE_URL = "sqlite:///./guarded_autonomy.db"

# Project root, derived from this file's location rather than the process
# working directory, so loading config does not depend on where the app was
# started. This locates files only; it is NOT a security boundary and must
# never be used as one. The sandbox root is policy-declared (C.4), not here.
PROJECT_ROOT = Path(__file__).parent.parent
ENV_FILE = PROJECT_ROOT / ".env"



class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    PROJECT_NAME: str = "Guarded Action & Autonomy Control Layer"

    # Keyless local run: SQLite instead of Postgres, stubbed Qdrant and Gemini.
    OFFLINE_MODE: bool = False

    # None means "derive from OFFLINE_MODE"; an explicit DATABASE_URL always wins.
    DATABASE_URL: Optional[str] = None

    # Dev-only default so the app boots; override in any real deployment.
    SECRET_KEY: str = "dev-insecure-secret-change-me"
    JWT_ALGORITHM: str = "HS256"

    # Cloud Qdrant is the embedding store. Accept either environment name;
    # QDRANT_URL takes precedence when both are provided. Empty => offline stub.
    QDRANT_URL: str = Field(
        default="",
        validation_alias=AliasChoices("QDRANT_URL", "QDRANT_CLUSTER_URL"),
    )
    QDRANT_API_KEY: str = ""
    QDRANT_COLLECTION: str = "policy_prompts"

    # Optional language-model providers. Model IDs are user-specified defaults.
    LLM_PROVIDER: Literal["gemini", "groq"] = "gemini"
    GEMINI_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    GEMINI_LLM_MODEL: str = "gemini-3.5-flash-lite"
    GROQ_LLM_MODEL: str = "qwen/qwen3.8-27b"

    # Keep the existing Sentence Transformer as the default embedding provider.
    # Gemini embedding uses the optional provider adapter.
    EMBEDDING_PROVIDER: Literal["sentence_transformers", "gemini"] = "sentence_transformers"
    SENTENCE_TRANSFORMER_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
    GEMINI_EMBEDDING_MODEL: str = "gemini-embedding-2"
    GEMINI_EMBEDDING_DIMENSIONS: int = Field(default=768, ge=128, le=3072)

    # Promotion thresholds live in settings, never in the policy artifact:
    # everything in the artifact is hashed into policy_hash, and a threshold
    # tweak must not mint a new policy version (C.4, C.8).
    PROMOTION_THRESHOLD_ASSISTED: int = 5
    PROMOTION_THRESHOLD_LIVE: int = 20
    PROMOTION_THRESHOLD_POLICY: int = 20

    # Advisory cutoff. 0.5 separates offline binary scores (0/1); vector cosine
    # scores need separate calibration against the deployed seeded collection.
    SEMANTIC_THRESHOLD: float = 0.5

    # Token lifetime is deliberately NOT a setting. A token's TTL is consumed by
    # the decision -- it becomes EvaluationResult.ttl_seconds and the issued
    # token's exp -- so it lives in the artifact's ttl_map, which is hashed into
    # policy_hash and replayed exactly. A second source for one value would let
    # config silently disagree with the recorded decision, which is the same
    # tension that keeps sandbox_root in the artifact (C.4, C.8).

    @model_validator(mode="after")
    def _resolve_google_api_keys(self) -> "Settings":
        # Allow either convention without replacing explicitly distinct keys.
        if not self.GEMINI_API_KEY:
            self.GEMINI_API_KEY = self.GOOGLE_API_KEY
        if not self.GOOGLE_API_KEY:
            self.GOOGLE_API_KEY = self.GEMINI_API_KEY
        return self

    @model_validator(mode="after")
    def _resolve_database_url(self) -> "Settings":
        if not self.DATABASE_URL:
            self.DATABASE_URL = OFFLINE_SQLITE_URL if self.OFFLINE_MODE else LOCAL_POSTGRES_URL
        return self


settings = Settings()
