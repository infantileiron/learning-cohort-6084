from datetime import datetime,timezone
from sqlalchemy import Column,Integer,String,JSON,Text,ForeignKey,UniqueConstraint
from src.config.database import Base
class User(Base):
 __tablename__='users'
 id=Column(Integer,primary_key=True);email=Column(String(100),unique=True,nullable=False);password=Column(String(200));role=Column(String(20))
class Event(Base):
 __tablename__='events'
 id=Column(Integer,primary_key=True);code=Column(String(60),unique=True,nullable=False);learner=Column(String(60),nullable=False,index=True);occurred_at=Column(String(40),nullable=False);course=Column(String(40));note=Column(Text,default='')
 recorded_at=Column(String(40),default=lambda:datetime.now(timezone.utc).isoformat(timespec='seconds'))
class Cohort(Base):
 __tablename__='cohorts'
 id=Column(Integer,primary_key=True);name=Column(String(100));start_week=Column(String(10));end_week=Column(String(10));as_of=Column(String(10));horizon=Column(Integer);state=Column(String(20),default='queued');result=Column(JSON,nullable=True);error=Column(Text,default='')
class Intervention(Base):
 __tablename__='interventions'
 id=Column(Integer,primary_key=True);cohort_id=Column(Integer,ForeignKey('cohorts.id'));cohort_week=Column(String(10));week_index=Column(Integer);note=Column(Text);members=Column(JSON);status=Column(String(20),default='draft')
class Audit(Base):
 __tablename__='audits'
 id=Column(Integer,primary_key=True);actor=Column(String(100));action=Column(String(200));created=Column(String(40))

# ---- 课程前置规则、重放核算与证书 ----
class RuleVersion(Base):
 __tablename__='rule_versions'
 id=Column(Integer,primary_key=True)
 name=Column(String(100),nullable=False)
 status=Column(String(20),nullable=False,default='draft',index=True)
 courses=Column(JSON,nullable=False)
 prerequisites=Column(JSON,nullable=False,default=dict)
 checksum=Column(String(64),nullable=False)
 current_key=Column(String(20),unique=True,nullable=True)
 created_at=Column(String(40),nullable=False)
 published_at=Column(String(40),nullable=True)
 published_by=Column(String(100),nullable=True)

class RuleRun(Base):
 __tablename__='rule_runs'
 id=Column(Integer,primary_key=True)
 rule_version_id=Column(Integer,ForeignKey('rule_versions.id'),nullable=False,index=True)
 state=Column(String(20),nullable=False,default='pending',index=True)
 as_of=Column(String(40),nullable=False)
 input_checksum=Column(String(64),nullable=False)
 input=Column(JSON,nullable=False)
 result=Column(JSON,nullable=True)
 error=Column(Text,default='')
 attempt=Column(Integer,nullable=False,default=0)
 progress=Column(Integer,nullable=False,default=0)
 total=Column(Integer,nullable=False,default=0)
 phase=Column(String(40),default='待核算')
 trigger_reason=Column(String(40),default='manual')
 triggered_by=Column(String(100),default='')
 created_at=Column(String(40),nullable=False)
 started_at=Column(String(40),nullable=True)
 finished_at=Column(String(40),nullable=True)
 superseded_by=Column(Integer,nullable=True)

class Credit(Base):
 __tablename__='credits'
 id=Column(Integer,primary_key=True)
 run_id=Column(Integer,ForeignKey('rule_runs.id'),nullable=False,index=True)
 rule_version_id=Column(Integer,ForeignKey('rule_versions.id'),nullable=False,index=True)
 learner=Column(String(60),nullable=False,index=True)
 course=Column(String(40),nullable=False)
 event_id=Column(Integer,nullable=False)
 event_code=Column(String(60),nullable=False)
 occurred_at=Column(String(40),nullable=False)
 status=Column(String(20),nullable=False,default='active',index=True)
 created_at=Column(String(40),nullable=False)
 __table_args__=(UniqueConstraint('run_id','learner','course',name='uq_credit_run_learner_course'),)

class Certificate(Base):
 __tablename__='certificates'
 id=Column(Integer,primary_key=True)
 public_id=Column(String(64),unique=True,nullable=False)
 learner=Column(String(60),nullable=False,index=True)
 rule_version_id=Column(Integer,ForeignKey('rule_versions.id'),nullable=False,index=True)
 run_id=Column(Integer,ForeignKey('rule_runs.id'),nullable=False)
 status=Column(String(20),nullable=False,default='active',index=True)
 active_key=Column(String(120),unique=True,nullable=True)
 basis=Column(JSON,nullable=False)
 proof=Column(String(64),nullable=False)
 issued_at=Column(String(40),nullable=False)
 revoked_at=Column(String(40),nullable=True)
 revoked_by_run_id=Column(Integer,nullable=True)
 revoke_reason=Column(Text,default='')
 __table_args__=(UniqueConstraint('run_id','learner',name='uq_certificate_run_learner'),)


class SystemLock(Base):
 __tablename__='system_locks'
 id=Column(Integer,primary_key=True)
 name=Column(String(40),unique=True,nullable=False)
