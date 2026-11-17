from datetime import date


def make_event(client, headers, code, learner, when, course):
    r = client.post('/events', headers=headers, json={
        'code': code, 'learner': learner, 'occurred_at': when, 'course': course,
    })
    assert r.status_code == 200, r.text
    return r.json()


def create_rule(client, headers, courses, prereqs):
    r = client.post('/rules', headers=headers, json={
        'name': '课程前置规则', 'courses': courses, 'prerequisites': prereqs,
    })
    return r


def publish_and_run(client, admin, analyst, version_id, as_of='2026-11-14'):
    p = client.post(f'/rules/{version_id}/publish', headers=admin)
    assert p.status_code == 200, p.text
    rid = p.json().get('recompute_run_id')
    if rid is None:
        run = client.post('/runs', headers=analyst, json={'rule_version_id': version_id, 'as_of': as_of})
        assert run.status_code == 200, run.text
        rid = run.json()['id']
    done = client.post(f'/runs/{rid}/process', headers=analyst)
    assert done.status_code == 200, done.text
    assert done.json()['state'] == 'done', done.text
    return done.json()


def test_rule_cycle_rejected(client, auth):
    r = create_rule(client, auth['analyst'], ['A', 'B', 'C'], {'A': ['B'], 'B': ['C'], 'C': ['A']})
    assert r.status_code == 422
    assert '成环' in r.json()['detail']
    r = create_rule(client, auth['analyst'], ['A', 'B'], {'A': ['B', 'B']})
    assert r.status_code == 422


def test_credit_requires_strictly_earlier_prerequisite(client, auth, push_jobs):
    # B at 09:00, A at 10:00 same day; then B again 11:00 and C 12:00
    make_event(client, auth['analyst'], 'B-early', 'L1', '2026-09-01T01:00:00Z', 'B')
    make_event(client, auth['analyst'], 'A-1', 'L1', '2026-09-01T02:00:00Z', 'A')
    make_event(client, auth['analyst'], 'B-2', 'L1', '2026-09-01T03:00:00Z', 'B')
    make_event(client, auth['analyst'], 'C-1', 'L1', '2026-09-01T04:00:00Z', 'C')
    v = create_rule(client, auth['analyst'], ['A', 'B', 'C'], {'B': ['A'], 'C': ['A', 'B']}).json()
    run = publish_and_run(client, auth['admin'], auth['analyst'], v['id'])
    assert run['state'] == 'done'
    row = next(x for x in run['result']['learners'] if x['learner'] == 'L1')
    assert row['eligible'] is True
    assert row['completed'] == ['A', 'B', 'C']
    assert any(e['course'] == 'B' and e['event_code'] == 'B-early' for e in row['early_records'])
    # B-early was not credited; credited B is B-2 with A proof
    b = next(c for c in row['credits'] if c['course'] == 'B')
    assert b['event_code'] == 'B-2'
    assert [p['course'] for p in b['prerequisites']] == ['A']


def test_same_moment_is_not_prerequisite(client, auth, push_jobs):
    make_event(client, auth['analyst'], 'A-1', 'L1', '2026-09-02T01:00:00Z', 'A')
    make_event(client, auth['analyst'], 'B-1', 'L1', '2026-09-02T01:00:00Z', 'B')
    v = create_rule(client, auth['analyst'], ['A', 'B'], {'B': ['A']}).json()
    run = publish_and_run(client, auth['admin'], auth['analyst'], v['id'])
    row = run['result']['learners'][0]
    assert row['completed'] == ['A']
    assert row['eligible'] is False
    assert row['next_action'] == 'B'
    assert any('同刻' in e['reason'] for e in row['early_records'])
    assert run['result']['summary']['eligible'] == 0


def test_missing_items_and_study_plan(client, auth, push_jobs):
    make_event(client, auth['analyst'], 'A-1', 'L1', '2026-09-02T01:00:00Z', 'A')
    make_event(client, auth['analyst'], 'C-1', 'L1', '2026-09-02T02:00:00Z', 'C')
    v = create_rule(client, auth['analyst'], ['A', 'B', 'C'], {'B': ['A'], 'C': ['B']}).json()
    run = publish_and_run(client, auth['admin'], auth['analyst'], v['id'])
    row = run['result']['learners'][0]
    assert [m['course'] for m in row['missing']] == ['B', 'C']
    assert row['next_action'] == 'B'
    plan = row['plan']
    assert plan[0]['course'] == 'B' and plan[0]['can_start_now'] is True
    assert plan[1]['course'] == 'C' and plan[1]['requires_first'] == ['B']
    assert plan[1]['can_start_now'] is False and plan[1]['start_after_step'] == 1


