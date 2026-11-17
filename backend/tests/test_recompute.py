from datetime import date

from src.config.database import Session
from src.models.entities import Certificate, Credit, RuleRun
from src.services.rules import (
    RuleConflict,
    create_rule,
    create_run,
    finalize_run,
    latest_published,
    publish_rule,
    recover_interrupted,
    retry_run,
)
from src.schemas.business import RuleInput


def seed_events(s, events):
    from src.models.entities import Event
    for i, (code, learner, when, course) in enumerate(events, start=1):
        s.add(Event(id=i, code=code, learner=learner, occurred_at=when, course=course))
    s.commit()


def new_version(s, courses, prereqs, name='规则'):
    v = create_rule(s, RuleInput(name=name, courses=courses, prerequisites=prereqs), 'analyst@test.local')
    return publish_rule(s, v, 'admin@test.local')


def test_interrupted_run_resumes_once_and_issues_one_certificate(schema):
    with Session() as s:
        seed_events(s, [
            ('A-1', 'L1', '2026-09-02T01:00:00Z', 'A'),
            ('B-1', 'L1', '2026-09-03T01:00:00Z', 'B'),
        ])
        v = new_version(s, ['A', 'B'], {'B': ['A']})
        run = create_run(s, v, date(2026, 11, 14))
        run.state = 'running'
        run.phase = '重放历史'
        run.attempt = 1
        s.commit()
        assert recover_interrupted(s) == 1
        resumed = s.get(RuleRun, run.id)
        assert resumed.state == 'pending'
        done = finalize_run(s, run.id)
        assert done.state == 'done'
        assert done.attempt == 2
        certs = s.query(Certificate).all()
        assert len(certs) == 1
        # Re-processing a done run is a no-op, no duplicate certificate
        again = finalize_run(s, run.id)
        assert again.state == 'done'
        assert s.query(Certificate).count() == 1


def test_double_claim_rejected(schema):
    with Session() as s:
        seed_events(s, [('A-1', 'L1', '2026-09-02T01:00:00Z', 'A')])
        v = new_version(s, ['A'], {})
        run = create_run(s, v, date(2026, 11, 14))
        run.state = 'running'
        s.commit()
        try:
            finalize_run(s, run.id)
        except RuleConflict as exc:
            assert '核算中' in str(exc)
        else:
            raise AssertionError('expected RuleConflict')


def test_stale_run_never_issues_certificate_after_event_correction(schema):
    with Session() as s:
        seed_events(s, [
            ('A-1', 'L1', '2026-09-02T01:00:00Z', 'A'),
            ('B-1', 'L1', '2026-09-03T01:00:00Z', 'B'),
        ])
        v = new_version(s, ['A', 'B'], {'B': ['A']})
        stale = create_run(s, v, date(2026, 11, 14))
        from src.models.entities import Event
        event = s.query(Event).filter_by(code='B-1').first()
        event.occurred_at = '2026-09-01T01:00:00Z'  # B now precedes A -> not credited
        s.commit()
        result = finalize_run(s, stale.id)
        assert result.state == 'superseded'
        assert s.query(Certificate).count() == 0
        assert s.query(Credit).count() == 0


def test_older_cutoff_replay_is_historical_and_does_not_touch_certificate(schema):
    with Session() as s:
        seed_events(s, [
            ('A-1', 'L1', '2026-09-02T01:00:00Z', 'A'),
            ('B-1', 'L1', '2026-09-03T01:00:00Z', 'B'),
        ])
        v = new_version(s, ['A', 'B'], {'B': ['A']})
        late = finalize_run(s, create_run(s, v, date(2026, 12, 1)).id)
        assert late.state == 'done'
        assert late.result['certificate_policy'] == 'authoritative'
        cert = s.query(Certificate).filter_by(status='active').one()
        public_id = cert.public_id

        early = finalize_run(s, create_run(s, v, date(2026, 11, 14)).id)
        assert early.state == 'done'
        assert early.result['certificate_policy'] == 'historical_only'
        assert '不改动证书' in early.result['certificate_policy_note']
        cert_now = s.query(Certificate).filter_by(status='active').one()
        assert cert_now.public_id == public_id
        assert s.query(Certificate).count() == 1


def test_retry_failed_run_keeps_progress_and_idempotent_certificate(schema):
    with Session() as s:
        seed_events(s, [
            ('A-1', 'L1', '2026-09-02T01:00:00Z', 'A'),
            ('B-1', 'L1', '2026-09-03T01:00:00Z', 'B'),
        ])
        v = new_version(s, ['A', 'B'], {'B': ['A']})
        run = create_run(s, v, date(2026, 11, 14))
        run.state = 'failed'
        run.phase = '核算失败'
        run.error = '模拟中断'
        run.attempt = 1
        s.commit()
        retried = retry_run(s, run, 'analyst@test.local')
        assert retried.state == 'pending' and retried.attempt == 1
        done = finalize_run(s, run.id)
        assert done.state == 'done'
        certs = s.query(Certificate).all()
        assert len(certs) == 1
        # retry on done is a no-op and does not re-issue
        assert retry_run(s, done, 'analyst@test.local').state == 'done'
        assert s.query(Certificate).count() == 1


def test_duplicate_run_reuses_pending_job(schema):
    with Session() as s:
        seed_events(s, [('A-1', 'L1', '2026-09-02T01:00:00Z', 'A')])
        v = new_version(s, ['A'], {})
        first = create_run(s, v, date(2026, 11, 14))
        second = create_run(s, v, date(2026, 11, 14))
        assert first.id == second.id
        assert s.query(RuleRun).count() == 1


def test_credit_proof_chain_in_certificate_basis(schema):
    with Session() as s:
        seed_events(s, [
            ('A-1', 'L1', '2026-09-02T01:00:00Z', 'A'),
            ('B-1', 'L1', '2026-09-03T01:00:00Z', 'B'),
        ])
        v = new_version(s, ['A', 'B'], {'B': ['A']})
        done = finalize_run(s, create_run(s, v, date(2026, 11, 14)).id)
        cert = s.query(Certificate).filter_by(status='active').one()
        basis = cert.basis
        assert basis['rule_version_id'] == v.id
        assert basis['rule_checksum'] == v.checksum
        b = next(c for c in basis['credits'] if c['course'] == 'B')
        assert b['prerequisites'][0]['course'] == 'A'
        assert b['prerequisites'][0]['event_code'] == 'A-1'
        assert done.result['summary']['eligible'] == 1
        assert cert.proof


def test_replay_failure_marks_failed_then_retry_succeeds(schema, monkeypatch):
    from datetime import date as _date
    import src.services.rules as rules_mod

    with Session() as s:
        seed_events(s, [('A-1', 'L1', '2026-09-02T01:00:00Z', 'A')])
        v = new_version(s, ['A'], {})
        run = create_run(s, v, _date(2026, 11, 14))
        original = rules_mod.replay

        def boom(*a, **k):
            raise RuntimeError('模拟重放中断')

        monkeypatch.setattr(rules_mod, 'replay', boom)
        try:
            finalize_run(s, run.id)
        except RuntimeError:
            pass
        s.expire_all()
        failed = s.get(RuleRun, run.id)
        assert failed.state == 'failed'
        assert '模拟重放中断' in failed.error
        assert s.query(Certificate).count() == 0

        monkeypatch.setattr(rules_mod, 'replay', original)
        retried = retry_run(s, failed, 'analyst@test.local')
        done = finalize_run(s, retried.id)
        assert done.state == 'done'
        assert s.query(Certificate).count() == 1
