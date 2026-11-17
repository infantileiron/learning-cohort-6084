from datetime import date

from src.config.database import Session
from src.models.entities import Certificate, RuleRun
from src.services.rules import create_run, finalize_run
from src.schemas.business import RuleInput

from tests.test_recompute import new_version, seed_events


def test_archived_rule_can_be_replayed_for_audit_without_certificate_authority(schema):
    with Session() as s:
        seed_events(s, [
            ('A-1', 'L1', '2026-09-02T01:00:00Z', 'A'),
            ('B-1', 'L1', '2026-09-03T01:00:00Z', 'B'),
        ])
        v1 = new_version(s, ['A', 'B'], {'B': ['A']})
        done1 = finalize_run(s, create_run(s, v1, date(2026, 11, 14)).id)
        assert done1.state == 'done'
        cert = s.query(Certificate).filter_by(status='active').one()
        public_id = cert.public_id
        v2 = new_version(s, ['A', 'B', 'C'], {'B': ['A'], 'C': ['B']})
        # v1 is now archived; replaying it must still work for historical audit
        historical = create_run(s, v1, date(2026, 11, 20))
        done = finalize_run(s, historical.id)
        assert done.state == 'done'
        assert done.result['certificate_policy'] == 'historical_only'
        assert s.query(Certificate).filter_by(public_id=public_id).one().status == 'active'
