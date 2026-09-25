"""场景六：历史时点查询、截止点候补提升、重启后未决确认。"""

import tempfile
import unittest
from pathlib import Path

from tests._world import F2F, PHONE, make_world, when, World
from src.continuity.clock import Clock
from src.continuity.store import EventStore
from src.continuity.service import ContinuityService


class HistoricalQueryTest(unittest.TestCase):
    def test_course_membership_support_and_balance_as_of(self) -> None:
        w = make_world(start="2026-08-31T09:00:00+08:00")
        svc, clock = w.svc, w.clock
        svc.place_enrollment("e1", "learner-1", F2F, time_slots=["wed_am", "phone_am"],
                             support_needs=["hearing_loop"], support_level="high",
                             needs_subsidy=True)
        clock.freeze(when("2026-09-02T10:00:00+08:00"))
        svc.record_attendance("e1", "f2f-s1", "练习一")
        clock.freeze(when("2026-09-15T08:00:00+08:00"))
        svc.pause_enrollment("e1")
        prop = svc.propose_transfer("e1", PHONE)
        pid = prop.payload["proposal_id"]
        clock.freeze(when("2026-09-16T10:00:00+08:00"))
        svc.confirm_proposal(pid, "learner-1", "accept")

        # 9/1：课程归属在面授班、支持决定 high、补贴余额 8
        sep1 = svc.state(when("2026-09-01T00:00:00+08:00"))
        self.assertIn("e1", sep1.course(F2F).seated)
        self.assertEqual(sep1.enrollment("e1").support_level, "high")
        self.assertEqual(sep1.enrollment("e1").covered_sessions, 8)
        # 此时电话班还没有承接报名
        self.assertEqual(sep1.course(PHONE).seated, [])

        # 9/10：练习一已完成，补贴余额 7
        sep10 = svc.state(when("2026-09-10T00:00:00+08:00"))
        self.assertEqual(sep10.enrollment("e1").completed_sessions, ["f2f-s1"])
        self.assertEqual(sep10.enrollment("e1").covered_sessions, 7)
        self.assertFalse(sep10.enrollment("e1").paused)

        # 9/15 暂停前一刻：未暂停；暂停后一刻：冻结名单已产生
        before = svc.state(when("2026-09-15T07:59:00+08:00"))
        after = svc.state(when("2026-09-15T08:01:00+08:00"))
        self.assertFalse(before.enrollment("e1").paused)
        self.assertTrue(after.enrollment("e1").paused)

        # 9/16 迁移前：仍归属面授班；迁移后：归属电话班
        pre = svc.state(when("2026-09-16T09:59:00+08:00"))
        post = svc.state(when("2026-09-16T10:01:00+08:00"))
        self.assertEqual(pre.enrollment("e1").course_id, F2F)
        self.assertEqual(post.enrollment("e1").status, "transferred")
        target = [e for e in post.enrollments.values() if e.origin][0]
        self.assertEqual(target.course_id, PHONE)
        # 历史时点的权益余额：迁移后台账存在、迁移前不存在
        self.assertEqual(pre.learner("learner-1").ledger, [])
        self.assertEqual(len(post.learner("learner-1").ledger), 1)

    def test_membership_intervals_reflect_history(self) -> None:
        w = make_world(start="2026-08-31T09:00:00+08:00")
        svc, clock = w.svc, w.clock
        svc.place_enrollment("e1", "learner-1", F2F, time_slots=["wed_am", "phone_am"],
                             support_needs=[], needs_subsidy=False)
        clock.freeze(when("2026-09-15T08:00:00+08:00"))
        svc.pause_enrollment("e1")
        pid = svc.propose_transfer("e1", PHONE).payload["proposal_id"]
        svc.confirm_proposal(pid, "learner-1", "accept")
        state = svc.state()
        source = state.enrollment("e1")
        # 源报名在面授班的归属区间已闭合
        self.assertEqual(
            [(m.course_id, m.valid_to is not None) for m in source.memberships],
            [(F2F, True)],
        )
        # 承接报名在电话班的归属自迁移时点起开放
        target = [e for e in state.enrollments.values() if e.origin][0]
        self.assertEqual(
            [(m.course_id, m.valid_to is None) for m in target.memberships],
            [(PHONE, True)],
        )


