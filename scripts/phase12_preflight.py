import json
from urllib.request import urlopen
from sqlalchemy import create_engine, text
from src.config import settings
from src.services.semantic_engine import _cloud_client
from src.services.embeddings import _redis_client
result={}
try:
 with urlopen('http://127.0.0.1:8000/api/v1/health',timeout=3) as response:
  result['fastapi']={'status_code':response.status,'body':response.read().decode('utf-8')}
except Exception as exc: result['fastapi']={'error_type':type(exc).__name__}
try:
 engine=create_engine(settings.DATABASE_URL, connect_args={'connect_timeout':5})
 with engine.connect() as connection:
  result['database']={table:connection.execute(text('SELECT count(*) FROM '+table)).scalar_one() for table in ['policies','decision_records','autonomy_state','escalation_queue']}
 engine.dispose()
except Exception as exc: result['database']={'error_type':type(exc).__name__}
try:
 client=_cloud_client()
 try:
  result['qdrant']={}
  names={c.name for c in client.get_collections().collections}
  for name in [settings.QDRANT_COLLECTION,settings.SEMANTIC_V3_COLLECTION]:
   if name not in names: result['qdrant'][name]={'exists':False}; continue
   info=client.get_collection(name)
   result['qdrant'][name]={'exists':True,'points':info.points_count,'vectors':info.config.params.vectors.model_dump(mode='json')}
 finally: client.close()
except Exception as exc: result['qdrant']={'error_type':type(exc).__name__}
try:
 client=_redis_client(settings.REDIS_URL)
 result['redis']={'ping':client.ping(),'namespace':settings.REDIS_NAMESPACE}
except Exception as exc: result['redis']={'error_type':type(exc).__name__}
print(json.dumps(result,indent=2))
