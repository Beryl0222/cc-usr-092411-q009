"""银龄课程中断接续与支持迁移服务。"""

from src.continuity.clock import ControlledClock
from src.continuity.errors import (
    AuthorizationError,
    DomainError,
    IncompatibleCourseError,
    ReplayConflictError,
)
from src.continuity.events import DomainEvent
from src.continuity.journal import EventJournal
from src.continuity.registry import Registry
from src.continuity.service import ContinuityService

__all__ = [
    "ControlledClock",
    "ContinuityService",
    "DomainEvent",
    "EventJournal",
    "Registry",
    "DomainError",
    "AuthorizationError",
    "IncompatibleCourseError",
    "ReplayConflictError",
]
