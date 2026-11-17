import os,time,json,logging
from pathlib import Path
import redis
from src.config.database import Session
from src.models.entities import Cohort,RuleRun
from src.services.analysis import calculate
from src.services.rules import finalize_run,recover_interrupted,RuleConflict
r=redis.Redis.from_url(os.environ['REDIS_URL'])
with Session() as recovery:
 for pending in recovery.query(Cohort).filter(Cohort.state.in_(['queued','running'])).all():
  pending.state='queued';r.rpush('cohort:jobs',pending.id)
 recovery.commit()
 try:
  recovered=recover_interrupted(recovery)
  if recovered:logging.warning('recovered %s interrupted rule runs',recovered)
 except Exception:logging.exception('rule recovery')
while True:
 Path('/tmp/heartbeat').touch()
 try:
  item=r.blpop(['rule:jobs','cohort:jobs'],timeout=3)
  if not item:continue
  kind,raw=item[0].decode(),item[1]
  if kind=='rule:jobs':
   with Session() as s:
    run=s.get(RuleRun,int(raw))
    if not run or run.state!='pending':continue
    try:
     finalize_run(s,run.id)
    except RuleConflict as e:
     s.rollback();logging.warning('rule run %s skipped: %s',raw,e)
    except Exception:
     # finalize_run 已把失败落库为 failed；这里只记录日志，任务可原地重试。
     s.rollback();logging.exception('rule worker')
   continue
  with Session() as s:
   row=s.get(Cohort,int(raw))
   if not row or row.state!='queued':continue
   snapshot=row.result['snapshot'];row.state='running';s.commit()
   try:
    row.result={**calculate(snapshot),'snapshot':snapshot};row.state='done'
    Path('/artifacts/'+str(row.id)+'.json').write_text(json.dumps(row.result,ensure_ascii=False))
   except Exception as e:row.state='failed';row.error=str(e)
   s.commit()
 except Exception:logging.exception('worker');time.sleep(2)
