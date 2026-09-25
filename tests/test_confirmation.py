"""场景五：教师变更/课程取消的差异方案、照护人更换与待核规则。"""

import unittest

from tests._world import F2F, PHONE, make_world, when
from src.continuity.errors import Conflict, NotFound, PendingReview
from src.continuity.model import HIGH


class ProposalConfirmationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = make_world(start="2026-08-31T09:00:00+08:00")
        svc = self.w.svc
        svc.place_enrollment("e1", "learner-1", F2F, time_slots=["wed_am", "phone_am"],
                             support_needs=["hearing_loop"], support_level=HIGH,
                             needs_subsidy=True)

    def _pid(self, event) -> str:
        return event.payload["proposal_id"]

    def test_teacher_change_generates_diff_and_confirms(self) -> None:
        svc = self.w.svc
        events = svc.teacher_changed(
            F2F,
            {"id": "teacher-b", "name": "李老师",
             "qualifications": ["老年照护一级", "急救证", "心理辅导证"]},
        )
        self.assertEqual(len(events), 1)
        pid = self._pid(events[0])
        diff = events[0].payload["diff"]
        self.assertTrue(diff["teacher"]["changed"])
        self.assertEqual(diff["teacher"]["before"]["id"], "teacher-a")
        self.assertEqual(diff["teacher"]["after"]["id"], "teacher-b")
        self.assertIn("心理辅导证", diff["teacher"]["qualification_diff"]["added"])

        # 确认前读模型仍是原教师；学员本人确认后才生效
        self.assertEqual(svc.state().course(F2F).teacher.teacher_id, "teacher-a")
        svc.confirm_proposal(pid, "learner-1", "accept")
        self.assertEqual(svc.state().course(F2F).teacher.teacher_id, "teacher-b")
        # 教师变更确认不产生迁移、不动席位
        self.assertIn("e1", svc.state().course(F2F).seated)
        self.assertEqual(svc.state().course(F2F).quota_used, 1)

    def test_duplicate_confirmation_is_idempotent(self) -> None:
        svc = self.w.svc
        pid = self._pid(svc.teacher_changed(F2F, {"id": "teacher-b", "name": "李老师"})[0])
        svc.confirm_proposal(pid, "learner-1", "accept")
        # 再次确认：无新事件、无异常
        result = svc.confirm_proposal(pid, "learner-1", "accept")
        self.assertEqual(result, [])

    def test_course_cancellation_proposes_alternatives(self) -> None:
        svc = self.w.svc
        events = svc.cancel_course(F2F, "教室装修停办", {"e1": PHONE})
        self.assertTrue(svc.state().course(F2F).cancelled)
        proposal_events = [e for e in events if e.event_type == "TRANSFER_PROPOSED"]
        self.assertEqual(len(proposal_events), 1)
        self.assertEqual(proposal_events[0].payload["target_course_id"], PHONE)
        self.assertEqual(proposal_events[0].payload["reason"], "course_cancelled")

    def test_parallel_divergent_choices_enter_hold_without_override(self) -> None:
        svc = self.w.svc
        # 学员与女儿（照护人）同时需要确认电话迁移
        svc.authorize_caregiver("learner-1", "cg-daughter", note="女儿")
        pid = self._pid(svc.propose_transfer("e1", PHONE))
        events = svc.submit_parallel_choices(
            pid, [("learner-1", "accept"), ("cg-daughter", "reject")]
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "PROPOSAL_HELD")

        state = svc.state()
        _, proposal = state.get_proposal(pid)
        self.assertEqual(proposal.status, "held")
        # 两种意见都保留，谁也没覆盖谁；迁移未发生
        self.assertEqual({c["decision"] for c in proposal.choices}, {"accept", "reject"})
        self.assertIn("e1", state.course(F2F).seated)
        self.assertEqual(len([e for e in state.enrollments.values() if e.origin]), 0)

        # 待核期间任何一方再次提交都被拒绝，不允许抢先覆盖
        with self.assertRaises(PendingReview):
            svc.confirm_proposal(pid, "learner-1", "accept")
        with self.assertRaises(PendingReview):
            svc.confirm_proposal(pid, "cg-daughter", "reject")

        # 教务核决接受后迁移才落地
        svc.resolve_held_proposal(pid, "staff-1", "accept")
        final = svc.state()
        self.assertEqual(final.enrollment("e1").status, "transferred")

    def test_parallel_identical_choices_apply_directly(self) -> None:
        svc = self.w.svc
        svc.authorize_caregiver("learner-1", "cg-daughter", note="女儿")
        pid = self._pid(svc.propose_transfer("e1", PHONE))
        events = svc.submit_parallel_choices(
            pid, [("learner-1", "accept"), ("cg-daughter", "accept")]
        )
        kinds = [e.event_type for e in events]
        self.assertIn("TRANSFER_CONFIRMED", kinds)
        self.assertNotIn("PROPOSAL_HELD", kinds)
        self.assertEqual(svc.state().enrollment("e1").status, "transferred")

    def test_caregiver_authorization_is_time_bounded(self) -> None:
        svc = self.w.svc
        # 第一位照护人授权到 10/15
        svc.authorize_caregiver(
            "learner-1", "cg-old",
            valid_from=when("2026-08-01T00:00:00+08:00"),
            valid_to=when("2026-10-15T00:00:00+08:00"),
        )
        # 10/13 发起接续方案（确认窗口 240 小时，至 10/23）
        self.w.clock.freeze(when("2026-10-13T09:00:00+08:00"))
        pid = self._pid(svc.propose_transfer("e1", PHONE))

        # 10/14：旧照护人仍有效
        self.w.clock.freeze(when("2026-10-14T12:00:00+08:00"))
        self.assertTrue(svc.state().active_caregiver("learner-1", "cg-old",
                                                      self.w.clock.now()))

        # 更换照护人：新增 10/16 起生效的新授权，旧授权记录不被改写
        svc.authorize_caregiver(
            "learner-1", "cg-new",
            valid_from=when("2026-10-16T00:00:00+08:00"),
            note="更换为儿子",
        )
        self.w.clock.freeze(when("2026-10-17T12:00:00+08:00"))
        self.assertFalse(svc.state().active_caregiver("learner-1", "cg-old",
                                                       self.w.clock.now()))
        self.assertTrue(svc.state().active_caregiver("learner-1", "cg-new",
                                                      self.w.clock.now()))
        with self.assertRaises(Conflict):
            svc.confirm_proposal(pid, "cg-old", "accept")
        svc.confirm_proposal(pid, "cg-new", "accept")
        self.assertEqual(svc.state().enrollment("e1").status, "transferred")

    def test_expired_proposal_cannot_be_confirmed(self) -> None:
        svc = self.w.svc
        pid = self._pid(svc.propose_transfer("e1", PHONE))  # 窗口 240 小时
        self.w.clock.advance(days=11)
        with self.assertRaises(Conflict):
            svc.confirm_proposal(pid, "learner-1", "accept")
        # 逾期扫描把方案标记拒绝
        swept = svc.sweep_expired_proposals()
        self.assertTrue(any(e.event_type == "PROPOSAL_REJECTED" for e in swept))
        self.assertEqual(svc.state().get_proposal(pid)[1].status, "rejected")


if __name__ == "__main__":
    unittest.main()
