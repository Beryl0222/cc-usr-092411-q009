"""读模型与事件折叠。

所有状态都只能由 ``apply_event`` 逐个事件得到，因此：
- 折叠全量事件得到当前状态；
- 折叠 ``occurred_at <= 某时点`` 的事件得到历史状态；
- 进程重启后重放日志即可恢复，不存在会“重新计算”候补顺序或成果的旁路。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from src.continuity.events import (
    CAREGIVER_GRANTED,
    CAREGIVER_REVOKED,
    COURSE_CANCELLED,
    COURSE_PUBLISHED,
    ENROLLMENT_ENDED,
    ENROLLMENT_PLACED,
    PAUSE_FROZEN,
    PAUSE_RESUMED,
    PRACTICE_RECORDED,
    PROPOSAL_CHOICE_RECORDED,
    PROPOSAL_CONFIRMED,
    PROPOSAL_DISPUTED,
    PROPOSAL_EXPIRED,
    PROPOSAL_OPENED,
    PROPOSAL_REJECTED,
    PROPOSAL_RESOLVED,
    SESSION_RECORDED,
    SESSIONS_MIGRATED,
    SUBSIDY_ALLOCATED,
    SUBSIDY_RELINKED,
    SUBSIDY_RETURNED,
    TEACHER_CHANGED,
    WAITLIST_PROMOTED,
    WAITLIST_RELEASED,
)

# 支持等级排序：迁移时只能保持或提高，自动降级被禁止。
SUPPORT_RANK = {"standard": 1, "enhanced": 2, "full": 3}
SUPPORT_LABEL = {"standard": "标准", "enhanced": "辅助", "full": "高支持"}

SEAT = "SEAT"
WAITLIST = "WAITLIST"
MIGRATED = "MIGRATED"
ENDED = "ENDED"

CONFIRM = "confirm"
REJECT = "reject"

SELF = "SELF"
CAREGIVER = "CAREGIVER"


@dataclass
class Session:
    session_id: str
    scheduled_at: datetime
    slot: str
    state: str = "SCHEDULED"  # SCHEDULED / FROZEN / MIGRATED / CANCELLED
    attended: bool = False
    migrated_to: Optional[tuple[str, str]] = None  # (目标课程, 目标课次)


@dataclass
class TeacherTerm:
    teacher_id: str
    qualification: str
    changed_at: datetime


@dataclass
class CourseState:
    course_id: str
    title: str
    modality: str  # in_person / phone / hybrid
    capacity: int
    supports: tuple[str, ...]
    slot_labels: frozenset[str]
    promotion_cutoff: Optional[datetime]
    teacher_history: list[TeacherTerm] = field(default_factory=list)
    sessions: dict[str, Session] = field(default_factory=dict)
    cancelled: bool = False
    version: int = 0

    @property
    def teacher(self) -> TeacherTerm:
        return self.teacher_history[-1]


@dataclass
class EnrollmentState:
    enrollment_id: str
    learner_id: str
    course_id: str
    slots: tuple[str, ...]
    needed_support: str
    granted_support: str
    status: str
    waitlist_seq: Optional[int]
    placed_at: datetime
    source_enrollment_id: Optional[str] = None
    paused: bool = False
    frozen_sessions: set[str] = field(default_factory=set)
    attended_sessions: set[str] = field(default_factory=set)
    migrated_sessions: dict[str, tuple[str, str]] = field(default_factory=dict)
    scoped_sessions: set[str] = field(default_factory=set)  # 迁移承接报名只覆盖这些目标课次
    chain_to: Optional[str] = None  # 本报名承接后产生的下游报名
    entitlement_id: Optional[str] = None
    subsidy_requested: Optional[dict[str, Any]] = None
    end_reason: Optional[str] = None
    version: int = 0

    def remaining_session_ids(
        self, course: CourseState, as_of: datetime | None = None
    ) -> list[str]:
        """尚未完成、且仍归属本报名的课次（按计划时间排序）。

        ``as_of`` 给出时，已过计划时间却既未签到也未冻结的课次视为
        “错过”，不再属于可承接的剩余课次（例如住院当天上午已开始的面授课）。
        """
        done = self.attended_sessions | set(self.migrated_sessions)
        scope = self.scoped_sessions or set(course.sessions)
        result = []
        for sid, s in sorted(course.sessions.items(), key=lambda kv: kv[1].scheduled_at):
            if sid not in scope or sid in done:
                continue
            if s.state == "CANCELLED":
                continue
            # 冻结课次受暂停保护：即便计划时间已过仍是可承接的剩余课次；
            # 只有“未冻结且已过去”的课次才视为错过（如住院当天已开始的面授课）。
            if as_of is not None and sid not in self.frozen_sessions and s.scheduled_at <= as_of:
                continue
            result.append(sid)
        return result


@dataclass
class CareGrant:
    caregiver_id: str
    valid_from: datetime
    valid_until: Optional[datetime]
    revoked_at: Optional[datetime] = None


@dataclass
class LearnerState:
    learner_id: str
    caregivers: dict[str, CareGrant] = field(default_factory=dict)
    version: int = 0


@dataclass
class LearningRecord:
    learner_id: str
    practices: dict[str, dict[str, Any]] = field(default_factory=dict)  # practice_id -> 元数据
    version: int = 0


@dataclass
class Entitlement:
    """补贴权益：在整条迁移链上守恒，不随迁移重新分配。"""

    entitlement_id: str
    learner_id: str
    total_units: int
    current_enrollment_id: str
    current_course_id: str
    active: bool = True
    used_units: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Choice:
    party_id: str
    role: str
    decision: str
    channel: str
    at: datetime
    superseded: bool = False


@dataclass
class ProposalState:
    proposal_id: str
    kind: str  # MIGRATION / TEACHER_CHANGE / CANCELLATION
    learner_id: str
    source_enrollment_id: str
    source_course_id: str
    target_course_id: Optional[str]
    plan: dict[str, Any]
    opened_at: datetime
    expires_at: datetime
    choices: dict[str, Choice] = field(default_factory=dict)  # party_id -> 最新选择
    status: str = "OPEN"  # OPEN / CONFIRMED / REJECTED / DISPUTED / EXPIRED / RESOLVED
    resolution: Optional[dict[str, Any]] = None
    effectuated: bool = False
    version: int = 0


@dataclass
class Registry:
    courses: dict[str, CourseState] = field(default_factory=dict)
    enrollments: dict[str, EnrollmentState] = field(default_factory=dict)
    learners: dict[str, LearnerState] = field(default_factory=dict)
    records: dict[str, LearningRecord] = field(default_factory=dict)
    entitlements: dict[str, Entitlement] = field(default_factory=dict)
    proposals: dict[str, ProposalState] = field(default_factory=dict)
    waitlist_counter: dict[str, int] = field(default_factory=dict)
    # learner_id -> 报名链（按时间），便于课程归属查询
    learner_enrollments: dict[str, list[str]] = field(default_factory=dict)
    # enrollment_id -> 整体迁出/结束时间，供历史时点归属查询
    ended_at: dict[str, datetime] = field(default_factory=dict)
    # 已执行过截止点候补提升的课程
    cutoff_processed: set[str] = field(default_factory=set)

    # ---- 查询辅助 ----

    def course_enrollments(self, course_id: str) -> list[EnrollmentState]:
        return [e for e in self.enrollments.values() if e.course_id == course_id]

    def seats_used(self, course_id: str) -> int:
        """占用中的正式席位。

        整体迁出/结束的报名不再占座；分批承接产生的 scoped 报名在其
        承接课次全部完成或转出后也不再占座；待确认/待核的迁移方案
        各自预留一个席位，确认后转为承接报名席位，拒绝或过期自动释放。
        """
        total = 0
        for e in self.course_enrollments(course_id):
            if e.status != SEAT:
                continue
            if e.scoped_sessions and not e.remaining_session_ids(
                self.courses[course_id], as_of=None
            ):
                continue
            total += 1
        total += len(self.reserved_proposals(course_id))
        return total

    def reserved_proposals(self, course_id: str) -> list["ProposalState"]:
        """以该课程为承接目标、仍待确认或待核的方案。"""
        return [
            p
            for p in self.proposals.values()
            if p.target_course_id == course_id and p.status in ("OPEN", "DISPUTED")
        ]

    def reserved_sessions(self, course_id: str) -> set[str]:
        """被待确认/待核迁移方案预留的目标课次。"""
        result: set[str] = set()
        for prop in self.reserved_proposals(course_id):
            for row in prop.plan.get("sessions", []):
                result.add(row["target_session_id"])
        return result

    def subsidy_used(self, course_id: str) -> int:
        return sum(
            1
            for g in self.entitlements.values()
            if g.current_course_id == course_id and g.active
        )

    def waitlist(self, course_id: str) -> list[EnrollmentState]:
        return sorted(
            (e for e in self.course_enrollments(course_id) if e.status == WAITLIST),
            key=lambda e: (e.waitlist_seq is None, e.waitlist_seq),
        )

    def active_caregivers(self, learner_id: str, at: datetime) -> list[str]:
        learner = self.learners.get(learner_id)
        if learner is None:
            return []
        result = []
        for cid, grant in learner.caregivers.items():
            if grant.revoked_at is not None:
                continue
            if grant.valid_from <= at and (grant.valid_until is None or at < grant.valid_until):
                result.append(cid)
        return result

    def entitlement_for(self, enrollment_id: str) -> Optional[Entitlement]:
        for g in self.entitlements.values():
            if g.current_enrollment_id == enrollment_id and g.active:
                return g
        return None

    def learner_entitlement(self, learner_id: str) -> Optional[Entitlement]:
        for g in self.entitlements.values():
            if g.learner_id == learner_id and g.active:
                return g
        return None

    def subsidy_balance(self, learner_id: str) -> Optional[dict[str, int]]:
        grant = self.learner_entitlement(learner_id)
        if grant is None:
            return None
        return {
            "total": grant.total_units,
            "used": grant.used_units,
            "remaining": grant.total_units - grant.used_units,
            "entitlement_id": grant.entitlement_id,
            "course_id": grant.current_course_id,
        }

    def completed_practices(self, learner_id: str) -> dict[str, dict[str, Any]]:
        record = self.records.get(learner_id)
        return dict(record.practices) if record else {}

    def courses_of(self, learner_id: str, at: datetime) -> list[str]:
        """历史时点的课程归属：该时点处于在册（含暂停/候补）状态的课程。"""
        result = []
        for eid in self.learner_enrollments.get(learner_id, []):
            e = self.enrollments[eid]
            if e.placed_at > at:
                continue
            ended_at = self.ended_at.get(eid)
            if ended_at is not None and ended_at <= at:
                continue
            result.append(e.course_id)
        return result


def apply_event(reg: Registry, event: Any) -> None:
    """把单个事件折叠进注册表。跨聚合的联动（迁移、补贴账本）在此一次性收敛。"""
    p = event.payload
    t = event.event_type

    if t == COURSE_PUBLISHED:
        reg.courses[event.aggregate_id] = CourseState(
            course_id=event.aggregate_id,
            title=p["title"],
            modality=p["modality"],
            capacity=p["capacity"],
            supports=tuple(p["supports"]),
            slot_labels=frozenset(p["slot_labels"]),
            promotion_cutoff=p.get("promotion_cutoff"),
            teacher_history=[
                TeacherTerm(p["teacher_id"], p["teacher_qualification"], event.occurred_at)
            ],
            sessions={
                sid: Session(sid, dt, slot)
                for sid, dt, slot in p["sessions"]
            },
            version=1,
        )
        return

    if t == TEACHER_CHANGED:
        course = reg.courses[event.aggregate_id]
        course.teacher_history.append(
            TeacherTerm(p["teacher_id"], p["teacher_qualification"], event.occurred_at)
        )
        course.version += 1
        return

    if t == COURSE_CANCELLED:
        course = reg.courses[event.aggregate_id]
        course.cancelled = True
        for s in course.sessions.values():
            if s.state in ("SCHEDULED", "FROZEN"):
                s.state = "CANCELLED"
        course.version += 1
        return

    if t == ENROLLMENT_PLACED:
        e = EnrollmentState(
            enrollment_id=event.aggregate_id,
            learner_id=p["learner_id"],
            course_id=p["course_id"],
            slots=tuple(p["slots"]),
            needed_support=p["needed_support"],
            granted_support=p["granted_support"],
            status=p["status"],
            waitlist_seq=p.get("waitlist_seq"),
            placed_at=event.occurred_at,
            source_enrollment_id=p.get("source_enrollment_id"),
        )
        if p.get("scoped_session_ids"):
            e.scoped_sessions = set(p["scoped_session_ids"])
        e.subsidy_requested = p.get("subsidy_requested")
        if e.status == SEAT and p.get("frozen_session_ids"):
            e.paused = True
            e.frozen_sessions = set(p["frozen_session_ids"])
        reg.enrollments[event.aggregate_id] = e
        reg.learner_enrollments.setdefault(p["learner_id"], []).append(event.aggregate_id)
        if p.get("waitlist_seq") is not None:
            reg.waitlist_counter[p["course_id"]] = max(
                reg.waitlist_counter.get(p["course_id"], 0), p["waitlist_seq"]
            )
        return

    e = reg.enrollments.get(event.aggregate_id)

    if t == WAITLIST_PROMOTED:
        assert e is not None
        e.status = SEAT
        e.waitlist_seq = None
        e.version += 1
        return

    if t == ENROLLMENT_ENDED:
        assert e is not None
        e.status = ENDED
        e.end_reason = p.get("reason")
        reg.ended_at[e.enrollment_id] = event.occurred_at
        e.version += 1
        return

    if t == WAITLIST_RELEASED:
        assert e is not None
        e.status = ENDED
        e.end_reason = "WAITLIST_RELEASED"
        reg.ended_at[e.enrollment_id] = event.occurred_at
        e.version += 1
        return

    if t == PAUSE_FROZEN:
        assert e is not None
        e.paused = True
        e.frozen_sessions = set(p["session_ids"])
        # 冻结是报名维度：同班其他学员的课次不受影响，课程级课次状态不变
        e.version += 1
        return

    if t == PAUSE_RESUMED:
        assert e is not None
        e.paused = False
        e.frozen_sessions = set()
        e.version += 1
        return

    if t == SESSION_RECORDED:
        assert e is not None
        if p.get("attended", True):
            e.attended_sessions.add(p["session_id"])
            course = reg.courses[e.course_id]
            course.sessions[p["session_id"]].attended = True
            # 权益按学员在整条迁移链上计数，无论课次挂在链上哪个报名
            grant = reg.learner_entitlement(e.learner_id)
            if grant is not None:
                grant.used_units += 1
        e.version += 1
        return

    if t == PRACTICE_RECORDED:
        record = reg.records.setdefault(event.aggregate_id, LearningRecord(event.aggregate_id))
        record.practices.setdefault(
            p["practice_id"],
            {"title": p.get("title", ""), "completed_at": event.occurred_at},
        )
        record.version += 1
        return

    if t == CAREGIVER_GRANTED:
        learner = reg.learners.setdefault(event.aggregate_id, LearnerState(event.aggregate_id))
        learner.caregivers[p["caregiver_id"]] = CareGrant(
            caregiver_id=p["caregiver_id"],
            valid_from=p["valid_from"],
            valid_until=p.get("valid_until"),
        )
        learner.version += 1
        return

    if t == CAREGIVER_REVOKED:
        learner = reg.learners[event.aggregate_id]
        learner.caregivers[p["caregiver_id"]].revoked_at = event.occurred_at
        learner.version += 1
        return

    if t == SUBSIDY_ALLOCATED:
        assert e is not None
        ent = Entitlement(
            entitlement_id=p["entitlement_id"],
            learner_id=p["learner_id"],
            total_units=p["total_units"],
            current_enrollment_id=event.aggregate_id,
            current_course_id=e.course_id,
        )
        ent.history.append(
            {"at": event.occurred_at, "action": "ALLOCATED", "enrollment_id": e.enrollment_id}
        )
        e.entitlement_id = p["entitlement_id"]
        reg.entitlements[p["entitlement_id"]] = ent
        e.version += 1
        return

    if t == SUBSIDY_RELINKED:
        ent = reg.entitlements[p["entitlement_id"]]
        ent.history.append(
            {
                "at": event.occurred_at,
                "action": "RELINKED",
                "from_enrollment_id": p["from_enrollment_id"],
                "to_enrollment_id": p["to_enrollment_id"],
                "to_course_id": p["to_course_id"],
            }
        )
        ent.current_enrollment_id = p["to_enrollment_id"]
        ent.current_course_id = p["to_course_id"]
        target = reg.enrollments[p["to_enrollment_id"]]
        target.entitlement_id = ent.entitlement_id
        source = reg.enrollments.get(p["from_enrollment_id"])
        if source is not None and source.entitlement_id == ent.entitlement_id:
            source.entitlement_id = None
        return

    if t == SUBSIDY_RETURNED:
        ent = reg.entitlements[p["entitlement_id"]]
        ent.active = False
        ent.history.append({"at": event.occurred_at, "action": "RETURNED"})
        if e is not None:
            e.entitlement_id = None
        return

    if t == SESSIONS_MIGRATED:
        assert e is not None
        target = reg.enrollments[p["target_enrollment_id"]]
        for item in p["session_map"]:
            sid, tid = item["source_session_id"], item["target_session_id"]
            # 迁出只在报名维度记录：同班其他学员仍可正常上该课；
            # 目标课次是新的未完成课次，不复制出勤状态
            e.frozen_sessions.discard(sid)
            e.migrated_sessions[sid] = (p["target_course_id"], tid)
        e.chain_to = target.enrollment_id
        if p.get("final"):
            e.status = MIGRATED
            reg.ended_at[e.enrollment_id] = event.occurred_at
        e.version += 1
        target.version += 1
        return

    if t == PROPOSAL_OPENED:
        reg.proposals[event.aggregate_id] = ProposalState(
            proposal_id=event.aggregate_id,
            kind=p["kind"],
            learner_id=p["learner_id"],
            source_enrollment_id=p["source_enrollment_id"],
            source_course_id=p["source_course_id"],
            target_course_id=p.get("target_course_id"),
            plan=p["plan"],
            opened_at=event.occurred_at,
            expires_at=p["expires_at"],
        )
        return

    if t == PROPOSAL_CHOICE_RECORDED:
        prop = reg.proposals[event.aggregate_id]
        prev = prop.choices.get(p["party_id"])
        if prev is not None and p.get("supersedes"):
            prev.superseded = True
        prop.choices[p["party_id"]] = Choice(
            party_id=p["party_id"],
            role=p["role"],
            decision=p["decision"],
            channel=p["channel"],
            at=event.occurred_at,
        )
        prop.version += 1
        return

    if t == PROPOSAL_DISPUTED:
        prop = reg.proposals[event.aggregate_id]
        prop.status = "DISPUTED"
        prop.version += 1
        return

    if t == PROPOSAL_CONFIRMED:
        prop = reg.proposals[event.aggregate_id]
        prop.status = "CONFIRMED"
        prop.effectuated = p.get("effectuated", prop.effectuated)
        prop.version += 1
        return

    if t == PROPOSAL_REJECTED:
        prop = reg.proposals[event.aggregate_id]
        prop.status = "REJECTED"
        prop.version += 1
        return

    if t == PROPOSAL_EXPIRED:
        prop = reg.proposals[event.aggregate_id]
        prop.status = "EXPIRED"
        prop.version += 1
        return

    if t == PROPOSAL_RESOLVED:
        prop = reg.proposals[event.aggregate_id]
        prop.status = "RESOLVED"
        prop.resolution = {
            "decision": p["decision"],
            "note": p.get("note", ""),
            "by": p.get("staff_id", ""),
            "at": event.occurred_at,
        }
        prop.effectuated = p.get("effectuated", prop.effectuated)
        prop.version += 1
        return


def fold(events: Any, reg: Optional[Registry] = None) -> Registry:
    reg = reg or Registry()
    for event in events:
        apply_event(reg, event)
    return reg
