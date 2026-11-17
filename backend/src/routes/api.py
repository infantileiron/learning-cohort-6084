import os,json
import redis
from fastapi import APIRouter,Depends,HTTPException,Response
from sqlalchemy.exc import IntegrityError
from src.config.database import db
from src.models.entities import User,Event,Cohort,Intervention,Audit
from src.schemas.business import Login,EventInput,CohortInput,InterventionInput,NoteInput,RuleInput,RunInput
from src.services.replay import RuleError
from src.middlewares.auth import current,editor,admin
from src.utils.security import verify,token
from src.services.business import audit,dump
from src.services.interventions import draft
from src.controllers.business import ensure_cohort,ensure_intervention
router=APIRouter()
@router.post('/auth/login')
def login(data:Login,s=Depends(db)):
 u=s.query(User).filter_by(email=data.email).first()
 if not u or not verify(data.password,u.password):raise HTTPException(401,'账号或密码错误')
 return {'token':token(u),'user':dump(u)}
@router.get('/me')
def me(u=Depends(current)):return dump(u)
@router.get('/events')
def events(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Event).order_by(Event.id.desc()).all()]
@router.post('/events')
def event(data:EventInput,u=Depends(editor),s=Depends(db)):
 from src.services.rules import enqueue_recompute,lock_ledger
 # 与颁证/规则发布互斥：事件写入期间不会有人拿旧快照发证。
 lock_ledger(s)
 existing=s.query(Event).filter_by(code=data.code).first()
 if existing:
  s.commit()
  if all(getattr(existing,k)==v for k,v in data.model_dump().items()):return dump(existing)
  raise HTTPException(409,'同事件编号的内容冲突，请修订原记录或使用新编号')
 r=Event(**data.model_dump());s.add(r)
 try:s.commit()
 except IntegrityError:
  s.rollback();other=s.query(Event).filter_by(code=data.code).first()
  if other and all(getattr(other,k)==v for k,v in data.model_dump().items()):return dump(other)
  raise HTTPException(409,'同事件编号的内容冲突')
 run=enqueue_recompute(s,'event_recorded',u.email)
 if run:
  try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('rule:jobs',run.id)
  except redis.RedisError:pass
 audit(s,u,'录入学习事件 '+r.code);return {**dump(r),'recompute_run_id':run.id if run else None}
@router.put('/events/{i}')
def edit_event(i:int,data:EventInput,u=Depends(editor),s=Depends(db)):
 from src.services.rules import enqueue_recompute,lock_ledger
 lock_ledger(s)
 r=s.get(Event,i)
 if not r:raise HTTPException(404,'事件不存在')
 payload=data.model_dump()
 if all(getattr(r,k)==v for k,v in payload.items()):
  # 原内容重传不产生新修订、不触发重算（幂等）。
  s.commit();return {**dump(r),'recompute_run_id':None}
 for k,v in payload.items():setattr(r,k,v)
 try:s.commit()
 except IntegrityError:s.rollback();raise HTTPException(409,'事件编号重复')
 run=enqueue_recompute(s,'event_corrected',u.email)
 if run:
  try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('rule:jobs',run.id)
  except redis.RedisError:pass
 audit(s,u,'修订学习事件 '+str(i));return {**dump(r),'recompute_run_id':run.id if run else None}
@router.get('/cohorts')
def cohorts(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Cohort).order_by(Cohort.id.desc()).all()]
@router.post('/cohorts')
def cohort(data:CohortInput,u=Depends(editor),s=Depends(db)):
 cfg=data.model_dump(mode='json');snapshot={'config':cfg,'events':[dump(r) for r in s.query(Event).order_by(Event.id).all()]};r=Cohort(**cfg,result={'snapshot':snapshot});s.add(r);s.commit()
 try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('cohort:jobs',r.id)
 except redis.RedisError:r.state='failed';r.error='队列暂不可用，请稍后重试';s.commit();raise HTTPException(503,r.error)
 audit(s,u,'提交留存队列 '+str(r.id));return dump(r)
