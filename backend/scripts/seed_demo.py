import random
from datetime import date,timedelta,datetime,timezone
from src.config.database import Base,engine,Session
from src.models.entities import User,Event,RuleVersion,RuleRun
from src.utils.security import hash_password
def seed():
 Base.metadata.create_all(engine);rng=random.Random(3114);start=date(2026,9,21)
 with Session() as s:
  for role in ('admin','analyst','viewer'):
   email=role+'@demo.local'
   if not s.query(User).filter_by(email=email).first():s.add(User(email=email,role=role,password=hash_password('Demo123!')))
  courses=('数据素养','服务沟通','安全规范')
  for i in range(200):
   first=i//25;learner=f'CQ-{i+1:03}'
   for w in range(first,9):
    if w!=first and rng.random()>.82-.07*(w-first):continue
    day=start+timedelta(weeks=w,days=1+i%3);stamp=day.isoformat()+'T02:00:00+00:00'
    course=courses[min(w-first,2)]
    code=f'{learner}-W{w}'
    if not s.query(Event).filter_by(code=code).first():
     recorded=(day-timedelta(days=1)).isoformat()+'T03:00:00+00:00' if i==7 and w==first else ((day+timedelta(days=2)).isoformat()+'T03:00:00+00:00' if i%29==0 else stamp)
     s.add(Event(code=code,learner=learner,occurred_at=stamp,course=course,note='学习平台正常上报',recorded_at=recorded))
    if i%17==0:
     code+='-DUP'
     if not s.query(Event).filter_by(code=code).first():s.add(Event(code=code,learner=learner,occurred_at=day.isoformat()+'T04:00:00+00:00',course=course,note='同周重复回访，不重复计人数',recorded_at=day.isoformat()+'T04:00:00+00:00'))
  s.commit()
  seed_rule(s)
  print({'users':s.query(User).count(),'events':s.query(Event).count(),'learners':len({r.learner for r in s.query(Event).all()}),'rules':s.query(RuleVersion).count(),'runs':s.query(RuleRun).count()})
def seed_rule(s):
 import os
 from datetime import date
 from src.schemas.business import RuleInput
 from src.services.rules import create_rule,publish_rule,create_run,RuleConflict
 if s.query(RuleVersion).count():return
 try:
  v=create_rule(s,RuleInput(name='常青课程前置 v1',courses=['数据素养','服务沟通','安全规范'],prerequisites={'服务沟通':['数据素养'],'安全规范':['服务沟通']}),'seed@demo.local')
  publish_rule(s,v,'seed@demo.local')
  run=create_run(s,v,date(2026,11,14),'seed','seed@demo.local')
  try:
   import redis
   redis.Redis.from_url(os.environ['REDIS_URL']).rpush('rule:jobs',run.id)
  except Exception:pass
 except RuleConflict:pass
if __name__=='__main__':seed()
