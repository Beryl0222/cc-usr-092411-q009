"""场景四：面授 → 电话辅导的分批迁移、补贴守恒与支持不降级。"""

import unittest

from tests._world import F2F, PHONE, make_world, when
from src.continuity.errors import CompatibilityError
from src.continuity.model import HIGH


class TransferTest(unittest.TestCase):
    def setUp(self) -> None:
        # 8/31 报名；9/2、9/9 完成两次课；9/15 住院登记暂停（9/16 起的课次全部冻结）
        self.w = make_world(start="2026-08-31T09:00:00+08:00")
        svc = self.w.svc
        svc.place_enrollment("e1", "learner-1", F2F, time_slots=["wed_am", "phone_am"],
                             support_needs=["hearing_loop"], support_level=HIGH,
                             needs_subsidy=True)
        self.w.clock.freeze(when("2026-09-02T10:00:00+08:00"))
        svc.record_attendance("e1", "f2f-s1", "练习一")
        self.w.clock.freeze(when("2026-09-09T10:00:00+08:00"))
        svc.record_attendance("e1", "f2f-s2", "练习二")
        self.w.clock.freeze(when("2026-09-15T08:00:00+08:00"))
        svc.pause_enrollment("e1", reason="短期住院")
        self.assertEqual(svc.state().enrollment("e1").frozen_sessions,
                         ["f2f-s3", "f2f-s4", "f2f-s5", "f2f-s6", "f2f-s7", "f2f-s8"])
        # 补贴共 8 次，完成 2 次后余额 6 次
        self.assertEqual(svc.state().enrollment("e1").covered_sessions, 6)

    def _proposal_id(self, event) -> str:
        return event.payload["proposal_id"]

    def test_proposal_carries_remaining_and_diff(self) -> None:
        event = self.w.svc.propose_transfer("e1", PHONE, reason="medical_to_phone")
        p = event.payload
        self.assertEqual(p["sessions_carried"], 6)
        # 差异说明：模式变化、教师差异、支持不降级、补贴守恒都写清楚
        self.assertEqual(p["diff"]["mode"], {"before": "face_to_face", "after": "phone"})
        self.assertTrue(p["diff"]["teacher"]["changed"])
        self.assertEqual(p["diff"]["supports"]["level_before"], HIGH)
        self.assertEqual(p["diff"]["supports"]["level_after"], HIGH)
        self.assertEqual(p["diff"]["subsidy"]["covered_before"],
                         p["diff"]["subsidy"]["covered_after"])

    def test_incompatible_course_rejects_high_support_downgrade(self) -> None:
        self.w.svc.publish_course(
            "c-low", title="普通电话班", mode="phone",
            teacher={"id": "t-low"}, capacity=5, supports_offered=["hearing_loop"],
            high_support=False,
            sessions=[{"session_id": f"x{i}", "starts_at": when("2026-10-01T10:00:00+08:00"),
                       "slot": "phone_am"} for i in range(6)],
            subsidy_quota=5, subsidy_per_session=8,
        )
        with self.assertRaises(CompatibilityError):
            self.w.svc.propose_transfer("e1", "c-low")

    def test_batched_transfer_conserves_seat_quota_and_achievements(self) -> None:
        svc = self.w.svc
        # 第一批：迁 3 个剩余课次（补贴余额随之划转 3 次）
        prop1 = svc.propose_transfer("e1", PHONE, sessions_carried=3)
        pid1 = self._proposal_id(prop1)
        svc.confirm_proposal(pid1, "learner-1", "accept")

        mid = svc.state()
        source = mid.enrollment("e1")
        # 源报名：仍保留 3 个冻结课次与 3 次补贴余额
        self.assertEqual(source.frozen_sessions, ["f2f-s6", "f2f-s7", "f2f-s8"])
        self.assertEqual(source.covered_sessions, 3)
        self.assertEqual(source.status, "transferring")

        # 目标课程只有一个承接报名（不重复占座）；面授班席位已释放、电话班占 1
        targets = [e for e in mid.enrollments.values()
                   if (e.origin or {}).get("from_enrollment") == "e1"]
        self.assertEqual(len(targets), 1)
        target = targets[0]
        self.assertEqual(target.support_level, HIGH)  # 高支持不降级
        self.assertEqual(target.covered_sessions, 3)
        # 住院前成果完整带入，不被当作未完成
        self.assertEqual(set(target.completed_sessions), {"f2f-s1", "f2f-s2"})
        self.assertEqual(mid.course(F2F).seated, [])
        self.assertEqual(len(mid.course(PHONE).seated), 1)
        # 补贴名额全局守恒：面授 -1、电话 +1
        self.assertEqual(mid.course(F2F).quota_used, 0)
        self.assertEqual(mid.course(PHONE).quota_used, 1)

        # 第二批：迁完剩余 3 课次，挂到同一承接报名，不再占新座位
        prop2 = svc.propose_transfer("e1", PHONE, sessions_carried=3)
        pid2 = self._proposal_id(prop2)
        svc.confirm_proposal(pid2, "learner-1", "accept")
        final = svc.state()
        self.assertEqual(final.enrollment("e1").status, "transferred")
        self.assertEqual(final.enrollment("e1").frozen_sessions, [])
        self.assertEqual(final.enrollment("e1").covered_sessions, 0)
        targets2 = [e for e in final.enrollments.values()
                    if (e.origin or {}).get("from_enrollment") == "e1"]
        self.assertEqual(len(targets2), 1)
        self.assertEqual(targets2[0].covered_sessions, 6)
        self.assertEqual(final.course(PHONE).quota_used, 1)
        # 权益台账完整记录两批守恒划转，学员补贴总额不变（6=3+3）
        ledger = final.learner("learner-1").ledger
        self.assertEqual([row["covered_sessions"] for row in ledger], [3, 3])
        self.assertTrue(all(row["type"] == "transfer_move" for row in ledger))

    def test_one_shot_transfer_uses_single_seat(self) -> None:
        svc = self.w.svc
        prop = svc.propose_transfer("e1", PHONE)
        svc.confirm_proposal(self._proposal_id(prop), "learner-1", "accept")
        state = svc.state()
        self.assertEqual(state.enrollment("e1").status, "transferred")
        targets = [e for e in state.enrollments.values()
                   if (e.origin or {}).get("from_enrollment") == "e1"]
        self.assertEqual(len(targets), 1)
        self.assertEqual(state.course(PHONE).quota_used, 1)

        # 权益余额守恒：迁移前后补贴课次总额不变，只是换到承接报名名下
        before = svc.state(when("2026-09-14T00:00:00+08:00")).benefit_balance("learner-1")
        after = state.benefit_balance("learner-1")
        total_before = sum(e["covered_sessions"] for e in before["enrollments"])
        total_after = sum(e["covered_sessions"] for e in after["enrollments"])
        self.assertEqual(total_before, 6)
        self.assertEqual(total_after, 6)
        self.assertEqual(len(after["ledger"]), 1)

    def test_reject_leaves_everything_in_place(self) -> None:
        svc = self.w.svc
        prop = svc.propose_transfer("e1", PHONE)
        svc.confirm_proposal(self._proposal_id(prop), "learner-1", "reject")
        state = svc.state()
        self.assertIn("e1", state.course(F2F).seated)
        self.assertEqual(state.course(F2F).quota_used, 1)
        self.assertEqual(state.enrollment("e1").frozen_sessions,
                         ["f2f-s3", "f2f-s4", "f2f-s5", "f2f-s6", "f2f-s7", "f2f-s8"])


if __name__ == "__main__":
    unittest.main()
