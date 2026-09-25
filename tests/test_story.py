"""端到端故事：学员短期住院，从面授班接续到电话辅导。

串联：发布固定 → 报名决定 → 住院前成果 → 暂停冻结 → 接续方案与差异 →
照护人确认（重启后）→ 守恒校验 → 历史时点回看。
"""

import tempfile
import unittest
from pathlib import Path

from tests._world import F2F, PHONE, make_world, when
from src.continuity.clock import Clock
from src.continuity.store import EventStore
from src.continuity.service import ContinuityService


class HospitalToPhoneStoryTest(unittest.TestCase):
    def test_full_story(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "story.jsonl"
            w = make_world(path, start="2026-08-31T09:00:00+08:00")
            svc, clock = w.svc, w.clock

            # 报名：周三上午可上课、需助听、高支持、申请补贴 → 正式席位
            svc.authorize_caregiver("zhou-ayi", "cg-nv-er", note="周阿姨的女儿")
            svc.place_enrollment(
                "e-zhou", "zhou-ayi", F2F,
                time_slots=["wed_am", "phone_am"],
                support_needs=["hearing_loop"], support_level="high",
                needs_subsidy=True,
            )
            course = svc.state().course(F2F)
            self.assertEqual(len(course.seated), 1)
            self.assertEqual(course.quota_used, 1)

            # 9/2、9/9 两次面授并完成练习
            clock.freeze(when("2026-09-02T10:00:00+08:00"))
            svc.record_attendance("e-zhou", "f2f-s1", "微信字体放大练习")
            # 重复签到（网络重试）——按内容判重，不产生第二条
            svc.record_attendance("e-zhou", "f2f-s1", "微信字体放大练习")
            clock.freeze(when("2026-09-09T10:00:00+08:00"))
            svc.record_attendance("e-zhou", "f2f-s2", "手机挂号练习")

            # 9/15 住院：暂停只冻结未来课次
            clock.freeze(when("2026-09-15T08:00:00+08:00"))
            svc.pause_enrollment("e-zhou", reason="短期住院")
            paused = svc.state().enrollment("e-zhou")
            self.assertEqual(
                paused.frozen_sessions,
                ["f2f-s3", "f2f-s4", "f2f-s5", "f2f-s6", "f2f-s7", "f2f-s8"],
            )
            self.assertEqual(paused.completed_sessions, ["f2f-s1", "f2f-s2"])

            # 教务发起面授 → 电话辅导接续方案（带差异说明）
            proposal = svc.propose_transfer("e-zhou", PHONE, reason="medical_to_phone")
            pid = proposal.payload["proposal_id"]
            self.assertEqual(proposal.payload["diff"]["supports"]["level_after"], "high")

            # 服务重启：女儿在电话里确认未决方案
            store2 = EventStore(path)
            clock2 = Clock()
            clock2.freeze(when("2026-09-16T11:00:00+08:00"))
            svc2 = ContinuityService(store2, clock2)
            svc2.confirm_proposal(pid, "cg-nv-er", "accept")

            state = svc2.state()
            source = state.enrollment("e-zhou")
            target = next(
                e for e in state.enrollments.values()
                if (e.origin or {}).get("from_enrollment") == "e-zhou"
            )
            # 旧问题全部消除：
            # 1) 住院前练习没有被当作未完成
            self.assertEqual(target.completed_sessions, ["f2f-s1", "f2f-s2"])
            # 2) 高支持没有降级
            self.assertEqual(target.support_level, "high")
            # 3) 补贴名额没有重复占用（面授 0、电话 1）
            self.assertEqual(state.course(F2F).quota_used, 0)
            self.assertEqual(state.course(PHONE).quota_used, 1)
            # 4) 剩余补贴课次守恒（8 次权益，用掉 2 次，余 6 次随迁）
            self.assertEqual(target.covered_sessions, 6)
            self.assertEqual(source.status, "transferred")

            # 历史时点回看：9/10 时仍归属面授班、两次练习已完成、补贴余额 6
            past = svc2.state(when("2026-09-10T12:00:00+08:00"))
            self.assertEqual(past.enrollment("e-zhou").course_id, F2F)
            self.assertEqual(past.enrollment("e-zhou").completed_sessions, ["f2f-s1", "f2f-s2"])
            self.assertEqual(past.enrollment("e-zhou").covered_sessions, 6)


if __name__ == "__main__":
    unittest.main()
