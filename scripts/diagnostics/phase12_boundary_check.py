"""Isolated API boundary check, not a cloud accuracy benchmark."""
import json
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine
from sqlalchemy.pool import StaticPool
from src.main import create_app
from src.config import settings
from src.services.semantic_engine import SemanticAssessment
from src.services import request_analysis, semantic_pipeline

engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
original=settings.SEMANTIC_PIPELINE_MODE
results=[]

def analysis(source):
 return {'status':'applied','result':{'actions':[{'action_class':'read_file','excerpt':'read_file'}], 'intent_summary':'Read named file','potential_risks':[], 'unknowns':[],'unmapped_concerns':[]}}

def retrieve(*args,**kwargs):
 return {'candidates':[{'example_id':'fixture','risk_aspects':['data_exposure']}],'coverage_gaps':[]}

def judgment(*args):
 return {'status':'applied','result':{'findings':[{'category':'data_exposure','assessment':'does_not_apply','excerpts':['read_file'],'example_ids':['fixture'],'reason':'Public fixture','missing_information':[]}],'coverage_complete':True,'unmapped_concerns':[],'explanation':'Fixture'}}

payload={'agent_id':'phase12_boundary','actor_role':'workspace_agent','action_class':'read_file','target_resource':'/srv/agent/workspace/public-note.txt','parameters':{},'environment':'development'}
try:
 with patch('src.services.advisory_evidence.semantic_engine.assess',return_value=SemanticAssessment(True,.8,'baseline fixture warning','fixture',.5)), patch('src.services.request_analysis.analyze',side_effect=analysis), patch('src.services.semantic_retrieval.retrieve',side_effect=retrieve), patch('src.services.semantic_judge.judge',side_effect=judgment), patch('src.services.review_explanation.invoke_for_purpose',side_effect=RuntimeError('provider disabled in boundary check')):
  with TestClient(create_app(engine)) as client:
   settings.SEMANTIC_PIPELINE_MODE='observe'
   observed=client.post('/api/v1/actions/evaluate',json=payload)
   assert observed.status_code==200,observed.text
   assert observed.json()['decision']=='ESCALATED',observed.text
   results.append('observe preserves baseline warning')
   settings.SEMANTIC_PIPELINE_MODE='review'
   reviewed=client.post('/api/v1/actions/evaluate',json=payload)
   assert reviewed.status_code==200,reviewed.text
   assert reviewed.json()['decision']=='SHADOW_LOGGED',reviewed.text
   results.append('review clears similarity-only warning while preserving SHADOW')
   decision_id=reviewed.json()['decision_id']
   with patch('src.services.request_analysis.analyze',side_effect=AssertionError('replay called analyzer')), patch('src.services.semantic_retrieval.retrieve',side_effect=AssertionError('replay called retrieval')), patch('src.services.semantic_judge.judge',side_effect=AssertionError('replay called judge')):
    replayed=client.get('/api/v1/replay/'+decision_id)
    assert replayed.status_code==200 and replayed.json()['is_consistent'],replayed.text
   results.append('replay is consistent without analyzer, retrieval or judge calls')
   denied=client.post('/api/v1/actions/evaluate',json=dict(payload,target_resource='/etc/passwd'))
   assert denied.status_code==200 and denied.json()['decision']=='DENIED',denied.text
   results.append('deterministic sandbox denial remains authoritative')
   with patch('src.services.semantic_judge.judge',return_value={'status':'unavailable','error':'TimeoutError'}):
    unavailable=client.post('/api/v1/actions/evaluate',json=payload)
    assert unavailable.status_code==200 and unavailable.json()['decision']=='ESCALATED',unavailable.text
   results.append('judge failure requires review')
   pending=unavailable.json()['decision_id']
   package=next(item for item in client.get('/api/v1/escalations').json() if item['decision_id']==pending)
   rejected=client.post('/api/v1/control/approve/'+pending,json={'decision_id':pending,'reviewer_id':'phase12_diagnostic','approve':False,'note':'Isolated boundary check','package_fingerprint':package['package_fingerprint']})
   assert rejected.status_code==200 and rejected.json()['status']=='REJECTED',rejected.text
   results.append('human rejection records no authorization')
   compound=analysis('')
   compound['result']['actions'].append({'action_class':'delete_file','excerpt':'delete'})
   with patch('src.services.request_analysis.analyze',return_value=compound):
    response=client.post('/api/v1/actions/evaluate',json=payload)
    assert response.status_code==400,response.text
   results.append('compound analysis rejected with no partial decision')
   class CombinedParser:
    def invoke(self,messages):
     return {'action':{'action_class':'read_file','target_resource':'/srv/agent/workspace/public-note.txt','parameters':{},'environment':'production'},
      'analysis':{'actions':[{'action_class':'read_file','excerpt':'Read'}],'intent_summary':'Read the specified file','potential_risks':[],'unknowns':[],'unmapped_concerns':[]}}
   with patch('src.services.llm_routing.make_parser',return_value=CombinedParser()) as parser_factory:
    natural=client.post('/api/v1/actions/evaluate-prompt',json={'agent_id':'phase12_nl_boundary','actor_role':'workspace_agent','raw_prompt':'Read /srv/agent/workspace/public-note.txt'})
    assert natural.status_code==200,natural.text
    assert parser_factory.call_count==1
    assert natural.json()['decision']=='SHADOW_LOGGED',natural.text
   results.append('natural-language parsing and analysis share one front-stage response')
   class CompoundParser(CombinedParser):
    def invoke(self,messages):
     response=super().invoke(messages)
     response['action']=None
     response['analysis']['actions'].append({'action_class':'delete_file','excerpt':'delete'})
     return response
   with patch('src.services.llm_routing.make_parser',return_value=CompoundParser()):
    natural=client.post('/api/v1/actions/evaluate-prompt',json={'agent_id':'phase12_nl_compound','actor_role':'workspace_agent','raw_prompt':'Read /srv/agent/workspace/public-note.txt, then delete /srv/agent/workspace/scratch.tmp.'})
    assert natural.status_code==400 and 'split_required' in natural.json()['detail'],natural.text
   results.append('natural-language compound output with null action is rejected')
   unsupported=analysis('')
   unsupported['result']['actions']=[]
   unsupported['result']['unsupported_actions']=[{'description':'Unsupported operation','excerpt':'read_file'}]
   with patch('src.services.request_analysis.analyze',return_value=unsupported):
    response=client.post('/api/v1/actions/evaluate',json=payload)
    assert response.status_code==400,response.text
   results.append('unsupported operations cannot be silently mapped into authorization')
   queue=client.get('/api/v1/escalations')
   assert queue.status_code==200 and all('semantic_findings' in p for p in queue.json()),queue.text
   results.append('review packages expose frozen semantic findings')
finally:
 settings.SEMANTIC_PIPELINE_MODE=original
 engine.dispose()
report={'scope':'In-process API boundary checks with isolated SQLite and controlled provider responses; not cloud E2E or risk accuracy testing.','passed':results}
Path('reports/semantic/phase12/phase12_boundary_checks.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
