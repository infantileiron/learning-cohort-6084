"""课程前置规则发布、按版本重放、证书签发与核查。"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import secrets

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from src.models.entities import Certificate, Credit, Event, RuleRun, RuleVersion, SystemLock
from src.services.business import dump
from src.services.replay import (
    RuleError,
    digest,
    normalize_rule,
    replay,
    rule_checksum,
)


class RuleConflict(Exception):
    pass


def lock_ledger(s) -> None:
    """全局账本锁（SELECT ... FOR UPDATE），事件更正、规则发布、颁证互斥。

    必须是事务里的第一条加锁读取：后续普通读取的快照在锁之后建立，
    才能保证读到的是持锁时刻的最新已提交状态。
    """
    row = s.query(SystemLock).filter(SystemLock.name == 'ledger').with_for_update().first()
    if row is not None:
        return
    s.rollback()
    s.add(SystemLock(name='ledger'))
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
    s.query(SystemLock).filter(SystemLock.name == 'ledger').with_for_update().first()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def today_shanghai() -> date:
    return datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date()


def latest_published(s):
    return (
        s.query(RuleVersion)
        .filter(RuleVersion.status == 'published')
        .order_by(RuleVersion.published_at.desc(), RuleVersion.id.desc())
        .first()
    )


def events_snapshot(s) -> list:
    return [
        {
            'id': row.id,
            'code': row.code,
            'learner': row.learner,
            'occurred_at': row.occurred_at,
            'course': row.course,
            'recorded_at': row.recorded_at,
        }
        for row in s.query(Event).order_by(Event.id).all()
    ]


def run_snapshot(s, version: RuleVersion, as_of: date) -> dict:
    normalized = normalize_rule(version.courses, version.prerequisites or {})
    return {
        'rule_version_id': version.id,
        'rule_name': version.name,
        'rule': {
            'courses': normalized['courses'],
            'prerequisites': {
                course: list(normalized['prerequisites'].get(course, []))
                for course in normalized['courses']
            },
            'checksum': version.checksum,
        },
        'events': events_snapshot(s),
        'as_of': as_of.isoformat(),
    }


def input_checksum(snapshot: dict) -> str:
    return digest(snapshot)


# ---------- 规则版本 ----------

def create_rule(s, data, actor: str) -> RuleVersion:
    normalized = normalize_rule(data.courses, data.prerequisites or {})
    checksum = rule_checksum(normalized)
    duplicate = s.query(RuleVersion).filter(RuleVersion.checksum == checksum).first()
    if duplicate:
        raise RuleConflict(f'相同课程前置关系已存在，规则版本 #{duplicate.id}')
    version = RuleVersion(
        name=data.name,
        status='draft',
        courses=normalized['courses'],
        prerequisites=normalized['prerequisites'],
        checksum=checksum,
        current_key=None,
        created_at=now_iso(),
    )
    s.add(version)
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        raise RuleConflict('规则版本冲突，请重试')
    return version


def publish_rule(s, version: int | RuleVersion, actor: str) -> RuleVersion:
    """发布规则：先取账本锁，再锁版本行，保证归档旧版与入队重算的次序一致。"""
    lock_ledger(s)
    version_id = version.id if isinstance(version, RuleVersion) else version
    version = s.get(RuleVersion, version_id, with_for_update=True)
    if version is None:
        raise RuleConflict('规则版本不存在')
    normalized = normalize_rule(version.courses, version.prerequisites or {})
    version.courses = normalized['courses']
    version.prerequisites = normalized['prerequisites']
    version.checksum = rule_checksum(normalized)
    if version.status == 'published':
        return version
    version.status = 'published'
    version.published_at = now_iso()
    version.published_by = actor
    # 同一时刻只保留一个当前发布版本；旧版本归档，仍用于旧凭证溯源与历史核查。
    s.query(RuleVersion).filter(
        RuleVersion.id != version.id,
        RuleVersion.status == 'published',
    ).update({'status': 'archived', 'current_key': None}, synchronize_session=False)
    s.query(RuleVersion).filter(
        RuleVersion.id != version.id, RuleVersion.current_key == 'current'
    ).update({'current_key': None}, synchronize_session=False)
    version.current_key = 'current'
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        raise RuleConflict('发布冲突，请重试')
    return version


# ---------- 重算任务 ----------

def find_reusable_run(s, version_id: int, as_of: date, checksum: str):
    return (
        s.query(RuleRun)
        .filter(
            RuleRun.rule_version_id == version_id,
            RuleRun.as_of == as_of.isoformat(),
            RuleRun.input_checksum == checksum,
            RuleRun.state.in_(['pending', 'running', 'done']),
        )
        .order_by(RuleRun.id.desc())
        .first()
    )


def create_run(s, version: RuleVersion, as_of: date | None = None, reason: str = 'manual', actor: str = 'system') -> RuleRun:
    if version.status not in ('published', 'archived'):
        raise RuleConflict('只有已发布（或已归档）的规则版本可以重放核算')
    if as_of is None:
        as_of = today_shanghai()
    snapshot = run_snapshot(s, version, as_of)
    checksum = input_checksum(snapshot)
    reusable = find_reusable_run(s, version.id, as_of, checksum)
    if reusable:
        return reusable
    run = RuleRun(
        rule_version_id=version.id,
        state='pending',
        as_of=as_of.isoformat(),
        input_checksum=checksum,
        input=snapshot,
        attempt=0,
        progress=0,
        total=len({event['learner'] for event in snapshot['events'] if event.get('learner')}),
        phase='待核算',
        trigger_reason=reason,
        triggered_by=actor,
        created_at=now_iso(),
    )
    s.add(run)
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        raise RuleConflict('重算任务创建冲突，请重试')
    return run


def retry_run(s, run: RuleRun, actor: str) -> RuleRun:
    if run.state == 'done':
        return run
    if run.state == 'running':
        raise RuleConflict('重算任务正在核算中，请等待完成后再决定是否重试')
    if run.state == 'superseded':
        raise RuleConflict('该重算已作废，请用当前发布版本新建重算')
    version = s.get(RuleVersion, run.rule_version_id)
    if not version or version.status not in ('published', 'archived'):
        raise RuleConflict('规则版本已不可用，请用当前发布版本新建重算')
    as_of = date.fromisoformat(run.as_of)
    snapshot = run_snapshot(s, version, as_of)
    run.input = snapshot
    run.input_checksum = input_checksum(snapshot)
    run.state = 'pending'
    run.phase = '待核算'
    run.progress = 0
    run.total = len({event['learner'] for event in snapshot['events'] if event.get('learner')})
    run.error = ''
    run.result = None
    run.started_at = None
    run.finished_at = None
    run.triggered_by = actor
    s.commit()
    return run


# ---------- 核算与颁证 ----------

def _ensure_certificate(s, run: RuleRun, learner: str, credits: list, basis_common: dict) -> Certificate | None:
    basis = {
        **basis_common,
        'learner': learner,
        'credits': [
            {
                'course': credit['course'],
                'event_code': credit['event_code'],
                'event_id': credit['event_id'],
                'occurred_at': credit['occurred_at'],
                'prerequisites': credit['prerequisites'],
            }
            for credit in credits
        ],
    }
    proof = digest(basis)
    # 达标状态未翻转时保留原证：证书记录“签发当时”的规则与学分证据，
    # 不因每次重算而改号；只有失格才撤销，恢复达标后再另发新证。
    existing = (
        s.query(Certificate)
        .filter(Certificate.learner == learner, Certificate.status == 'active')
        .order_by(Certificate.id.desc())
        .first()
    )
    if existing and existing.rule_version_id == run.rule_version_id:
        return existing
    if existing:
        existing.status = 'superseded'
        existing.active_key = None
        existing.revoked_at = now_iso()
        existing.revoke_reason = f'规则换版至 v{run.rule_version_id} 且仍达标，凭新依据另发新证；旧证保留可核查'
        s.flush()
    certificate = Certificate(
        public_id='CERT-' + secrets.token_hex(12).upper(),
        learner=learner,
        rule_version_id=run.rule_version_id,
        run_id=run.id,
        status='active',
        active_key=learner,
        basis=basis,
        proof=proof,
        issued_at=now_iso(),
    )
    s.add(certificate)
    s.flush()
    return certificate


def _revoke_active(s, run: RuleRun, keep_learners: set, reason: str):
    active = s.query(Certificate).filter(Certificate.status == 'active').all()
    for certificate in active:
        if certificate.learner in keep_learners:
            continue
        certificate.status = 'revoked'
        certificate.active_key = None
        certificate.revoked_at = now_iso()
        certificate.revoked_by_run_id = run.id
        certificate.revoke_reason = reason
    s.flush()


def claim_run(s, run: RuleRun) -> RuleRun:
    """原子抢占待核算任务；并发调用只有一个能把 pending 改为 running。"""
    claimed = (
        s.query(RuleRun)
        .filter(RuleRun.id == run.id, RuleRun.state == 'pending')
        .update(
            {
                'state': 'running',
                'phase': '重放历史',
                'started_at': now_iso(),
                'attempt': (run.attempt or 0) + 1,
                'progress': 0,
                'error': '',
            },
            synchronize_session=False,
        )
    )
    s.commit()
    s.expire_all()
    current = s.get(RuleRun, run.id)
    if claimed != 1:
        raise RuleConflict(f'重算任务 #{run.id} 当前状态为 {current.state if current else "缺失"}，不能重复核算')
    return current


def recover_interrupted(s) -> int:
    """把崩溃时停留在 running 的任务退回待核算；由单 worker 启动时调用。"""
    count = (
        s.query(RuleRun)
        .filter(RuleRun.state == 'running')
        .update({'state': 'pending', 'phase': '待核算（中断恢复）', 'progress': 0}, synchronize_session=False)
    )
    s.commit()
    s.expire_all()
    return count


def _later_authoritative_run(s, run: RuleRun):
    """同一规则版本中比本任务更晚（截至日更大，或同截至日但更晚创建）的有效重算。

    跨版本的新旧关系由“当前发布版本”判断，不能让归档版本的运行阻塞现行版本颁证。
    """
    return (
        s.query(RuleRun)
        .filter(
            RuleRun.id != run.id,
            RuleRun.rule_version_id == run.rule_version_id,
            RuleRun.state.in_(['pending', 'running', 'done']),
            (RuleRun.as_of > run.as_of)
            | ((RuleRun.as_of == run.as_of) & (RuleRun.id > run.id)),
        )
        .order_by(RuleRun.as_of.desc(), RuleRun.id.desc())
        .first()
    )


def _valid_input(s, run: RuleRun, version: RuleVersion) -> bool:
    if version is None or version.status not in ('published', 'archived'):
        return False
    try:
        snapshot = run_snapshot(s, version, date.fromisoformat(run.as_of))
    except RuleError:
        return False
    return input_checksum(snapshot) == run.input_checksum


def _mark_superseded(s, run: RuleRun, reason: str, newer: RuleRun | None = None) -> RuleRun:
    run.state = 'superseded'
    run.phase = '已作废'
    run.superseded_by = newer.id if newer else None
    run.error = reason
    run.finished_at = now_iso()
    s.commit()
    return run


def finalize_run(s, run_id: int, progress_callback=None) -> RuleRun:
    """重放 + 单事务“复核新鲜度 → 写学分 → 颁证/撤证 → 置完成”。

    重放使用任务创建时冻结的输入快照（run.input）；真正的写入前再拿账本锁并
    从数据库重新计算输入校验和。任何创建后的事件更正、规则切换或并发新任务
    都会使本任务作废，绝不把基于失效学分的证书发出去。
    """
    run = s.get(RuleRun, run_id)
    if run is None:
        raise RuleConflict('重算任务不存在')
    if run.state == 'done':
        return run
    if run.state not in ('pending', 'running'):
        raise RuleConflict(f'重算任务当前状态为 {run.state}，不能核算；失败任务请先重试')
    if run.state == 'running':
        raise RuleConflict('重算任务已在核算中，请勿重复执行')
    run = claim_run(s, run)
    version = s.get(RuleVersion, run.rule_version_id)
    if not _valid_input(s, run, version):
        return _mark_superseded(s, run, '创建任务后规则或事件已变化，本任务不签发失效学分证书，请重新核算')

    snapshot = run.input
    last_mark = [0]

    def report(done: int, total: int):
        # 进度写回：重放与写入在同一会话中，用短提交把进度报给前端。
        # 后续写入仍在账本锁内并会重新核验输入，进度提交不影响证书一致性。
        if progress_callback is not None:
            progress_callback(done, total)
            return
        if done != total and done - last_mark[0] < max(1, total // 10):
            return
        last_mark[0] = done
        row = s.get(RuleRun, run_id)
        if row is not None and row.state == 'running':
            row.progress = done
            row.total = total
            s.commit()

    try:
        result = replay(snapshot['rule'], snapshot['events'], date.fromisoformat(run.as_of), report)
    except Exception as exc:
        # 单次核算失败要落为可查、可重试的 failed，而不是把任务永久留在 running。
        s.rollback()
        row = s.get(RuleRun, run_id)
        if row and row.state in ('pending', 'running'):
            row.state = 'failed'
            row.phase = '核算失败'
            row.error = str(exc) or exc.__class__.__name__
            row.finished_at = now_iso()
            s.commit()
        raise

    try:
        # 写入边界：先结束重放前的只读事务（否则 MySQL REPEATABLE READ 会沿用旧快照），
        # 再取账本锁，然后从持锁时刻的已提交状态重新读取并核验输入。
        s.rollback()
        lock_ledger(s)
        s.expire_all()
        run = s.get(RuleRun, run_id)
        version = s.get(RuleVersion, run.rule_version_id)
        if not _valid_input(s, run, version):
            return _mark_superseded(s, run, '核算期间规则或事件变化，已放弃颁证，请以最新重算为准')

        basis_common = {
            'rule_version_id': version.id,
            'rule_name': version.name,
            'rule_checksum': version.checksum,
            'run_id': run.id,
            'input_checksum': run.input_checksum,
            'as_of': run.as_of,
            'prerequisites': version.prerequisites or {},
        }
        # 证书只由“当前发布版本 + 最新截至日”的重算改写；其余重算仅作历史快照，
        # 不得撤销或改写基于更晚事实签发的凭证。
        later = _later_authoritative_run(s, run)
        if version.current_key != 'current':
            authoritative = False
            policy_note = '该规则版本已归档，本次仅作历史回看，不改动证书'
        elif later:
            authoritative = False
            policy_note = f'存在更晚或并行的重算 #{later.id}（截至 {later.as_of}），本次仅作历史回看，不改动证书'
        else:
            authoritative = True
            policy_note = '本次重算为证书权威来源'
        result['certificate_policy'] = 'authoritative' if authoritative else 'historical_only'
        result['certificate_policy_note'] = policy_note
        if authoritative:
            eligible = {row['learner'] for row in result['learners'] if row['eligible']}
            _revoke_active(
                s,
                run,
                eligible,
                f'规则版本 #{version.id}《{version.name}》按截至 {run.as_of} 重算后不再满足全部课程前置，原证撤销',
            )
        s.query(Credit).filter(Credit.run_id == run.id).delete(synchronize_session=False)
        for row in result['learners']:
            for credit in row['credits']:
                s.add(Credit(
                    run_id=run.id,
                    rule_version_id=run.rule_version_id,
                    learner=row['learner'],
                    course=credit['course'],
                    event_id=credit['event_id'],
                    event_code=credit['event_code'],
                    occurred_at=credit['occurred_at'],
                    status='active',
                    created_at=now_iso(),
                ))
        if authoritative:
            s.query(Credit).filter(
                Credit.rule_version_id == run.rule_version_id,
                Credit.run_id != run.id,
            ).update({'status': 'superseded'}, synchronize_session=False)
            for row in result['learners']:
                if row['eligible']:
                    _ensure_certificate(s, run, row['learner'], row['credits'], basis_common)
        run.result = result
        run.state = 'done'
        run.phase = '核算完成'
        run.progress = run.total
        run.finished_at = now_iso()
        run.error = ''
        s.commit()
        return run
    except Exception as exc:
        s.rollback()
        row = s.get(RuleRun, run_id)
        if row is not None and row.state == 'running':
            row.state = 'failed'
            row.phase = '核算失败'
            row.error = str(exc) or exc.__class__.__name__
            row.finished_at = now_iso()
            s.commit()
        raise


def default_cutoff(s) -> date:
    """自动重算沿用最近一次约定的截至日；没有历史重算时才用今天。

    截至日是“已完整收集至该业务日结束”的人为约定，不能因为补录/更正就
    自动把半天的今天当成完整日；需要扩大观察范围时由用户显式提交新的截至日。
    """
    latest = s.query(func.max(RuleRun.as_of)).scalar()
    return date.fromisoformat(latest) if latest else today_shanghai()


def enqueue_recompute(s, reason: str, actor: str):
    """规则发布或事件更正后自动入队重算；无已发布规则时返回 None。

    重复调用是幂等的：相同规则版本、截至日与输入校验和会复用未完成任务，
    不会重复生成任务或证书。
    """
    version = latest_published(s)
    if not version:
        return None
    return create_run(s, version, default_cutoff(s), reason, actor)


# ---------- 查询 ----------

def verify_certificate(s, public_id: str) -> dict:
    certificate = s.query(Certificate).filter(Certificate.public_id == public_id).first()
    if not certificate:
        return {'found': False, 'public_id': public_id}
    version = s.get(RuleVersion, certificate.rule_version_id)
    history = (
        s.query(Certificate)
        .filter(Certificate.learner == certificate.learner)
        .order_by(Certificate.id)
        .all()
    )
    return {
        'found': True,
        'public_id': certificate.public_id,
        'learner': certificate.learner,
        'status': certificate.status,
        'valid_now': certificate.status == 'active',
        'rule': {
            'id': version.id,
            'name': version.name,
            'checksum': version.checksum,
            'status': version.status,
        } if version else None,
        'basis': certificate.basis,
        'proof': certificate.proof,
        'proof_valid': digest(certificate.basis) == certificate.proof,
        'issued_at': certificate.issued_at,
        'revoked_at': certificate.revoked_at,
        'revoked_by_run_id': certificate.revoked_by_run_id,
        'revoke_reason': certificate.revoke_reason,
        'history': [dump(item) for item in history],
    }


def learner_transcript(s, run: RuleRun, learner: str) -> dict:
    if run.state != 'done' or not run.result:
        raise RuleConflict('重算完成后才能查看逐人核算')
    row = next((item for item in run.result['learners'] if item['learner'] == learner), None)
    if not row:
        raise RuleConflict('该学员在本次重放的规则课程中没有记录')
    certificates = (
        s.query(Certificate)
        .filter(Certificate.learner == learner)
        .order_by(Certificate.id)
        .all()
    )
    credits = (
        s.query(Credit)
        .filter(Credit.learner == learner)
        .order_by(Credit.id)
        .all()
    )
    return {
        'run_id': run.id,
        'rule_version_id': run.rule_version_id,
        'learner': learner,
        'row': row,
        'credits_in_run': [
            {
                'course': hit.course,
                'event_code': hit.event_code,
                'occurred_at': hit.occurred_at,
                'status': hit.status,
            }
            for hit in credits
            if hit.run_id == run.id
        ],
        'certificates': [dump(item) for item in certificates],
    }