class WaitlistPromotionTest(unittest.TestCase):
    def _deadline_course(self, svc, *, capacity=1, quota=1):
        svc.publish_course(
            "c-deadline", title="有固定确认截止的班", mode="face_to_face",
            teacher={"id": "t-d"}, capacity=capacity, supports_offered=[],
            high_support=False,
            sessions=[{"session_id": "d1", "starts_at": when("2026-10-01T09:00:00+08:00"),
                       "slot": "wed_am"}],
            subsidy_quota=quota, subsidy_per_session=8,
            seat_confirm_deadline=when("2026-09-05T18:00:00+08:00"),
            confirm_window_hours=24,
        )

    def test_waitlist_order_is_never_recomputed(self) -> None:
        w = make_world(start="2026-08-31T09:00:00+08:00")
        svc = w.svc
        svc.place_enrollment("e1", "learner-1", F2F, time_slots=["wed_am"],
                             support_needs=[], needs_subsidy=True)
        svc.place_enrollment("e2", "learner-2", F2F, time_slots=["wed_am"],
                             support_needs=[], needs_subsidy=True)
        svc.place_enrollment("w1", "learner-3", F2F, time_slots=["wed_am"],
                             support_needs=[], needs_subsidy=False)
        svc.place_enrollment("w2", "learner-4", F2F, time_slots=["wed_am"],
                             support_needs=[], needs_subsidy=True)
        # 候补期间暂停、登记记录都不重排队列
        svc.pause_enrollment("w1")
        svc.record_attendance("w1", "f2f-s9", "候补学员的预习记录",
                              completed_at=when("2026-09-01T10:00:00+08:00"))
        svc.resume_enrollment("w1")
        self.assertEqual(svc.state().course(F2F).waitlist, ["w1", "w2"])

    def test_deadline_release_and_promotion_keeps_order(self) -> None:
        w = make_world(start="2026-08-31T09:00:00+08:00")
        svc, clock = w.svc, w.clock
        self._deadline_course(svc)
        svc.place_enrollment("d-seat", "learner-9", "c-deadline", time_slots=["wed_am"],
                             support_needs=[], needs_subsidy=True)
        svc.place_enrollment("d-wait", "learner-10", "c-deadline", time_slots=["wed_am"],
                             support_needs=[], needs_subsidy=True)
        self.assertEqual(svc.state().course("c-deadline").waitlist, ["d-wait"])

        # 截止前扫描：不释放、不提升
        clock.freeze(when("2026-09-05T17:59:00+08:00"))
        self.assertEqual(svc.promote_waitlist(), [])
        self.assertIn("d-seat", svc.state().course("c-deadline").seated)

        # 截止点过后：释放 d-seat（席位与补贴名额），d-wait 顺位提升并获得新期限
        clock.freeze(when("2026-09-05T18:01:00+08:00"))
        events = svc.promote_waitlist()
        types = [e.event_type for e in events]
        self.assertIn("SEAT_RELEASED", types)
        self.assertIn("WAITLIST_PROMOTED", types)
        final = svc.state().course("c-deadline")
        self.assertNotIn("d-seat", final.seated)
        self.assertIn("d-wait", final.seated)
        self.assertEqual(final.waitlist, [])
        self.assertEqual(final.quota_used, 1)
        promoted = svc.state().enrollment("d-wait")
        self.assertEqual(promoted.deadline, when("2026-09-06T18:01:00+08:00"))
        # 历史时点：被释放者此前仍是正式席位
        self.assertEqual(svc.state(when("2026-09-04T00:00:00+08:00"))
                         .enrollment("d-seat").status, "active")

    def test_waitlist_stops_at_head_blocked_by_quota(self) -> None:
        w = make_world(start="2026-08-31T09:00:00+08:00")
        svc, clock = w.svc, w.clock
        # 容量 2、补贴名额 1：一个补贴席位（已电话确认，不会释放），
        # 一个无补贴席位（逾期释放，只归还座位、不归还名额）。
        svc.publish_course(
            "c-q", title="补贴紧张班", mode="face_to_face", teacher={"id": "t-q"},
            capacity=2, supports_offered=[], high_support=False,
            sessions=[{"session_id": "q1", "starts_at": when("2026-10-01T09:00:00+08:00"),
                       "slot": "wed_am"}],
            subsidy_quota=1, subsidy_per_session=8,
            seat_confirm_deadline=when("2026-09-05T18:00:00+08:00"),
            confirm_window_hours=24,
        )
        svc.place_enrollment("q-keep", "l-0", "c-q", time_slots=["wed_am"],
                             support_needs=[], needs_subsidy=True)
        svc.record_phone_confirmation("q-keep", "l-0", "确认参加，占用唯一补贴名额")
        svc.place_enrollment("q-expire", "l-1", "c-q", time_slots=["wed_am"],
                             support_needs=[], needs_subsidy=False)
        svc.place_enrollment("q-w1", "l-2", "c-q", time_slots=["wed_am"],
                             support_needs=[], needs_subsidy=True)
        svc.place_enrollment("q-w2", "l-3", "c-q", time_slots=["wed_am"],
                             support_needs=[], needs_subsidy=False)
        clock.freeze(when("2026-09-05T18:01:00+08:00"))
        svc.promote_waitlist()
        course = svc.state().course("c-q")
        # 座位空出一个，但唯一补贴名额仍被 q-keep 占用：
        # 队首 q-w1 需补贴无法提升，队尾 q-w2 不得被跳过抢先。
        self.assertEqual(course.waitlist, ["q-w1", "q-w2"])
        self.assertIn("q-keep", course.seated)
        self.assertNotIn("q-expire", course.seated)


