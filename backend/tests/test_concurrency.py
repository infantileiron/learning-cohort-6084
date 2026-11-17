"""并发语义在 SQLite 上只能模拟到逻辑层：验证原子抢占与幂等约束。

真实 MySQL 行锁（SELECT ... FOR UPDATE）由部署环境提供；这里覆盖：
- 两个核算入口同时尝试同一任务，只有一个成功认领；
- 同一学员同一 run 的证书唯一键阻止重复发证。
"""
from datetime import date

from sqlalchemy.exc import IntegrityError

from src.config.database import Session
from src.models.entities import Certificate, RuleRun
from src.services.rules import RuleConflict, claim_run, create_run, finalize_run
from src.services.rules import create_rule, publish_rule
from src.schemas.business import RuleInput

from tests.test_recompute import seed_events


def test_claim_is_atomic(schema):
    with Session() as s:
        seed_events(s, [('A-1', 'L1', '2026-09-02T01:00:00Z', 'A')])
        v = create_rule(s, RuleInput(name='规则', courses=['A'], prerequisites={}), 'a')
        publish_rule(s, v, 'a')
        run = create_run(s, v, date(2026, 11, 14))
        claimed = claim_run(s, s.get(RuleRun, run.id))
        assert claimed.state == 'running'
        try:
            claim_run(s, s.get(RuleRun, run.id))
        except RuleConflict as exc:
            assert '不能重复核算' in str(exc)
        else:
            raise AssertionError('second claim should fail')


def test_claim_then_finalize_rejects_in_progress(schema):
    with Session() as s:
        seed_events(s, [('A-1', 'L1', '2026-09-02T01:00:00Z', 'A')])
        v = create_rule(s, RuleInput(name='规则', courses=['A'], prerequisites={}), 'a')
        publish_rule(s, v, 'a')
        run = create_run(s, v, date(2026, 11, 14))
        claim_run(s, s.get(RuleRun, run.id))
        try:
            finalize_run(s, run.id)
        except RuleConflict as exc:
            assert '核算中' in str(exc)
        else:
            raise AssertionError('expected conflict')
        assert s.query(Certificate).count() == 0


def test_certificate_unique_per_run_learner(schema):
    with Session() as s:
        seed_events(s, [
            ('A-1', 'L1', '2026-09-02T01:00:00Z', 'A'),
            ('B-1', 'L1', '2026-09-03T01:00:00Z', 'B'),
        ])
        v = create_rule(s, RuleInput(name='规则', courses=['A', 'B'], prerequisites={'B': ['A']}), 'a')
        publish_rule(s, v, 'a')
        run = create_run(s, v, date(2026, 11, 14))
        done = finalize_run(s, run.id)
        assert done.state == 'done'
        first = s.query(Certificate).one()
        # 重复处理同一任务不新增证书
        finalize_run(s, run.id)
        assert s.query(Certificate).count() == 1
        # 数据库唯一键兜底：同一 run 同一学员不能再插入
        s.add(Certificate(public_id='CERT-DUPLICATE', learner='L1', rule_version_id=v.id, run_id=run.id,
                          status='active', active_key='L1-dup', basis={}, proof='x', issued_at='now'))
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
        else:
            raise AssertionError('unique constraint should reject duplicate certificate')
        assert first.public_id != 'CERT-DUPLICATE'