def test_certificate_issue_revoke_and_verify(client, auth, push_jobs):
    make_event(client, auth['analyst'], 'A-1', 'L1', '2026-09-02T01:00:00Z', 'A')
    make_event(client, auth['analyst'], 'B-1', 'L1', '2026-09-03T01:00:00Z', 'B')
    v1 = create_rule(client, auth['analyst'], ['A', 'B'], {'B': ['A']}).json()
    run1 = publish_and_run(client, auth['admin'], auth['analyst'], v1['id'])
    certs = client.get('/certificates', headers=auth['analyst']).json()
    assert len(certs) == 1 and certs[0]['status'] == 'active'
    public_id = certs[0]['public_id']
    assert client.get(f'/verify/{public_id}').json()['valid_now'] is True

    # Rule v2 adds C as prerequisite of B; learner no longer qualifies -> revoke
    v2 = create_rule(client, auth['analyst'], ['A', 'B', 'C'], {'B': ['A', 'C']}).json()
    run2 = publish_and_run(client, auth['admin'], auth['analyst'], v2['id'], '2026-11-30')
    assert run2['state'] == 'done'
    verified = client.get(f'/verify/{public_id}').json()
    assert verified['status'] == 'revoked' and verified['valid_now'] is False
    assert '不再满足' in verified['revoke_reason']

    # Learner completes C and re-takes B after C -> new certificate; old still verifiable
    make_event(client, auth['analyst'], 'C-1', 'L1', '2026-11-20T01:00:00Z', 'C')
    make_event(client, auth['analyst'], 'B-2', 'L1', '2026-11-21T01:00:00Z', 'B')
    run3 = client.post('/runs', headers=auth['analyst'], json={'rule_version_id': v2['id'], 'as_of': '2026-12-15'}).json()
    assert run3['state'] == 'pending'
    done3 = client.post(f"/runs/{run3['id']}/process", headers=auth['analyst']).json()
    assert done3['state'] == 'done', done3
    certs = client.get('/certificates', headers=auth['analyst']).json()
    active = [c for c in certs if c['status'] == 'active']
    assert len(active) == 1 and active[0]['public_id'] != public_id
    assert client.get(f'/verify/{public_id}').json()['valid_now'] is False
    assert client.get(f"/verify/{active[0]['public_id']}").json()['valid_now'] is True


def test_event_correction_creates_new_run_and_invalidates_old(client, auth, push_jobs):
    make_event(client, auth['analyst'], 'A-1', 'L1', '2026-09-02T01:00:00Z', 'A')
    ev = make_event(client, auth['analyst'], 'B-1', 'L1', '2026-09-03T01:00:00Z', 'B')
    v = create_rule(client, auth['analyst'], ['A', 'B'], {'B': ['A']}).json()
    run1 = publish_and_run(client, auth['admin'], auth['analyst'], v['id'])
    assert run1['result']['learners'][0]['eligible'] is True
    # Correct B to be before A
    edit = client.put(f"/events/{ev['id']}", headers=auth['analyst'], json={
        'code': 'B-1', 'learner': 'L1', 'occurred_at': '2026-09-01T01:00:00Z', 'course': 'B',
    })
    assert edit.status_code == 200, edit.text
    new_run_id = edit.json()['recompute_run_id']
    assert new_run_id
    done = client.post(f'/runs/{new_run_id}/process', headers=auth['analyst']).json()
    assert done['state'] == 'done'
    assert done['result']['learners'][0]['eligible'] is False
    certs = client.get('/certificates', headers=auth['analyst']).json()
    assert all(c['status'] != 'active' for c in certs)


def test_viewer_cannot_publish_or_run(client, auth):
    r = create_rule(client, auth['analyst'], ['A'], {})
    v = r.json()
    assert client.post(f"/rules/{v['id']}/publish", headers=auth['viewer']).status_code == 403
    assert client.post('/runs', headers=auth['viewer'], json={}).status_code == 403


def test_rule_version_immutable_after_publish(client, auth, push_jobs):
    make_event(client, auth['analyst'], 'A-1', 'L1', '2026-09-02T01:00:00Z', 'A')
    v1 = create_rule(client, auth['analyst'], ['A'], {}).json()
    client.post(f"/rules/{v1['id']}/publish", headers=auth['admin'])
    v2 = create_rule(client, auth['analyst'], ['A', 'B'], {'B': ['A']}).json()
    client.post(f"/rules/{v2['id']}/publish", headers=auth['admin'])
    rules = {r['id']: r for r in client.get('/rules', headers=auth['analyst']).json()}
    assert rules[v1['id']]['status'] == 'archived'
    assert rules[v2['id']]['status'] == 'published'
    # old archived rule can still be inspected for certificate provenance
    old = client.get(f"/rules/{v1['id']}", headers=auth['analyst']).json()
    assert old['checksum'] == v1['checksum']