@router.get('/cohorts/{i}')
def detail(i:int,u=Depends(current),s=Depends(db)):return dump(ensure_cohort(s,i))
@router.get('/cohorts/{i}/export')
def export(i:int,u=Depends(current),s=Depends(db)):
 r=ensure_cohort(s,i)
 if r.state!='done':raise HTTPException(409,'计算成功后才能导出')
 return Response(json.dumps(dump(r),ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="learning-cohort-{i}.json"'})
@router.get('/interventions')
def interventions(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Intervention).order_by(Intervention.id.desc()).all()]
@router.post('/interventions')
def intervention(data:InterventionInput,u=Depends(editor),s=Depends(db)):
 r=draft(s,ensure_cohort(s,data.cohort_id),data);audit(s,u,'生成干预清单 '+str(r.id));return dump(r)
@router.put('/interventions/{i}')
def edit_intervention(i:int,data:NoteInput,u=Depends(editor),s=Depends(db)):
 r=ensure_intervention(s,i)
 if r.status!='draft':raise HTTPException(409,'已签核清单不可修改')
 r.note=data.note;s.commit();audit(s,u,'修订干预说明 '+str(i));return dump(r)
@router.post('/interventions/{i}/approve')
def approve(i:int,u=Depends(admin),s=Depends(db)):
 r=ensure_intervention(s,i)
 if r.status!='draft':raise HTTPException(409,'已签核，不可重复签核')
 r.status='approved';s.commit();audit(s,u,'签核干预清单 '+str(i));return dump(r)
@router.get('/interventions/{i}/export')
def action_export(i:int,u=Depends(current),s=Depends(db)):
 r=ensure_intervention(s,i);return dump(r)
@router.get('/audits')
def audits(u=Depends(admin),s=Depends(db)):return [dump(r) for r in s.query(Audit).order_by(Audit.id.desc()).limit(100)]

# ---------------- 课程前置规则 / 重放核算 / 证书 ----------------

def _rule_version(s,i):
 from src.models.entities import RuleVersion
 r=s.get(RuleVersion,i)
 if not r:raise HTTPException(404,'规则版本不存在')
 return r

def _run_payload(run):
 from src.services.business import dump as _dump
 return {k:v for k,v in _dump(run).items() if k!='input'}

def _rule_run(s,i):
 from src.models.entities import RuleRun
 r=s.get(RuleRun,i)
 if not r:raise HTTPException(404,'重算任务不存在')
 return r

@router.post('/rules')
def create_rule(data:RuleInput,u=Depends(editor),s=Depends(db)):
 from src.services.rules import create_rule,RuleConflict
 try:r=create_rule(s,data,u.email)
 except RuleError as e:raise HTTPException(422,str(e))
 except RuleConflict as e:raise HTTPException(409,str(e))
 audit(s,u,f'创建课程前置规则 v{r.id}');return dump(r)

@router.get('/rules')
def list_rules(u=Depends(current),s=Depends(db)):
 from src.models.entities import RuleVersion
 return [dump(r) for r in s.query(RuleVersion).order_by(RuleVersion.id.desc()).all()]

@router.get('/rules/{i}')
def rule_detail(i:int,u=Depends(current),s=Depends(db)):
 from src.services.replay import topo_order
 r=_rule_version(s,i)
 return {**dump(r),'topological_order':topo_order({'courses':r.courses,'prerequisites':r.prerequisites or {}})}

@router.post('/rules/{i}/publish')
def publish_rule(i:int,u=Depends(admin),s=Depends(db)):
 from src.services.rules import publish_rule,enqueue_recompute,RuleConflict
 try:r=publish_rule(s,i,u.email)
 except RuleError as e:raise HTTPException(422,str(e))
 except RuleConflict as e:raise HTTPException(409,str(e))
 run=enqueue_recompute(s,'rule_published',u.email)
 if run:
  try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('rule:jobs',run.id)
  except redis.RedisError:pass
 audit(s,u,f'发布课程前置规则 v{r.id}');return {**dump(r),'recompute_run_id':run.id if run else None}

@router.post('/runs')
def create_run(data:RunInput,u=Depends(editor),s=Depends(db)):
 from src.services.rules import create_run,latest_published,RuleConflict,today_shanghai
 version=_rule_version(s,data.rule_version_id) if data.rule_version_id else latest_published(s)
 if not version:raise HTTPException(409,'尚无已发布规则，不能重放核算')
 try:run=create_run(s,version,data.as_of or today_shanghai(),'manual',u.email)
 except RuleConflict as e:raise HTTPException(409,str(e))
 try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('rule:jobs',run.id)
 except redis.RedisError:raise HTTPException(503,'核算队列暂不可用，请稍后重试')
 audit(s,u,f'提交规则重算 r{run.id}');return _run_payload(run)

@router.get('/runs')
def list_runs(u=Depends(current),s=Depends(db)):
 from src.models.entities import RuleRun
 return [{k:v for k,v in dump(r).items() if k not in ('input','result')} for r in s.query(RuleRun).order_by(RuleRun.id.desc()).limit(100).all()]

@router.get('/runs/{i}')
def run_detail(i:int,u=Depends(current),s=Depends(db)):
 from src.models.entities import Certificate,Credit
 run=_rule_run(s,i)
 certs=[dump(c) for c in s.query(Certificate).filter(Certificate.run_id==i).order_by(Certificate.id).all()]
 credits=s.query(Credit).filter(Credit.run_id==i).all()
 return {**_run_payload(run),'certificates':certs,'credit_rows':len(credits)}

@router.post('/runs/{i}/retry')
def retry_run(i:int,u=Depends(editor),s=Depends(db)):
 from src.services.rules import retry_run,RuleConflict
 try:run=retry_run(s,_rule_run(s,i),u.email)
 except RuleConflict as e:raise HTTPException(409,str(e))
 try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('rule:jobs',run.id)
 except redis.RedisError:raise HTTPException(503,'核算队列暂不可用，请稍后重试')
 audit(s,u,f'重试规则重算 r{run.id}');return _run_payload(run)

@router.post('/runs/{i}/process')
def process_run(i:int,u=Depends(editor),s=Depends(db)):
 """同步核算入口：便于可查的状态推进与本地验证；worker 使用同一 finalize_run。"""
 from src.services.rules import finalize_run,RuleConflict
 try:run=finalize_run(s,i)
 except RuleConflict as e:raise HTTPException(409,str(e))
 except Exception as e:
  raise HTTPException(422,f'核算失败，任务已标记 failed，可在重算列表重试：{e}')
 audit(s,u,f'同步执行规则重算 r{run.id}');return _run_payload(run)

@router.get('/runs/{i}/learners/{learner}')
def run_learner(i:int,learner:str,u=Depends(current),s=Depends(db)):
 from src.services.rules import learner_transcript,RuleConflict
 try:return learner_transcript(s,_rule_run(s,i),learner)
 except RuleConflict as e:raise HTTPException(409,str(e))

@router.get('/certificates')
def certificates(u=Depends(current),s=Depends(db)):
 from src.models.entities import Certificate
 return [dump(c) for c in s.query(Certificate).order_by(Certificate.id.desc()).all()]

@router.get('/verify/{public_id}')
def verify(public_id:str,s=Depends(db)):
 """公开核查：不需要登录；撤销后的旧凭证仍返回其历史与撤销依据。"""
 from src.services.rules import verify_certificate
 return verify_certificate(s,public_id)
