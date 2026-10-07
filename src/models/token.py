# Consumed tokens SQLModel table
import datetime
from sqlmodel import Field, SQLModel

class ConsumedToken(SQLModel, table=True):
    __tablename__ = "consumed_tokens"

    nonce: str = Field(primary_key=True)
    decision_id: str = Field(index=True)
    consumed_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))