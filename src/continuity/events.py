"""不可变领域事件与内容指纹。

事件一经接收，标识、发生时间、版本与载荷均不得原地改写；
业务更正只能产生后继记录（新版本事件）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from .clock import Clock


class EventType(str, Enum):
    COURSE_PUBLISHED = "COURSE_PUBLISHED"
    COURSE_CANCELLED = "COURSE_CANCELLED"
    ENROLLMENT_PLACED = "ENROLLMENT_PLACED"
    ENROLLMENT_PAUSED = "ENROLLMENT_PAUSED"
    ENROLLMENT_RESUMED = "ENROLLMENT_RESUMED"
    SESSION_FROZEN = "SESSION_FROZEN"
    SEAT_RELEASED = "SEAT_RELEASED"
    WAITLIST_PROMOTED = "WAITLIST_PROMOTED"
    TRANSFER_PROPOSED = "TRANSFER_PROPOSED"
    TRANSFER_CONFIRMED = "TRANSFER_CONFIRMED"
    TRANSFER_BATCH_APPLIED = "TRANSFER_BATCH_APPLIED"
    PROPOSAL_REJECTED = "PROPOSAL_REJECTED"
    PROPOSAL_HELD = "PROPOSAL_HELD"
    SUBSIDY_LEDGERED = "SUBSIDY_LEDGERED"
    ATTENDANCE_RECORDED = "ATTENDANCE_RECORDED"
    PHONE_CONFIRMATION_RECORDED = "PHONE_CONFIRMATION_RECORDED"
    CAREGIVER_AUTHORIZED = "CAREGIVER_AUTHORIZED"


# 幂等判定只依据业务内容，不依据 event_id 或递送时间：
# 同一来源（aggregate_id）提交相同内容视为重放。
_FINGERPRINT_KEYS = (
    "event_type",
    "aggregate_type",
    "aggregate_id",
    "actor",
    "payload",
)


def canonical_json(value: Any) -> str:
    """以稳定序列化表示业务内容（键排序、无空白、UTC 时间）。"""

    def default(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return Clock.coerce(obj).isoformat()
        if isinstance(obj, Enum):
            return obj.value
        raise TypeError(f"不可序列化的类型：{type(obj)!r}")

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=default,
    )


def content_fingerprint(record: dict) -> str:
    material = {key: record.get(key) for key in _FINGERPRINT_KEYS}
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Event:
    """一条不可变领域事件。"""

    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: datetime
    version: int
    summary: str
    payload: dict = field(default_factory=dict)
    actor: str | None = None
    causation_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": Clock.coerce(self.occurred_at).isoformat(),
            "version": self.version,
            "summary": self.summary,
            "payload": self.payload,
            "actor": self.actor,
            "causation_id": self.causation_id,
        }

    @classmethod
    def from_dict(cls, record: dict) -> "Event":
        return cls(
            event_id=record["event_id"],
            event_type=record["event_type"],
            aggregate_type=record["aggregate_type"],
            aggregate_id=record["aggregate_id"],
            occurred_at=datetime.fromisoformat(record["occurred_at"]),
            version=record["version"],
            summary=record["summary"],
            payload=record.get("payload", {}),
            actor=record.get("actor"),
            causation_id=record.get("causation_id"),
        )

    def fingerprint(self) -> str:
        return content_fingerprint(self.to_dict())
