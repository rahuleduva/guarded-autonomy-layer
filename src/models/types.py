from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

# Dialect-aware JSON: JSONB on Postgres, plain JSON on SQLite.
# This is what lets one migration run on both Neon (Postgres) and the offline SQLite fallback.
JSONType: JSON = JSON().with_variant(JSONB(), "postgresql")
