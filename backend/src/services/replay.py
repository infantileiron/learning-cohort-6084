"""按已发布课程规则重放学习历史。

核心纪律：
- 学分只在事件发生时判定，前置课程必须在**严格更早**的时刻已取得学分；
- 同刻事件不构成先修，提前学习不追认；
- 规则版本冻结后，重放结果只取决于该版本的课程/前置关系与事件快照。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import hashlib
import json

TZ = ZoneInfo('Asia/Shanghai')


class RuleError(ValueError):
    pass


def parse_time(value: str) -> datetime:
    raw = str(value).replace('Z', '+00:00')
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise RuleError(f'事件时间无法解析：{value}') from exc
    if ts.tzinfo is None:
        raise RuleError(f'事件时间必须带时区：{value}')
    return ts.astimezone(timezone.utc)


def shanghai_day(value: str) -> date:
    return parse_time(value).astimezone(TZ).date()


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def normalize_rule(courses, prerequisites) -> dict:
    """校验并规范化课程前置关系；拒绝自环、未知课程、重复项与任何成环。"""
    if not isinstance(courses, (list, tuple)) or not courses:
        raise RuleError('规则至少需要一门课程')
    normalized_courses = []
    for course in courses:
        if not isinstance(course, str) or not course.strip():
            raise RuleError('课程名称不能为空')
        course = course.strip()
        if course in normalized_courses:
            raise RuleError(f'课程重复：{course}')
        normalized_courses.append(course)
    course_set = set(normalized_courses)
    if prerequisites is None:
        prerequisites = {}
    if not isinstance(prerequisites, dict):
        raise RuleError('前置关系必须是“课程 -> 先修课程列表”的对象')
    normalized = {course: [] for course in normalized_courses}
    for course, prereqs in prerequisites.items():
        if course not in course_set:
            raise RuleError(f'前置关系中出现未知课程：{course}')
        if not isinstance(prereqs, (list, tuple)):
            raise RuleError(f'{course} 的先修课程必须是列表')
        seen = []
        for prereq in prereqs:
            if not isinstance(prereq, str) or prereq.strip() == '':
                raise RuleError(f'{course} 的先修课程名称不能为空')
            prereq = prereq.strip()
            if prereq == course:
                raise RuleError(f'{course} 不能把自己设为先修课程')
            if prereq not in course_set:
                raise RuleError(f'{course} 的先修课程不在规则课程中：{prereq}')
            if prereq in seen:
                raise RuleError(f'{course} 的先修课程重复：{prereq}')
            seen.append(prereq)
        normalized[course] = seen
    # 环检测：DFS 三色标记
    color = {course: 0 for course in normalized_courses}

    def visit(course, trail):
        if color[course] == 1:
            cycle = trail[trail.index(course):] + [course]
            raise RuleError('前置关系成环：' + ' → '.join(cycle))
        if color[course] == 2:
            return
        color[course] = 1
        for prereq in normalized[course]:
            visit(prereq, trail + [course])
        color[course] = 2

    for course in normalized_courses:
        visit(course, [])
    return {'courses': normalized_courses, 'prerequisites': normalized}


def rule_checksum(rule: dict) -> str:
    return digest({'courses': list(rule['courses']), 'prerequisites': {c: list(rule['prerequisites'].get(c, [])) for c in rule['courses']}})


def topo_order(rule: dict) -> list:
    """稳定拓扑序：先修课程在前；输入课程列表顺序作为平局裁决。"""
    courses = list(rule['courses'])
    prereqs = {c: list(rule['prerequisites'].get(c, [])) for c in courses}
    order, state = [], {c: 0 for c in courses}

    def visit(course):
        if state[course] == 1:
            raise RuleError('规则前置关系成环，无法生成补学顺序')
        if state[course] == 2:
            return
        state[course] = 1
        for prereq in prereqs[course]:
            visit(prereq)
        state[course] = 2
        order.append(course)

    for course in courses:
        visit(course)
    return order


def _credit_record(event: dict, ts: datetime, prereq_credits: dict) -> dict:
    return {
        'course': event['course'],
        'event_id': event.get('id'),
        'event_code': event.get('code'),
        'occurred_at': ts.isoformat().replace('+00:00', 'Z'),
        'prerequisites': [
            {
                'course': p,
                'event_id': prereq_credits[p]['event_id'],
                'event_code': prereq_credits[p]['event_code'],
                'occurred_at': prereq_credits[p]['occurred_at'],
            }
            for p in prereq_credits
        ],
    }


def replay(rule: dict, events: list, as_of: date | None = None, progress=None) -> dict:
    """重放全部事件，返回逐人学分、缺项、补学顺序与总体摘要。"""
    courses = list(rule['courses'])
    course_set = set(courses)
    prereqs = {c: list(rule['prerequisites'].get(c, [])) for c in courses}
    order = topo_order(rule)

    future = 0
    invalid = 0
    out_of_rule = 0
    by_learner: dict[str, list] = defaultdict(list)
    for event in events:
        try:
            ts = parse_time(event['occurred_at'])
        except (KeyError, RuleError):
            invalid += 1
            continue
        if as_of is not None and ts.astimezone(TZ).date() > as_of:
            future += 1
            continue
        if event.get('course') not in course_set:
            out_of_rule += 1
            continue
        by_learner[event.get('learner') or ''].append((ts, event))

    learners = sorted(learner for learner in by_learner if learner)
    total = len(learners)
    rows = []
    for index, learner in enumerate(learners, start=1):
        events_for_learner = sorted(
            by_learner[learner],
            key=lambda pair: (pair[0], pair[1].get('id') or 0, pair[1].get('code') or ''),
        )
        credited: dict[str, dict] = {}
        early: dict[str, list] = defaultdict(list)
        for ts, event in events_for_learner:
            course = event['course']
            if course in credited:
                continue
            missing_pre = [p for p in prereqs[course] if p not in credited]
            if missing_pre:
                early[course].append({
                    'event_id': event.get('id'),
                    'event_code': event.get('code'),
                    'occurred_at': ts.isoformat().replace('+00:00', 'Z'),
                    'reason': '前置课程尚未达标，提前学习不追认',
                    'missing_prerequisites': missing_pre,
                })
                continue
            same_moment = [p for p in prereqs[course] if credited[p]['_at'] >= ts]
            if same_moment:
                early[course].append({
                    'event_id': event.get('id'),
                    'event_code': event.get('code'),
                    'occurred_at': ts.isoformat().replace('+00:00', 'Z'),
                    'reason': '同刻或晚于本事件的前置记录不算先修，提前学习不追认',
                    'missing_prerequisites': same_moment,
                })
                continue
            prereq_evidence = {p: credited[p] for p in prereqs[course]}
            record = _credit_record(event, ts, prereq_evidence)
            record['_at'] = ts
            credited[course] = record

        completed_courses = [c for c in order if c in credited]
        credits = []
        for course in completed_courses:
            record = {k: v for k, v in credited[course].items() if k != '_at'}
            credits.append(record)

        missing = []
        early_records = []
        for course in order:
            for record in early.get(course, []):
                early_records.append({'course': course, **record})
            if course in credited:
                continue
            blocked = [p for p in prereqs[course] if p not in credited]
            if blocked:
                reason = '前置课程未达标'
            elif course in early:
                reason = '只有前置达标前的提前学习记录，不追认；需重新学习'
            else:
                reason = '尚未学习'
            missing.append({
                'course': course,
                'reason': reason,
                'blocked_by': blocked,
                'early_records': early.get(course, []),
            })

        missing_courses = [item['course'] for item in missing]
        plan = []
        for step, item in enumerate(missing, start=1):
            course = item['course']
            requires_first = [p for p in prereqs[course] if p in missing_courses]
            blocked_steps = [missing_courses.index(p) + 1 for p in requires_first]
            plan.append({
                'step': step,
                'course': course,
                'requires_first': requires_first,
                'prerequisites_completed': [p for p in prereqs[course] if p in credited],
                'can_start_now': not requires_first,
                'start_after_step': max(blocked_steps) if blocked_steps else 0,
                'reason': item['reason'],
            })

        eligible = len(completed_courses) == len(courses) and len(courses) > 0
        rows.append({
            'learner': learner,
            'eligible': eligible,
            'completed': completed_courses,
            'credits': credits,
            'early_records': early_records,
            'missing': missing,
            'plan': plan,
            'next_action': next((p['course'] for p in plan if p['can_start_now']), None),
            'credit_count': len(completed_courses),
            'course_count': len(courses),
        })
        if progress is not None:
            progress(index, total)

    # 迟到记录：事件发生时刻晚于其录入时刻不能出现；发生时刻早于录入时刻则标记为补录，
    # 仍按“事件发生时”的状态判定学分，不因录入晚而追认提前学习。
    late_records = []
    for event in events:
        try:
            occurred = parse_time(event['occurred_at'])
            recorded = parse_time(event['recorded_at']) if event.get('recorded_at') else None
        except (KeyError, RuleError):
            continue
        if as_of is not None and occurred.astimezone(TZ).date() > as_of:
            continue
        if recorded and recorded < occurred:
            late_records.append({
                'event_code': event.get('code'),
                'learner': event.get('learner'),
                'occurred_at': occurred.isoformat().replace('+00:00', 'Z'),
                'recorded_at': recorded.isoformat().replace('+00:00', 'Z'),
                'reason': '录入时间早于事件发生时间，时间口径可疑',
            })
    backfilled = sorted(
        (
            {
                'event_code': event.get('code'),
                'learner': event.get('learner'),
                'occurred_at': parse_time(event['occurred_at']).isoformat().replace('+00:00', 'Z'),
                'recorded_at': parse_time(event['recorded_at']).isoformat().replace('+00:00', 'Z'),
            }
            for event in events
            if event.get('recorded_at')
            and parse_time(event['recorded_at']) > parse_time(event['occurred_at'])
            and (as_of is None or parse_time(event['occurred_at']).astimezone(TZ).date() <= as_of)
        ),
        key=lambda item: item['recorded_at'],
    )

    return {
        'late_records': late_records,
        'backfilled_records': backfilled,
        'rule': {
            'courses': courses,
            'prerequisites': prereqs,
            'checksum': rule_checksum(rule),
            'topological_order': order,
        },
        'as_of': as_of.isoformat() if as_of else None,
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'learners': rows,
        'summary': {
            'learners': len(rows),
            'eligible': sum(1 for row in rows if row['eligible']),
            'ineligible': sum(1 for row in rows if not row['eligible']),
            'credits': sum(row['credit_count'] for row in rows),
            'early_records_not_credited': sum(len(row['early_records']) for row in rows),
            'future_events_excluded': future,
            'invalid_events_skipped': invalid,
            'out_of_rule_events_skipped': out_of_rule,
            'late_records': len(late_records),
            'backfilled_records': len(backfilled),
        },
        'method': '按Asia/Shanghai业务日筛至截至日；在每名学员的时间线上按事件发生顺序重放。只有事件发生时全部先修课程已在严格更早时刻取得学分，才记该课程学分；同刻不算先修，前置达标前的提前学习不追认，之后重新学习才计。缺项按规则拓扑序生成补学清单。',
    }