class RestartTest(unittest.TestCase):
    def test_pending_confirmation_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            w = make_world(path, start="2026-08-31T09:00:00+08:00")
            svc, clock = w.svc, w.clock
            svc.authorize_caregiver("learner-1", "cg-daughter")
            svc.place_enrollment("e1", "learner-1", F2F,
                                 time_slots=["wed_am", "phone_am"],
                                 support_needs=["hearing_loop"], support_level="high",
                                 needs_subsidy=True)
            clock.freeze(when("2026-09-15T08:00:00+08:00"))
            svc.pause_enrollment("e1")
            pid = svc.propose_transfer("e1", PHONE).payload["proposal_id"]

            # 服务重启：新时钟、新存储、新服务实例，从 JSONL 恢复
            clock2 = Clock()
            clock2.freeze(when("2026-09-16T10:00:00+08:00"))
            store2 = EventStore(path)
            svc2 = ContinuityService(store2, clock2)

            _, proposal = svc2.state().get_proposal(pid)
            self.assertEqual(proposal.status, "pending")
            self.assertTrue(svc2.state().active_caregiver("learner-1", "cg-daughter",
                                                          clock2.now()))
            # 未决确认在重启后仍可由当时有效的照护人完成
            svc2.confirm_proposal(pid, "cg-daughter", "accept")
            state = svc2.state()
            self.assertEqual(state.enrollment("e1").status, "transferred")
            self.assertEqual(state.enrollment("e1").frozen_sessions, [])
            self.assertEqual(state.course(PHONE).quota_used, 1)

    def test_replay_after_restart_still_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            w = make_world(path)
            svc = w.svc
            svc.place_enrollment("e1", "learner-1", F2F, time_slots=["wed_am"],
                                 support_needs=[])
            first = svc.record_attendance("e1", "f2f-s1", "练习答案 A")

            store2 = EventStore(path)
            clock2 = Clock()
            clock2.freeze(when("2026-09-25T10:00:00+08:00"))
            svc2 = ContinuityService(store2, clock2)
            again = svc2.record_attendance("e1", "f2f-s1", "练习答案 A")
            self.assertEqual(first.event_id, again.event_id)
            self.assertEqual(len(svc2.state().enrollment("e1").attendance), 1)


if __name__ == "__main__":
    unittest.main()
