"""Retry only the judge on frozen evidence from one prior semantic decision."""
import argparse
import json
from datetime import datetime,timezone
from pathlib import Path
from sqlmodel import Session
from src.db.database import engine
from src.models.decision import DecisionRecord
from src.services.semantic_judge import judge

parser=argparse.ArgumentParser()
parser.add_argument('decision_id')
args=parser.parse_args()
with Session(engine) as session:
    record=session.get(DecisionRecord,args.decision_id)
    if record is None: raise SystemExit('decision not found')
    pipeline=record.evidence_json.get('semantic_pipeline')
    if not pipeline: raise SystemExit('decision has no frozen Phase 12 evidence')
# The session must be closed before invoking the cloud judge.
result=judge(pipeline['source_text'],pipeline['analysis'],pipeline['retrieval'])
report={'created_at':datetime.now(timezone.utc).isoformat(),'decision_id':args.decision_id,
        'source':'Previously frozen synthetic request and Qdrant candidates; no new retrieval, decision or execution.',
        'provider':result.get('provider'),'model':result.get('model'),'status':result.get('status'),
        'error':result.get('error'),'http_status':result.get('http_status'),
        'findings':result.get('result',{}).get('findings',[])}
Path('reports/phase12_judge_retry.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
