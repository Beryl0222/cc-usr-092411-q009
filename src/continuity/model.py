"""领域读模型：把不可变事件折叠成某一业务时点的状态。

所有查询都经过 :func:`fold`——传入历史时点即可得到当时的课程归属、
支持决定与权益余额，而不会看到“后来发生的事”。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .clock import Clock
from .events import Event, EventType

STANDARD = "standard"
HIGH = "high"

SEATED = "seated"
WAITLISTED = "waitlisted"

PROPOSAL_PENDING = "pending"
PROPOSAL_CONFIRMED = "confirmed"
PROPOSAL_REJECTED = "rejected"
PROPOSAL_HELD = "held"


@dataclass
class Teacher:
    teacher_id: str
    name: str
    qualifications: list[str]


@dataclass
class ScheduledSession:
    session_id: str
    starts_at: datetime
    slot: str


@dataclass
class CourseState:
    course_id: str
    title: str
    mode: str
    teacher: Teacher
    capacity: int
    supports_offered: list[str]
    high_support: bool
    sessions: list[ScheduledSession]
    subsidy_quota: int
    subsidy_per_session: int
    seat_confirm_deadline: Optional[datetime]
    confirm_window_hours: int
    published_at: datetime
    cancelled: bool = False
    cancel_reason: Optional[str] = None
    seated: list[str] = field(default_factory=list)
    waitlist: list[str] = field(default_factory=list)
    quota_used: int = 0

    def sessions_after(self, moment: datetime) -> list[ScheduledSession]:
        moment = Clock.coerce(moment)
        return [s for s in self.sessions if Clock.coerce(s.starts_at) > moment]

    def waitlist_position(self, enrollment_id: str) -> Optional[int]:
        if enrollment_id in self.waitlist:
            return self.waitlist.index(enrollment_id) + 1
        return None


@dataclass
class Membership:
    course_id: str
    reason: str
    valid_from: datetime
    valid_to: Optional[datetime] = None


@dataclass
class Proposal:
    proposal_id: str
    target_course_id: str
    reason: str
    sessions_carried: int
    covered_sessions: int
    support_needs: list[str]
    support_level: str
    diff: dict
    expires_at: datetime
    status: str = PROPOSAL_PENDING
    choices: list[dict] = field(default_factory=list)
    target_enrollment_id: Optional[str] = None
    held_reason: Optional[str] = None
    new_teacher: Optional[dict] = None


@dataclass
class EnrollmentState:
    enrollment_id: str
    learner_id: str
    course_id: str
    created_at: datetime
    time_slots: list[str]
    support_needs: list[str]
    support_level: str
    decision: str
    priority_key: tuple
    seat_confirmed: bool
    deadline: Optional[datetime]
    needs_subsidy: bool
    counts_quota: bool
    subsidy_quota: bool
    covered_sessions: int
    paused: bool = False
    frozen_sessions: list[str] = field(default_factory=list)
    transferred_sessions: list[str] = field(default_factory=list)
    completed_sessions: list[str] = field(default_factory=list)
    attendance: list[dict] = field(default_factory=list)
    memberships: list[Membership] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    status: str = "active"  # active / transferred / released / closed
    origin: Optional[dict] = None
    carried_achievements: list[str] = field(default_factory=list)

    def proposal(self, proposal_id: str) -> Proposal:
        for proposal in self.proposals:
            if proposal.proposal_id == proposal_id:
                return proposal
        raise KeyError(proposal_id)


@dataclass
class CaregiverGrant:
    caregiver_id: str
    valid_from: datetime
    valid_to: Optional[datetime]
    status: str
    note: str


@dataclass
class LearnerState:
    learner_id: str
    caregivers: list[CaregiverGrant] = field(default_factory=list)
    ledger: list[dict] = field(default_factory=list)


@dataclass
class ReadModel:
    courses: dict[str, CourseState] = field(default_factory=dict)
    enrollments: dict[str, EnrollmentState] = field(default_factory=dict)
    learners: dict[str, LearnerState] = field(default_factory=dict)
    proposals: dict[str, str] = field(default_factory=dict)  # proposal_id -> enrollment_id
    phone_confirmations: set[str] = field(default_factory=set)

    # ------------------------------------------------------------ 查询辅助

    def course(self, course_id: str) -> CourseState:
        try:
            return self.courses[course_id]
        except KeyError:
            from .errors import NotFound

            raise NotFound(f"课程不存在：{course_id}") from None

    def enrollment(self, enrollment_id: str) -> EnrollmentState:
        try:
            return self.enrollments[enrollment_id]
        except KeyError:
            from .errors import NotFound

            raise NotFound(f"报名不存在：{enrollment_id}") from None

    def get_proposal(self, proposal_id: str) -> tuple[EnrollmentState, Proposal]:
        enrollment_id = self.proposals.get(proposal_id)
        if enrollment_id is None:
            from .errors import NotFound

            raise NotFound(f"接续方案不存在：{proposal_id}")
        enrollment = self.enrollment(enrollment_id)
        return enrollment, enrollment.proposal(proposal_id)

    def learner(self, learner_id: str) -> LearnerState:
        return self.learners.setdefault(learner_id, LearnerState(learner_id))

    def active_caregiver(self, learner_id: str, person_id: str, moment: datetime) -> bool:
        """当时是否有效：只看覆盖该时点且状态有效的授权区间。"""
        moment = Clock.coerce(moment)
        learner = self.learners.get(learner_id)
        if learner is None:
            return False
        for grant in learner.caregivers:
            if grant.caregiver_id != person_id or grant.status != "active":
                continue
            if Clock.coerce(grant.valid_from) <= moment and (
                grant.valid_to is None or moment <= Clock.coerce(grant.valid_to)
            ):
                return True
        return False

    def memberships_of(self, learner_id: str) -> list[EnrollmentState]:
        return [e for e in self.enrollments.values() if e.learner_id == learner_id]

    def benefit_balance(self, learner_id: str) -> dict:
        """权益余额：各报名持有的补贴课次与名额占用，加上守恒划转台账。"""
        enrollments = [
            {
                "enrollment_id": state.enrollment_id,
                "course_id": state.course_id,
                "status": state.status,
                "decision": state.decision,
                "subsidy_quota": state.subsidy_quota,
                "counts_quota": state.counts_quota,
                "covered_sessions": state.covered_sessions,
            }
            for state in self.memberships_of(learner_id)
        ]
        return {
            "enrollments": enrollments,
            "ledger": list(self.learner(learner_id).ledger),
        }


# ---------------------------------------------------------------------- 折叠


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return Clock.coerce(value)
    return Clock.coerce(datetime.fromisoformat(value))


def fold(events: list[Event]) -> ReadModel:
    """按发生时刻折叠事件（同刻保持接收先后），得到读模型。"""
    model = ReadModel()
    for event in sorted(events, key=lambda e: Clock.coerce(e.occurred_at)):
        _apply(model, event)
    return model


def _apply(model: ReadModel, event: Event) -> None:
    p = event.payload
    et = event.event_type

    if et == EventType.COURSE_PUBLISHED.value:
        teacher = p["teacher"]
        model.courses[event.aggregate_id] = CourseState(
            course_id=event.aggregate_id,
            title=p["title"],
            mode=p["mode"],
            teacher=Teacher(
                teacher_id=teacher["id"],
                name=teacher.get("name", teacher["id"]),
                qualifications=list(teacher.get("qualifications", [])),
            ),
            capacity=int(p["capacity"]),
            supports_offered=list(p["supports_offered"]),
            high_support=bool(p.get("high_support", False)),
            sessions=[
                ScheduledSession(
                    session_id=s["session_id"],
                    starts_at=_as_datetime(s["starts_at"]),
                    slot=s.get("slot", ""),
                )
                for s in p["sessions"]
            ],
            subsidy_quota=int(p["subsidy_quota"]),
            subsidy_per_session=int(p.get("subsidy_per_session", 0)),
            seat_confirm_deadline=(
                _as_datetime(p["seat_confirm_deadline"]) if p.get("seat_confirm_deadline") else None
            ),
            confirm_window_hours=int(p.get("confirm_window_hours", 72)),
            published_at=Clock.coerce(event.occurred_at),
        )
        return

    if et == EventType.COURSE_CANCELLED.value:
        course = model.course(event.aggregate_id)
        course.cancelled = True
        course.cancel_reason = p.get("reason")
        return

    if et == EventType.CAREGIVER_AUTHORIZED.value:
        model.learner(event.aggregate_id).caregivers.append(
            CaregiverGrant(
                caregiver_id=p["caregiver_id"],
                valid_from=_as_datetime(p["valid_from"]),
                valid_to=_as_datetime(p["valid_to"]) if p.get("valid_to") else None,
                status=p.get("status", "active"),
                note=p.get("note", ""),
            )
        )
        return

    if et == EventType.SUBSIDY_LEDGERED.value:
        model.learner(event.aggregate_id).ledger.append(
            {"at": Clock.coerce(event.occurred_at), **p}
        )
        return

    if et == EventType.ENROLLMENT_PLACED.value:
        subsidy = p.get("subsidy", {})
        state = EnrollmentState(
            enrollment_id=event.aggregate_id,
            learner_id=p["learner_id"],
            course_id=p["course_id"],
            created_at=Clock.coerce(event.occurred_at),
            time_slots=list(p["time_slots"]),
            support_needs=list(p["support_needs"]),
            support_level=p["support_level"],
            decision=p["decision"],
            priority_key=tuple(p["priority_key"]),
            seat_confirmed=bool(p["seat_confirmed"]),
            deadline=_as_datetime(p["deadline"]) if p.get("deadline") else None,
            needs_subsidy=bool(p.get("needs_subsidy", False)),
            counts_quota=bool(p.get("counts_quota", False)),
            subsidy_quota=bool(subsidy.get("quota", False)),
            covered_sessions=int(subsidy.get("covered_sessions", 0)),
            origin=p.get("origin"),
            carried_achievements=list(p.get("carried_achievements", [])),
        )
        state.completed_sessions.extend(p.get("completed_sessions", []))
        state.memberships.append(
            Membership(
                course_id=p["course_id"],
                reason=(p.get("origin") or {}).get("type", "enrollment"),
                valid_from=Clock.coerce(event.occurred_at),
            )
        )
        model.enrollments[event.aggregate_id] = state
        course = model.course(p["course_id"])
        if state.decision == SEATED:
            course.seated.append(event.aggregate_id)
            if state.counts_quota:
                course.quota_used += 1
        else:
            course.waitlist.append(event.aggregate_id)
        return

    enrollment = model.enrollments.get(event.aggregate_id)

    if et == EventType.ENROLLMENT_PAUSED.value:
        enrollment.paused = True
        return

    if et == EventType.SESSION_FROZEN.value:
        for sid in p["session_ids"]:
            if sid not in enrollment.frozen_sessions:
                enrollment.frozen_sessions.append(sid)
        return

    if et == EventType.ENROLLMENT_RESUMED.value:
        enrollment.paused = False
        for sid in p.get("unfrozen_session_ids", []):
            if sid in enrollment.frozen_sessions:
                enrollment.frozen_sessions.remove(sid)
        return

    if et == EventType.SEAT_RELEASED.value:
        course = model.course(enrollment.course_id)
        if enrollment.enrollment_id in course.seated:
            course.seated.remove(enrollment.enrollment_id)
            if enrollment.counts_quota:
                course.quota_used -= 1
        enrollment.status = "released"
        enrollment.seat_confirmed = False
        enrollment.counts_quota = False
        enrollment.subsidy_quota = False
        return

    if et == EventType.WAITLIST_PROMOTED.value:
        course = model.course(p["course_id"])
        if enrollment.enrollment_id in course.waitlist:
            course.waitlist.remove(enrollment.enrollment_id)
        course.seated.append(enrollment.enrollment_id)
        if p.get("grants_subsidy"):
            course.quota_used += 1
            enrollment.subsidy_quota = True
            enrollment.counts_quota = True
            enrollment.covered_sessions = course.subsidy_per_session
        enrollment.decision = SEATED
        enrollment.status = "active"
        enrollment.seat_confirmed = False
        enrollment.deadline = _as_datetime(p["deadline"]) if p.get("deadline") else None
        return

    if et == EventType.ATTENDANCE_RECORDED.value:
        sid = p["session_id"]
        newly_completed = sid not in enrollment.completed_sessions
        if newly_completed:
            enrollment.completed_sessions.append(sid)
            # 每完成一个课次消耗一次补贴权益（余额视角）。
            if enrollment.covered_sessions > 0:
                enrollment.covered_sessions -= 1
        enrollment.attendance.append(
            {
                "session_id": sid,
                "content": p.get("content", ""),
                "completed_at": _as_datetime(p.get("completed_at", event.occurred_at)),
            }
        )
        return

    if et == EventType.PHONE_CONFIRMATION_RECORDED.value:
        model.phone_confirmations.add(event.event_id)
        if p.get("purpose") == "seat":
            enrollment.seat_confirmed = True
            enrollment.deadline = None
        return

    if et == EventType.TRANSFER_PROPOSED.value:
        pid = p["proposal_id"]
        enrollment.proposals.append(
            Proposal(
                proposal_id=pid,
                target_course_id=p["target_course_id"],
                reason=p.get("reason", "learner_request"),
                sessions_carried=int(p["sessions_carried"]),
                covered_sessions=int(p["covered_sessions"]),
                support_needs=list(p["support_needs"]),
                support_level=p["support_level"],
                diff=dict(p["diff"]),
                expires_at=_as_datetime(p["expires_at"]),
                choices=[dict(c) for c in p.get("choices", [])],
                new_teacher=p.get("new_teacher"),
            )
        )
        model.proposals[pid] = enrollment.enrollment_id
        return

    if et == EventType.PROPOSAL_HELD.value:
        proposal = enrollment.proposal(p["proposal_id"])
        proposal.status = PROPOSAL_HELD
        proposal.held_reason = p.get("reason")
        proposal.choices.extend(dict(c) for c in p.get("choices", []))
        return

    if et == EventType.PROPOSAL_REJECTED.value:
        proposal = enrollment.proposal(p["proposal_id"])
        proposal.status = PROPOSAL_REJECTED
        proposal.choices.extend(dict(c) for c in p.get("choices", []))
        return

    if et == EventType.TRANSFER_CONFIRMED.value:
        proposal = enrollment.proposal(p["proposal_id"])
        proposal.status = PROPOSAL_CONFIRMED
        proposal.choices.extend(dict(c) for c in p.get("choices", []))
        proposal.target_enrollment_id = p.get("target_enrollment_id")

        # 教师变更确认：留痕并在课程读模型生效（发布快照本身不被改写，
        # 变化由“提案→确认”事件链承载，历史时点仍可看到原教师）。
        if p.get("kind") == "teacher_change_ack":
            new_teacher = proposal.new_teacher
            if new_teacher:
                from .events import Event as _Event  # 复用投影内构造
                course = model.course(enrollment.course_id)
                course.teacher = Teacher(
                    teacher_id=new_teacher["id"],
                    name=new_teacher.get("name", new_teacher["id"]),
                    qualifications=list(new_teacher.get("qualifications", [])),
                )
            return

        # 本批迁走的冻结课次从源报名扣除；分批迁移时其余冻结课次保留。
        consumed = set(p.get("consumed_session_ids", []))
        enrollment.frozen_sessions = [
            sid for sid in enrollment.frozen_sessions if sid not in consumed
        ]
        for sid in consumed:
            if sid not in enrollment.transferred_sessions:
                enrollment.transferred_sessions.append(sid)
        enrollment.covered_sessions = max(
            0, enrollment.covered_sessions - int(p.get("covered_moved", 0))
        )

        if p.get("first_batch", True):
            # 首批：源席位与名额即换位到目标课程（目标承接报名的
            # ENROLLMENT_PLACED 会把名额加回去，全局占用守恒）。
            source_course = model.course(enrollment.course_id)
            if enrollment.enrollment_id in source_course.seated:
                source_course.seated.remove(enrollment.enrollment_id)
                if enrollment.counts_quota:
                    source_course.quota_used -= 1
            enrollment.counts_quota = False
            for membership in enrollment.memberships:
                if membership.course_id == enrollment.course_id and membership.valid_to is None:
                    membership.valid_to = Clock.coerce(event.occurred_at)

        if p.get("final_batch", True):
            enrollment.subsidy_quota = enrollment.covered_sessions > 0
            enrollment.status = "transferred"
        else:
            # 分批中间批次：源报名保留剩余冻结课次与补贴权益，等待下一批。
            enrollment.status = "transferring"
        return

    if et == EventType.TRANSFER_BATCH_APPLIED.value:
        # 后续批次累加到既有承接报名；不新增席位、不重复占用补贴名额。
        enrollment.covered_sessions += int(p.get("covered_added", 0))
        for sid in p.get("achievement_session_ids", []):
            if sid not in enrollment.completed_sessions:
                enrollment.completed_sessions.append(sid)
        if p.get("final_batch"):
            enrollment.status = "active"
        return


def model_as_of(store, as_of: Optional[datetime] = None) -> ReadModel:
    """从事件存储折叠出某业务时点的读模型（默认为最新）。"""
    events = store.all() if as_of is None else store.all_as_of(as_of)
    return fold(events)
