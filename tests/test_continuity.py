"""中断接续服务的场景测试。

覆盖：住院面授转电话、暂停冻结与成果保留、席位/候补决定、
分批迁移与权益守恒、照护人更换、冲突待核、截止点候补提升、
幂等重放、历史时点查询、以及基于 JSONL 日志的服务重启恢复。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from src.continuity import ControlledClock, ContinuityService, EventJournal
from src.continuity.clock import cst
from src.continuity.errors import (
    AuthorizationError,
    IncompatibleCourseError,
    ProposalClosedError,
    ReplayConflictError,
)


def weekly_sessions(prefix: str, start, count: int = 8, slot: str = "TUE_AM"):
    return [(f"{prefix}-S{i + 1}", start + timedelta(weeks=i), slot) for i in range(count)]


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ControlledClock(cst(2026, 9, 1, 9, 0))
        self.svc = ContinuityService(self.clock)
        # 面授班：容量 2，周二上午；电话班：容量 5，提供全部支持等级
        self.svc.publish_course(
            "IP1",
            title="智能手机面授班",
            modality="in_person",
            teacher_id="T-LI",
            teacher_qualification="高级讲师",
            capacity=2,
            supports=["standard", "enhanced", "full"],
            slot_labels=["TUE_AM"],
            sessions=weekly_sessions("IP1", cst(2026, 9, 8, 9, 0)),
        )
        self.svc.publish_course(
            "PH1",
            title="电话辅导班",
            modality="phone",
            teacher_id="T-WANG",
            teacher_qualification="中级辅导员",
            capacity=5,
            supports=["standard", "enhanced", "full"],
            slot_labels=["TUE_AM"],
            sessions=weekly_sessions("PH1", cst(2026, 9, 8, 9, 0)),
        )

    def hospital_transfer_setup(self):
        """构造住院前状态：L1 正式席位（高支持+8 单位补贴），完成 S1 与一次练习。"""
        svc = self.svc
        svc.grant_caregiver("L1", "CG1")
        svc.enroll(
            "E1", learner_id="L1", course_id="IP1", slots=["TUE_AM"],
            needed_support="full", subsidy_units=8,
        )
        svc.record_practice("L1", "P1", title="微信视频练习")
        self.clock.freeze(cst(2026, 9, 2, 9, 0))
        svc.enroll("E2", learner_id="L2", course_id="IP1", slots=["TUE_AM"],
                   needed_support="standard")
        e3 = svc.enroll("E3", learner_id="L3", course_id="IP1", slots=["TUE_AM"],
                        needed_support="standard", subsidy_units=8)
        e4 = svc.enroll("E4", learner_id="L4", course_id="IP1", slots=["TUE_AM"],
                        needed_support="standard")
        self.assertEqual(e3["waitlist_seq"], 1)
        self.assertEqual(e4["waitlist_seq"], 2)
        # S1（9/8）完成
        self.clock.freeze(cst(2026, 9, 14, 10, 0))
        svc.record_attendance("E1", "IP1-S1")
        # 9/15 当天住院：S2（9/15 09:00）已开始未能参加，冻结此后课次
        self.clock.freeze(cst(2026, 9, 15, 10, 0))
        svc.pause("E1", reason="短期住院")
        return e3, e4


class TestEnrollmentRules(ServiceTestBase):
    def test_seat_and_waitlist_decision(self) -> None:
        r1 = self.svc.enroll("A1", learner_id="LA", course_id="IP1",
                             slots=["TUE_AM"], needed_support="standard")
        r2 = self.svc.enroll("A2", learner_id="LB", course_id="IP1",
                             slots=["TUE_AM"], needed_support="standard")
        r3 = self.svc.enroll("A3", learner_id="LC", course_id="IP1",
                             slots=["TUE_AM"], needed_support="standard")
        self.assertEqual(r1["status"], "SEAT")
        self.assertEqual(r2["status"], "SEAT")
        self.assertEqual(r3["status"], "WAITLIST")
        self.assertEqual(r3["waitlist_seq"], 1)

    def test_unsupported_need_is_rejected_not_downgraded(self) -> None:
        self.svc.publish_course(
            "PH-BASIC", title="基础电话班", modality="phone", teacher_id="T-Z",
            teacher_qualification="初级", capacity=5, supports=["standard"],
            slot_labels=["TUE_AM"],
            sessions=weekly_sessions("PB", cst(2026, 10, 6, 9, 0)),
        )
        with self.assertRaises(IncompatibleCourseError):
            self.svc.enroll("A1", learner_id="LA", course_id="PH-BASIC",
                            slots=["TUE_AM"], needed_support="full")

    def test_waitlist_does_not_consume_subsidy(self) -> None:
        self.svc.enroll("A1", learner_id="LA", course_id="IP1",
                        slots=["TUE_AM"], needed_support="standard", subsidy_units=8)
        self.svc.enroll("A2", learner_id="LB", course_id="IP1",
                        slots=["TUE_AM"], needed_support="standard", subsidy_units=8)
        self.svc.enroll("A3", learner_id="LC", course_id="IP1",
                        slots=["TUE_AM"], needed_support="standard", subsidy_units=8)
        self.assertEqual(self.svc.registry.subsidy_used("IP1"), 2)
        self.assertIsNone(self.svc.registry.subsidy_balance("LC"))


class TestPausePreservation(ServiceTestBase):
    def test_pause_freezes_only_future_sessions_and_keeps_everything(self) -> None:
        self.hospital_transfer_setup()
        enr = self.svc.registry.enrollments["E1"]
        # S1 已出勤、S2 住院当天错过，冻结从 S3 开始的未开课次
        self.assertIn("IP1-S1", enr.attended_sessions)
        self.assertNotIn("IP1-S2", enr.frozen_sessions)
        self.assertEqual(
            sorted(enr.frozen_sessions),
            [f"IP1-S{i}" for i in range(3, 9)],
        )
        # 练习成果仍在；暂停不改变席位
        self.assertIn("P1", self.svc.registry.completed_practices("L1"))
        self.assertEqual(enr.status, "SEAT")
        # 候补序号不因他人暂停而重算
        self.assertEqual(self.svc.registry.enrollments["E3"].waitlist_seq, 1)

    def test_cannot_record_frozen_session(self) -> None:
        self.hospital_transfer_setup()
        with self.assertRaises(Exception):
            self.svc.record_attendance("E1", "IP1-S3")

    def test_pause_and_migration_are_enrollment_scoped_not_course_scoped(self) -> None:
        self.hospital_transfer_setup()
        # L1 迁出：课程级课次状态不被改动
        self.clock.freeze(cst(2026, 9, 20, 9, 0))
        self.svc.open_migration_proposal("PR1", source_enrollment_id="E1", target_course_id="PH1")
        self.svc.record_choice("PR1", party_id="CG1", decision="confirm", channel="phone")
        self.svc.record_choice("PR1", party_id="L1", decision="confirm")
        self.assertEqual(self.svc.registry.courses["IP1"].sessions["IP1-S4"].state, "SCHEDULED")

        # L2 同班未受任何影响：报名仍在面授班，同名课次可正常签到
        self.assertEqual(self.svc.registry.enrollments["E2"].status, "SEAT")
        self.clock.freeze(cst(2026, 9, 22, 10, 0))
        self.svc.record_attendance("E2", "IP1-S3")
        self.assertIn("IP1-S3", self.svc.registry.enrollments["E2"].attended_sessions)


class TestHospitalToPhoneTransfer(ServiceTestBase):
    def test_transfer_preserves_priority_entitlement_and_outcomes(self) -> None:
        self.hospital_transfer_setup()
        self.clock.freeze(cst(2026, 9, 20, 9, 0))

        opened = self.svc.open_migration_proposal(
            "PR1", source_enrollment_id="E1", target_course_id="PH1"
        )
        plan = opened["plan"]
        source_ids = {row["source_session_id"] for row in plan["sessions"]}
        # 只承接剩余课次：S1 已完成、S2 已错过，都不在方案里
        self.assertEqual(source_ids, {f"IP1-S{i}" for i in range(3, 9)})
        # 方案带差异说明：形态、教师、时段、支持保持
        diff_text = " ".join(plan["differences"])
        self.assertIn("in_person -> phone", diff_text)
        self.assertIn("辅助支持保持高支持等级", diff_text)
        # 高支持需求不降级
        self.assertEqual(plan["support"]["granted"], "full")
        # 待确认期间已预留目标席位
        self.assertEqual(self.svc.registry.seats_used("PH1"), 1)

        before = self.svc.registry.subsidy_balance("L1")
        self.assertEqual(before["remaining"], 7)

        # 照护人电话确认后仍需本人（两名有效授权方，意见需齐）
        r1 = self.svc.record_choice("PR1", party_id="CG1", decision="confirm",
                                    channel="phone", event_key="call-CG1-001")
        self.assertEqual(r1["status"], "OPEN")
        self.assertEqual(r1["waiting_for"], ["L1"])
        r2 = self.svc.record_choice("PR1", party_id="L1", decision="confirm",
                                    channel="counter")
        self.assertEqual(r2["status"], "CONFIRMED")

        reg = self.svc.registry
        # 原报名整体迁出并释放面授席位；候补按原序号提升，不重算
        self.assertEqual(reg.enrollments["E1"].status, "MIGRATED")
        promoted = self.svc.promote_waitlist("IP1")
        self.assertEqual(promoted, ["E3"])
        self.assertEqual(reg.enrollments["E3"].status, "SEAT")
        self.assertEqual(reg.enrollments["E4"].status, "WAITLIST")
        self.assertEqual(reg.enrollments["E4"].waitlist_seq, 2)

        # 承接报名：只覆盖承接课次、支持等级保持 full
        mig = reg.enrollments["ENR-MIG-PR1"]
        self.assertEqual(mig.granted_support, "full")
        self.assertEqual(len(mig.scoped_sessions), 6)

        # 补贴权益守恒：同一份权益过户，余额不变，面授班不再挂 L1 的名额
        after = reg.subsidy_balance("L1")
        self.assertEqual(after["entitlement_id"], before["entitlement_id"])
        self.assertEqual(after["remaining"], 7)
        self.assertEqual(after["course_id"], "PH1")
        l1_entitlements = [g for g in reg.entitlements.values() if g.learner_id == "L1"]
        self.assertEqual(len(l1_entitlements), 1)
        self.assertTrue(all(g.current_course_id != "IP1" for g in l1_entitlements))
        # 面授班当前补贴名额属于提升上来的 E3，不是 L1 重复占用
        self.assertEqual(reg.subsidy_used("IP1"), 1)
        self.assertEqual(reg.subsidy_used("PH1"), 1)

        # 住院前练习仍是已完成，没有被当作未完成
        self.assertIn("P1", reg.completed_practices("L1"))

    def test_target_without_full_support_is_incompatible(self) -> None:
        self.hospital_transfer_setup()
        self.clock.freeze(cst(2026, 9, 20, 9, 0))
        self.svc.publish_course(
            "PH-BASIC", title="基础电话班", modality="phone", teacher_id="T-Z",
            teacher_qualification="初级", capacity=5, supports=["standard"],
            slot_labels=["TUE_AM"],
            sessions=weekly_sessions("PB", cst(2026, 9, 29, 9, 0)),
        )
        with self.assertRaises(IncompatibleCourseError):
            self.svc.open_migration_proposal(
                "PR-BAD", source_enrollment_id="E1", target_course_id="PH-BASIC"
            )


class TestIdempotentReplay(ServiceTestBase):
    def test_attendance_same_content_is_noop_conflicting_content_rejected(self) -> None:
        self.svc.enroll("E1", learner_id="L1", course_id="IP1",
                        slots=["TUE_AM"], needed_support="standard")
        self.clock.freeze(cst(2026, 9, 8, 10, 0))
        a1 = self.svc.record_attendance("E1", "IP1-S1", event_key="kiosk-001")
        a2 = self.svc.record_attendance("E1", "IP1-S1", event_key="kiosk-001")
        self.assertEqual(a1.event_id, a2.event_id)
        self.assertEqual(len(self.svc.registry.enrollments["E1"].attended_sessions), 1)
        # 同一键不同内容 -> 冲突拒绝，不能把出勤改成未出勤
        with self.assertRaises(ReplayConflictError):
            self.svc.record_attendance("E1", "IP1-S1", attended=False, event_key="kiosk-001")
        self.assertIn("IP1-S1", self.svc.registry.enrollments["E1"].attended_sessions)

    def test_phone_confirmation_replay_by_content(self) -> None:
        self.hospital_transfer_setup()
        self.clock.freeze(cst(2026, 9, 20, 9, 0))
        self.svc.open_migration_proposal("PR1", source_enrollment_id="E1", target_course_id="PH1")
        c1 = self.svc.record_choice("PR1", party_id="CG1", decision="confirm",
                                    channel="phone", event_key="call-7788")
        c2 = self.svc.record_choice("PR1", party_id="CG1", decision="confirm",
                                    channel="phone", event_key="call-7788")
        self.assertEqual(c1, c2)
        events = [e for e in self.svc.journal.events if e.event_type == "PROPOSAL_CHOICE_RECORDED"]
        self.assertEqual(len(events), 1)
        with self.assertRaises(ReplayConflictError):
            self.svc.record_choice("PR1", party_id="CG1", decision="reject",
                                   channel="phone", event_key="call-7788")

    def test_command_redelivery_enroll_and_pause_is_idempotent(self) -> None:
        args = dict(learner_id="L1", course_id="IP1", slots=["TUE_AM"],
                    needed_support="full", subsidy_units=8, event_key="signup-001")
        r1 = self.svc.enroll("E1", **args)
        r2 = self.svc.enroll("E1", **args)
        self.assertEqual(r1, r2)
        self.assertEqual(len([e for e in self.svc.journal.events
                              if e.event_type == "ENROLLMENT_PLACED"]), 1)
        # 暂停命令在相同时刻重复投递：空转，不产生第二条冻结事件
        self.clock.freeze(cst(2026, 9, 15, 10, 0))
        self.svc.pause("E1", reason="住院", event_key="pause-001")
        self.svc.pause("E1", reason="住院", event_key="pause-001")
        self.assertEqual(len([e for e in self.svc.journal.events
                              if e.event_type == "PAUSE_FROZEN"]), 1)

    def test_migration_proposal_generation_redelivery_returns_same_document(self) -> None:
        self.hospital_transfer_setup()
        self.clock.freeze(cst(2026, 9, 20, 9, 0))
        o1 = self.svc.open_migration_proposal(
            "PR1", source_enrollment_id="E1", target_course_id="PH1",
            event_key="open-pr1")
        o2 = self.svc.open_migration_proposal(
            "PR1", source_enrollment_id="E1", target_course_id="PH1",
            event_key="open-pr1")
        self.assertEqual(o1["event_id"], o2["event_id"])
        self.assertEqual(o1["plan"]["sessions"], o2["plan"]["sessions"])
        self.assertEqual(len([e for e in self.svc.journal.events
                              if e.event_type == "PROPOSAL_OPENED"]), 1)


class TestHistoricalQueries(ServiceTestBase):
    def test_queries_at_historical_points(self) -> None:
        self.hospital_transfer_setup()
        self.clock.freeze(cst(2026, 9, 20, 9, 0))
        self.svc.open_migration_proposal("PR1", source_enrollment_id="E1", target_course_id="PH1")
        self.svc.record_choice("PR1", party_id="CG1", decision="confirm", channel="phone")
        self.svc.record_choice("PR1", party_id="L1", decision="confirm", channel="counter")

        # 9/10：仍归属面授班、未暂停、补贴 8 个单位未用
        at_sep10 = cst(2026, 9, 10, 9, 0)
        self.assertEqual(self.svc.course_assignment_at("L1", at_sep10), ["IP1"])
        support = self.svc.support_decision_at("E1", at_sep10)
        self.assertEqual(support["granted_support"], "full")
        self.assertFalse(support["paused"])
        balance = self.svc.entitlement_balance_at("L1", at_sep10)
        self.assertEqual(balance["remaining"], 8)
        self.assertEqual(balance["course_id"], "IP1")

        # 9/16：暂停中
        support_paused = self.svc.support_decision_at("E1", cst(2026, 9, 16, 9, 0))
        self.assertTrue(support_paused["paused"])

        # 9/25：归属电话班；S1 已用，余额 7
        at_sep25 = cst(2026, 9, 25, 9, 0)
        self.assertEqual(self.svc.course_assignment_at("L1", at_sep25), ["PH1"])
        balance_after = self.svc.entitlement_balance_at("L1", at_sep25)
        self.assertEqual(balance_after["remaining"], 7)
        self.assertEqual(balance_after["course_id"], "PH1")


class TestBatchMigration(ServiceTestBase):
    def test_pause_then_migrate_in_batches_with_single_entitlement(self) -> None:
        svc = self.svc
        svc.enroll("E1", learner_id="L1", course_id="IP1", slots=["TUE_AM"],
                   needed_support="full", subsidy_units=8)
        svc.record_attendance("E1", "IP1-S1")
        # 9/14 暂停：S2..S8 全部冻结（尚未发生）
        self.clock.freeze(cst(2026, 9, 14, 8, 0))
        svc.pause("E1", reason="住院，分批安排电话课")

        # 电话班课次从 9/29 起，便于两批都选未来课次
        svc.publish_course(
            "PH2", title="晚间电话班", modality="phone", teacher_id="T-CHEN",
            teacher_qualification="高级辅导员", capacity=3,
            supports=["standard", "enhanced", "full"], slot_labels=["TUE_AM"],
            sessions=weekly_sessions("PH2", cst(2026, 9, 29, 9, 0)),
        )
        self.clock.freeze(cst(2026, 9, 20, 9, 0))

        # 第一批：只承接 S2、S3，源报名不关闭
        svc.open_migration_proposal(
            "PR-B1", source_enrollment_id="E1", target_course_id="PH2",
            session_map=[("IP1-S2", "PH2-S1"), ("IP1-S3", "PH2-S2")],
        )
        # 无有效照护人时学员本人一票即决
        svc.record_choice("PR-B1", party_id="L1", decision="confirm")
        e1 = svc.registry.enrollments["E1"]
        self.assertEqual(e1.status, "SEAT")  # 非最终批次，源报名仍在册
        mig1 = svc.registry.enrollments["ENR-MIG-PR-B1"]
        self.assertEqual(sorted(mig1.scoped_sessions), ["PH2-S1", "PH2-S2"])
        balance = svc.registry.subsidy_balance("L1")
        self.assertEqual(balance["course_id"], "PH2")
        self.assertEqual(balance["remaining"], 7)

        # 第二批：承接 S4..S8，方案应自动判定为最终批
        svc.open_migration_proposal(
            "PR-B2", source_enrollment_id="E1", target_course_id="PH2",
        )
        plan2 = svc.registry.proposals["PR-B2"].plan
        self.assertTrue(plan2["closes_source"])
        second_sources = {row["source_session_id"] for row in plan2["sessions"]}
        self.assertEqual(second_sources, {f"IP1-S{i}" for i in range(4, 9)})
        # 第一批占用的目标课次不能再被第二批选走
        target_ids = {row["target_session_id"] for row in plan2["sessions"]}
        self.assertNotIn("PH2-S1", target_ids)
        self.assertNotIn("PH2-S2", target_ids)
        svc.record_choice("PR-B2", party_id="L1", decision="confirm")

        reg = svc.registry
        self.assertEqual(reg.enrollments["E1"].status, "MIGRATED")
        final_balance = reg.subsidy_balance("L1")
        self.assertEqual(final_balance["remaining"], 7)
        self.assertEqual(final_balance["course_id"], "PH2")
        # 全程只有一份权益
        entitlements = [g for g in reg.entitlements.values() if g.learner_id == "L1"]
        self.assertEqual(len(entitlements), 1)
        relinks = [e for e in svc.journal.events if e.event_type == "SUBSIDY_RELINKED"]
        self.assertEqual(len(relinks), 2)

        # 在第一批承接报名上课，权益在整条链上计数
        svc.record_attendance("ENR-MIG-PR-B1", "PH2-S1")
        self.assertEqual(reg.subsidy_balance("L1")["remaining"], 6)


class TestCaregiverReplacementAndDispute(ServiceTestBase):
    def test_revoked_caregiver_choice_is_void_and_new_one_decides(self) -> None:
        self.hospital_transfer_setup()
        self.clock.freeze(cst(2026, 9, 20, 9, 0))
        self.svc.open_migration_proposal("PR1", source_enrollment_id="E1", target_course_id="PH1")
        # 旧照护人先点了拒绝
        self.svc.record_choice("PR1", party_id="CG1", decision="reject", channel="phone")
        # 更换照护人：撤销 CG1，授权 CG2
        self.svc.revoke_caregiver("L1", "CG1")
        self.svc.grant_caregiver("L1", "CG2")
        # 旧照护人已无权操作
        with self.assertRaises(AuthorizationError):
            self.svc.record_choice("PR1", party_id="CG1", decision="confirm")
        # 学员与新照护人同意：旧选择作废，方案执行
        self.svc.record_choice("PR1", party_id="CG2", decision="confirm", channel="phone")
        result = self.svc.record_choice("PR1", party_id="L1", decision="confirm")
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(self.svc.registry.enrollments["E1"].status, "MIGRATED")

    def test_simultaneous_different_choices_become_disputed(self) -> None:
        self.hospital_transfer_setup()
        self.clock.freeze(cst(2026, 9, 20, 9, 0))
        self.svc.open_migration_proposal("PR1", source_enrollment_id="E1", target_course_id="PH1")
        self.svc.record_choice("PR1", party_id="L1", decision="confirm")
        result = self.svc.record_choice("PR1", party_id="CG1", decision="reject",
                                        channel="phone")
        self.assertEqual(result["status"], "DISPUTED")
        # 待核期间任一方都不能再覆盖
        with self.assertRaises(ProposalClosedError):
            self.svc.record_choice("PR1", party_id="CG1", decision="confirm")
        # 迁移尚未执行，席位仍被待核方案预留但未产生承接报名
        self.assertNotIn("ENR-MIG-PR1", self.svc.registry.enrollments)
        self.assertEqual(self.svc.registry.seats_used("PH1"), 1)
        # 教务裁决同意 -> 执行
        resolved = self.svc.resolve_dispute("PR1", decision="confirm",
                                            staff_id="STAFF-9", note="电话核实本人意愿")
        self.assertEqual(resolved["status"], "RESOLVED_CONFIRMED")
        self.assertEqual(self.svc.registry.enrollments["E1"].status, "MIGRATED")

    def test_unknown_party_rejected(self) -> None:
        self.hospital_transfer_setup()
        self.clock.freeze(cst(2026, 9, 20, 9, 0))
        self.svc.open_migration_proposal("PR1", source_enrollment_id="E1", target_course_id="PH1")
        with self.assertRaises(AuthorizationError):
            self.svc.record_choice("PR1", party_id="STRANGER", decision="confirm")


class TestCutoffPromotion(ServiceTestBase):
    def test_promotion_at_cutoff_keeps_order_and_releases_rest(self) -> None:
        # 容量 1 的短课程带提升截止点
        self.svc.publish_course(
            "C1", title="短期班", modality="in_person", teacher_id="T-LI",
            teacher_qualification="高级讲师", capacity=1,
            supports=["standard", "enhanced", "full"], slot_labels=["TUE_AM"],
            sessions=weekly_sessions("C1", cst(2026, 10, 6, 9, 0), count=4),
            promotion_cutoff=cst(2026, 9, 25, 18, 0),
        )
        self.svc.enroll("C1-E1", learner_id="W1", course_id="C1",
                        slots=["TUE_AM"], needed_support="full", subsidy_units=4)
        w2 = self.svc.enroll("C1-E2", learner_id="W2", course_id="C1",
                             slots=["TUE_AM"], needed_support="standard", subsidy_units=4)
        w3 = self.svc.enroll("C1-E3", learner_id="W3", course_id="C1",
                             slots=["TUE_AM"], needed_support="standard")
        self.assertEqual((w2["status"], w2["waitlist_seq"]), ("WAITLIST", 1))
        self.assertEqual(w3["waitlist_seq"], 2)

        # W1 在截止点前转去电话班，空出席位
        self.clock.freeze(cst(2026, 9, 20, 9, 0))
        self.svc.open_migration_proposal(
            "PR-W1", source_enrollment_id="C1-E1", target_course_id="PH1"
        )
        self.svc.record_choice("PR-W1", party_id="W1", decision="confirm")

        # 截止点前：系统不抢跑
        self.clock.freeze(cst(2026, 9, 25, 17, 59))
        self.svc.tick()
        self.assertEqual(self.svc.registry.enrollments["C1-E2"].status, "WAITLIST")
        # 越过截止点：W2 按序号提升并占用补贴名额，W3 释放
        self.svc.tick(to=cst(2026, 9, 25, 18, 0))
        self.assertEqual(self.svc.registry.enrollments["C1-E2"].status, "SEAT")
        self.assertEqual(self.svc.registry.subsidy_balance("W2")["course_id"], "C1")
        self.assertEqual(self.svc.registry.enrollments["C1-E3"].status, "ENDED")
        # 候补序号没有被重排（W2 保留报名时拿到的 1 号直到提升）
        self.assertIsNone(self.svc.registry.enrollments["C1-E2"].waitlist_seq)

    def test_confirmation_deadline_closes_proposal(self) -> None:
        self.hospital_transfer_setup()
        self.clock.freeze(cst(2026, 9, 20, 9, 0))
        self.svc.open_migration_proposal(
            "PR1", source_enrollment_id="E1", target_course_id="PH1",
            expires_in=timedelta(days=3),
        )
        self.assertEqual(self.svc.registry.seats_used("PH1"), 1)
        # 期限到达、无人确认 -> 过期，预留席位释放
        self.svc.tick(to=cst(2026, 9, 24, 0, 0))
        self.assertEqual(self.svc.registry.proposals["PR1"].status, "EXPIRED")
        self.assertEqual(self.svc.registry.seats_used("PH1"), 0)
        self.assertEqual(self.svc.registry.enrollments["E1"].status, "SEAT")


class TestTeacherChangeAndCancellation(ServiceTestBase):
    def test_teacher_change_proposals_with_difference_and_single_event(self) -> None:
        self.svc.enroll("E1", learner_id="L1", course_id="IP1",
                        slots=["TUE_AM"], needed_support="full")
        self.svc.enroll("E2", learner_id="L2", course_id="IP1",
                        slots=["TUE_AM"], needed_support="standard")
        ids = self.svc.propose_teacher_change(
            "IP1", new_teacher_id="T-ZHAO", new_qualification="资深讲师"
        )
        self.assertEqual(len(ids), 2)
        plan = self.svc.registry.proposals[ids[0]].plan
        self.assertIn("T-LI（高级讲师） -> T-ZHAO（资深讲师）", " ".join(plan["differences"]))
        # 两位学员各自确认；课程级 TEACHER_CHANGED 只产生一次
        for pid in ids:
            enr = self.svc.registry.proposals[pid]
            self.svc.record_choice(pid, party_id=enr.learner_id, decision="confirm")
        changes = [e for e in self.svc.journal.events if e.event_type == "TEACHER_CHANGED"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(self.svc.registry.courses["IP1"].teacher.teacher_id, "T-ZHAO")
        self.assertEqual(self.svc.registry.courses["IP1"].teacher.qualification, "资深讲师")

    def test_cancel_course_ends_enrollment_and_returns_subsidy(self) -> None:
        self.svc.enroll("E1", learner_id="L1", course_id="IP1",
                        slots=["TUE_AM"], needed_support="full", subsidy_units=8)
        self.clock.freeze(cst(2026, 9, 14, 10, 0))
        self.svc.record_attendance("E1", "IP1-S1")
        self.clock.freeze(cst(2026, 9, 16, 9, 0))
        ids = self.svc.cancel_course("IP1", alternative_course_ids=["PH1"])
        proposal = self.svc.registry.proposals[ids[0]]
        self.assertEqual(proposal.plan["alternatives"][0]["course_id"], "PH1")
        self.assertTrue(self.svc.registry.courses["IP1"].cancelled)
        self.svc.record_choice(ids[0], party_id="L1", decision="confirm")
        self.assertEqual(self.svc.registry.enrollments["E1"].status, "ENDED")
        self.assertIsNone(self.svc.registry.subsidy_balance("L1"))
        # 权益记录（含余额历史）仍可追溯
        ent = self.svc.registry.entitlements
        mine = [g for g in ent.values() if g.learner_id == "L1"][0]
        self.assertFalse(mine.active)
        self.assertEqual(mine.used_units, 1)


class TestRestartRecovery(ServiceTestBase):
    def _journal_service(self, path: str, clock: ControlledClock) -> ContinuityService:
        return ContinuityService(clock, EventJournal(path))

    def test_pending_confirmation_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "events.jsonl")
            svc = self._journal_service(path, self.clock)
            svc.publish_course(
                "IP1", title="面授班", modality="in_person", teacher_id="T-LI",
                teacher_qualification="高级讲师", capacity=2,
                supports=["standard", "enhanced", "full"], slot_labels=["TUE_AM"],
                sessions=weekly_sessions("IP1", cst(2026, 9, 8, 9, 0)),
            )
            svc.publish_course(
                "PH1", title="电话班", modality="phone", teacher_id="T-WANG",
                teacher_qualification="中级辅导员", capacity=5,
                supports=["standard", "enhanced", "full"], slot_labels=["TUE_AM"],
                sessions=weekly_sessions("PH1", cst(2026, 9, 8, 9, 0)),
            )
            svc.grant_caregiver("L1", "CG1")
            svc.enroll("E1", learner_id="L1", course_id="IP1", slots=["TUE_AM"],
                       needed_support="full", subsidy_units=8)
            self.clock.freeze(cst(2026, 9, 15, 10, 0))
            svc.pause("E1", reason="住院")
            self.clock.freeze(cst(2026, 9, 20, 9, 0))
            svc.open_migration_proposal("PR1", source_enrollment_id="E1", target_course_id="PH1")
            svc.record_choice("PR1", party_id="CG1", decision="confirm", channel="phone")

            # 服务重启：新实例 + 新时钟，从日志重建
            clock2 = ControlledClock(cst(2026, 9, 22, 9, 0))
            svc2 = self._journal_service(path, clock2)
            prop = svc2.registry.proposals["PR1"]
            self.assertEqual(prop.status, "OPEN")
            self.assertEqual(prop.plan["support"]["granted"], "full")
            self.assertTrue(svc2.registry.enrollments["E1"].paused)
            self.assertEqual(svc2.registry.subsidy_balance("L1")["remaining"], 8)
            # 重启后继续处理未决确认
            result = svc2.record_choice("PR1", party_id="L1", decision="confirm")
            self.assertEqual(result["status"], "CONFIRMED")
            self.assertEqual(svc2.registry.enrollments["E1"].status, "MIGRATED")

            # 再次重启：状态为已确认。同一条确认命令重发按重放空转；
            # 新的提交则被拒绝，且没有重复执行迁移
            clock3 = ControlledClock(cst(2026, 9, 23, 9, 0))
            svc3 = self._journal_service(path, clock3)
            self.assertEqual(svc3.registry.proposals["PR1"].status, "CONFIRMED")
            replay = svc3.record_choice("PR1", party_id="L1", decision="confirm")
            self.assertEqual(replay["status"], "CONFIRMED")
            with self.assertRaises(ProposalClosedError):
                svc3.record_choice("PR1", party_id="L1", decision="confirm",
                                   event_key="late-confirmation-002")
            migrations = [
                e for e in svc3.journal.events if e.event_type == "SESSIONS_MIGRATED"
            ]
            self.assertEqual(len(migrations), 1)

    def test_disputed_proposal_survives_restart_and_can_be_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "events.jsonl")
            svc = self._journal_service(path, self.clock)
            svc.publish_course(
                "IP1", title="面授班", modality="in_person", teacher_id="T-LI",
                teacher_qualification="高级讲师", capacity=2,
                supports=["standard", "enhanced", "full"], slot_labels=["TUE_AM"],
                sessions=weekly_sessions("IP1", cst(2026, 9, 8, 9, 0)),
            )
            svc.publish_course(
                "PH1", title="电话班", modality="phone", teacher_id="T-WANG",
                teacher_qualification="中级辅导员", capacity=5,
                supports=["standard", "enhanced", "full"], slot_labels=["TUE_AM"],
                sessions=weekly_sessions("PH1", cst(2026, 9, 8, 9, 0)),
            )
            svc.grant_caregiver("L1", "CG1")
            svc.enroll("E1", learner_id="L1", course_id="IP1", slots=["TUE_AM"],
                       needed_support="full", subsidy_units=8)
            self.clock.freeze(cst(2026, 9, 20, 9, 0))
            svc.open_migration_proposal("PR1", source_enrollment_id="E1", target_course_id="PH1")
            svc.record_choice("PR1", party_id="L1", decision="confirm")
            svc.record_choice("PR1", party_id="CG1", decision="reject", channel="phone")
            self.assertEqual(svc.registry.proposals["PR1"].status, "DISPUTED")

            clock2 = ControlledClock(cst(2026, 9, 21, 9, 0))
            svc2 = self._journal_service(path, clock2)
            self.assertEqual(svc2.registry.proposals["PR1"].status, "DISPUTED")
            out = svc2.resolve_dispute("PR1", decision="confirm", staff_id="STAFF-1")
            self.assertEqual(out["status"], "RESOLVED_CONFIRMED")
            self.assertEqual(svc2.registry.enrollments["E1"].status, "MIGRATED")


if __name__ == "__main__":
    unittest.main()
