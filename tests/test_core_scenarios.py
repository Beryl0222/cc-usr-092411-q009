"""场景一至三：发布固定、报名决定、暂停保留、重放幂等。"""

import unittest

from tests._world import F2F, PHONE, World, make_world, when
from src.continuity.errors import CompatibilityError, Conflict
from src.continuity.model import HIGH, SEATED, WAITLISTED


class PublishAndEnrollmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = make_world()

    def test_teacher_capacity_support_fixed_at_publish(self) -> None:
        state = self.w.svc.state(when("2026-09-01T10:00:00+08:00"))
        course = state.course(F2F)
        self.assertEqual(course.teacher.teacher_id, "teacher-a")
        self.assertEqual(course.teacher.qualifications, ["老年照护一级", "急救证"])
        self.assertEqual(course.capacity, 2)
        self.assertTrue(course.high_support)
        self.assertEqual(course.supports_offered, ["hearing_loop", "wheelchair"])

    def test_slot_and_support_decide_seat_or_waitlist(self) -> None:
        svc = self.w.svc
        # 正式席位：时段匹配、支持可提供
        svc.place_enrollment("e1", "learner-1", F2F, time_slots=["wed_am"],
                             support_needs=["hearing_loop"], support_level=HIGH,
                             needs_subsidy=True)
        self.assertEqual(svc.state().enrollment("e1").decision, SEATED)
        # 第二个席位也正常
        svc.place_enrollment("e2", "learner-2", F2F, time_slots=["wed_am"],
                             support_needs=[], needs_subsidy=True)
        # 第三个：座位满 → 候补，且不因报名动作重复占名额
        svc.place_enrollment("e3", "learner-3", F2F, time_slots=["wed_am"],
                             support_needs=[], needs_subsidy=True)
        state = svc.state()
        self.assertEqual(state.enrollment("e3").decision, WAITLISTED)
        self.assertEqual(state.course(F2F).waitlist_position("e3"), 1)
        self.assertEqual(state.course(F2F).quota_used, 2)

    def test_unsupported_need_and_slot_mismatch_rejected(self) -> None:
        svc = self.w.svc
        with self.assertRaises(CompatibilityError):
            svc.place_enrollment("e-x", "learner-x", F2F, time_slots=["wed_am"],
                                 support_needs=["sign_language"], support_level=HIGH)
        with self.assertRaises(CompatibilityError):
            svc.place_enrollment("e-y", "learner-y", PHONE, time_slots=["wed_pm"],
                                 support_needs=[])

    def test_high_support_cannot_land_on_low_support_course(self) -> None:
        # 临时发布一门不具备高支持能力的课程
        self.w.clock.advance(hours=1)
        self.w.svc.publish_course(
            "c-low", title="普通班", mode="face_to_face",
            teacher={"id": "t-low"}, capacity=5, supports_offered=[],
            high_support=False,
            sessions=[{"session_id": "l1", "starts_at": when("2026-10-01T09:00:00+08:00"),
                       "slot": "wed_am"}],
            subsidy_quota=0, subsidy_per_session=0,
        )
        with self.assertRaises(CompatibilityError):
            self.w.svc.place_enrollment("e-h", "learner-h", "c-low",
                                        time_slots=["wed_am"], support_needs=[],
                                        support_level=HIGH)


class PausePreservationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = make_world(start="2026-09-24T08:00:00+08:00")
        svc = self.w.svc
        # 学员已上两次课（9/2、9/9 已完成），第三次前住院
        svc.place_enrollment("e1", "learner-1", F2F, time_slots=["wed_am"],
                             support_needs=["hearing_loop"], support_level=HIGH,
                             needs_subsidy=True)
        svc.record_attendance("e1", "f2f-s1", "签到+练习一已完成",
                              completed_at=when("2026-09-02T10:00:00+08:00"))
        svc.record_attendance("e1", "f2f-s2", "签到+练习二已完成",
                              completed_at=when("2026-09-09T10:00:00+08:00"))
        self.events = svc.pause_enrollment("e1", reason="住院手术")

    def test_pause_freezes_only_future_sessions(self) -> None:
        state = self.w.svc.state()
        e = state.enrollment("e1")
        self.assertTrue(e.paused)
        # 9/23 尚未发生（时钟 9/24？注意 9/23 已过），冻结的是 9/30、10/7、10/14、10/21
        self.assertEqual(e.frozen_sessions, ["f2f-s5", "f2f-s6", "f2f-s7", "f2f-s8"])
        self.assertNotIn("f2f-s1", e.frozen_sessions)

    def test_pause_keeps_seat_priority_and_quota(self) -> None:
        course = self.w.svc.state().course(F2F)
        self.assertIn("e1", course.seated)
        self.assertEqual(course.quota_used, 1)
        e = self.w.svc.state().enrollment("e1")
        self.assertTrue(e.subsidy_quota)
        # 补贴共 8 次，住院前完成 2 次，余额 6 次随暂停保留
        self.assertEqual(e.covered_sessions, 6)

    def test_prior_completed_exercises_remain_completed(self) -> None:
        e = self.w.svc.state().enrollment("e1")
        self.assertEqual(e.completed_sessions, ["f2f-s1", "f2f-s2"])


class ReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = make_world()

    def test_duplicate_attendance_same_content_is_replay(self) -> None:
        svc = self.w.svc
        svc.place_enrollment("e1", "learner-1", F2F, time_slots=["wed_am"],
                             support_needs=[])
        first = svc.record_attendance("e1", "f2f-s1", "练习答案 A")
        # 不同 event_id、不同递送时间，但内容相同 → 重放
        self.w.clock.advance(hours=2)
        again = svc.record_attendance("e1", "f2f-s1", "练习答案 A")
        self.assertEqual(first.event_id, again.event_id)
        e = svc.state().enrollment("e1")
        self.assertEqual(len(e.attendance), 1)
        # 同课次但内容不同 → 新记录，不被判重
        other = svc.record_attendance("e1", "f2f-s1", "练习答案 B（补交）")
        self.assertNotEqual(first.event_id, other.event_id)
        self.assertEqual(len(svc.state().enrollment("e1").attendance), 2)

    def test_duplicate_phone_confirmation_same_content_is_replay(self) -> None:
        svc = self.w.svc
        svc.place_enrollment(
            "e1", "learner-1", F2F, time_slots=["wed_am"], support_needs=[],
        )
        first = svc.record_phone_confirmation(
            "e1", "learner-1", "我确认转电话辅导的安排", purpose="transfer"
        )
        self.w.clock.advance(hours=3)
        again = svc.record_phone_confirmation(
            "e1", "learner-1", "我确认转电话辅导的安排", purpose="transfer"
        )
        self.assertEqual(first.event_id, again.event_id)
        # 内容不同的再次来电是新记录
        other = svc.record_phone_confirmation(
            "e1", "learner-1", "再问一下上课地点", purpose="inquiry"
        )
        self.assertNotEqual(first.event_id, other.event_id)
        self.assertEqual(len(self.w.store.stream("enrollment", "e1")), 3)


if __name__ == "__main__":
    unittest.main()
