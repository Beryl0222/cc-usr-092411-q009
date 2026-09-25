"""领域事件定义。

事件一经写入日志即不可变；业务更正只能追加后继事件。
``event_key`` 是命令级幂等键：同一键重放且内容一致时跳过，内容冲突时拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

# --- 聚合类型 ---
AGG_COURSE = "course_offering"
AGG_ENROLLMENT = "enrollment"
AGG_LEARNER = "learner_profile"
AGG_RECORD = "learning_record"
AGG_PROPOSAL = "continuity_proposal"

# --- 课程 / 报名 ---
COURSE_PUBLISHED = "COURSE_PUBLISHED"
ENROLLMENT_PLACED = "ENROLLMENT_PLACED"
WAITLIST_PROMOTED = "WAITLIST_PROMOTED"
PAUSE_FROZEN = "PAUSE_FROZEN"
PAUSE_RESUMED = "PAUSE_RESUMED"
ENROLLMENT_ENDED = "ENROLLMENT_ENDED"
WAITLIST_RELEASED = "WAITLIST_RELEASED"
SUBSIDY_ALLOCATED = "SUBSIDY_ALLOCATED"
SUBSIDY_RETURNED = "SUBSIDY_RETURNED"
SUBSIDY_RELINKED = "SUBSIDY_RELINKED"

# --- 学习记录 ---
SESSION_RECORDED = "SESSION_RECORDED"
PRACTICE_RECORDED = "PRACTICE_RECORDED"

# --- 照护授权 ---
CAREGIVER_GRANTED = "CAREGIVER_GRANTED"
CAREGIVER_REVOKED = "CAREGIVER_REVOKED"

# --- 教师 / 课程变更 ---
TEACHER_CHANGED = "TEACHER_CHANGED"
COURSE_CANCELLED = "COURSE_CANCELLED"

# --- 迁移 / 变更方案 ---
PROPOSAL_OPENED = "PROPOSAL_OPENED"
PROPOSAL_CHOICE_RECORDED = "PROPOSAL_CHOICE_RECORDED"
PROPOSAL_CONFIRMED = "PROPOSAL_CONFIRMED"
PROPOSAL_REJECTED = "PROPOSAL_REJECTED"
PROPOSAL_DISPUTED = "PROPOSAL_DISPUTED"
PROPOSAL_EXPIRED = "PROPOSAL_EXPIRED"
PROPOSAL_RESOLVED = "PROPOSAL_RESOLVED"
SESSIONS_MIGRATED = "SESSIONS_MIGRATED"

EVENT_TYPES = frozenset(
    {
        COURSE_PUBLISHED,
        ENROLLMENT_PLACED,
        WAITLIST_PROMOTED,
        PAUSE_FROZEN,
        PAUSE_RESUMED,
        ENROLLMENT_ENDED,
        WAITLIST_RELEASED,
        SUBSIDY_ALLOCATED,
        SUBSIDY_RETURNED,
        SUBSIDY_RELINKED,
        SESSION_RECORDED,
        PRACTICE_RECORDED,
        CAREGIVER_GRANTED,
        CAREGIVER_REVOKED,
        PROPOSAL_OPENED,
        PROPOSAL_CHOICE_RECORDED,
        PROPOSAL_CONFIRMED,
        PROPOSAL_REJECTED,
        PROPOSAL_DISPUTED,
        PROPOSAL_EXPIRED,
        PROPOSAL_RESOLVED,
        SESSIONS_MIGRATED,
        TEACHER_CHANGED,
        COURSE_CANCELLED,
    }
)


def _normalize(value: Any) -> Any:
    """把 JSON 反序列化后的载荷还原为运行时类型（主要是 ISO 时间）。"""
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, str) and len(value) >= 19 and value[4] == "-" and "T" in value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


def _encode(value: Any) -> Any:
    """把运行时载荷编码为可 JSON 序列化形式（datetime -> ISO 字符串）。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if isinstance(value, set):
        return [_encode(v) for v in sorted(value)]
    return value


@dataclass(frozen=True)
class DomainEvent:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: datetime
    version: int
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["occurred_at"] = self.occurred_at.isoformat()
        data["payload"] = _encode(self.payload)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DomainEvent":
        return cls(
            event_id=data["event_id"],
            event_type=data["event_type"],
            aggregate_type=data["aggregate_type"],
            aggregate_id=data["aggregate_id"],
            occurred_at=datetime.fromisoformat(data["occurred_at"]),
            version=data["version"],
            summary=data["summary"],
            payload=_normalize(data.get("payload", {})),
            event_key=data.get("event_key"),
        )
