"""端到端 API 流程：同一秒先修、迟到补录、撤证后恢复、中断恢复与幂等发证。"""


def ev(client, h, code, learner, when, course):
    r = client.post('/events', headers=h, json={'code': code, 'learner': learner, 'occurred_at': when, 'course': course})
    assert r.status_code == 200, r.text
    return r.json()


def rule(client, h, courses, prereqs, name='规则'):
    r = client.post('/rules', headers=h, json={'name': name, 'courses': courses, 'prerequisites': prereqs})
    assert r.status_code == 200, r.text
    return r.json()


def publish(client, h, vid):
    r = client.post(f'/rules/{vid}/publish', headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def process(client, h, rid):
    r = client.post(f'/runs/{rid}/process', headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_full_flow_same_second_late_record_revoke_restore(client, auth, push_jobs):
    # A and B in same second: B must not take credit
    ev(client, auth['analyst'], 'A-1', 'L1', '2026-09-02T01:00:00Z', '数据素养')
    ev(client, auth['analyst'], 'B-1', 'L1', '2026-09-02T01:00:00Z', '服务沟通')
    v1 = rule(client, auth['analyst'], ['数据素养', '服务沟通'], {'服务沟通': ['数据素养']})
    published = publish(client, auth['admin'], v1['id'])
    first = process(client, auth['analyst'], published['recompute_run_id'])
    row = first['result']['learners'][0]
    assert row['eligible'] is False and row['next_action'] == '服务沟通'
    assert any('同刻' in item['reason'] for item in row['early_records'])
    assert first['result']['summary']['late_records'] == 0

    # Late B after A -> qualifies, certificate issued
    ev(client, auth['analyst'], 'B-2', 'L1', '2026-09-03T01:00:00Z', '服务沟通')
    runs = client.get('/runs', headers=auth['analyst']).json()
    pending = [r for r in runs if r['state'] == 'pending']
    assert pending, 'event insert should enqueue a recompute'
    done = process(client, auth['analyst'], pending[0]['id'])
    assert done['result']['learners'][0]['eligible'] is True
    certs = client.get('/certificates', headers=auth['analyst']).json()
    active = [c for c in certs if c['status'] == 'active']
    assert len(active) == 1
    pid = active[0]['public_id']
    assert client.get(f'/verify/{pid}').json()['valid_now'] is True

    # Rule change invalidates; old certificate revoked but verifiable
    v2 = rule(client, auth['analyst'], ['数据素养', '服务沟通', '安全规范'], {'服务沟通': ['数据素养'], '安全规范': ['服务沟通']}, 'v2')
    pub2 = publish(client, auth['admin'], v2['id'])
    done2 = process(client, auth['analyst'], pub2['recompute_run_id'])
    assert done2['result']['learners'][0]['eligible'] is False
    old = client.get(f'/verify/{pid}').json()
    assert old['status'] == 'revoked' and old['valid_now'] is False
    assert old['proof_valid'] is True and old['history']

    # Learner catches up; new certificate; old remains inspectable
    ev(client, auth['analyst'], 'C-1', 'L1', '2026-09-04T01:00:00Z', '安全规范')
    pending = [r for r in client.get('/runs', headers=auth['analyst']).json() if r['state'] == 'pending']
    done3 = process(client, auth['analyst'], pending[0]['id'])
    assert done3['result']['learners'][0]['eligible'] is True
    active = [c for c in client.get('/certificates', headers=auth['analyst']).json() if c['status'] == 'active']
    assert len(active) == 1 and active[0]['public_id'] != pid
    assert client.get(f'/verify/{pid}').json()['status'] == 'revoked'

    # Idempotent: processing the same done run again does not issue another certificate
    assert process(client, auth['analyst'], pending[0]['id'])['state'] == 'done'
    assert len([c for c in client.get('/certificates', headers=auth['analyst']).json() if c['status'] == 'active']) == 1


def test_late_record_before_event_time_is_flagged(client, auth, push_jobs):
    from src.config.database import Session
    from src.models.entities import Event
    ev(client, auth['analyst'], 'A-1', 'L1', '2026-09-02T01:00:00Z', '数据素养')
    with Session() as s:
        e = s.query(Event).filter_by(code='A-1').one()
        e.recorded_at = '2026-09-01T00:00:00+00:00'  # recorded before it occurred
        s.commit()
    v = rule(client, auth['analyst'], ['数据素养'], {})
    pub = publish(client, auth['admin'], v['id'])
    done = process(client, auth['analyst'], pub['recompute_run_id'])
    assert done['result']['summary']['late_records'] == 1
    assert done['result']['late_records'][0]['event_code'] == 'A-1'


def test_retry_endpoint_after_failure(client, auth, push_jobs):
    ev(client, auth['analyst'], 'A-1', 'L1', '2026-09-02T01:00:00Z', '数据素养')
    v = rule(client, auth['analyst'], ['数据素养'], {})
    pub = publish(client, auth['admin'], v['id'])
    rid = pub['recompute_run_id']
    from src.config.database import Session
    from src.models.entities import RuleRun
    with Session() as s:
        r = s.get(RuleRun, rid)
        r.state = 'failed'
        r.error = '模拟核算中断'
        s.commit()
    assert client.post(f'/runs/{rid}/retry', headers=auth['analyst']).status_code == 200
    assert client.get(f'/runs/{rid}', headers=auth['analyst']).json()['state'] == 'pending'
    done = process(client, auth['analyst'], rid)
    assert done['state'] == 'done'
    assert done['attempt'] == 1
    assert len(client.get('/certificates', headers=auth['analyst']).json()) == 1


def test_event_recorded_after_run_created_invalidates_queued_run(client, auth, push_jobs):
    ev(client, auth['analyst'], 'A-1', 'L1', '2026-09-02T01:00:00Z', '数据素养')
    ev(client, auth['analyst'], 'B-1', 'L1', '2026-09-03T01:00:00Z', '服务沟通')
    v = rule(client, auth['analyst'], ['数据素养', '服务沟通'], {'服务沟通': ['数据素养']})
    pub = publish(client, auth['admin'], v['id'])
    stale = pub['recompute_run_id']
    # 任务创建后补录一条更早的“服务沟通”，按事件时刻应看 B 在前、A 在后
    ev(client, auth['analyst'], 'B-0', 'L1', '2026-09-01T01:00:00Z', '服务沟通')
    runs = client.get('/runs', headers=auth['analyst']).json()
    fresh = next(r['id'] for r in runs if r['state'] == 'pending')
    assert fresh != stale
    stale_done = process(client, auth['analyst'], stale)
    assert stale_done['state'] == 'superseded'
    assert '已变化' in stale_done['error']
    assert client.get('/certificates', headers=auth['analyst']).json() == []
    fresh_done = process(client, auth['analyst'], fresh)
    assert fresh_done['state'] == 'done'
    # 新任务基于含补录 B-0 的新快照：B-0 不追认，但 B-1 在 A 之后，仍可获学分，
    # 且证明旧任务没有把 B-0 计入。
    row = fresh_done['result']['learners'][0]
    assert row['eligible'] is True
    assert any(item['event_code'] == 'B-0' for item in row['early_records'])
    active = [c for c in client.get('/certificates', headers=auth['analyst']).json() if c['status'] == 'active']
    assert len(active) == 1
