"""Read one decision's Phase 12 evidence without returning capability tokens."""
import argparse
import json
from sqlmodel import Session
from src.db.database import engine
from src.models.decision import DecisionRecord

parser=argparse.ArgumentParser()
parser.add_argument('decision_id')
parser.add_argument('--summary',action='store_true',help='Show pipeline statuses and findings without request text or full retrieved documents')
args=parser.parse_args()
with Session(engine) as session:
    record=session.get(DecisionRecord,args.decision_id)
    if record is None:
        raise SystemExit('decision not found')
    pipeline=record.evidence_json.get('semantic_pipeline')
    if args.summary:
        p=pipeline or {}
        summary={'analysis':{k:p.get('analysis',{}).get(k) for k in ('status','provider','model','error','http_status')},
          'retrieval':{k:p.get('retrieval',{}).get(k) for k in ('status','version','collection','provisional','coverage_gaps','error')},
          'candidate_count':len(p.get('retrieval',{}).get('candidates',[])),
          'judge':{k:p.get('judgment',{}).get(k) for k in ('status','provider','model','error','http_status')},
          'findings':p.get('judgment',{}).get('result',{}).get('findings',[]),
          'proposed_flag':p.get('proposed_flag'),'proposed_warning':p.get('proposed_warning')}
        print(json.dumps({'decision_id':record.decision_id,'outcome':record.outcome,'reason':record.reason,'semantic':summary},indent=2))
        raise SystemExit(0)
    print(json.dumps({'decision_id':record.decision_id,'outcome':record.outcome,
        'risk_level':record.risk_level,'autonomy_level':record.autonomy_level,
        'reason':record.reason,'semantic_flag':record.evidence_json.get('semantic_flag'),
        'semantic_warning':record.evidence_json.get('semantic_warning'),
        'semantic_pipeline':record.evidence_json.get('semantic_pipeline')},indent=2))
