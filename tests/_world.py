"""测试夹具：搭建面授班 + 电话辅导两门课程的世界。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.continuity.clock import Clock
from src.continuity.store import EventStore
from src.continuity.service import ContinuityService

TZ = timezone(timedelta(hours=8))

F2F = "c-f2f"
PHONE = "c-phone"


def when(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _sessions(prefix: str, slot: str, hour: int) -> list[dict]:
    dates = [
        "2026-09-02", "2026-09-09", "2026-09-16", "2026-09-23",
        "2026-09-30", "2026-10-07", "2026-10-14", "2026-10-21",
    ]
    return [
        {
            "session_id": f"{prefix}-s{i + 1}",
            "starts_at": when(f"{d}T{hour:02d}:00:00+08:00"),
            "slot": slot,
        }
        for i, d in enumerate(dates)
    ]


@dataclass
class World:
    svc: ContinuityService
    store: EventStore
    clock: Clock


def make_world(path=None, *, start: str = "2026-09-01T09:00:00+08:00") -> World:
    clock = Clock()
    clock.freeze(when(start))
    store = EventStore(path)
    svc = ContinuityService(store, clock)

    svc.publish_course(
        F2F,
        title="银龄智能手机面授班",
        mode="face_to_face",
        teacher={
            "id": "teacher-a",
            "name": "王老师",
            "qualifications": ["老年照护一级", "急救证"],
        },
        capacity=2,
        supports_offered=["hearing_loop", "wheelchair"],
        high_support=True,
        sessions=_sessions("f2f", "wed_am", 9),
        subsidy_quota=2,
        subsidy_per_session=8,
        confirm_window_hours=240,
    )
    svc.publish_course(
        PHONE,
        title="银龄智能手机电话辅导",
        mode="phone",
        teacher={
            "id": "teacher-p",
            "name": "陈辅导",
            "qualifications": ["老年照护一级", "电话辅导认证"],
        },
        capacity=4,
        supports_offered=["hearing_loop", "wheelchair"],
        high_support=True,
        sessions=_sessions("phone", "phone_am", 10),
        subsidy_quota=4,
        subsidy_per_session=8,
        confirm_window_hours=240,
    )
    return World(svc=svc, store=store, clock=clock)
