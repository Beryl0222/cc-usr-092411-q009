"""中断接续应用服务。

关键不变量：
* 课程发布后教师资历、容量、可提供支持即为固定条款；
* 候补序号在报名时一次确定，暂停、迁移都不重算；
* 练习成果挂在学员聚合，暂停/迁移不影响；
* 暂停只冻结“尚未发生”的课次，已完成课次与既有优先级保留；
* 迁移只承接剩余课次；补贴权益随迁移链原子过户，绝不重复占用；
* 支持等级只能保持或提高，不会因迁移自动降级；
* 教师变更/课程取消生成带差异说明的方案，由本人或当时有效的授权照护人确认；
  两方选择不一致 -> 待核，任何一方都不能抢先覆盖；
* 签到、电话确认等命令带幂等键，按内容判定重放；
* 一切时间判断只走可控业务时钟。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from src.continuity.clock import ControlledClock
from src.continuity.errors import (
    AuthorizationError,
    DomainError,
    IncompatibleCourseError,
    ProposalClosedError,
    ReplayConflictError,
)
from src.continuity.events import (
    AGG_COURSE,
    AGG_ENROLLMENT,
    AGG_LEARNER,
    AGG_PROPOSAL,
    AGG_RECORD,
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
    DomainEvent,
)
from src.continuity.journal import EventJournal
from src.continuity.models import (
    CONFIRM,
    REJECT,
    SEAT,
    SUPPORT_LABEL,
    SUPPORT_RANK,
    WAITLIST,
    CourseState,
    EnrollmentState,
    Registry,
    apply_event,
    fold,
)

DEFAULT_CONFIRM_WINDOW = timedelta(days=7)


class ContinuityService:
    def __init__(self, clock: ControlledClock | None = None, journal: EventJournal | None = None) -> None:
        self.clock = clock or ControlledClock()
        self.journal = journal or EventJournal()
        self.registry: Registry = fold(self.journal.replay())

    # ------------------------------------------------------------------ 基础

    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        summary: str,
        event_key: str | None = None,
    ) -> DomainEvent:
        version = 1 + sum(1 for ev in self.journal.events if ev.aggregate_id == aggregate_id)
        if event_key is not None:
            event_id = "ev-" + hashlib.sha1(event_key.encode("utf-8")).hexdigest()[:16]
        else:
            event_id = "ev-" + uuid.uuid4().hex[:16]
        event = DomainEvent(
            event_id=event_id,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=self.clock.now(),
            version=version,
            summary=summary,
            payload=payload,
            event_key=event_key,
        )
        self.journal.append(event)
        apply_event(self.registry, event)
        return event

    @staticmethod
    def _canonical(content: Any) -> str:
        return hashlib.sha256(
            json_dumps(content).encode("utf-8")
        ).hexdigest()

    def _replay_guard(self, key: str | None, content: dict[str, Any]) -> DomainEvent | None:
        """命中已处理命令：内容一致返回旧事件（调用方据此空转），不一致则拒绝。"""
        if key is None:
            return None
        prior = self.journal.seen_keys().get(key)
        if prior is None:
            return None
        prior_content = {k: v for k, v in prior.payload.items() if k != "_replay_hash"}
        if self._canonical(prior_content) == self._canonical(content):
            return prior
        raise ReplayConflictError(
            f"幂等键 {key} 曾以不同内容提交：既有事件 {prior.event_id}"
        )

    def _emit_idempotent(
        self,
        key: str | None,
        content: dict[str, Any],
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        summary: str,
    ) -> DomainEvent:
        prior = self._replay_guard(key, content)
        if prior is not None:
            return prior
        payload = dict(content)
        payload["_replay_hash"] = self._canonical(content)
        return self._emit(event_type, aggregate_type, aggregate_id, payload, summary, key)

    def _require_course(self, course_id: str) -> CourseState:
        course = self.registry.courses.get(course_id)
        if course is None:
            raise DomainError(f"课程不存在：{course_id}")
        return course

    def _require_enrollment(self, enrollment_id: str) -> EnrollmentState:
        enr = self.registry.enrollments.get(enrollment_id)
        if enr is None:
            raise DomainError(f"报名不存在：{enrollment_id}")
        return enr

    # ------------------------------------------------------------ 课程发布

    def publish_course(
        self,
        course_id: str,
        *,
        title: str,
        modality: str,
        teacher_id: str,
        teacher_qualification: str,
        capacity: int,
        supports: list[str],
        slot_labels: list[str],
        sessions: list[tuple[str, datetime, str]],
        promotion_cutoff: datetime | None = None,
        event_key: str | None = None,
    ) -> DomainEvent:
        """发布课程。发布时刻固定教师资历、容量、可提供支持与课次表。"""
        key = event_key or f"publish:{course_id}"
        content = {
            "title": title,
            "modality": modality,
            "teacher_id": teacher_id,
            "teacher_qualification": teacher_qualification,
            "capacity": capacity,
            "supports": list(supports),
            "slot_labels": list(slot_labels),
            "sessions": [[sid, when, slot] for sid, when, slot in sessions],
            "promotion_cutoff": promotion_cutoff,
        }
        # 重放判定先于“课程已存在”，重复投递按内容空转或报冲突
        prior = self._replay_guard(key, content)
        if prior is not None:
            return prior
        if course_id in self.registry.courses:
            raise DomainError(f"课程已发布：{course_id}（固定条款不允许原地改写）")
        if capacity < 1:
            raise DomainError("容量必须为正整数")
        unknown = [s for s in supports if s not in SUPPORT_RANK]
        if unknown:
            raise DomainError(f"未知支持等级：{unknown}")
        for sid, when, slot in sessions:
            if slot not in slot_labels:
                raise DomainError(f"课次 {sid} 的时段 {slot} 不在课程公布时段内")
        return self._emit_idempotent(
            key,
            content,
            COURSE_PUBLISHED,
            AGG_COURSE,
            course_id,
            f"发布课程《{title}》，固定容量 {capacity}、教师 {teacher_id}（{teacher_qualification}）",
        )

    # ------------------------------------------------------------ 照护授权

    def grant_caregiver(
        self,
        learner_id: str,
        caregiver_id: str,
        *,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        event_key: str | None = None,
    ) -> DomainEvent:
        content = {
            "caregiver_id": caregiver_id,
            "valid_from": valid_from or self.clock.now(),
            "valid_until": valid_until,
        }
        start = content["valid_from"]
        return self._emit_idempotent(
            event_key or f"grant:{learner_id}:{caregiver_id}:{start.isoformat()}",
            content,
            CAREGIVER_GRANTED,
            AGG_LEARNER,
            learner_id,
            f"登记授权照护人 {caregiver_id}",
        )

    def revoke_caregiver(
        self, learner_id: str, caregiver_id: str, *, event_key: str | None = None
    ) -> DomainEvent:
        key = event_key or f"revoke:{learner_id}:{caregiver_id}"
        content = {"caregiver_id": caregiver_id}
        prior = self._replay_guard(key, content)
        if prior is not None:
            return prior
        learner = self.registry.learners.get(learner_id)
        grant = learner.caregivers.get(caregiver_id) if learner else None
        if grant is None or grant.revoked_at is not None:
            raise DomainError(f"有效照护授权不存在：{learner_id}/{caregiver_id}")
        return self._emit_idempotent(
            key,
            content,
            CAREGIVER_REVOKED,
            AGG_LEARNER,
            learner_id,
            f"撤销照护人 {caregiver_id} 的授权",
        )

    # ---------------------------------------------------------------- 报名

    def enroll(
        self,
        enrollment_id: str,
        *,
        learner_id: str,
        course_id: str,
        slots: list[str],
        needed_support: str,
        subsidy_units: int | None = None,
        event_key: str | None = None,
    ) -> dict[str, Any]:
        """结合学员时段与辅助需求决定正式席位或候补位置；候补序号一次确定。"""
        key = event_key or f"enroll:{enrollment_id}"
        request = {
            "learner_id": learner_id,
            "course_id": course_id,
            "slots": list(slots),
            "needed_support": needed_support,
            "subsidy_units": subsidy_units,
        }
        # 早期重放判定：先于任何依赖当前状态的决定，避免重复投递被误判为重复报名
        prior = self.journal.seen_keys().get(key)
        if prior is not None:
            stored = {k: prior.payload.get(k) for k in request}
            if self._canonical(stored) != self._canonical(request):
                raise ReplayConflictError(f"幂等键 {key} 曾以不同报名请求提交")
            return {
                "event_id": prior.event_id,
                "status": prior.payload["status"],
                "waitlist_seq": prior.payload.get("waitlist_seq"),
                "granted_support": prior.payload["granted_support"],
            }

        course = self._require_course(course_id)
        if course.cancelled:
            raise DomainError(f"课程已取消：{course_id}")
        if needed_support not in SUPPORT_RANK:
            raise DomainError(f"未知支持等级：{needed_support}")
        if needed_support not in course.supports:
            # 课程无法提供所需支持：不得降级接收
            raise IncompatibleCourseError(
                f"课程 {course_id} 不提供{SUPPORT_LABEL[needed_support]}支持，不能接收该学员"
            )
        bad_slots = [s for s in slots if s not in course.slot_labels]
        if bad_slots:
            raise DomainError(f"学员时段不在课程公布时段内：{bad_slots}")
        if enrollment_id in self.registry.enrollments:
            raise DomainError(f"报名已存在：{enrollment_id}")

        seat_free = self.registry.seats_used(course_id) < course.capacity
        if seat_free:
            status, waitlist_seq = SEAT, None
        else:
            status = WAITLIST
            waitlist_seq = self.registry.waitlist_counter.get(course_id, 0) + 1

        # 权益 ID 由学员与报名确定派生：同一命令重放必须得到相同内容
        entitlement_id = f"ENT-{learner_id}-{enrollment_id}" if subsidy_units else None
        content = {
            "learner_id": learner_id,
            "course_id": course_id,
            "slots": list(slots),
            "needed_support": needed_support,
            "subsidy_units": subsidy_units,
            "granted_support": needed_support,
            "status": status,
            "waitlist_seq": waitlist_seq,
            "subsidy_requested": (
                {"entitlement_id": entitlement_id, "total_units": subsidy_units}
                if subsidy_units
                else None
            ),
        }
        placed = self._emit_idempotent(
            event_key or f"enroll:{enrollment_id}",
            content,
            ENROLLMENT_PLACED,
            AGG_ENROLLMENT,
            enrollment_id,
            f"学员 {learner_id} 报名 {course_id}，"
            + ("获得正式席位" if status == SEAT else f"进入候补，序号 {waitlist_seq}"),
        )
        # 补贴名额只在取得正式席位时占用；候补期间不占用
        if status == SEAT and subsidy_units:
            self._emit_idempotent(
                f"subsidy:{enrollment_id}",
                {
                    "entitlement_id": entitlement_id,
                    "learner_id": learner_id,
                    "total_units": subsidy_units,
                },
                SUBSIDY_ALLOCATED,
                AGG_ENROLLMENT,
                enrollment_id,
                f"为学员 {learner_id} 分配补贴权益 {entitlement_id}，共 {subsidy_units} 个课次单位",
            )
        return {
            "event_id": placed.event_id,
            "status": status,
            "waitlist_seq": waitlist_seq,
            "granted_support": needed_support,
        }

    # ------------------------------------------------------------ 暂停/恢复

    def pause(self, enrollment_id: str, *, reason: str = "", event_key: str | None = None) -> DomainEvent:
        """短期中断：只冻结暂停时刻之后尚未发生的课次，成果与优先级原样保留。"""
        key = event_key or f"pause:{enrollment_id}"
        enr = self._require_enrollment(enrollment_id)
        course = self._require_course(enr.course_id)
        now = self.clock.now()
        freeze_ids = [
            sid
            for sid, s in sorted(course.sessions.items(), key=lambda kv: kv[1].scheduled_at)
            if sid in (enr.scoped_sessions or set(course.sessions))
            and s.scheduled_at > now
            and s.state == "SCHEDULED"
            and sid not in enr.attended_sessions
            and sid not in enr.migrated_sessions
        ]
        content = {"session_ids": freeze_ids, "reason": reason}
        prior = self._replay_guard(key, content)
        if prior is not None:
            return prior
        if enr.status != SEAT:
            raise DomainError("只有在册正式席位可以暂停（候补位置始终保留）")
        if enr.paused:
            raise DomainError("报名已处于暂停状态")
        if not freeze_ids:
            raise DomainError("没有可冻结的未开课次")
        return self._emit_idempotent(
            key,
            content,
            PAUSE_FROZEN,
            AGG_ENROLLMENT,
            enrollment_id,
            f"暂停报名 {enrollment_id}，冻结 {len(freeze_ids)} 个尚未发生的课次",
        )

    def resume(self, enrollment_id: str, *, event_key: str | None = None) -> DomainEvent:
        enr = self._require_enrollment(enrollment_id)
        content: dict[str, Any] = {}
        prior = self._replay_guard(event_key or f"resume:{enrollment_id}", content)
        if prior is not None:
            return prior
        if not enr.paused:
            raise DomainError("报名当前未暂停")
        return self._emit_idempotent(
            event_key or f"resume:{enrollment_id}",
            content,
            PAUSE_RESUMED,
            AGG_ENROLLMENT,
            enrollment_id,
            f"恢复报名 {enrollment_id}，冻结课次重新排期",
        )

    # -------------------------------------------------------- 签到与练习成果

    def record_attendance(
        self,
        enrollment_id: str,
        session_id: str,
        *,
        attended: bool = True,
        event_key: str | None = None,
    ) -> DomainEvent:
        """签到按 (报名,课次) 内容判定重放：同结果空转，不同结果冲突。"""
        key = event_key or f"attendance:{enrollment_id}:{session_id}"
        content = {"session_id": session_id, "attended": attended}
        prior = self._replay_guard(key, content)
        if prior is not None:
            return prior
        enr = self._require_enrollment(enrollment_id)
        course = self._require_course(enr.course_id)
        session = course.sessions.get(session_id)
        if session is None or session_id not in (enr.scoped_sessions or set(course.sessions)):
            raise DomainError(f"课次不属于该报名：{session_id}")
        if session_id in enr.frozen_sessions:
            raise DomainError("课次已冻结，暂停期间不能签到")
        if session.state == "CANCELLED":
            raise DomainError("课次已取消，不能签到")
        return self._emit_idempotent(
            key,
            content,
            SESSION_RECORDED,
            AGG_ENROLLMENT,
            enrollment_id,
            f"课次 {session_id} 签到：{'出勤' if attended else '未出勤'}",
        )

    def record_practice(
        self,
        learner_id: str,
        practice_id: str,
        *,
        title: str,
        event_key: str | None = None,
    ) -> DomainEvent:
        """登记学员练习成果。成果属于学员本人，暂停或迁移都不会把它变回未完成。"""
        content = {"practice_id": practice_id, "title": title}
        return self._emit_idempotent(
            event_key or f"practice:{learner_id}:{practice_id}",
            content,
            PRACTICE_RECORDED,
            AGG_RECORD,
            learner_id,
            f"记录练习成果《{title}》",
        )

    # ------------------------------------------------------------ 候补提升

    def promote_waitlist(self, course_id: str, *, limit: int | None = None) -> list[str]:
        """有空位即按候补序号提升；提升时才占用补贴名额，顺序不会被重算。"""
        course = self._require_course(course_id)
        promoted: list[str] = []
        for enr in self.registry.waitlist(course_id):
            if self.registry.seats_used(course_id) >= course.capacity:
                break
            if limit is not None and len(promoted) >= limit:
                break
            self._emit_idempotent(
                f"promote:{course_id}:{enr.enrollment_id}",
                {},
                WAITLIST_PROMOTED, AGG_ENROLLMENT, enr.enrollment_id,
                f"候补序号 {enr.waitlist_seq} 提升为正式席位",
            )
            promoted.append(enr.enrollment_id)
            requested = enr.subsidy_requested
            if requested:
                self._emit_idempotent(
                    f"subsidy:{enr.enrollment_id}",
                    {
                        "entitlement_id": requested["entitlement_id"],
                        "learner_id": enr.learner_id,
                        "total_units": requested["total_units"],
                    },
                    SUBSIDY_ALLOCATED,
                    AGG_ENROLLMENT,
                    enr.enrollment_id,
                    f"候补提升后为学员 {enr.learner_id} 占用补贴名额",
                )
        return promoted

    def tick(self, *, to: datetime | None = None, delta: timedelta | None = None) -> None:
        """推进业务时钟，并在截止点处理候补提升与确认期限。"""
        if to is not None:
            self.clock.freeze(to)
        elif delta is not None:
            self.clock.advance(delta)
        now = self.clock.now()
        self._sweep_cutoffs(now)
        self._sweep_proposal_expiry(now)

    def _sweep_cutoffs(self, now: datetime) -> None:
        for course_id, course in list(self.registry.courses.items()):
            if course.cancelled or course_id in self.registry.cutoff_processed:
                continue
            if course.promotion_cutoff is None or now < course.promotion_cutoff:
                continue
            self.registry.cutoff_processed.add(course_id)
            self.promote_waitlist(course_id)
            # 截止点后仍在候补的释放
            for enr in self.registry.waitlist(course_id):
                self._emit_idempotent(
                    f"cutoff-release:{course_id}:{enr.enrollment_id}",
                    {"reason": "PROMOTION_CUTOFF_PASSED"},
                    WAITLIST_RELEASED, AGG_ENROLLMENT, enr.enrollment_id,
                    f"已过候补提升截止点，释放候补序号 {enr.waitlist_seq}",
                )

    def _sweep_proposal_expiry(self, now: datetime) -> None:
        for pid, prop in list(self.registry.proposals.items()):
            if prop.status != "OPEN" or now < prop.expires_at:
                continue
            votes = self._live_voters(prop)
            decisions = set(votes.values())
            if CONFIRM in decisions and REJECT in decisions:
                self._emit(
                    PROPOSAL_DISPUTED, AGG_PROPOSAL, pid,
                    {"choices": sorted(votes)},
                    "确认期限到达时选择仍不一致，方案进入待核",
                )
            elif CONFIRM in decisions:
                self._effectuate(pid)
            elif REJECT in decisions:
                self._emit(
                    PROPOSAL_REJECTED, AGG_PROPOSAL, pid,
                    {}, "确认期限到达，按已提交的拒绝关闭方案",
                )
            else:
                self._emit(
                    PROPOSAL_EXPIRED, AGG_PROPOSAL, pid,
                    {}, "确认期限已到且无人确认，方案按未确认关闭",
                )

    # ------------------------------------------------------------ 接续方案

    def open_migration_proposal(
        self,
        proposal_id: str,
        *,
        source_enrollment_id: str,
        target_course_id: str,
        session_map: list[tuple[str, str]] | None = None,
        expires_in: timedelta = DEFAULT_CONFIRM_WINDOW,
        event_key: str | None = None,
    ) -> dict[str, Any]:
        """为“面授 -> 电话辅导”等承接生成带差异说明的方案（此时不发生任何迁移）。"""
        key = event_key or f"proposal:{proposal_id}"
        prior = self.journal.seen_keys().get(key)
        if prior is not None:
            return {"event_id": prior.event_id, "proposal_id": proposal_id,
                    "plan": prior.payload["plan"]}
        source = self._require_enrollment(source_enrollment_id)
        target_course = self._require_course(target_course_id)
        if source.status not in (SEAT,):
            raise DomainError("只有在册报名可以发起接续迁移")
        if source.course_id == target_course_id:
            raise DomainError("目标课程与原课程相同，无需迁移")
        if target_course.cancelled:
            raise DomainError(f"目标课程已取消：{target_course_id}")
        if source.needed_support not in target_course.supports:
            raise IncompatibleCourseError(
                f"目标课程无法提供{SUPPORT_LABEL[source.needed_support]}支持；"
                "高支持需求不能因迁移自动降级"
            )
        mapping = self._build_session_map(source, target_course, session_map)
        if not mapping:
            raise DomainError("没有可承接的剩余课次")
        if self.registry.seats_used(target_course_id) >= target_course.capacity:
            raise DomainError("目标课程暂无空余席位")

        now = self.clock.now()
        remaining_ids = set(
            source.remaining_session_ids(self._require_course(source.course_id), now)
        )
        mapped_ids = {row["source_session_id"] for row in mapping}
        closes_source = mapped_ids >= remaining_ids
        plan = self._migration_plan(source, target_course, mapping)
        plan["closes_source"] = closes_source
        return self._open_proposal(
            proposal_id,
            kind="MIGRATION",
            source=source,
            target_course_id=target_course_id,
            plan=plan,
            expires_in=expires_in,
            event_key=event_key,
        )

    def _build_session_map(
        self,
        source: EnrollmentState,
        target_course: CourseState,
        explicit: list[tuple[str, str]] | None,
    ) -> list[dict[str, str]]:
        source_course = self._require_course(source.course_id)
        remaining = source.remaining_session_ids(source_course, self.clock.now())
        now = self.clock.now()
        consumed: set[str] = set()
        for enr in self.registry.course_enrollments(target_course.course_id):
            consumed |= enr.scoped_sessions
        consumed |= self.registry.reserved_sessions(target_course.course_id)
        available = [
            (sid, s)
            for sid, s in sorted(target_course.sessions.items(), key=lambda kv: kv[1].scheduled_at)
            if sid not in consumed and s.state == "SCHEDULED" and s.scheduled_at >= now
        ]
        if explicit:
            result = []
            used: set[str] = set()
            for src_sid, tgt_sid in explicit:
                if src_sid not in remaining:
                    raise DomainError(f"课次 {src_sid} 不是可承接的剩余课次")
                tgt = target_course.sessions.get(tgt_sid)
                if tgt is None or tgt_sid in used or tgt_sid in consumed or tgt.state != "SCHEDULED":
                    raise DomainError(f"目标课次 {tgt_sid} 不可用")
                used.add(tgt_sid)
                result.append({"source_session_id": src_sid, "target_session_id": tgt_sid})
            return result
        # 自动配对：优先同学员时段，再按时间先后
        result = []
        pool = list(available)
        for src_sid in remaining:
            src = source_course.sessions[src_sid]
            choice = next((x for x in pool if x[1].slot in source.slots), None) or (
                pool[0] if pool else None
            )
            if choice is None:
                break
            pool.remove(choice)
            result.append({"source_session_id": src_sid, "target_session_id": choice[0]})
        return result

    def _migration_plan(
        self, source: EnrollmentState, target_course: CourseState, mapping: list[dict[str, str]]
    ) -> dict[str, Any]:
        source_course = self._require_course(source.course_id)
        grant = self.registry.learner_entitlement(source.learner_id)
        rows = []
        for item in mapping:
            s = source_course.sessions[item["source_session_id"]]
            t = target_course.sessions[item["target_session_id"]]
            rows.append(
                {
                    "source_session_id": s.session_id,
                    "source_slot": s.slot,
                    "source_scheduled_at": s.scheduled_at,
                    "target_session_id": t.session_id,
                    "target_slot": t.slot,
                    "target_scheduled_at": t.scheduled_at,
                }
            )
        differences = [
            f"上课形态：{source_course.modality} -> {target_course.modality}",
            (
                f"教师：{source_course.teacher.teacher_id}（{source_course.teacher.qualification}）"
                f" -> {target_course.teacher.teacher_id}（{target_course.teacher.qualification}）"
            ),
        ]
        for row in rows:
            if row["source_slot"] != row["target_slot"]:
                differences.append(
                    f"课次时段调整：{row['source_session_id']}（{row['source_slot']}）"
                    f" -> {row['target_session_id']}（{row['target_slot']}）"
                )
        support_note = (
            f"辅助支持保持{SUPPORT_LABEL[source.needed_support]}等级，不因迁移降级"
        )
        differences.append(support_note)
        plan: dict[str, Any] = {
            "source_course_id": source.course_id,
            "target_course_id": target_course.course_id,
            "remaining_session_count": len(mapping),
            "teacher": {
                "from": {
                    "teacher_id": source_course.teacher.teacher_id,
                    "qualification": source_course.teacher.qualification,
                },
                "to": {
                    "teacher_id": target_course.teacher.teacher_id,
                    "qualification": target_course.teacher.qualification,
                },
            },
            "modality": {"from": source_course.modality, "to": target_course.modality},
            "support": {
                "needed": source.needed_support,
                "granted": source.needed_support,
                "target_provides": source.needed_support in target_course.supports,
                "decision": "KEEP",
            },
            "sessions": rows,
            "subsidy": {
                "entitlement_id": grant.entitlement_id if grant else None,
                "total_units": grant.total_units if grant else 0,
                "used_units": grant.used_units if grant else 0,
                "remaining_units": (grant.total_units - grant.used_units) if grant else 0,
                "action": "RELINK_SAME_ENTITLEMENT" if grant else "NONE",
                "note": "补贴权益随报名链原子过户，剩余单位与余额保持不变，不在新课程重新占名额",
            },
            "differences": differences,
        }
        return plan

    def _open_proposal(
        self,
        proposal_id: str,
        *,
        kind: str,
        source: EnrollmentState,
        target_course_id: str | None,
        plan: dict[str, Any],
        expires_in: timedelta,
        event_key: str | None,
    ) -> dict[str, Any]:
        if proposal_id in self.registry.proposals:
            raise DomainError(f"方案已存在：{proposal_id}")
        now = self.clock.now()
        eligible = self._eligible_parties(source.learner_id, now)
        plan = dict(plan)
        plan["eligible_parties"] = list(eligible)
        key = event_key or f"proposal:{proposal_id}"
        # 同一“生成方案”命令重发：方案为不可变文档，直接取回原文档
        prior = self.journal.seen_keys().get(key)
        if prior is not None:
            return {"event_id": prior.event_id, "proposal_id": proposal_id, "plan": prior.payload["plan"]}
        if proposal_id in self.registry.proposals:
            raise DomainError(f"方案已存在：{proposal_id}")
        content = {
            "kind": kind,
            "learner_id": source.learner_id,
            "source_enrollment_id": source.enrollment_id,
            "source_course_id": source.course_id,
            "target_course_id": target_course_id,
            "plan": plan,
            "expires_at": now + expires_in,
        }
        event = self._emit_idempotent(
            key,
            content,
            PROPOSAL_OPENED,
            AGG_PROPOSAL,
            proposal_id,
            f"生成{kind}接续方案 {proposal_id}，等待本人或有效照护人确认",
        )
        return {"event_id": event.event_id, "proposal_id": proposal_id, "plan": plan}

    def _eligible_parties(self, learner_id: str, at: datetime) -> list[dict[str, str]]:
        parties = [{"party_id": learner_id, "role": "SELF"}]
        for cid in self.registry.active_caregivers(learner_id, at):
            parties.append({"party_id": cid, "role": "CAREGIVER"})
        return parties

    # ------------------------------------------------------------ 方案确认

    def record_choice(
        self,
        proposal_id: str,
        *,
        party_id: str,
        decision: str,
        channel: str = "counter",
        event_key: str | None = None,
    ) -> dict[str, Any]:
        """提交确认/拒绝。两方选择不一致时进入待核，不会互相覆盖。

        channel 可区分电话确认（phone）等；同一键按内容判定重放。
        """
        prop = self.registry.proposals.get(proposal_id)
        key = event_key or f"choice:{proposal_id}:{party_id}:{decision}:{channel}"
        content = {
            "party_id": party_id,
            "role": self._party_role(prop, party_id) if prop else "UNKNOWN",
            "decision": decision,
            "channel": channel,
        }
        # 同一条确认消息重发（含授权已变化、方案已结算的情形）按原事件空转
        prior = self._replay_guard(key, content)
        if prior is not None:
            return self._choice_snapshot(proposal_id)
        if prop is None:
            raise DomainError(f"方案不存在：{proposal_id}")
        if prop.status != "OPEN":
            raise ProposalClosedError(f"方案当前状态为 {prop.status}，不能再提交选择")
        if decision not in (CONFIRM, REJECT):
            raise DomainError("decision 只能是 confirm 或 reject")
        self._assert_authorized(prop, party_id)

        content["role"] = self._party_role(prop, party_id)
        self._emit_idempotent(
            key,
            content,
            PROPOSAL_CHOICE_RECORDED,
            AGG_PROPOSAL,
            proposal_id,
            f"{party_id} 通过{channel}提交{'同意' if decision == CONFIRM else '拒绝'}",
        )
        return self._settle(proposal_id)

    def _assert_authorized(self, prop: Any, party_id: str) -> None:
        if party_id == prop.learner_id:
            return
        if party_id in self.registry.active_caregivers(prop.learner_id, self.clock.now()):
            return
        raise AuthorizationError(
            f"{party_id} 既非学员本人，也不是 {prop.learner_id} 当前有效的授权照护人"
        )

    @staticmethod
    def _party_role(prop: Any, party_id: str) -> str:
        if party_id == prop.learner_id:
            return "SELF"
        return "CAREGIVER"

    def _live_voters(self, prop: Any) -> dict[str, str]:
        """仍有效授权方的最新选择（被更换掉的旧照护人选择作废）。"""
        now = self.clock.now()
        valid = {prop.learner_id}
        valid |= set(self.registry.active_caregivers(prop.learner_id, now))
        return {
            pid: c.decision
            for pid, c in prop.choices.items()
            if not c.superseded and pid in valid
        }

    def _choice_snapshot(self, proposal_id: str) -> dict[str, Any]:
        """当前选择状态快照：重放返回与首次提交返回保持同一形状。"""
        prop = self.registry.proposals[proposal_id]
        if prop.status != "OPEN":
            return {"proposal_id": proposal_id, "status": prop.status}
        now = self.clock.now()
        eligible = {prop.learner_id} | set(
            self.registry.active_caregivers(prop.learner_id, now)
        )
        voted = set(self._live_voters(prop))
        waiting = sorted(eligible - voted)
        if waiting:
            return {"proposal_id": proposal_id, "status": "OPEN", "waiting_for": waiting}
        return {"proposal_id": proposal_id, "status": "OPEN"}

    def _settle(self, proposal_id: str) -> dict[str, Any]:
        """根据当前有效授权方及其选择收敛方案。

        * 有效选择中既有同意又有拒绝 -> 待核（两方都不能抢先覆盖）；
        * 仅有一名有效授权方（通常是学员本人）-> 一票即决；
        * 存在多名有效授权方 -> 必须全部表态才收敛，未表态保持开放，
          沉默方由确认期限到期时的规则处理。
        """
        prop = self.registry.proposals[proposal_id]
        now = self.clock.now()
        eligible = {prop.learner_id}
        eligible |= set(self.registry.active_caregivers(prop.learner_id, now))
        votes = self._live_voters(prop)
        decisions = set(votes.values())

        if CONFIRM in decisions and REJECT in decisions:
            self._emit(
                PROPOSAL_DISPUTED, AGG_PROPOSAL, proposal_id,
                {"choices": sorted(votes)},
                "学员与照护人选择不一致，方案进入待核",
            )
            return {"proposal_id": proposal_id, "status": "DISPUTED"}

        if len(eligible) > 1 and not eligible.issubset(votes):
            return self._choice_snapshot(proposal_id)

        if not votes:
            return self._choice_snapshot(proposal_id)

        decision = next(iter(decisions))
        if decision == REJECT:
            self._emit(
                PROPOSAL_REJECTED, AGG_PROPOSAL, proposal_id,
                {}, "有效授权方拒绝方案",
            )
            return {"proposal_id": proposal_id, "status": "REJECTED"}
        self._effectuate(proposal_id)
        return {"proposal_id": proposal_id, "status": "CONFIRMED"}

    def resolve_dispute(
        self,
        proposal_id: str,
        *,
        decision: str,
        staff_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        """教务对待核方案作出裁决；裁决同意才执行迁移/变更。"""
        prop = self.registry.proposals.get(proposal_id)
        if prop is None:
            raise DomainError(f"方案不存在：{proposal_id}")
        if prop.status != "DISPUTED":
            raise ProposalClosedError(f"方案状态为 {prop.status}，无需裁决")
        if decision not in (CONFIRM, REJECT):
            raise DomainError("decision 只能是 confirm 或 reject")
        if decision == CONFIRM:
            self._effectuate(proposal_id, resolution={"staff_id": staff_id, "note": note})
            return {"proposal_id": proposal_id, "status": "RESOLVED_CONFIRMED"}
        self._emit_idempotent(
            f"settle-resolve:{proposal_id}",
            {"decision": REJECT, "staff_id": staff_id, "note": note, "effectuated": False},
            PROPOSAL_RESOLVED, AGG_PROPOSAL, proposal_id,
            "教务裁决维持拒绝，方案关闭",
        )
        return {"proposal_id": proposal_id, "status": "RESOLVED_REJECTED"}

    def _effectuate(self, proposal_id: str, resolution: dict[str, Any] | None = None) -> None:
        """执行已确认方案。

        派生事件使用确定性幂等键，且各执行分支自带“已执行则短路”，
        因此在执行中途崩溃、重启后由截止点扫描重入时不会重复迁移或重复结算。
        """
        prop = self.registry.proposals[proposal_id]
        if prop.kind == "MIGRATION":
            self._effectuate_migration(prop)
        elif prop.kind == "TEACHER_CHANGE":
            self._effectuate_teacher_change(prop)
        elif prop.kind == "CANCELLATION":
            self._effectuate_cancellation_choice(prop)
        else:  # pragma: no cover - 防御
            raise DomainError(f"未知方案类型：{prop.kind}")
        if resolution is None:
            self._emit_idempotent(
                f"settle-confirm:{proposal_id}", {"effectuated": True},
                PROPOSAL_CONFIRMED, AGG_PROPOSAL, proposal_id,
                "方案确认并执行完成",
            )
        else:
            self._emit_idempotent(
                f"settle-resolve:{proposal_id}",
                {"decision": CONFIRM, "effectuated": True, **resolution},
                PROPOSAL_RESOLVED, AGG_PROPOSAL, proposal_id,
                "待核方案经教务裁决同意并执行完成",
            )

    def _effectuate_migration(self, prop: Any) -> str:
        target_enrollment_id = f"ENR-MIG-{prop.proposal_id}"
        # 崩溃恢复短路：承接报名已存在说明派生事件已写出，只补终态事件
        if target_enrollment_id in self.registry.enrollments:
            return target_enrollment_id
        source = self.registry.enrollments[prop.source_enrollment_id]
        target_course_id = prop.target_course_id
        target_course = self._require_course(target_course_id)
        final = bool(prop.plan.get("closes_source", True))
        mapping = [
            (row["source_session_id"], row["target_session_id"]) for row in prop.plan["sessions"]
        ]
        target_ids = [t for _, t in mapping]
        target_slots = sorted({target_course.sessions[t].slot for t in target_ids})

        self._emit_idempotent(
            f"migrate-enroll:{prop.proposal_id}",
            {
                "learner_id": prop.learner_id,
                "course_id": target_course_id,
                "slots": target_slots,
                "needed_support": source.needed_support,
                "granted_support": source.needed_support,
                "status": SEAT,
                "waitlist_seq": None,
                "source_enrollment_id": source.enrollment_id,
                "scoped_session_ids": target_ids,
                "frozen_session_ids": [],
            },
            ENROLLMENT_PLACED,
            AGG_ENROLLMENT,
            target_enrollment_id,
            f"承接报名：{source.enrollment_id} -> {target_enrollment_id}（{target_course_id}）",
        )
        self._emit_idempotent(
            f"migrate-sessions:{prop.proposal_id}",
            {
                "target_enrollment_id": target_enrollment_id,
                "target_course_id": target_course_id,
                "session_map": [
                    {"source_session_id": s, "target_session_id": t} for s, t in mapping
                ],
                "final": final,
            },
            SESSIONS_MIGRATED,
            AGG_ENROLLMENT,
            source.enrollment_id,
            ("最后一批：" if final else "分批迁移：")
            + f"承接 {len(mapping)} 个剩余课次；已完成课次与练习成果保留在原记录",
        )
        # 权益按学员迁移链过户：从当前链头（可能已是上一批承接报名）原子过户，
        # 全程只有一份权益，绝不因分批而重复占用。
        grant = self.registry.learner_entitlement(prop.learner_id)
        if grant is not None and grant.current_enrollment_id != target_enrollment_id:
            self._emit_idempotent(
                f"migrate-relink:{prop.proposal_id}",
                {
                    "entitlement_id": grant.entitlement_id,
                    "from_enrollment_id": grant.current_enrollment_id,
                    "to_enrollment_id": target_enrollment_id,
                    "to_course_id": target_course_id,
                },
                SUBSIDY_RELINKED,
                AGG_ENROLLMENT,
                grant.current_enrollment_id,
                f"补贴权益 {grant.entitlement_id} 原子过户到承接报名，余额守恒",
            )
        return target_enrollment_id

    # ------------------------------------------------------------ 教师变更/取消

    def propose_teacher_change(
        self,
        course_id: str,
        *,
        new_teacher_id: str,
        new_qualification: str,
        expires_in: timedelta = DEFAULT_CONFIRM_WINDOW,
        id_prefix: str = "PTC",
    ) -> list[str]:
        """教师变更：为每位在册学员生成带资历差异说明的方案。"""
        course = self._require_course(course_id)
        proposal_ids: list[str] = []
        now = self.clock.now()
        active = [
            e for e in self.registry.course_enrollments(course_id)
            if e.status in (SEAT, WAITLIST)
        ]
        for idx, enr in enumerate(active, start=1):
            pid = f"{id_prefix}-{course_id}-{idx}"
            differences = [
                (
                    f"教师：{course.teacher.teacher_id}（{course.teacher.qualification}）"
                    f" -> {new_teacher_id}（{new_qualification}）"
                ),
                "课程容量、可提供支持与课次表不变",
            ]
            plan = {
                "source_course_id": course_id,
                "target_course_id": None,
                "remaining_session_count": len(enr.remaining_session_ids(course)),
                "teacher": {
                    "from": {
                        "teacher_id": course.teacher.teacher_id,
                        "qualification": course.teacher.qualification,
                    },
                    "to": {"teacher_id": new_teacher_id, "qualification": new_qualification},
                },
                "support": {"decision": "UNCHANGED"},
                "sessions": [],
                "subsidy": {"action": "UNCHANGED"},
                "differences": differences,
            }
            opened = self._open_proposal(
                pid,
                kind="TEACHER_CHANGE",
                source=enr,
                target_course_id=None,
                plan=plan,
                expires_in=expires_in,
                event_key=f"proposal:{pid}",
            )
            proposal_ids.append(opened["proposal_id"])
        return proposal_ids

    def _effectuate_teacher_change(self, prop: Any) -> None:
        course = self.registry.courses[prop.source_course_id]
        new_id = prop.plan["teacher"]["to"]["teacher_id"]
        new_qual = prop.plan["teacher"]["to"]["qualification"]
        if course.teacher.teacher_id == new_id and course.teacher.qualification == new_qual:
            return  # 其他学员的方案已执行过课程级变更，不重复产生事件
        self._emit_idempotent(
            f"teacher-change:{course.course_id}:{new_id}",
            {"teacher_id": new_id, "teacher_qualification": new_qual},
            TEACHER_CHANGED,
            AGG_COURSE,
            course.course_id,
            f"课程教师变更为 {new_id}（{new_qual}）",
        )

    def cancel_course(
        self,
        course_id: str,
        *,
        alternative_course_ids: list[str] | None = None,
        expires_in: timedelta = DEFAULT_CONFIRM_WINDOW,
    ) -> list[str]:
        """课程取消：课程级取消事件 + 为每位在册学员生成去向方案（可承接课程及差异）。"""
        course = self._require_course(course_id)
        self._emit_idempotent(
            f"cancel:{course_id}",
            {"reason": "ADMIN_CANCELLED"},
            COURSE_CANCELLED, AGG_COURSE, course_id,
            f"课程 {course_id} 取消，未开课次全部取消",
        )
        alternatives = []
        for alt_id in alternative_course_ids or []:
            alt = self._require_course(alt_id)
            alternatives.append(
                {
                    "course_id": alt_id,
                    "modality": alt.modality,
                    "provides_full_support": "full" in alt.supports,
                    "free_seats": alt.capacity - self.registry.seats_used(alt_id),
                }
            )
        proposal_ids = []
        active = [
            e for e in self.registry.course_enrollments(course_id) if e.status in (SEAT, WAITLIST)
        ]
        for idx, enr in enumerate(active, start=1):
            pid = f"PCC-{course_id}-{idx}"
            plan = {
                "source_course_id": course_id,
                "target_course_id": None,
                "remaining_session_count": len(enr.remaining_session_ids(course)),
                "teacher": {
                    "from": {
                        "teacher_id": course.teacher.teacher_id,
                        "qualification": course.teacher.qualification,
                    },
                    "to": None,
                },
                "support": {"needed": enr.needed_support, "decision": "PRESERVE_ON_TRANSFER"},
                "sessions": [],
                "subsidy": {"action": "RETURN_OR_RELINK"},
                "alternatives": alternatives,
                "differences": [
                    "原课程取消，未开课次不再进行",
                    "可在备选兼容课程中承接剩余课次，补贴余额守恒、支持等级不降",
                ],
            }
            opened = self._open_proposal(
                pid,
                kind="CANCELLATION",
                source=enr,
                target_course_id=None,
                plan=plan,
                expires_in=expires_in,
                event_key=f"proposal:{pid}",
            )
            proposal_ids.append(opened["proposal_id"])
        return proposal_ids

    def _effectuate_cancellation_choice(self, prop: Any) -> None:
        enr = self.registry.enrollments[prop.source_enrollment_id]
        if enr.status == "ENDED":
            return  # 崩溃恢复重入：派生事件已写出
        self._emit_idempotent(
            f"cancel-end:{enr.enrollment_id}",
            {"reason": "COURSE_CANCELLED_ACCEPTED"},
            ENROLLMENT_ENDED, AGG_ENROLLMENT, enr.enrollment_id,
            "学员确认课程取消安排，原报名结束",
        )
        grant = self.registry.learner_entitlement(prop.learner_id)
        if grant is not None and grant.current_enrollment_id == enr.enrollment_id:
            self._emit_idempotent(
                f"cancel-return:{enr.enrollment_id}",
                {"entitlement_id": grant.entitlement_id},
                SUBSIDY_RETURNED, AGG_ENROLLMENT, enr.enrollment_id,
                f"补贴权益 {grant.entitlement_id} 随课程取消结清返还，余额记录保留",
            )

    # ------------------------------------------------------------ 历史时点查询

    def state_at(self, as_of: datetime) -> Registry:
        """按历史时点重放事件，得到该时点的课程归属、支持决定与权益余额。"""
        return fold(self.journal.replay(as_of=as_of))

    def course_assignment_at(self, learner_id: str, as_of: datetime) -> list[str]:
        return self.state_at(as_of).courses_of(learner_id, as_of)

    def support_decision_at(self, enrollment_id: str, as_of: datetime) -> dict[str, Any] | None:
        reg = self.state_at(as_of)
        enr = reg.enrollments.get(enrollment_id)
        if enr is None:
            return None
        course = reg.courses.get(enr.course_id)
        return {
            "enrollment_id": enrollment_id,
            "course_id": enr.course_id,
            "needed_support": enr.needed_support,
            "granted_support": enr.granted_support,
            "course_provides": sorted(course.supports) if course else [],
            "paused": enr.paused,
            "status": enr.status,
        }

    def entitlement_balance_at(self, learner_id: str, as_of: datetime) -> dict[str, Any] | None:
        return self.state_at(as_of).subsidy_balance(learner_id)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
