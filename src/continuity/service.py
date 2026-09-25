"""中断接续应用服务。

修复的三类旧系统问题：
1. 暂停/迁移后候补顺序被重算、补贴名额被重复占用、住院前练习被清零；
2. 教师变更/课程取消时没有差异说明，并行确认互相覆盖；
3. 期限与重放依赖墙钟与请求标识。

本服务所有时间取自可控业务时钟；重放按业务内容判定。
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Optional

from .clock import Clock
from .errors import CompatibilityError, Conflict, NotFound, PendingReview
from .events import Event, EventType
from .model import (
    HIGH,
    STANDARD,
    WAITLISTED,
    SEATED,
    PROPOSAL_HELD,
    PROPOSAL_PENDING,
    CourseState,
    EnrollmentState,
    Proposal,
    model_as_of,
)
from .store import EventStore

SUPPORT_RANK = {STANDARD: 1, HIGH: 2}


def _short_id() -> str:
    return uuid.uuid4().hex[:12]


class ContinuityService:
    def __init__(self, store: EventStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    # ================================================================ 读模型

    def state(self, as_of=None):
        """按业务时点折叠状态；不传则为当前。"""
        return model_as_of(self.store, as_of)

    # ================================================================ 内部工具

    def _emit(
        self,
        event_type: EventType,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict,
        summary: str,
        *,
        actor: Optional[str] = None,
        causation_id: Optional[str] = None,
        occurred_at=None,
    ) -> Event:
        moment = Clock.coerce(occurred_at or self.clock.now())
        version = self.store.version_of(aggregate_type, aggregate_id) + 1
        event = Event(
            event_id=f"{event_type.value.lower()}-{_short_id()}",
            event_type=event_type.value,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=moment,
            version=version,
            summary=summary,
            payload=payload,
            actor=actor,
            causation_id=causation_id,
        )
        return self.store.append(event)

    def _find_replay(self, event_type, aggregate_id, material: dict, *, actor=None) -> Optional[Event]:
        """按业务内容在该聚合历史中查找重放事件。

        ``material`` 只包含参与重放判定的载荷键——例如签到只看课次与内容，
        不看递送时间或事件标识。
        """
        for event in self.store.stream("enrollment", aggregate_id):
            if event.event_type != event_type:
                continue
            if actor is not None and event.actor != actor:
                continue
            if all(event.payload.get(key) == value for key, value in material.items()):
                return event
        return None

    # ------------------------------------------------------------ 课程发布

    def publish_course(
        self,
        course_id: str,
        *,
        title: str,
        mode: str,
        teacher: dict,
        capacity: int,
        supports_offered: list[str],
        sessions: list[dict],
        subsidy_quota: int,
        subsidy_per_session: int,
        high_support: bool = False,
        seat_confirm_deadline=None,
        confirm_window_hours: int = 72,
    ) -> Event:
        """发布课程；教师资历、容量与可提供支持自发布时固定，后续不得原地改写。"""
        model = self.state()
        if course_id in model.courses:
            raise Conflict(f"课程已发布：{course_id}")
        if capacity < 1 or subsidy_quota < 0:
            raise Conflict("容量必须为正、补贴名额不可为负")
        payload = {
            "title": title,
            "mode": mode,
            "teacher": {
                "id": teacher["id"],
                "name": teacher.get("name", teacher["id"]),
                "qualifications": list(teacher.get("qualifications", [])),
            },
            "capacity": capacity,
            "supports_offered": list(supports_offered),
            "high_support": high_support,
            "sessions": [
                {
                    "session_id": s["session_id"],
                    "starts_at": Clock.coerce(s["starts_at"]).isoformat(),
                    "slot": s.get("slot", ""),
                }
                for s in sessions
            ],
            "subsidy_quota": subsidy_quota,
            "subsidy_per_session": subsidy_per_session,
            "seat_confirm_deadline": (
                Clock.coerce(seat_confirm_deadline).isoformat() if seat_confirm_deadline else None
            ),
            "confirm_window_hours": confirm_window_hours,
        }
        return self._emit(
            EventType.COURSE_PUBLISHED, "course_offering", course_id, payload,
            f"发布课程《{title}》，教师资历/容量/支持能力固定",
        )

    def cancel_course(self, course_id: str, reason: str, alternatives: Optional[dict[str, str]] = None) -> list[Event]:
        """取消课程，并为每位在读/暂停学员生成带差异说明的接续方案。"""
        model = self.state()
        model.course(course_id)
        events = [
            self._emit(
                EventType.COURSE_CANCELLED, "course_offering", course_id,
                {"reason": reason}, f"课程取消：{reason}",
            )
        ]
        for enrollment in list(model.enrollments.values()):
            if enrollment.course_id != course_id or enrollment.status not in ("active", "transferring"):
                continue
            target_id = (alternatives or {}).get(enrollment.enrollment_id)
            if target_id is None:
                continue
            # 取消场景下即使备选不兼容也生成方案，差异中写明阻塞项，交学员/照护人处置。
            events.append(
                self._propose(
                    model, enrollment, target_id, reason="course_cancelled",
                    sessions_carried=None, actor="system", force=True,
                )
            )
        return events

    def teacher_changed(self, course_id: str, new_teacher: dict) -> list[Event]:
        """教师变更：为在读学员生成差异方案，由学员或有效授权照护人确认。"""
        model = self.state()
        course = model.course(course_id)
        events = []
        for enrollment in list(model.enrollments.values()):
            if enrollment.course_id != course_id or enrollment.status != "active":
                continue
            diff = self._diff(
                course, course, enrollment, 0,
                teacher_override=new_teacher,
            )
            payload = {
                "proposal_id": f"prop-{_short_id()}",
                "target_course_id": course_id,
                "reason": "teacher_change",
                "sessions_carried": 0,
                "covered_sessions": enrollment.covered_sessions,
                "support_needs": list(enrollment.support_needs),
                "support_level": enrollment.support_level,
                "diff": diff,
                "expires_at": (
                    self.clock.now() + timedelta(hours=course.confirm_window_hours)
                ).isoformat(),
                "choices": [],
                "new_teacher": {
                    "id": new_teacher["id"],
                    "name": new_teacher.get("name", new_teacher["id"]),
                    "qualifications": list(new_teacher.get("qualifications", [])),
                },
            }
            events.append(
                self._emit(
                    EventType.TRANSFER_PROPOSED, "enrollment", enrollment.enrollment_id,
                    payload, "教师变更，生成差异说明待确认", actor="system",
                )
            )
        return events

    # ------------------------------------------------------------ 照护人授权

    def authorize_caregiver(
        self,
        learner_id: str,
        caregiver_id: str,
        *,
        valid_from=None,
        valid_to=None,
        note: str = "",
        status: str = "active",
    ) -> Event:
        """登记授权照护人及有效期；更换照护人通过新授权区间实现，旧区间不被改写。"""
        valid_from = Clock.coerce(valid_from or self.clock.now())
        if valid_to is not None:
            valid_to = Clock.coerce(valid_to)
            if valid_to <= valid_from:
                raise Conflict("照护人授权结束时间必须晚于生效时间")
        payload = {
            "caregiver_id": caregiver_id,
            "valid_from": valid_from.isoformat(),
            "valid_to": valid_to.isoformat() if valid_to else None,
            "note": note,
            "status": status,
        }
        return self._emit(
            EventType.CAREGIVER_AUTHORIZED, "learner_profile", learner_id, payload,
            f"授权照护人 {caregiver_id}", actor=learner_id,
        )

    # ------------------------------------------------------------ 报名

    def place_enrollment(
        self,
        enrollment_id: str,
        learner_id: str,
        course_id: str,
        *,
        time_slots: list[str],
        support_needs: list[str],
        support_level: str = STANDARD,
        needs_subsidy: bool = False,
    ) -> Event:
        """结合学员时段与辅助需求决定正式席位或候补位置。

        正式席位需同时满足座位与补贴名额；任一不足进入候补。候补位置按
        到达顺序固定，任何后来者都不会重排既有学员的位置。
        """
        model = self.state()
        if enrollment_id in model.enrollments:
            raise Conflict(f"报名已存在：{enrollment_id}")
        course = model.course(course_id)
        if course.cancelled:
            raise Conflict("课程已取消，无法报名")
        if support_level not in SUPPORT_RANK:
            raise Conflict(f"未知支持等级：{support_level}")
        missing = [need for need in support_needs if need not in course.supports_offered]
        if missing:
            raise CompatibilityError(f"课程无法提供辅助：{missing}")
        if support_level == HIGH and not course.high_support:
            raise CompatibilityError("课程不具备高支持承接能力")
        if not set(time_slots) & {s.slot for s in course.sessions if s.slot}:
            raise CompatibilityError("学员可上课时段与课程安排无交集")

        seat_free = len(course.seated) < course.capacity
        quota_free = (not needs_subsidy) or course.quota_used < course.subsidy_quota
        decision = SEATED if (seat_free and quota_free) else WAITLISTED

        now = self.clock.now()
        gets_quota = needs_subsidy and decision == SEATED
        # 优先级元组随报名固定；暂停、迁移都沿用它，不重新计算。
        priority_key = [-SUPPORT_RANK[support_level], now.isoformat(), enrollment_id]
        deadline = course.seat_confirm_deadline if decision == SEATED else None

        payload = {
            "learner_id": learner_id,
            "course_id": course_id,
            "time_slots": list(time_slots),
            "support_needs": list(support_needs),
            "support_level": support_level,
            "decision": decision,
            "priority_key": priority_key,
            "seat_confirmed": False,
            "deadline": deadline.isoformat() if deadline else None,
            "needs_subsidy": needs_subsidy,
            "counts_quota": gets_quota,
            "subsidy": {
                "quota": gets_quota,
                "covered_sessions": course.subsidy_per_session if gets_quota else 0,
            },
        }
        return self._emit(
            EventType.ENROLLMENT_PLACED, "enrollment", enrollment_id, payload,
            f"报名 {course_id}，决定：{'正式席位' if decision == SEATED else '候补'}",
            actor=learner_id,
        )

    # ------------------------------------------------------------ 暂停与恢复

    def pause_enrollment(self, enrollment_id: str, reason: str = "medical") -> list[Event]:
        """暂停只冻结尚未发生的课次；席位/候补位置、补贴名额与已有成果全部保留。"""
        model = self.state()
        enrollment = model.enrollment(enrollment_id)
        if enrollment.paused:
            raise Conflict("报名已处于暂停状态")
        now = self.clock.now()
        course = model.course(enrollment.course_id)
        future_ids = [
            s.session_id
            for s in course.sessions_after(now)
            if s.session_id not in enrollment.completed_sessions
        ]
        events = [
            self._emit(
                EventType.ENROLLMENT_PAUSED, "enrollment", enrollment_id,
                {"reason": reason, "paused_at": now.isoformat()},
                f"暂停报名：{reason}；席位、候补优先级与成果保留", actor=enrollment.learner_id,
            )
        ]
        if future_ids:
            events.append(
                self._emit(
                    EventType.SESSION_FROZEN, "enrollment", enrollment_id,
                    {"session_ids": future_ids},
                    f"冻结 {len(future_ids)} 个尚未发生的课次", actor=enrollment.learner_id,
                )
            )
        return events

    def resume_enrollment(self, enrollment_id: str) -> Event:
        model = self.state()
        enrollment = model.enrollment(enrollment_id)
        if not enrollment.paused:
            raise Conflict("报名未处于暂停状态")
        now = self.clock.now()
        course = model.course(enrollment.course_id)
        unfrozen = [
            sid
            for sid in enrollment.frozen_sessions
            if any(
                s.session_id == sid and Clock.coerce(s.starts_at) > now
                for s in course.sessions
            )
        ]
        return self._emit(
            EventType.ENROLLMENT_RESUMED, "enrollment", enrollment_id,
            {"unfrozen_session_ids": unfrozen, "resumed_at": now.isoformat()},
            "恢复上课，未过期课次解除冻结", actor=enrollment.learner_id,
        )

    # ------------------------------------------------------------ 签到与电话确认

    def record_attendance(self, enrollment_id: str, session_id: str, content: str, *, completed_at=None) -> Event:
        """签到/练习完成按内容判定重放：同一课次同一内容的重复提交只保留一条。

        住院前已记录的练习在暂停、迁移后继续算作已完成。
        """
        model = self.state()
        enrollment = model.enrollment(enrollment_id)
        moment = Clock.coerce(completed_at or self.clock.now())
        payload = {
            "session_id": session_id,
            "content": content,
            "completed_at": moment.isoformat(),
        }
        replay = self._find_replay(
            EventType.ATTENDANCE_RECORDED.value, enrollment_id,
            {"session_id": session_id, "content": content},
        )
        if replay is not None:
            return replay
        return self._emit(
            EventType.ATTENDANCE_RECORDED, "enrollment", enrollment_id, payload,
            f"记录课次 {session_id} 的签到与练习成果", actor=enrollment.learner_id,
        )

    def record_phone_confirmation(self, enrollment_id: str, person_id: str, content: str, *, purpose: str = "seat") -> Event:
        """电话确认按通话内容判定重放；内容相同的重复来电不产生第二条记录。"""
        model = self.state()
        enrollment = model.enrollment(enrollment_id)
        if not self._actor_allowed(model, enrollment, person_id):
            raise Conflict("来电人既不是学员本人，也不是当前有效的授权照护人")
        material = {"purpose": purpose, "person_id": person_id, "content": content}
        replay = self._find_replay(
            EventType.PHONE_CONFIRMATION_RECORDED.value, enrollment_id, material, actor=person_id,
        )
        if replay is not None:
            return replay
        if purpose == "seat" and not enrollment.seat_confirmed and enrollment.deadline is None:
            raise Conflict("该报名当前没有待确认的席位期限")
        return self._emit(
            EventType.PHONE_CONFIRMATION_RECORDED, "enrollment", enrollment_id, material,
            f"电话确认（{purpose}）", actor=person_id,
        )

    # ------------------------------------------------------------ 接续方案

    def _remaining_carried(self, model, enrollment: EnrollmentState) -> int:
        course = model.course(enrollment.course_id)
        done = set(enrollment.completed_sessions)
        moved = set(enrollment.transferred_sessions)
        return len([s for s in course.sessions if s.session_id not in done and s.session_id not in moved])

    def _diff(
        self,
        source: CourseState,
        target: CourseState,
        enrollment: EnrollmentState,
        carried: int,
        *,
        teacher_override: Optional[dict] = None,
        compatible: bool = True,
        blockers: Optional[list[str]] = None,
    ) -> dict:
        new_teacher = teacher_override or {
            "id": target.teacher.teacher_id,
            "name": target.teacher.name,
            "qualifications": target.teacher.qualifications,
        }
        quals_before = source.teacher.qualifications
        quals_after = list(new_teacher.get("qualifications", target.teacher.qualifications))
        return {
            "compatible": compatible,
            "blockers": blockers or [],
            "teacher": {
                "before": {
                    "id": source.teacher.teacher_id,
                    "name": source.teacher.name,
                    "qualifications": quals_before,
                },
                "after": {
                    "id": new_teacher["id"],
                    "name": new_teacher.get("name", new_teacher["id"]),
                    "qualifications": quals_after,
                },
                "changed": source.teacher.teacher_id != new_teacher["id"],
                "qualification_diff": {
                    "added": [q for q in quals_after if q not in quals_before],
                    "removed": [q for q in quals_before if q not in quals_after],
                },
            },
            "mode": {"before": source.mode, "after": target.mode},
            "sessions": {"carried_remaining": carried, "target_total": len(target.sessions)},
            "supports": {
                "needed": list(enrollment.support_needs),
                "target_offers": list(target.supports_offered),
                "missing": [n for n in enrollment.support_needs if n not in target.supports_offered],
                "level_before": enrollment.support_level,
                "level_after": enrollment.support_level,  # 迁移不允许自动降级
            },
            "subsidy": {
                "covered_before": enrollment.covered_sessions,
                "covered_after": enrollment.covered_sessions,  # 守恒：随学员带走
                "quota_before": enrollment.subsidy_quota,
                "quota_after": enrollment.subsidy_quota,
            },
        }

    def _check_compatible(self, model, enrollment: EnrollmentState, target: CourseState, carried: int) -> list[str]:
        blockers: list[str] = []
        if target.cancelled:
            blockers.append("目标课程已取消")
        slots = {s.slot for s in target.sessions if s.slot}
        if not set(enrollment.time_slots) & slots:
            blockers.append("目标课程时段与学员可用时段无交集")
        missing = [n for n in enrollment.support_needs if n not in target.supports_offered]
        if missing:
            blockers.append(f"目标课程缺少辅助：{missing}")
        if enrollment.support_level == HIGH and not target.high_support:
            blockers.append("目标课程不具备高支持能力，高支持需求不得降级")
        if carried > len(target.sessions):
            blockers.append("目标课程课次不足以承接剩余课次")
        # 分批迁移的后续批次：学员自己的承接席位/名额不能反过来挡住自己。
        existing = self._existing_target_enrollment(model, enrollment, target.course_id)
        seated = [eid for eid in target.seated if eid != existing]
        quota_used = target.quota_used - (
            1 if existing and model.enrollments[existing].counts_quota else 0
        )
        if len(seated) >= target.capacity:
            blockers.append("目标课程席位已满")
        if enrollment.subsidy_quota and quota_used >= target.subsidy_quota:
            blockers.append("目标课程补贴名额不足，权益无法守恒迁移")
        return blockers

    def _propose(
        self,
        model,
        enrollment: EnrollmentState,
        target_course_id: str,
        *,
        reason: str,
        sessions_carried: Optional[int],
        actor: str,
        force: bool = False,
    ) -> Event:
        target = model.course(target_course_id)
        remaining = self._remaining_carried(model, enrollment)
        carried = remaining if sessions_carried is None else sessions_carried
        if carried < 0 or carried > remaining:
            raise Conflict(f"承接课次数须在 0..{remaining} 之间")
        blockers = self._check_compatible(model, enrollment, target, carried)
        if blockers and not force:
            raise CompatibilityError("；".join(blockers))
        source = model.course(enrollment.course_id)
        diff = self._diff(
            source, target, enrollment, carried,
            compatible=not blockers, blockers=blockers,
        )
        now = self.clock.now()
        payload = {
            "proposal_id": f"prop-{_short_id()}",
            "target_course_id": target_course_id,
            "reason": reason,
            "sessions_carried": carried,
            "remaining_after": remaining - carried,
            "covered_sessions": min(enrollment.covered_sessions, carried) if carried else 0,
            "support_needs": list(enrollment.support_needs),
            "support_level": enrollment.support_level,
            "diff": diff,
            "expires_at": (now + timedelta(hours=target.confirm_window_hours)).isoformat(),
            "choices": [],
        }
        return self._emit(
            EventType.TRANSFER_PROPOSED, "enrollment", enrollment.enrollment_id, payload,
            f"生成接续方案：{source.course_id} → {target_course_id}（本批 {carried} 课次）",
            actor=actor,
        )

    def propose_transfer(
        self,
        enrollment_id: str,
        target_course_id: str,
        *,
        sessions_carried: Optional[int] = None,
        reason: str = "learner_request",
    ) -> Event:
        """为兼容课程生成承接方案（剩余课次、补贴权益与支持等级随方案写明）。

        ``sessions_carried`` 小于剩余课次时为分批迁移：本批迁走后，源报名
        仍保留其余冻结课次与权益，可再次发起下一批。
        """
        model = self.state()
        enrollment = model.enrollment(enrollment_id)
        if enrollment.status not in ("active", "transferring"):
            raise Conflict(f"报名状态 {enrollment.status} 不可发起接续")
        if target_course_id == enrollment.course_id:
            raise Conflict("目标课程与当前课程相同")
        return self._propose(
            model, enrollment, target_course_id, reason=reason,
            sessions_carried=sessions_carried, actor=enrollment.learner_id,
        )

    def _actor_allowed(self, model, enrollment: EnrollmentState, person_id: str) -> bool:
        if person_id == enrollment.learner_id:
            return True
        return model.active_caregiver(enrollment.learner_id, person_id, self.clock.now())

    def _choice(self, person_id: str, role: str, decision: str):
        return {
            "person_id": person_id,
            "role": role,
            "decision": decision,
            "at": self.clock.now().isoformat(),
        }

    # ------------------------------------------------------------ 方案确认

    def confirm_proposal(self, proposal_id: str, person_id: str, decision: str) -> list[Event]:
        """学员本人或当时有效的授权照护人确认方案。

        - 同一人重复表达相同意见：按内容重放，无效果；
        - 方案已成立/已拒绝/待核/逾期：分别给出明确结果或错误；
        - 教师变更类方案确认只落确认记录，不产生迁移。
        """
        if decision not in ("accept", "reject"):
            raise Conflict("决定只能是 accept 或 reject")
        model = self.state()
        enrollment, proposal = model.get_proposal(proposal_id)
        now = self.clock.now()

        if proposal.status == "confirmed":
            return []
        if proposal.status == "rejected":
            raise Conflict("方案已被拒绝")
        if proposal.status == PROPOSAL_HELD:
            raise PendingReview("方案处于待核状态，需教务人工核对后处理")
        if now > Clock.coerce(proposal.expires_at):
            raise Conflict("确认期限已过，应先由教务按逾期流程处理")
        if not self._actor_allowed(model, enrollment, person_id):
            raise Conflict("确认人既非学员本人，也非当前有效的授权照护人")

        role = "learner" if person_id == enrollment.learner_id else "caregiver"
        prior_same = [c for c in proposal.choices if c["person_id"] == person_id]
        if prior_same and prior_same[-1]["decision"] == decision:
            return []  # 内容重放

        choice = self._choice(person_id, role, decision)
        if decision == "reject":
            return [
                self._emit(
                    EventType.PROPOSAL_REJECTED, "enrollment", enrollment.enrollment_id,
                    {"proposal_id": proposal_id, "choices": [choice]},
                    "方案被拒绝", actor=person_id,
                )
            ]
        return self._accept(model, enrollment, proposal, [choice], actor=person_id)

    def submit_parallel_choices(self, proposal_id: str, choices: list[tuple[str, str]]) -> list[Event]:
        """两方意见同时送达时的入口（同一业务时点，无用户意图上的先后）。

        选择相同 → 按该选择成立；选择不同 → 方案进入待核并完整保留两方意见，
        不会有任一方抢先覆盖另一方。
        """
        model = self.state()
        enrollment, proposal = model.get_proposal(proposal_id)
        if proposal.status != PROPOSAL_PENDING:
            raise Conflict(f"方案当前状态 {proposal.status}，不可并行表决")
        if self.clock.now() > Clock.coerce(proposal.expires_at):
            raise Conflict("确认期限已过")
        recorded: list[dict] = []
        for person_id, decision in choices:
            if decision not in ("accept", "reject"):
                raise Conflict("决定只能是 accept 或 reject")
            if not self._actor_allowed(model, enrollment, person_id):
                raise Conflict(f"{person_id} 不是学员本人或当前有效授权照护人")
            role = "learner" if person_id == enrollment.learner_id else "caregiver"
            recorded.append(self._choice(person_id, role, decision))

        decisions = {c["decision"] for c in recorded}
        if len(decisions) > 1:
            return [
                self._emit(
                    EventType.PROPOSAL_HELD, "enrollment", enrollment.enrollment_id,
                    {"proposal_id": proposal_id, "reason": "divergent_choices", "choices": recorded},
                    "学员与照护人选择不一致，方案进入待核", actor="system",
                )
            ]
        decision = recorded[0]["decision"]
        if decision == "reject":
            return [
                self._emit(
                    EventType.PROPOSAL_REJECTED, "enrollment", enrollment.enrollment_id,
                    {"proposal_id": proposal_id, "choices": recorded},
                    "两方一致拒绝方案", actor="system",
                )
            ]
        return self._accept(model, enrollment, proposal, recorded, actor="system")

    def resolve_held_proposal(self, proposal_id: str, staff_id: str, decision: str) -> list[Event]:
        """教务对待核方案的人工裁决；意见痕迹与裁决事件全部保留。"""
        if decision not in ("accept", "reject"):
            raise Conflict("决定只能是 accept 或 reject")
        model = self.state()
        enrollment, proposal = model.get_proposal(proposal_id)
        if proposal.status != PROPOSAL_HELD:
            raise Conflict("仅待核方案可人工裁决")
        choice = {
            "person_id": staff_id, "role": "staff", "decision": decision,
            "at": self.clock.now().isoformat(), "resolution": "staff_review",
        }
        if decision == "reject":
            return [
                self._emit(
                    EventType.PROPOSAL_REJECTED, "enrollment", enrollment.enrollment_id,
                    {"proposal_id": proposal_id, "choices": [choice], "resolution": "staff_review"},
                    "教务核决：拒绝方案", actor=staff_id,
                )
            ]
        return self._accept(model, enrollment, proposal, [choice], actor=staff_id, staff_review=True)

    def _accept(
        self,
        model,
        enrollment: EnrollmentState,
        proposal: Proposal,
        choices: list[dict],
        *,
        actor: str,
        staff_review: bool = False,
    ) -> list[Event]:
        # 教师变更：只确认差异，不迁移、不新建报名。
        if proposal.reason == "teacher_change" and proposal.target_course_id == enrollment.course_id:
            return [
                self._emit(
                    EventType.TRANSFER_CONFIRMED, "enrollment", enrollment.enrollment_id,
                    {"proposal_id": proposal.proposal_id, "choices": choices,
                     "kind": "teacher_change_ack",
                     "resolution": "staff_review" if staff_review else "party_confirm"},
                    "确认教师变更差异说明", actor=actor,
                )
            ]

        target = model.course(proposal.target_course_id)
        source = model.course(enrollment.course_id)
        remaining = self._remaining_carried(model, enrollment)
        final_batch = remaining - proposal.sessions_carried <= 0
        covered_moved = min(enrollment.covered_sessions, proposal.sessions_carried)
        # 本批课次按源课程顺序确定性选取（优先冻结名单，保证暂停与未暂停情形一致）。
        done = set(enrollment.completed_sessions)
        moved = set(enrollment.transferred_sessions)
        remaining_ids = [
            s.session_id for s in source.sessions
            if s.session_id not in done and s.session_id not in moved
        ]
        consumed = remaining_ids[: proposal.sessions_carried]

        # 已有承接报名说明是分批迁移的后续批次：席位与名额首批即已换位，
        # 后续批次只移动课次与补贴课次数，不再重复占座位/名额。
        existing_target_id = self._existing_target_enrollment(model, enrollment, target.course_id)
        if existing_target_id is None:
            blockers = self._check_compatible(model, enrollment, target, proposal.sessions_carried)
            if blockers:
                raise CompatibilityError("方案确认时承接条件已不满足：" + "；".join(blockers))
        else:
            # 后续批次只需目标课程仍开放。
            if target.cancelled:
                raise CompatibilityError("目标课程已取消，无法继续承接")

        if existing_target_id is None:
            target_enrollment_id = f"{enrollment.enrollment_id}-xfer-{_short_id()[:8]}"
        else:
            target_enrollment_id = existing_target_id

        events = [
            self._emit(
                EventType.TRANSFER_CONFIRMED, "enrollment", enrollment.enrollment_id,
                {"proposal_id": proposal.proposal_id, "choices": choices,
                 "kind": "transfer",
                 "first_batch": existing_target_id is None,
                 "target_course_id": target.course_id,
                 "target_enrollment_id": target_enrollment_id,
                 "consumed_session_ids": consumed,
                 "sessions_carried": proposal.sessions_carried,
                 "covered_moved": covered_moved,
                 "final_batch": final_batch,
                 "resolution": "staff_review" if staff_review else "party_confirm"},
                f"本批 {proposal.sessions_carried} 个剩余课次承接至 {target.course_id}"
                + ("（末批，源报名关闭）" if final_batch else "（分批，源报名保留剩余课次）"),
                actor=actor,
            )
        ]

        # 补贴守恒台账：逐批记录随迁补贴课次；首批同时完成名额换位，
        # 后续批次名额不变，整个窗口内全局占用既不增加也不减少。
        if enrollment.subsidy_quota and covered_moved > 0:
            events.append(
                self._emit(
                    EventType.SUBSIDY_LEDGERED, "learner_profile", enrollment.learner_id,
                    {"type": "transfer_move",
                     "from_course": enrollment.course_id, "to_course": target.course_id,
                     "covered_sessions": covered_moved,
                     "first_batch": existing_target_id is None,
                     "final_batch": final_batch,
                     "proposal_id": proposal.proposal_id},
                    f"补贴课次 {covered_moved} 次随迁守恒"
                    + ("，名额换位至目标课程" if existing_target_id is None else "，名额已在目标课程"),
                    causation_id=events[0].event_id,
                )
            )

        if existing_target_id is not None:
            # 后续批次：累加到既有承接报名，不新建席位。
            events.append(
                self._emit(
                    EventType.TRANSFER_BATCH_APPLIED, "enrollment", existing_target_id,
                    {"from_enrollment": enrollment.enrollment_id,
                     "proposal_id": proposal.proposal_id,
                     "covered_added": covered_moved,
                     "sessions_added": proposal.sessions_carried,
                     "final_batch": final_batch},
                    f"承接报名追加本批 {proposal.sessions_carried} 课次、补贴 {covered_moved} 次",
                    actor=actor, causation_id=events[0].event_id,
                )
            )
            return events

        # 首批：目标课程产生承接报名；住院前完成的练习作为成果一并带入，
        # 支持等级原样沿用，高支持需求不因迁移自动降级。
        achievements = list(enrollment.completed_sessions)
        target_payload = {
            "learner_id": enrollment.learner_id,
            "course_id": target.course_id,
            "time_slots": list(enrollment.time_slots),
            "support_needs": list(enrollment.support_needs),
            "support_level": enrollment.support_level,
            "decision": SEATED,
            "priority_key": list(enrollment.priority_key),
            "seat_confirmed": True,
            "deadline": None,
            "needs_subsidy": enrollment.subsidy_quota,
            "counts_quota": enrollment.subsidy_quota,
            "subsidy": {
                "quota": enrollment.subsidy_quota,
                "covered_sessions": covered_moved,
            },
            "completed_sessions": achievements,
            "carried_achievements": achievements,
            "origin": {"type": "transfer", "from_enrollment": enrollment.enrollment_id,
                       "proposal_id": proposal.proposal_id, "final_batch": final_batch},
        }
        events.append(
            self._emit(
                EventType.ENROLLMENT_PLACED, "enrollment", target_enrollment_id, target_payload,
                f"目标课程承接：成果 {len(achievements)} 项、补贴课次 {covered_moved} 次随迁",
                actor=actor, causation_id=events[0].event_id,
            )
        )
        return events

    def _existing_target_enrollment(self, model, enrollment: EnrollmentState, target_course_id: str) -> Optional[str]:
        """找到该迁移链上既有的承接报名（用于分批迁移的后续批次）。"""
        for other in model.enrollments.values():
            origin = other.origin or {}
            if (
                origin.get("type") == "transfer"
                and origin.get("from_enrollment") == enrollment.enrollment_id
                and other.course_id == target_course_id
            ):
                return other.enrollment_id
        return None

    # ------------------------------------------------------------ 期限与候补

    def sweep_expired_proposals(self) -> list[Event]:
        """超过确认期限仍未决定的方案按拒绝处理；期限以业务时钟判断。"""
        model = self.state()
        now = self.clock.now()
        events = []
        for enrollment in model.enrollments.values():
            for proposal in enrollment.proposals:
                if proposal.status != PROPOSAL_PENDING:
                    continue
                if now > Clock.coerce(proposal.expires_at):
                    events.append(
                        self._emit(
                            EventType.PROPOSAL_REJECTED, "enrollment", enrollment.enrollment_id,
                            {"proposal_id": proposal.proposal_id, "reason": "expired",
                             "choices": [{"person_id": "system", "role": "system",
                                          "decision": "reject", "at": now.isoformat()}]},
                            "方案逾期未确认，按拒绝处理", actor="system",
                        )
                    )
        return events

    def promote_waitlist(self) -> list[Event]:
        """截止点候补提升：先释放逾期未确认席位，再按候补固定顺位提升。

        全程使用业务时钟。队首若因座位或补贴名额不足不能提升则停止，
        不跳过、不挤压后续候补学员，任何人的候补位置都不重算。
        """
        model = self.state()
        now = self.clock.now()
        events: list[Event] = []

        for course in model.courses.values():
            if course.cancelled:
                continue
            for enrollment_id in list(course.seated):
                enrollment = model.enrollments.get(enrollment_id)
                if enrollment is None:
                    continue
                if (
                    not enrollment.seat_confirmed
                    and enrollment.deadline is not None
                    and now >= Clock.coerce(enrollment.deadline)
                ):
                    events.append(
                        self._emit(
                            EventType.SEAT_RELEASED, "enrollment", enrollment_id,
                            {"course_id": course.course_id, "reason": "confirm_deadline_passed",
                             "deadline": Clock.coerce(enrollment.deadline).isoformat()},
                            "席位确认逾期，释放席位与补贴名额", actor="system",
                        )
                    )

        # 重新折叠以反映释放结果，再做顺位提升。
        model = self.state()
        for course in model.courses.values():
            if course.cancelled:
                continue
            while course.waitlist:
                head = model.enrollments[course.waitlist[0]]
                seat_free = len(course.seated) < course.capacity
                quota_free = (not head.needs_subsidy) or course.quota_used < course.subsidy_quota
                if not seat_free or not quota_free:
                    break
                deadline = now + timedelta(hours=course.confirm_window_hours)
                events.append(
                    self._emit(
                        EventType.WAITLIST_PROMOTED, "enrollment", head.enrollment_id,
                        {"course_id": course.course_id, "promoted_at": now.isoformat(),
                         "grants_subsidy": head.needs_subsidy,
                         "deadline": deadline.isoformat()},
                        f"候补提升至正式席位，确认截止 {deadline.isoformat()}",
                        actor="system",
                    )
                )
                course.waitlist.pop(0)
                course.seated.append(head.enrollment_id)
                if head.needs_subsidy:
                    course.quota_used += 1
                head.decision = SEATED
                head.subsidy_quota = True
                head.covered_sessions = course.subsidy_per_session
                head.deadline = deadline
        return events
